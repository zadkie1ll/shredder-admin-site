"""Фоновый воркер «Инфраструктуры»: крутится внутри процесса сайта.

Стартует из web_app/wsgi.py (как censor_worker) и раз в
INFRA_WORKER_INTERVAL секунд выполняет обслуживание:

- ONLINE/OFFLINE серверов + Telegram-алерты с recovery (идёт ПЕРВЫМ шагом
  тика); массовая потеря heartbeat (≥ infra_mass_offline_threshold серверов
  за проход) — один сводный алерт «мониторинг ослеп» вместо N одинаковых:
  это не ноды, а коллектор ip-guard или Postgres;
- агрегация телеметрии (raw -> 1 мин -> 15 мин) и retention;
- алерты о длительной высокой нагрузке канала (порог/длительность из
  system_settings, гистерезис + кулдаун);
- anomaly detection: одновременное падение трафика и TCP-соединений
  против baseline того же времени суток -> принудительный «Замер ТСПУ»;
- обработка аномалий: ТСПУ подтвердил блокировку -> заявка на замену IP;
- state machine замены IP: node-agent ensure_ip -> Cloudflare ADD new ->
  Cloudflare REMOVE old -> Telegram (порядок принципиален).
- загрузка и атомарное обновление DB-IP City Lite для аналитики подключений.

Лидерство между gunicorn-воркерами — Postgres advisory lock (свой ключ,
отличный от censor_worker). Аномалия никогда не меняет DNS сама: только
подтверждение существующим механизмом «Замеров ТСПУ» запускает ротацию.

Каждый шаг тика идёт в своей сессии под statement_timeout
(INFRA_WORKER_STEP_TIMEOUT, SET LOCAL): на больной базе один зависший
запрос не должен останавливать весь тик (инцидент 2026-09-02: INSERT
агрегации висел 28 минут, и воркер не доходил ни до OFFLINE-проверки, ни
до детектора аномалий, ни до замен).

Бюджет тика (INFRA_MAINTENANCE_BUDGET_SECONDS) запрещает начинать следующий
шаг после его исчерпания. OFFLINE-проверка всегда первая и бюджетом не
отсекается; остальные шаги идут по сроку (самый просроченный первым).
Обрезанный бюджетом тик доделывается после короткой паузы (min(interval,
1) с), а не через полный interval, иначе под нагрузкой все шаги, включая
OFFLINE, шли бы реже, чем без бюджета.
"""

import logging
import random
import threading
from django.conf import settings
import time
from datetime import timedelta

from sqlalchemy import text

from database import engine
from database import session_factory

_ADVISORY_LOCK_KEY = 4820257002
_DEFAULT_INTERVAL = 30
# Каждый N-й тик: anomaly detection (раз в минуту при интервале 30с),
# prune (раз в ~10 минут), DNS-вотчер (раз в ~15 минут)
_ANOMALY_EVERY_TICKS = 2
_PRUNE_EVERY_TICKS = 20
_DNS_WATCH_EVERY_TICKS = 30
# statement_timeout одного шага тика, сек (Django settings
# INFRA_WORKER_STEP_TIMEOUT; 0 = без лимита)
_DEFAULT_STEP_TIMEOUT_SECONDS = 120
# Сколько имён серверов перечислять в сводном алерте
_MASS_ALERT_MAX_NAMES = 15

_started = False
_start_lock = threading.Lock()
_tick_counter = 0
_maintenance_due = {}

log = logging.getLogger("infra-worker")


def _fmt_mbps(bps) -> str:
    if bps is None:
        return "—"
    return f"{bps / 1_000_000:.0f} Mbit/s"


def _fmt_age_minutes(dt, now) -> int:
    if dt is None:
        return 0
    return max(0, int((now - dt).total_seconds() // 60))


def _send_alert(text_value: str) -> bool:
    from engine import notify

    return notify.send_admin_telegram_alert(text_value)


# --- OFFLINE / ONLINE -------------------------------------------------------


def _names_line(servers) -> str:
    from engine import infra

    names = [infra.server_title(server) for server in servers]
    line = ", ".join(names[:_MASS_ALERT_MAX_NAMES])
    rest = len(names) - _MASS_ALERT_MAX_NAMES
    if rest > 0:
        line += f" и ещё {rest}"
    return line


def check_offline(db_session) -> None:
    """OFFLINE/ONLINE-алерты по heartbeat.

    Одиночная потеря heartbeat — алерт по серверу. Если за один проход
    heartbeat потеряли сразу ≥ infra_mass_offline_threshold серверов, это
    не ноды: heartbeat не принимает коллектор ip-guard или лежит Postgres
    (инцидент 2026-09-02: все 35 нод «OFFLINE» разом из-за раздутой базы,
    35 одинаковых 🔴 и ни слова о том, что детектор аномалий и автозамена
    в этом состоянии не работают). Тогда уходит ОДИН сводный алерт с
    диагнозом; recovery — тоже сводный. Дедуп прежний: пара timestamps
    offline_alerted_at/recovered_alerted_at проставляется всем серверам
    сводного алерта, кулдаун флаппинга — по серверу.
    """
    from engine import infra

    cfg = infra.get_settings(db_session)
    now = infra.utcnow()
    cooldown = timedelta(minutes=cfg["infra_offline_alert_cooldown_minutes"])
    mass_threshold = int(cfg["infra_mass_offline_threshold"])
    servers = (
        db_session.query(infra.InfraServer)
        .filter(infra.InfraServer.is_archived.is_(False))
        .all()
    )
    went_offline = []
    recovered = []
    for server in servers:
        online = infra.is_online(server, cfg, now)
        # Инцидент «уже заалерчен», пока recovery-алерт не отправлен позже
        # offline-алерта; timestamps не сбрасываются — это и есть дедуп
        incident_alerted = server.offline_alerted_at is not None and (
            server.recovered_alerted_at is None
            or server.recovered_alerted_at < server.offline_alerted_at
        )
        if not online:
            if incident_alerted:
                continue
            # Кулдаун при флаппинге: новая пара 🔴/🟢 не чаще cooldown
            if (
                server.offline_alerted_at is not None
                and now - server.offline_alerted_at < cooldown
            ):
                continue
            went_offline.append(server)
        else:
            if not incident_alerted:
                continue
            if now - server.offline_alerted_at < timedelta(minutes=1):
                # Мгновенный флап: даём состоянию устаканиться
                continue
            recovered.append(server)

    if mass_threshold > 0 and len(went_offline) >= mass_threshold:
        delivered = _send_alert(
            "🔴 <b>Мониторинг ослеп</b>\n\n"
            f"Одновременно потеряли heartbeat {len(went_offline)} из "
            f"{len(servers)} серверов:\n{_names_line(went_offline)}\n\n"
            "Такое не бывает из-за нод. Скорее всего heartbeat не принимает "
            "коллектор ip-guard или тормозит Postgres (блокировки, раздутые "
            "таблицы, застрявший xmin-горизонт).\n"
            "Пока это не починено, детектор аномалий и автозамена IP "
            "не работают.\n\n"
            "Проверьте логи коллектора и pg_stat_activity "
            "(wait_event = Lock, age(backend_xmin))."
        )
        if delivered:
            for server in went_offline:
                server.offline_alerted_at = now
            log.warning(
                "infra: MASS OFFLINE %s/%s servers, single alert sent",
                len(went_offline), len(servers),
            )
    else:
        for server in went_offline:
            title = infra.server_title(server)
            minutes = _fmt_age_minutes(server.last_seen_at, now)
            delivered = _send_alert(
                "🔴 <b>Сервер недоступен</b>\n\n"
                f"Сервер: <b>{title}</b>\n"
                f"Последний heartbeat: {minutes} мин назад\n\n"
                "Node-agent больше не отвечает.\n"
                "Возможные причины: сервер выключен или недоступен, "
                "node-agent остановлен, проблема сети."
            )
            if delivered:
                server.offline_alerted_at = now
                log.warning("infra: server %s OFFLINE, alert sent", title)

    if mass_threshold > 0 and len(recovered) >= mass_threshold:
        delivered = _send_alert(
            "🟢 <b>Мониторинг восстановлен</b>\n\n"
            f"Heartbeat вернулся у {len(recovered)} серверов:\n"
            f"{_names_line(recovered)}"
        )
        if delivered:
            for server in recovered:
                server.recovered_alerted_at = now
            log.info(
                "infra: MASS ONLINE %s servers, single alert sent",
                len(recovered),
            )
    else:
        for server in recovered:
            title = infra.server_title(server)
            delivered = _send_alert(
                "🟢 <b>Сервер снова онлайн</b>\n\n"
                f"Сервер: <b>{title}</b>\n"
                f"Heartbeat восстановился."
            )
            if delivered:
                server.recovered_alerted_at = now
                log.info("infra: server %s back ONLINE", title)


# --- health xray (rw-core) --------------------------------------------------


def check_xray(db_session) -> None:
    """Алерты «xray умер / crash-loop» по health от node-agent v0.3+.

    Диагноз только по процессу rw-core: xray_process_running is False или
    crash_loop (access.log — не признак поломки, он может быть выключен;
    None — агент не сообщает, старая версия или нет pid: host — молчим).
    Падение подтверждается infra_xray_down_confirm_minutes, чтобы штатный
    рестарт при деплое конфига не алертил. Дедуп и recovery — по образцу
    check_offline: пара timestamps + кулдаун при флаппинге.
    """
    from engine import infra

    cfg = infra.get_settings(db_session)
    now = infra.utcnow()
    confirm = timedelta(minutes=cfg["infra_xray_down_confirm_minutes"])
    cooldown = timedelta(minutes=cfg["infra_xray_alert_cooldown_minutes"])
    servers = (
        db_session.query(infra.InfraServer)
        .filter(infra.InfraServer.is_archived.is_(False))
        .all()
    )
    for server in servers:
        if not infra.is_online(server, cfg, now):
            # Оффлайн покрывается своим алертом; протухший health не судим
            server.xray_down_since = None
            continue
        title = infra.server_title(server)
        down = (
            server.xray_process_running is False
            or server.xray_crash_loop is True
        )
        incident_alerted = server.xray_down_alerted_at is not None and (
            server.xray_recovered_alerted_at is None
            or server.xray_recovered_alerted_at < server.xray_down_alerted_at
        )
        if down:
            if server.xray_down_since is None:
                server.xray_down_since = now
            if incident_alerted:
                continue
            if now - server.xray_down_since < confirm:
                continue
            if (
                server.xray_down_alerted_at is not None
                and now - server.xray_down_alerted_at < cooldown
            ):
                continue
            reason = (
                "процесс постоянно перезапускается (crash-loop)"
                if server.xray_crash_loop
                else "процесс rw-core не найден"
            )
            delivered = _send_alert(
                "🟥 <b>XRAY не работает</b>\n\n"
                f"Сервер: <b>{title}</b>\n"
                f"Причина: {reason}.\n\n"
                "Клиенты не обслуживаются, хотя сервер и node-agent живы.\n"
                "Похоже на сломанный конфиг xray — проверьте:\n"
                "<code>docker exec remnanode tail -n 50 "
                "/var/log/supervisor/xray.err.log</code>\n\n"
                "Замеры ТСПУ и ротация IP по этому серверу приостановлены "
                "до восстановления xray."
            )
            if delivered:
                server.xray_down_alerted_at = now
                log.warning("infra: xray DOWN on %s, alert sent", title)
        else:
            server.xray_down_since = None
            if not incident_alerted:
                continue
            if now - server.xray_down_alerted_at < timedelta(minutes=1):
                # Мгновенный флап: даём состоянию устаканиться
                continue
            uptime = server.xray_process_uptime_seconds
            uptime_text = (
                f"{int(uptime // 60)} мин" if uptime is not None else "—"
            )
            delivered = _send_alert(
                "🟩 <b>XRAY снова работает</b>\n\n"
                f"Сервер: <b>{title}</b>\n"
                f"Процесс rw-core запущен, uptime {uptime_text}."
            )
            if delivered:
                server.xray_recovered_alerted_at = now
                log.info("infra: xray RECOVERED on %s", title)


# --- лимиты ноды ------------------------------------------------------------


def check_capacity(db_session) -> None:
    """Надзор за потолками ноды: conntrack, nginx, дескрипторы, память.

    Два уровня. «Предупреждение» — профилактика: нода работает, но пора
    поднимать лимит. «Критично» — потолок исчерпан, клиенты уже теряются;
    этот же уровень запрещает изменения DNS, потому что перегруженная нода
    снаружи выглядит точно так же, как заблокированная.
    """
    from engine import infra

    cfg = infra.get_settings(db_session)
    now = infra.utcnow()
    warn_pct = cfg["infra_capacity_warn_pct"]
    cooldown = timedelta(minutes=cfg["infra_capacity_alert_cooldown_minutes"])
    servers = (
        db_session.query(infra.InfraServer)
        .filter(infra.InfraServer.is_archived.is_(False))
        .all()
    )
    for server in servers:
        if not infra.is_online(server, cfg, now):
            continue
        if not server.capacity:
            continue  # агент старой версии или надзор выключен
        verdict = infra.evaluate_capacity(server.capacity, warn_pct)
        level = verdict["level"]
        title = infra.server_title(server)

        if level == "ok":
            server.capacity_warn_alerted_at = None
            server.capacity_crit_alerted_at = None
            continue

        field = (
            "capacity_crit_alerted_at" if level == "crit"
            else "capacity_warn_alerted_at"
        )
        last = getattr(server, field)
        if last is not None and now - last < cooldown:
            continue

        problems = "\n".join(f"· {item}" for item in verdict["problems"])
        details = "\n".join(verdict["details"])
        if level == "crit":
            delivered = _send_alert(
                "🟥 <b>Нода упёрлась в лимиты</b>\n\n"
                f"Сервер: <b>{title}</b>\n\n"
                f"{problems}\n\n"
                + (f"<b>Показатели:</b>\n{details}\n\n" if details else "")
                + "ТСПУ здесь ни при чём: клиенты теряются из-за исчерпанных "
                "потолков самой ноды. Изменения DNS по этому серверу "
                "остановлены."
            )
        elif verdict.get("not_applied"):
            # Заголовок «пора поднять лимиты» здесь врёт: лимит уже задан в
            # /etc/sysctl.d, он просто не пережил загрузку. Совет поднимать
            # потолок увёл бы в сторону — нужен другой ремонт.
            delivered = _send_alert(
                "🟠 <b>Настройка ноды не применилась</b>\n\n"
                f"Сервер: <b>{title}</b>\n\n"
                f"{problems}\n\n"
                + (f"<b>Показатели:</b>\n{details}\n\n" if details else "")
                + "Клиенты пока не затронуты. Лечится прогоном "
                "fix-conntrack-persistence.sh — он закрепляет уже заданное "
                "значение, чтобы оно переживало перезагрузку."
            )
        elif verdict.get("collision_only"):
            # Гонки вставки при свободной таблице — фоновое состояние, а не
            # событие: в телеграм не шлём, эта же оценка и так видна в
            # карточке сервера (capacity_payload использует тот же
            # evaluate_capacity). Совет «поднять лимиты» здесь вреден.
            continue
        else:
            delivered = _send_alert(
                "🟠 <b>Пора поднять лимиты ноды</b>\n\n"
                f"Сервер: <b>{title}</b>\n\n"
                f"{problems}\n\n"
                + (f"<b>Показатели:</b>\n{details}\n\n" if details else "")
                + "Нода работает штатно, клиенты не затронуты — это "
                "профилактика."
            )
        if delivered:
            setattr(server, field, now)
            log.warning("infra: capacity %s on %s", level, title)


# --- высокая нагрузка канала ------------------------------------------------


def check_load(db_session) -> None:
    from engine import infra

    cfg = infra.get_settings(db_session)
    now = infra.utcnow()
    threshold = cfg["infra_load_threshold_pct"]
    recover = cfg["infra_load_recover_pct"]
    duration = timedelta(minutes=cfg["infra_load_duration_minutes"])
    cooldown = timedelta(minutes=cfg["infra_load_alert_cooldown_minutes"])

    servers = (
        db_session.query(infra.InfraServer)
        .filter(infra.InfraServer.is_archived.is_(False))
        .all()
    )
    for server in servers:
        if not infra.is_online(server, cfg, now):
            server.load_high_since = None
            continue
        limit = infra.effective_bandwidth_mbps(server)
        if not limit:
            continue
        raw = (
            db_session.query(infra.InfraTelemetry)
            .filter(
                infra.InfraTelemetry.server_id == server.id,
                infra.InfraTelemetry.ts >= now - timedelta(minutes=5),
            )
            .all()
        )
        rx_values = [r.rx_bps for r in raw if r.rx_bps is not None]
        tx_values = [r.tx_bps for r in raw if r.tx_bps is not None]
        if not rx_values and not tx_values:
            continue
        rx_avg = sum(rx_values) / len(rx_values) if rx_values else None
        tx_avg = sum(tx_values) / len(tx_values) if tx_values else None
        util = infra.utilization_pct(rx_avg, tx_avg, limit)
        if util is None:
            continue

        title = infra.server_title(server)
        if util >= threshold:
            if server.load_high_since is None:
                server.load_high_since = now
                continue
            if now - server.load_high_since < duration:
                continue
            if (
                server.load_alerted_at is not None
                and now - server.load_alerted_at < cooldown
            ):
                continue
            delivered = _send_alert(
                "🟠 <b>Высокая нагрузка на сеть</b>\n\n"
                f"Сервер: <b>{title}</b>\n"
                f"Интерфейс: <code>{server.wan_interface or '—'}</code>\n"
                f"Текущая нагрузка: <b>{util:.0f}%</b>\n"
                f"RX: {_fmt_mbps(rx_avg)} · TX: {_fmt_mbps(tx_avg)}\n"
                f"Расчётный лимит: {limit} Mbit/s\n\n"
                f"Нагрузка выше {threshold}% держится более "
                f"{cfg['infra_load_duration_minutes']} минут."
            )
            if delivered:
                server.load_alerted_at = now
                log.warning(
                    "infra: server %s high load %.0f%%, alert sent", title, util
                )
        elif util <= recover:
            server.load_high_since = None
            if server.load_alerted_at is not None:
                delivered = _send_alert(
                    "✅ <b>Нагрузка на сеть нормализовалась</b>\n\n"
                    f"Сервер: <b>{title}</b>\n"
                    f"Текущая нагрузка: <b>{util:.0f}%</b> "
                    f"(лимит {limit} Mbit/s)"
                )
                # Состояние снимаем только после доставки — иначе recovery
                # при сбое Telegram терялся бы навсегда
                if delivered:
                    server.load_alerted_at = None
                    log.info(
                        "infra: server %s load recovered %.0f%%", title, util
                    )
        else:
            # Гистерезисная зона (recover..threshold): накопление сбрасываем,
            # состояние «алерт отправлен» держим до восстановления
            server.load_high_since = None


# --- DNS-вотчер: перебалансировка нагрузки ≠ блокировка ---------------------


def watch_dns_changes(db_session) -> None:
    """Следит за A-записями привязанных доменов через Cloudflare.

    Появление НОВОЙ A-записи означает, что админ (или ротация) добавил ноду
    и трафик со старой закономерно просядет — не бан. Детектор при этом НЕ
    отключается: вместо паузы baseline сервера умножается на отношение
    записей (1->2 => x0.5) на время окна перекалибровки. Разгрузка попадает
    в новую норму и тревоги не даёт, реальный бан (обвал почти к нулю)
    детектируется по-прежнему.
    """
    from engine import cloudflare_dns
    from engine import infra

    if not cloudflare_dns.is_enabled():
        return
    cfg = infra.get_settings(db_session)
    window_hours = cfg["infra_dns_rebalance_window_hours"]
    now = infra.utcnow()
    recheck_cutoff = now - timedelta(minutes=10)

    servers = {
        server.id: server
        for server in db_session.query(infra.InfraServer)
        .filter(infra.InfraServer.is_archived.is_(False))
        .all()
    }
    if not servers:
        return
    domains = (
        db_session.query(infra.InfraServerDomain)
        .filter(infra.InfraServerDomain.server_id.in_(list(servers)))
        .all()
    )
    # Промежуточные состояния нашей же ротации (новая запись добавлена,
    # старая ещё не удалена) — не перебалансировка: не перекалибруем ни
    # сервер с активной заменой, ни СОСЕДЕЙ по затронутым доменам (иначе
    # вотчер, попав в окно между dns_add и dns_remove чужой ротации,
    # занизил бы норму сервера, чей трафик не менялся)
    active_replacements = (
        db_session.query(infra.InfraIpReplacement)
        .filter(
            infra.InfraIpReplacement.status.in_(
                infra.REPLACEMENT_ACTIVE_STATUSES
            )
        )
        .all()
    )
    rotating_server_ids = {row.server_id for row in active_replacements}
    rotating_domains: set[str] = set()
    for row in active_replacements:
        rotating_domains.update(row.domains or [])

    # Один домен может быть привязан к нескольким серверам — Cloudflare
    # спрашиваем один раз за проход
    records_cache: dict[str, list | None] = {}
    # Фактор на сервер за ПРОХОД применяется один раз (min по его доменам):
    # у сервера с двумя доменами одна перебалансировка видна в обеих строках,
    # перемножение дало бы x0.25 вместо x0.5. Перемножаются только
    # последовательные события разных проходов.
    pass_factor: dict[int, float] = {}
    for row in domains:
        if row.dns_checked_at is not None and row.dns_checked_at > recheck_cutoff:
            continue
        if row.domain not in records_cache:
            try:
                records_cache[row.domain] = cloudflare_dns.list_a_records(
                    row.domain
                )
            except cloudflare_dns.CloudflareError as e:
                log.warning("infra: dns watch %s failed: %s", row.domain, e)
                records_cache[row.domain] = None
        records = records_cache[row.domain]
        if records is None:
            continue
        current = sorted({r["content"] for r in records if r["content"]})
        previous = set(row.last_a_ips or [])
        recent = dict(row.recent_a_ips or {})
        # Перебалансировкой считаем только адреса, не встречавшиеся в
        # последние ~14 дней: возврат недавно убранной записи (например,
        # после работ) — не новая нода, повторный x-фактор не нужен
        truly_new = [
            ip for ip in current if ip not in previous and ip not in recent
        ]

        # Пустой previous — первый снимок: нечего сравнивать, только запомним.
        # Реагируем только на РОСТ числа записей: удаление записи может быть
        # симптомом проблемы, чувствительность из-за него не снижаем.
        if (
            previous
            and truly_new
            and len(current) > len(previous)
            and window_hours > 0
        ):
            if (
                row.server_id not in rotating_server_ids
                and row.domain not in rotating_domains
            ):
                factor = len(previous) / len(current)
                pass_factor[row.server_id] = min(
                    factor, pass_factor.get(row.server_id, 1.0)
                )
                log.info(
                    "infra: новые A-записи %s у %s (перебалансировка DNS)",
                    truly_new,
                    row.domain,
                )
        # Память недавних адресов: актуальные обновляем, старше 14 дней
        # выкидываем
        now_iso = now.isoformat(sep=" ", timespec="seconds")
        for ip in current:
            recent[ip] = now_iso
        memory_cutoff = (now - timedelta(days=14)).isoformat(
            sep=" ", timespec="seconds"
        )
        row.recent_a_ips = {
            ip: seen for ip, seen in recent.items() if seen >= memory_cutoff
        }
        row.last_a_ips = current
        row.dns_checked_at = now

    for server_id, factor in pass_factor.items():
        server = servers.get(server_id)
        if server is None:
            continue
        effective = factor
        if (
            server.baseline_scale is not None
            and server.baseline_scale_until is not None
            and server.baseline_scale_until > now
        ):
            # Последовательные изменения (1->2, потом 2->3) перемножаются
            effective = float(server.baseline_scale) * factor
        server.baseline_scale = round(max(0.1, effective), 3)
        server.baseline_scale_until = now + timedelta(hours=window_hours)
        log.info(
            "infra: baseline сервера %s перекалиброван x%s до %s "
            "(детектор продолжает работать)",
            infra.server_title(server),
            server.baseline_scale,
            server.baseline_scale_until,
        )


# --- anomaly detection ------------------------------------------------------


def detect_anomalies(db_session) -> None:
    from engine import infra

    cfg = infra.get_settings(db_session)
    now = infra.utcnow()
    servers = (
        db_session.query(infra.InfraServer)
        .filter(infra.InfraServer.is_archived.is_(False))
        .all()
    )
    for server in servers:
        if not infra.is_online(server, cfg, now):
            continue
        # Постоянное исключение из слежки за ТСПУ (внутренние серверы,
        # добавленные только ради графиков): ни аномалий, ни RIPE-замеров
        if not server.tspu_checks_enabled:
            continue
        # Warm-up нового сервера: baseline ещё не накоплен
        if server.first_seen_at is None or now - server.first_seen_at < timedelta(
            hours=cfg["infra_anomaly_warmup_hours"]
        ):
            continue
        # Подавление после рестарта агента и ребута сервера
        if (
            server.agent_started_at is not None
            and now - server.agent_started_at
            < timedelta(minutes=infra.ANOMALY_AGENT_RESTART_MINUTES)
        ):
            continue
        if server.boot_time is not None and now - server.boot_time < timedelta(
            minutes=infra.ANOMALY_REBOOT_MINUTES
        ):
            continue
        if (
            server.last_anomaly_at is not None
            and now - server.last_anomaly_at
            < timedelta(minutes=cfg["infra_anomaly_cooldown_minutes"])
        ):
            continue
        # Пауза детектора: плановые работы (ручная кнопка в карточке)
        if (
            server.anomaly_suppressed_until is not None
            and server.anomaly_suppressed_until > now
        ):
            continue
        # Нода выведена из DNS (ни один активный IP не в A-записях доменов):
        # обвал трафика на ней — намеренный слив, а не бан; без этой проверки
        # детектор гонял бы холостые ТСПУ-замеры каждый кулдаун, пока
        # baseline неделю не перестроится к нулю
        if not infra.server_is_in_dns(db_session, server):
            continue
        # Мёртвый xray роняет трафик так же, как бан ТСПУ, но замер тут
        # бессмысленен (IP-то жив и отвечает) — check_xray алертит об этой
        # поломке отдельно, а детектор молчит до восстановления процесса
        if (
            server.xray_process_running is False
            or server.xray_crash_loop is True
        ):
            continue
        open_anomaly = (
            db_session.query(infra.InfraAnomaly)
            .filter(
                infra.InfraAnomaly.server_id == server.id,
                infra.InfraAnomaly.status == "checking",
            )
            .first()
        )
        if open_anomaly is not None:
            continue
        active_replacement = (
            db_session.query(infra.InfraIpReplacement)
            .filter(
                infra.InfraIpReplacement.server_id == server.id,
                infra.InfraIpReplacement.status.in_(
                    infra.REPLACEMENT_ACTIVE_STATUSES
                ),
            )
            .first()
        )
        if active_replacement is not None:
            continue

        baseline = infra.compute_baseline(db_session, server.id, now, cfg)
        if baseline is None:
            continue
        # Перекалибровка после перебалансировки DNS: норма умножается на
        # scale, детектор продолжает работать против новой нормы
        if (
            server.baseline_scale is not None
            and server.baseline_scale_until is not None
            and server.baseline_scale_until > now
        ):
            scale = float(server.baseline_scale)
            baseline = dict(
                baseline,
                traffic_bps=baseline["traffic_bps"] * scale,
                connections=baseline["connections"] * scale,
                # min-baseline проверяется по немасштабированной норме
                floor_traffic_bps=baseline["traffic_bps"],
            )
        metrics = infra.current_minute_metrics(
            db_session, server.id, now, cfg["infra_anomaly_duration_minutes"]
        )
        verdict = infra.evaluate_network_drop(metrics, baseline, cfg)
        if verdict is None:
            continue

        title = infra.server_title(server)
        anomaly = infra.InfraAnomaly(
            server_id=server.id,
            kind="network_drop",
            status="checking",
            traffic_ratio=verdict["traffic_ratio"],
            connection_ratio=verdict["connection_ratio"],
            details={
                "current": {
                    "traffic_bps": verdict["current_traffic_bps"],
                    "connections": verdict["current_connections"],
                },
                "baseline": {
                    "traffic_bps": verdict["baseline_traffic_bps"],
                    "connections": verdict["baseline_connections"],
                },
            },
        )
        db_session.add(anomaly)
        server.last_anomaly_at = now
        db_session.flush()

        # Фаза 1 диагностики: пробы АДРЕСОВ контрольным посторонним именем.
        # Имена проверяются потом и только на адресе, признанном живым —
        # на мёртвом адресе падает всё, и вывод об имени был бы ложным.
        result = infra.start_ip_diagnosis(
            db_session, server, reason="NETWORK_TRAFFIC_ANOMALY"
        )
        anomaly.censor_run_ids = list(result["runs"].values())
        anomaly.details = dict(
            anomaly.details or {},
            control_name=result.get("control_name") or "",
            ip_runs=result["runs"],
            diagnosis_errors=result.get("errors") or [],
        )
        log.warning(
            "infra: anomaly detected server=%s traffic_ratio=%s conn_ratio=%s "
            "ip_probes=%s errors=%s",
            title,
            verdict["traffic_ratio"],
            verdict["connection_ratio"],
            list(result["runs"].keys()),
            result["errors"],
        )
        if not result["runs"]:
            anomaly.status = "error"
            anomaly.resolved_at = now
            _send_alert(
                "🟠 <b>Аномалия нагрузки: проверка ТСПУ не запустилась</b>\n\n"
                f"Сервер: <b>{title}</b>\n"
                f"Трафик: {verdict['traffic_ratio'] * 100:.0f}% от baseline · "
                f"соединения: {verdict['connection_ratio'] * 100:.0f}%\n\n"
                + "\n".join(result["errors"])
                + "\n\nПроверьте сервер вручную."
            )


def _anomaly_probe_maps(db_session, details: dict) -> tuple:
    """Пробы аномалии из БД: (по адресам, по именам, есть_незавершённые)."""
    from engine import infra

    ip_probes_raw, ip_pending = infra.collect_probes(
        db_session, details.get("ip_runs") or {}
    )
    sni_probes_raw, sni_pending = infra.collect_probes(
        db_session, details.get("sni_runs") or {}
    )
    # Ключи в JSONB хранятся строкой "адрес|имя"; классификатору нужны
    # адрес отдельно и пара отдельно
    ip_probes = {}
    for key, probe in ip_probes_raw.items():
        ip_probes[key.split("|", 1)[0]] = probe
    sni_probes = {}
    for key, probe in sni_probes_raw.items():
        ip, _, sni = key.partition("|")
        sni_probes[(ip, sni)] = probe
    return ip_probes, sni_probes, ip_pending or sni_pending


def _handle_control_burn(
    db_session, verdict, details, title, evidence_text
) -> None:
    """Реакция на выгорание контрольного имени: ротация и алерт.

    Контрольное имя — единственная опора вердикта по адресу, и его молчаливое
    выгорание превращает живой адрес в «забаненный» и запускает замену. Пока
    выгорание не доказано (proven), имя не снимается автоматически: снять
    рабочий контроль по одному подозрению хуже, чем один раз не вынести
    вердикт.
    """
    from engine import infra
    from engine import infra_diagnosis as diag

    burn = verdict.get("control_burn") or ""
    if not burn:
        return
    control_name = verdict.get("burned_control_name") or (
        details.get("control_name") or ""
    )
    if burn == diag.CONTROL_BURN_SUSPECTED:
        _send_alert(
            "🟠 <b>ТСПУ: подозрение на выгорание контрольного имени</b>\n\n"
            f"Сервер: <b>{title}</b>\n"
            f"Контрольное имя: <code>{control_name or '—'}</code>\n\n"
            "Оно не проходит ни на одном адресе сервера, и опровергнуть это "
            "нечем: клиентские имена там тоже не проверялись или не прошли. "
            "Вердикты не вынесены, автозамена не запускалась.\n\n"
            "Если адреса точно живы — впишите имя в настройку "
            "<code>infra_control_names_burned</code>, диагностика перейдёт на "
            "следующее имя из списка.\n\n"
            "<b>Проверки:</b>\n" + evidence_text
        )
        return

    rotated = infra.mark_control_name_burned(db_session, control_name)
    if not rotated:
        return
    remaining = infra.active_control_names(infra.get_settings(db_session))
    _send_alert(
        "🟡 <b>ТСПУ: контрольное имя выгорело, переключено</b>\n\n"
        f"Сервер: <b>{title}</b>\n"
        f"Выгоревшее имя: <code>{control_name or '—'}</code>\n"
        "Оно не проходит там, где клиентское имя проходит: адрес принимает "
        "соединения, под фильтром само контрольное имя.\n\n"
        + (
            "Дальше диагностика пойдёт с именем "
            f"<code>{remaining[0]}</code>."
            if remaining
            else "⚠️ Свободных контрольных имён не осталось — добавьте новое в "
            "<code>infra_control_names</code>."
        )
        + "\n\nСнять пометку можно, очистив "
        "<code>infra_control_names_burned</code> в настройках.\n\n"
        "<b>Проверки:</b>\n" + evidence_text
    )


def _live_summary(verdict) -> str:
    """«Что сейчас живо»: адреса и имена, реально прошедшие на них.

    Главный вопрос владельца при разборе ночной аварии — какой адрес рабочий
    и с какими именами он отработал. Вытаскиваем это из evidence, чтобы не
    приходилось читать журнал целиком.
    """
    from engine import infra_diagnosis as diag

    live: dict = {}
    for item in verdict.get("evidence") or []:
        if item.get("step") != "sni_probe" or item.get("result") != "pass":
            continue
        live.setdefault(item.get("ip"), []).append(item.get("sni"))
    ok_ips = [
        ip for ip, state in (verdict.get("ips") or {}).items()
        if state == diag.IP_OK
    ]
    if not ok_ips:
        return "Живых адресов не найдено."
    lines = []
    for ip in ok_ips:
        names = live.get(ip) or []
        lines.append(
            f"<code>{ip}</code>: "
            + (
                "проходят " + ", ".join(f"<code>{n}</code>" for n in names)
                if names
                else "жив по контрольному имени, клиентские на нём не "
                "проверялись"
            )
        )
    return "Живые адреса:\n" + "\n".join(lines)


def _finalize_anomaly(db_session, anomaly, server, title, details) -> None:
    """Классификация проб и действия по её итогам.

    Вердикты по адресам и по именам независимы. Имя считается забаненным,
    только если не проходит ни на одном живом адресе волны — своём или
    соседних серверов (имена общие); падает на одних и проходит на других —
    бан пары «адрес + имя», имя чистое. Имена, которые проверить было не на
    чем (живого адреса не нашлось), — неизвестные, а не чистые: раньше
    пустой список забаненных читался как «все чисты», и xyz уехал на .132
    без единой проверки (2026-09-02).

    Адрес меняется во ВСЕХ доменах сервера, и вердикт по имени этого не
    блокирует (решение владельца 09.09.2026): имена проверяются на кандидате
    (шаг verifying_names) ради отчёта, а публикуется домен независимо от их
    исхода — оставить клиентов на мёртвом адресе хуже любого исхода здесь.
    """
    from engine import infra
    from engine import infra_diagnosis as diag

    cfg = infra.get_settings(db_session)
    now = infra.utcnow()
    ip_probes, sni_probes, _pending = _anomaly_probe_maps(db_session, details)

    all_domains = infra.server_domain_targets(db_session, anomaly.server_id)
    domain_names = [item["domain"] for item in all_domains]
    # Проверяются клиентские имена, а не домены A-записей: домен, у которого
    # есть своё имя, в рукопожатие не попадает и под фильтр не может. Имена
    # дедуплицируются — одно имя на нескольких доменах проверяется один раз
    # Непроверяемые имена (помечены после «ни разу не проходило») в вердикт
    # не входят: ответа от них не будет, а каждый раз «неизвестно» — шум
    skip_names = infra.unprobeable_snis(cfg)
    probe_names: list[str] = []
    skipped_names: list[str] = []
    for item in all_domains:
        for name in diag.domain_snis(item):
            if not name:
                continue
            if name.lower() in skip_names:
                if name not in skipped_names:
                    skipped_names.append(name)
            elif name not in probe_names:
                probe_names.append(name)
    # Имена общие для нескольких нод: что известно о них по соседним
    # серверам той же волны, входит в вердикт
    external = (
        infra.recent_name_results(
            db_session,
            probe_names,
            since=now - timedelta(minutes=infra.NAME_WAVE_WINDOW_MINUTES),
            exclude_anomaly_id=anomaly.id,
        )
        if probe_names
        else {}
    )

    verdict = diag.classify(
        ip_probes=ip_probes,
        sni_probes=sni_probes,
        known_good_snis=infra.names_ever_seen_passing(db_session, probe_names),
        control_name_set=set(
            infra.parse_control_names(cfg.get("infra_control_names"))
        ),
        node_healthy=not (
            server is not None
            and (
                server.xray_process_running is False
                or server.xray_crash_loop is True
            )
        ),
        control_name=details.get("control_name") or "",
        external_sni_results=external,
    )
    # Имя с вердиктом «не проверено» (срезано лимитом, некуда было проверить
    # или никогда не проходило) чистым не считается — оно будет проверено на
    # кандидате перед публикацией
    tested = {
        name for name, state in verdict["snis"].items()
        if state != diag.SNI_UNKNOWN
    }
    unknown_snis = [name for name in probe_names if name not in tested]
    unproven_snis = list(verdict.get("unproven_snis") or [])
    unconfirmed_ips = list(verdict.get("unconfirmed_ips") or [])
    if unknown_snis and verdict["actionable"] and verdict["blocked_ips"]:
        verdict["evidence"].append(
            {
                "step": "sni_unknown", "result": "warn", "snis": unknown_snis,
                "text": (
                    "Имена " + ", ".join(unknown_snis)
                    + ": на живом адресе не проверялись"
                    + (" — живого адреса не нашлось" if not sni_probes else "")
                    + "; статус неизвестен, чистыми не считаются: каждое "
                    "будет проверено на кандидате до публикации"
                ),
            }
        )
    if skipped_names:
        verdict["evidence"].append(
            {
                "step": "sni_skipped", "result": "info", "snis": skipped_names,
                "text": (
                    "Имена " + ", ".join(skipped_names)
                    + ": помечены непроверяемыми (ни разу не проходили пробу — "
                    "инбаунд Reality или имя, которого нода не обслуживает), "
                    "не проверялись; снять пометку — очистить "
                    "infra_unprobeable_snis"
                ),
            }
        )
    evidence_text = diag.evidence_text(verdict["evidence"])
    details["evidence"] = verdict["evidence"]
    details["verdict"] = {
        "blocked_ips": verdict["blocked_ips"],
        "blocked_snis": verdict["blocked_snis"],
        "pair_blocked": verdict["pair_blocked"],
        "unknown_snis": unknown_snis,
        "unproven_snis": unproven_snis,
        "unconfirmed_ips": unconfirmed_ips,
        "confidence": verdict["confidence"],
        "actionable": verdict["actionable"],
        "control_burn": verdict.get("control_burn") or "",
    }
    anomaly.details = dict(details)
    anomaly.resolved_at = now

    _handle_control_burn(db_session, verdict, details, title, evidence_text)

    # Такое имя помечается непроверяемым и алертится один раз: дальше оно
    # не проверяется и не попадает в «ни разу не проходило» на каждой аномалии
    newly_marked = infra.mark_snis_unprobeable(db_session, unproven_snis) if unproven_snis else []
    if newly_marked:
        _send_alert(
            "🟡 <b>ТСПУ: имя ни разу не проходило</b>\n\n"
            f"Сервер: <b>{title}</b>\n"
            "Имена: "
            + ", ".join(f"<code>{n}</code>" for n in newly_marked)
            + "\n\nПомечены непроверяемыми: больше не проверяются и не "
            "алертятся. Если имя должно проверяться — очистите "
            "<code>infra_unprobeable_snis</code> в настройках.\n\n"
            "Ни одного успешного замера за всю историю. Вердикт "
            "«забанено» намеренно НЕ вынесен — у такой картины есть причины "
            "помимо ТСПУ:\n\n"
            "1. <b>Имя ведёт на инбаунд Reality.</b> Проба — обычный "
            "ClientHello, Reality на него не отвечает: он пересылает "
            "соединение в <code>dest</code>, и если там nginx с "
            "<code>proxy_protocol</code>, соединение рвётся. Зонд видит "
            "«TCP проходит, обрыв на ClientHello» — неотличимо от фильтра. "
            "Клиенты при этом работают: у них полноценное рукопожатие "
            "Reality. <b>Такие имена этой пробой не проверяются в принципе, "
            "и вердикта по ним не будет никогда.</b>\n"
            "2. Опечатка в карточке сервера или имя, которого нода не "
            "обслуживает (ACL haproxy, serverNames инбаунда): на ноде без "
            "<code>default_backend</code> неизвестный SNI рвётся молча.\n"
            "3. Имя действительно под фильтром с самого начала.\n\n"
            "Отличить 1 и 2 от 3 можно с машины вне РФ: "
            "<code>openssl s_client -connect АДРЕС:443 -servername ИМЯ</code>. "
            "Если и оттуда рукопожатие не проходит — ТСПУ ни при чём.\n\n"
            "<b>Как это выяснено:</b>\n" + evidence_text
        )

    if unconfirmed_ips:
        # Молчаливое бездействие — такая же потеря доверия, как ложная
        # замена: админ должен видеть, что адрес подозрителен и почему
        # вердикт не вынесен
        _send_alert(
            "🟠 <b>ТСПУ: адрес подозрителен, подтвердить нечем</b>\n\n"
            f"Сервер: <b>{title}</b>\n"
            "Адреса: "
            + ", ".join(f"<code>{ip}</code>" for ip in unconfirmed_ips)
            + "\n\nКонтрольное имя на них не проходит, но второго "
            "свидетеля нет: клиентские имена на этих адресах не проверялись "
            "или ни разу не наблюдались рабочими. Автозамена НЕ запущена "
            "намеренно — одного контрольного имени для правки боевого DNS "
            "недостаточно, оно бывает и выгоревшим, и забаненным в паре "
            "именно с этим адресом.\n\nПроверьте адреса руками; если бан "
            "настоящий — замените адрес кнопкой в карточке.\n\n"
            "<b>Проверки:</b>\n" + evidence_text
        )

    blocked_ips = verdict["blocked_ips"]
    blocked_snis = verdict["blocked_snis"]
    pair_blocked = verdict["pair_blocked"]

    if not blocked_ips and not blocked_snis:
        anomaly.status = "dismissed"
        # Трафик упал, а ТСПУ ни при чём — детектор молчит всё дольше с
        # каждой чистой аномалией подряд (24 ч → 72 ч → 168 ч): нода,
        # выведенная из DNS, не гоняет замеры каждый кулдаун
        snooze_hours = infra.clean_anomaly_snooze_hours(
            db_session, anomaly.server_id, cfg, exclude_anomaly_id=anomaly.id
        )
        if server is not None and snooze_hours > 0:
            until = now + timedelta(hours=snooze_hours)
            if server.anomaly_suppressed_until is None or server.anomaly_suppressed_until < until:
                server.anomaly_suppressed_until = until
            details["clean_snooze_hours"] = snooze_hours
            anomaly.details = dict(details)
        log.info(
            "infra: anomaly dismissed server=%s (адреса и имена чисты; "
            "бан пары: %s; детектор на паузе %s ч)",
            title, pair_blocked or "нет", snooze_hours,
        )
        return

    anomaly.status = "confirmed"
    log.warning(
        "infra: anomaly confirmed server=%s blocked_ips=%s blocked_snis=%s "
        "pair_blocked=%s unknown_snis=%s confidence=%s",
        title, blocked_ips, blocked_snis, pair_blocked, unknown_snis,
        verdict["confidence"],
    )

    # A-запись переставляется у всех доменов независимо от вердикта по
    # именам; забаненное имя чинится правкой конфигов ноды и хостов
    # Remnawave, а не DNS, и уходит отдельным алертом
    safe, unsafe = diag.domains_safe_to_repoint(all_domains, blocked_snis)
    safe_names = [item["domain"] for item in safe]
    unsafe_names = [item["domain"] for item in unsafe]
    # Свод по доменам считаем ПОСЛЕ вердиктов по именам: имя — сущность
    # паркового уровня (одна маска стоит на многих нодах), домен — нет
    blocked_hits = diag.domains_with_blocked_sni(all_domains, blocked_snis)
    # A-запись переставляется у всех доменов; бан имени лечится в конфигах
    sni_banned = {hit["domain"]: hit["blocked"] for hit in blocked_hits}
    domain_snis = {
        item["domain"]: diag.domain_snis(item) for item in all_domains
    }

    if sni_banned:
        # Алерт обязан называть ИМЕННО забаненные имена: при трёх именах на
        # домен «первое имя домена» отправило бы менять работающий протокол
        _send_alert(
            "🟥 <b>ТСПУ: заблокировано клиентское имя (SNI)</b>\n\n"
            f"Сервер: <b>{title}</b>\n"
            "Под фильтром: "
            + ", ".join(f"<code>{n}</code>" for n in blocked_snis)
            + "\n\n"
            + "\n".join(
                f"<code>{hit['domain']}</code>: в бане "
                + ", ".join(f"<code>{n}</code>" for n in hit["blocked"])
                + (
                    "; продолжают работать "
                    + ", ".join(f"<code>{n}</code>" for n in hit["clean"])
                    if hit["clean"]
                    else "; чистых имён у домена не осталось"
                )
                for hit in blocked_hits
                if hit["domain"] in sni_banned
            )
            + "\n\n<b>Требуется ручное вмешательство:</b> сменить имя в "
            "конфигурации ноды (ACL haproxy и serverNames соответствующего "
            "инбаунда) и в хостах Remnawave.\n\n<b>DNS это не блокирует:</b> "
            "если адрес забанен, A-записи переставляются на живой адрес как "
            "обычно — клиенты остальных имён чинятся сразу, а забаненное имя "
            "не заработает ни на старом адресе, ни на новом.\n\n"
            "<b>Как это выяснено:</b>\n" + evidence_text
        )


    if not blocked_ips:
        return

    live_block = _live_summary(verdict)

    if not verdict["actionable"]:
        _send_alert(
            "🟠 <b>ТСПУ: похоже на бан адреса, но уверенности мало</b>\n\n"
            f"Сервер: <b>{title}</b>\n"
            "Адреса: "
            + ", ".join(f"<code>{ip}</code>" for ip in blocked_ips)
            + "\n\nАвтозамена не выполнена.\n\n"
            + live_block
            + "\n\n<b>Проверки:</b>\n" + evidence_text
        )
        return

    if not cfg["infra_auto_replace_enabled"]:
        _send_alert(
            "🔴 <b>ТСПУ подтвердил блокировку адреса, автозамена выключена</b>"
            f"\n\nСервер: <b>{title}</b>\n"
            "Адреса: "
            + ", ".join(f"<code>{ip}</code>" for ip in blocked_ips)
            + "\n\nЗамените адрес вручную "
            "(настройка infra_auto_replace_enabled).\n\n"
            + live_block
            + "\n\n<b>Проверки:</b>\n" + evidence_text
        )
        return

    # Гейт «менять адрес вообще» считается по ИМЕНАМ, а не по доменам: хотя
    # бы одно чистое, спорное или непроверенное имя — замена идёт (спорное
    # проверится на кандидате). Отменяем только когда у сервера не осталось
    # ни одного работающего имени: новый адрес ничего не починит, а резерв
    # сгорит. Пустой safe_names — тот же случай в терминах доменов: всем
    # доменам, уходящим в эфир самими собой, публиковаться нечем.
    all_names_blocked = bool(probe_names) and all(
        name in set(blocked_snis) for name in probe_names
    )
    if all_names_blocked or not safe_names:
        _send_alert(
            "🟥 <b>ТСПУ: заблокированы и адрес, и все имена сервера</b>\n\n"
            f"Сервер: <b>{title}</b>\n"
            "Адреса: "
            + ", ".join(f"<code>{ip}</code>" for ip in blocked_ips)
            + "\nИмена: "
            + ", ".join(
                f"<code>{n}</code>"
                for n in (blocked_snis if all_names_blocked else unsafe_names)
            )
            + "\n\nЗамена адреса не выполнялась: переводить клиентов внутри "
            "этого сервера некуда — сначала смените имена в конфигурации ноды "
            "и хостах Remnawave, потом замените адрес кнопкой в карточке.\n\n"
            "<b>Проверки:</b>\n" + evidence_text
        )
        return

    for blocked_ip in blocked_ips:
        try:
            infra.request_replacement(
                db_session,
                anomaly.server_id,
                blocked_ip,
                created_by="auto:anomaly",
                anomaly_id=anomaly.id,
                domains=domain_names,
                banned_names=unsafe_names,
                domain_snis=domain_snis,
                sni_banned=sni_banned,
            )
        except infra.InfraError as e:
            log.warning(
                "infra: cannot create replacement for %s: %s",
                blocked_ip, e.message,
            )


def process_anomalies(db_session) -> None:
    from engine import infra
    from engine import infra_diagnosis as diag

    now = infra.utcnow()
    anomalies = (
        db_session.query(infra.InfraAnomaly)
        .filter(infra.InfraAnomaly.status.in_(("checking", "checking_sni")))
        .all()
    )
    for anomaly in anomalies:
        server = db_session.get(infra.InfraServer, anomaly.server_id)
        title = infra.server_title(server) if server else f"#{anomaly.server_id}"
        details = dict(anomaly.details or {})
        timed_out = bool(
            anomaly.created_at
            and now - anomaly.created_at
            > timedelta(minutes=infra.ANOMALY_CHECK_TIMEOUT_MINUTES)
        )

        if "ip_runs" not in details:
            # Аномалия создана прежней версией: закрываем, не гадая по
            # старым данным — новая диагностика запустится следующей
            anomaly.status = "error"
            anomaly.resolved_at = now
            log.info(
                "infra: anomaly #%s in legacy format, closed without verdict",
                anomaly.id,
            )
            continue

        _ip_probes, _sni_probes, pending = _anomaly_probe_maps(
            db_session, details
        )
        if pending and not timed_out:
            continue

        if anomaly.status == "checking":
            ip_probes, _s, _p = _anomaly_probe_maps(db_session, details)
            live_ips = [
                ip for ip, probe in ip_probes.items()
                if diag.probe_passed(probe)
            ]
            # Фаза 2 идёт по ОБЕИМ группам адресов. На живом адресе она
            # отличает бан имени от бана пары «адрес + имя». На каждом
            # подозрительном отвечает на другой вопрос: ходит ли через него
            # хоть что-нибудь. Без этой пробы вердикт «адрес забанен»
            # держался бы на одном свидетеле — контрольном имени, — а оно
            # бывает и выгоревшим, и забаненным в паре именно с этим
            # адресом; так живая нода и уехала на резерв 07.09.2026.
            # Жёсткая форма (TCP не устанавливается) исключается: имя там не
            # пройдёт по определению — рукопожатия не было, имя в эфир не
            # уходило, — и зонды жгли бы кредиты зря.
            suspect_ips = [
                ip for ip, probe in ip_probes.items()
                if probe is not None
                and not diag.probe_passed(probe)
                and not diag.is_hard_block(probe)
            ]
            probe_ips = live_ips[: infra.SNI_PROBE_MAX_IPS]
            if probe_ips or suspect_ips:
                started = infra.start_sni_diagnosis(
                    db_session, server, probe_ips, suspect_ips=suspect_ips,
                    reason="ANOMALY_SNI_DIAGNOSIS",
                )
                if started["runs"]:
                    details["sni_runs"] = started["runs"]
                    details["sni_probe_ip"] = (probe_ips or suspect_ips)[0]
                    details["sni_probe_ips"] = probe_ips
                    details["sni_witness_ips"] = suspect_ips
                    details["sni_probe_mode"] = (
                        "live+witness" if probe_ips and suspect_ips
                        else "live" if probe_ips
                        else "witness"
                    )
                    anomaly.details = dict(details)
                    anomaly.censor_run_ids = list(
                        (details.get("ip_runs") or {}).values()
                    ) + list(started["runs"].values())
                    anomaly.status = "checking_sni"
                    continue
            # Проверять имена не на чем: вердикт выносится по тому, что уже
            # известно
            _finalize_anomaly(db_session, anomaly, server, title, details)
            continue

        _finalize_anomaly(db_session, anomaly, server, title, details)

# --- замена IP --------------------------------------------------------------


def _rlog(replacement, step: str, message: str, **data) -> None:
    """Запись в журнал замены. data — структурные поля шага (id прогонов,
    списки имён): журнал — единственное JSON-хранилище заявки, новых колонок
    под них не заводим; карточка показывает только ts и message."""
    from engine import infra

    # Повтор той же ошибки (например, Cloudflare недоступен на каждом тике)
    # не плодит одинаковые записи — лог не растёт бесконечно при ретраях
    existing = replacement.log or []
    if existing and existing[-1].get("step") == step and existing[-1].get(
        "message"
    ) == message:
        return
    entry = {"ts": infra.utcnow().isoformat(sep=" ", timespec="seconds"),
             "step": step, "message": message}
    entry.update({key: value for key, value in data.items() if value is not None})
    replacement.log = existing + [entry]
    log.info("infra: replacement #%s %s: %s", replacement.id, step, message)


def _rlog_last(replacement, step: str) -> dict | None:
    """Последняя запись журнала с таким шагом (или None)."""
    for entry in reversed(replacement.log or []):
        if entry.get("step") == step:
            return entry
    return None


def process_replacements(db_session) -> None:
    from engine import infra

    replacements = (
        db_session.query(infra.InfraIpReplacement)
        .filter(
            infra.InfraIpReplacement.status.in_(
                infra.REPLACEMENT_ACTIVE_STATUSES
            )
        )
        .order_by(infra.InfraIpReplacement.id)
        .all()
    )
    for replacement in replacements:
        try:
            _process_replacement(db_session, replacement)
        except Exception:
            log.exception(
                "infra: replacement #%s processing failed", replacement.id
            )


def _fail_replacement(db_session, replacement, server, message: str) -> None:
    from engine import infra

    replacement.status = "failed"
    replacement.error = message[:1000]
    replacement.finished_at = infra.utcnow()
    _rlog(replacement, "failed", message)
    # Висящую команду агенту гасим: замена закрыта, исполнять её поздно
    if replacement.command_id:
        command = db_session.get(infra.InfraAgentCommand, replacement.command_id)
        if command is not None and command.status in ("pending", "sent"):
            command.status = "expired"
            command.finished_at = infra.utcnow()
    title = infra.server_title(server) if server else f"#{replacement.server_id}"
    _send_alert(
        "🔴 <b>Замена IP не удалась</b>\n\n"
        f"Сервер: <b>{title}</b>\n"
        f"IP: <code>{replacement.old_ip}</code>"
        + (
            f" → <code>{replacement.new_ip}</code>"
            if replacement.new_ip
            else ""
        )
        + f"\n\n{message}\n\nТребуется ручное вмешательство."
    )


def _process_replacement(db_session, replacement) -> None:
    from engine import cloudflare_dns
    from engine import infra

    now = infra.utcnow()
    server = db_session.get(infra.InfraServer, replacement.server_id)
    if server is None:
        _fail_replacement(db_session, replacement, None, "Сервер удалён")
        return
    title = infra.server_title(server)

    # Зависшие замены закрываем с алертом: ни один шаг не должен висеть вечно.
    # Отсчёт от created_at: updated_at бампается каждым ретраем и никогда бы
    # не дал таймауту сработать при постоянном сбое Cloudflare/агента.
    if replacement.created_at and now - replacement.created_at > timedelta(
        minutes=infra.REPLACEMENT_STUCK_MINUTES
    ):
        _fail_replacement(
            db_session,
            replacement,
            server,
            f"Замена зависла в статусе {replacement.status} дольше "
            f"{infra.REPLACEMENT_STUCK_MINUTES} минут",
        )
        return

    if replacement.status == "pending":
        _replacement_step_pending(db_session, replacement, server, title)
    elif replacement.status == "installing":
        _replacement_step_installing(db_session, replacement, server)
    elif replacement.status == "verifying":
        _replacement_step_verifying(db_session, replacement, server)
    elif replacement.status == "verifying_names":
        _replacement_step_verifying_names(db_session, replacement, server)
    elif replacement.status == "dns_add":
        _replacement_step_dns_add(db_session, replacement, server)
    elif replacement.status == "dns_remove":
        _replacement_step_dns_remove(db_session, replacement, server, title)
    elif replacement.status == "confirming":
        _replacement_step_confirming(db_session, replacement, server, title)


def _replacement_step_pending(db_session, replacement, server, title) -> None:
    from engine import cloudflare_dns
    from engine import infra

    now = infra.utcnow()
    domains = replacement.domains or []
    if not domains:
        replacement.status = "manual_required"
        replacement.finished_at = now
        _rlog(replacement, "manual_required", "У сервера нет привязанных доменов")
        _send_alert(
            "🔴 <b>Требуется ручное вмешательство</b>\n\n"
            "ТСПУ подтвердил проблему с IP:\n"
            f"<code>{replacement.old_ip}</code>\n\n"
            f"Сервер: <b>{title}</b>\n\n"
            "У сервера нет привязанных доменов — автоматическое переключение "
            "DNS невозможно. Привяжите домены в карточке сервера."
        )
        return

    if not cloudflare_dns.is_enabled():
        _fail_replacement(
            db_session,
            replacement,
            server,
            "CLOUDFLARE_API_TOKEN не настроен — автозамена невозможна",
        )
        return

    # Текущие A-записи доменов: занятые адреса не кандидаты
    used_ips: set[str] = set()
    records_by_domain: dict[str, list[dict]] = {}
    try:
        for domain in domains:
            records = cloudflare_dns.list_a_records(domain)
            records_by_domain[domain] = records
            used_ips.update(
                record["content"] for record in records if record["content"]
            )
    except cloudflare_dns.CloudflareError as e:
        _rlog(replacement, "pending", f"Cloudflare недоступен: {e}")
        return  # retry на следующем тике; stuck-таймаут закроет при постоянном сбое

    # Старого IP нет ни в одной A-записи — переключать нечего (§31 ТЗ):
    # например, забаненный запасной адрес на интерфейсе, который клиентов
    # не обслуживал. Помечаем блок и закрываем без DNS-операций — иначе
    # замена съела бы резерв и дописала лишнюю запись в DNS.
    old_in_dns = any(
        replacement.old_ip in {r["content"] for r in records}
        for records in records_by_domain.values()
    )
    if not old_in_dns:
        replacement.status = "done"
        replacement.finished_at = now
        _rlog(
            replacement,
            "noop",
            f"IP {replacement.old_ip} не используется в A-записях доменов — "
            "переключать нечего, адрес помечен заблокированным",
        )
        _mark_old_ip_blocked(db_session, replacement)
        return

    candidate = _pick_replacement_candidate(
        db_session, replacement, used_ips
    )
    if candidate is None:
        _replacement_no_reserve(
            db_session, replacement, server, title, records_by_domain
        )
        return

    replacement.new_ip = candidate.ip
    _rlog(
        replacement,
        "candidate",
        f"Выбран резервный IP {candidate.ip} "
        f"({'на интерфейсе' if candidate.on_interface else 'требует установки'})",
    )
    if candidate.on_interface:
        # Адрес уже на интерфейсе: установка не нужна, а проверка адреса и
        # имён на нём — нужна (2026-09-02: .198 стоял на интерфейсе, был жив
        # по контролю, а два имени на нём не проходили)
        _start_candidate_verification(db_session, replacement, server)
        return
    try:
        command = infra.create_ensure_ip_command(
            db_session, server, candidate, created_by="auto:replacement"
        )
    except infra.InfraError as e:
        _fail_replacement(db_session, replacement, server, e.message)
        return
    replacement.command_id = command.id
    replacement.status = "installing"
    _rlog(
        replacement,
        "installing",
        f"Команда ensure_ip #{command.id} отправлена агенту",
    )


def _pick_replacement_candidate(db_session, replacement, used_ips):
    """Резервный IP: v4, не заблокирован, не старый, не занят в DNS.

    Порядок предпочтения: ручной резерв не на интерфейсе → ручной на
    интерфейсе → обнаруженный. IP другого сервера не рассматривается никогда.
    """
    import ipaddress as ipaddress_module

    from engine import infra

    rows = (
        db_session.query(infra.InfraServerIp)
        .filter(infra.InfraServerIp.server_id == replacement.server_id)
        .all()
    )
    other_active = (
        db_session.query(infra.InfraIpReplacement)
        .filter(
            infra.InfraIpReplacement.server_id == replacement.server_id,
            infra.InfraIpReplacement.status.in_(
                infra.REPLACEMENT_ACTIVE_STATUSES
            ),
            infra.InfraIpReplacement.id != replacement.id,
        )
        .all()
    )
    # Чужие old_ip и УЖЕ ВЫБРАННЫЕ new_ip параллельных замен исключаются:
    # при двух заблокированных IP на сервере обе замены в одном тике иначе
    # выбрали бы один и тот же резерв (в DNS он появится только на шаге
    # dns_add следующего тика)
    other_active_ips = {row.old_ip for row in other_active} | {
        row.new_ip for row in other_active if row.new_ip
    }

    def eligible(row):
        if row.ip == replacement.old_ip or row.ip in other_active_ips:
            return False
        if row.blocked_at is not None:
            return False
        if row.ip in used_ips:
            return False
        try:
            address = ipaddress_module.ip_address(row.ip)
        except ValueError:
            return False
        # Только публичные v4: приватные адреса docker/warp-интерфейсов
        # попадают в инвентарь со старых версий коллектора и не должны
        # оказаться в DNS ни при каких обстоятельствах
        return address.version == 4 and address.is_global

    candidates = [row for row in rows if eligible(row)]
    candidates.sort(
        key=lambda row: (
            0 if (row.source == "manual" and not row.on_interface) else
            1 if row.source == "manual" else
            2 if not row.on_interface else 3,
            row.ip,
        )
    )
    return candidates[0] if candidates else None


def _replacement_no_reserve(
    db_session, replacement, server, title, records_by_domain
) -> None:
    """Резерва нет: удалить заблокированную A-запись, где их несколько,
    иначе — только алерт о ручном вмешательстве (§29 ТЗ)."""
    from engine import cloudflare_dns
    from engine import infra

    now = infra.utcnow()
    old_ip = replacement.old_ip
    removable = []
    single_record_domains = []
    unaffected = []
    for domain, records in records_by_domain.items():
        contents = [record["content"] for record in records]
        if old_ip not in contents:
            unaffected.append(domain)
        elif len(records) >= 2:
            removable.append(domain)
        else:
            single_record_domains.append(domain)

    domains_line = "\n".join(
        f"<code>{domain}</code>" for domain in records_by_domain
    )
    if removable and not single_record_domains:
        try:
            for domain in removable:
                cloudflare_dns.delete_a_records(domain, old_ip)
                _rlog(
                    replacement,
                    "dns_cleanup",
                    f"Удалена A-запись {domain} -> {old_ip} "
                    "(остались другие A-записи)",
                )
        except cloudflare_dns.CloudflareError as e:
            _rlog(replacement, "dns_cleanup", f"Cloudflare недоступен: {e}")
            return  # retry
        replacement.status = "dns_cleanup"
        replacement.finished_at = now
        _mark_old_ip_blocked(db_session, replacement)
        _send_alert(
            "🟡 <b>Заблокированный IP удалён из DNS</b>\n\n"
            f"Сервер: <b>{title}</b>\n"
            f"IP: <code>{old_ip}</code>\n"
            "Домены:\n" + "\n".join(f"<code>{d}</code>" for d in removable)
            + "\n\nСвободных резервных IP у сервера нет, но у доменов было "
            "несколько A-записей — заблокированная удалена, остальные "
            "продолжают работать.\n\n"
            "⚠️ Добавьте серверу резервные IP: следующая блокировка "
            "потребует ручного вмешательства."
        )
        return

    replacement.status = "manual_required"
    replacement.finished_at = now
    _rlog(
        replacement,
        "manual_required",
        "Нет свободных резервных IP"
        + (
            f"; единственная A-запись у: {', '.join(single_record_domains)}"
            if single_record_domains
            else ""
        ),
    )
    _send_alert(
        "🔴 <b>Требуется ручное вмешательство</b>\n\n"
        "ТСПУ подтвердил проблему с IP:\n"
        f"<code>{old_ip}</code>\n\n"
        f"Сервер: <b>{title}</b>\n"
        + ("Домены:\n" + domains_line + "\n\n" if domains_line else "\n")
        + "Для данного сервера отсутствуют свободные резервные IP.\n"
        "Автоматическая замена невозможна."
        + (
            "\n\nУ доменов: "
            + ", ".join(f"<code>{d}</code>" for d in single_record_domains)
            + " единственная A-запись указывает на проблемный IP — "
            "удалять её нельзя, замените адрес вручную."
            if single_record_domains
            else ""
        )
    )


def _replacement_step_installing(db_session, replacement, server) -> None:
    from engine import infra

    command = (
        db_session.get(infra.InfraAgentCommand, replacement.command_id)
        if replacement.command_id
        else None
    )
    if command is None:
        _fail_replacement(
            db_session, replacement, server, "Команда ensure_ip потеряна"
        )
        return
    if command.status in ("pending", "sent"):
        return  # ждём агента
    if command.status == "ok":
        _rlog(
            replacement,
            "installed",
            f"IP {replacement.new_ip} установлен на интерфейс "
            f"(agent: {command.result or {}})",
        )
        _start_candidate_verification(db_session, replacement, server)
        return
    _fail_replacement(
        db_session,
        replacement,
        server,
        f"Агент не смог установить IP {replacement.new_ip}: "
        f"{command.error or command.status}",
    )


def _start_candidate_verification(db_session, replacement, server) -> None:
    """Проверка кандидата контрольным именем ДО публикации в DNS.

    Адрес уже поднят на интерфейсе, но клиентам ещё не отдан: измерение
    целится в адрес напрямую и несёт имя в рукопожатии, поэтому DNS для
    проверки не нужен. Публиковать адрес вслепую нельзя — именно так в
    аварии в DNS попал уже заблокированный адрес, и понять это было нечем.
    Контроль доказывает только чистоту адреса; следом каждое клиентское имя
    проверяется на кандидате отдельно (_start_names_verification).
    """
    from engine import infra

    cfg = infra.get_settings(db_session)
    control_names = infra.active_control_names(cfg)
    if not control_names:
        # Проверять нечем: не блокируем замену, но говорим об этом в журнале
        _rlog(
            replacement,
            "verify_skipped",
            "Контрольное имя не задано — кандидат публикуется без проверки "
            "адреса",
        )
        _start_names_verification(db_session, replacement)
        return

    try:
        started = infra.start_diagnosis_probes(
            db_session,
            [(replacement.new_ip, control_names[0])],
            reason="CANDIDATE_VERIFY",
            light=False,
        )
    except Exception as e:
        # Проверка — усиление, а не обязательное условие: если её нельзя
        # провести (нет ключа Atlas, недоступна сеть), замена продолжается
        # по прежнему сценарию, но след в журнале остаётся
        log.exception("infra: candidate verification could not start")
        _rlog(
            replacement,
            "verify_skipped",
            f"Проверку кандидата запустить не удалось ({e!r}) — публикуем "
            "без неё",
        )
        _start_names_verification(db_session, replacement)
        return
    run_ids = list(started["runs"].values())
    if not run_ids:
        _rlog(
            replacement,
            "verify_skipped",
            "Проверку кандидата запустить не удалось ("
            + "; ".join(started["errors"])
            + ") — публикуем без неё",
        )
        _start_names_verification(db_session, replacement)
        return

    replacement.verify_run_id = run_ids[0]
    replacement.status = "verifying"
    _rlog(
        replacement,
        "verifying",
        f"Проверяем кандидата {replacement.new_ip} контрольным именем "
        f"{control_names[0]} до публикации в DNS",
    )


def _replacement_step_verifying(db_session, replacement, server) -> None:
    """Итог проверки кандидата: перебирать дальше или проверять имена."""
    from engine import infra
    from engine import infra_diagnosis as diag

    run = (
        db_session.get(infra.CensorCheckRun, replacement.verify_run_id)
        if replacement.verify_run_id
        else None
    )
    if run is None:
        _rlog(
            replacement,
            "verify_skipped",
            "Прогон проверки кандидата потерян — публикуем без него",
        )
        _start_names_verification(db_session, replacement)
        return
    if run.status == "pending":
        return  # ждём зонды
    probe = infra.probe_from_run(run)
    if probe is None:
        _rlog(
            replacement,
            "verify_skipped",
            f"Проверка кандидата не дала результата ({run.error_message or run.status})"
            " — публикуем без неё",
        )
        _start_names_verification(db_session, replacement)
        return

    if diag.probe_passed(probe):
        _rlog(
            replacement,
            "verified",
            f"Кандидат {replacement.new_ip} чист: контрольное имя проходит "
            f"({diag.availability_pct(probe)}% зондов, "
            f"{probe['ok_probes']}/{probe['total_probes']}) — проверяем имена",
        )
        _start_names_verification(db_session, replacement)
        return

    # Кандидат сам под фильтром: в DNS он не попадает, помечаем и берём
    # следующий резерв — алгоритм перебирает варианты, а не сдаётся с первого
    stage = diag.dominant_stage(probe) or "нет ответа"
    _rlog(
        replacement,
        "candidate_blocked",
        f"Кандидат {replacement.new_ip} ЗАБЛОКИРОВАН: контрольное имя не "
        f"проходит ({diag.availability_pct(probe)}% зондов, "
        f"{probe['ok_probes']}/{probe['total_probes']}, стадия {stage}). "
        "В DNS не публикуется, пробуем следующий резерв",
    )
    _mark_ip_blocked(db_session, replacement.server_id, replacement.new_ip)
    replacement.new_ip = None
    replacement.command_id = None
    replacement.verify_run_id = None
    replacement.status = "pending"


def _banned_names(replacement) -> list:
    """Имена под баном по вердикту диагностики (хранятся в записи created)."""
    created = _rlog_last(replacement, "created") or {}
    return list(created.get("banned_names") or [])


def _domain_snis(replacement) -> dict:
    """{домен заявки: [имена, которые клиенты шлют в ClientHello]}.

    Заявки, созданные до появления имён, карты не хранят — там имя и есть
    домен, что и было единственной схемой на момент их создания.
    """
    created = _rlog_last(replacement, "created") or {}
    mapping = dict(created.get("domain_snis") or {})
    result = {}
    for domain in (replacement.domains or []):
        names = mapping.get(domain)
        if isinstance(names, str):  # заявка ранней версии: одно имя строкой
            names = [names]
        result[domain] = [n for n in (names or []) if n] or [domain]
    return result


def _sni_banned_names(replacement) -> dict:
    """{домен: [его забаненные имена]} по вердикту диагностики.

    Отказ такого имени на кандидате ожидаем и публикацию домена не
    отменяет: имя забанено везде, на новом адресе оно тоже не пройдёт — но
    домен в эфир не уходит, а старый адрес мёртв. Чинится сменой имени в
    конфигах, а не в DNS.
    """
    created = _rlog_last(replacement, "created") or {}
    raw = created.get("sni_banned") or {}
    if isinstance(raw, list):  # заявка ранней версии: список доменов
        snis = _domain_snis(replacement)
        return {domain: list(snis.get(domain) or []) for domain in raw}
    return {
        domain: [n for n in (names or []) if n]
        for domain, names in dict(raw).items()
    }


def _names_to_publish(replacement) -> list:
    """Под какие имена кандидата можно публиковать: домены заявки минус
    забаненные по вердикту."""
    banned = set(_banned_names(replacement))
    return [domain for domain in (replacement.domains or []) if domain not in banned]


def _published_names(replacement) -> list:
    """Имена, под которые кандидат опубликован (или будет опубликован)."""
    entry = _rlog_last(replacement, "names_verified")
    if entry is not None:
        return list(entry.get("publish") or []) + list(
            entry.get("unverified") or []
        )
    entry = _rlog_last(replacement, "names_verify_skipped")
    if entry is not None:
        return list(entry.get("publish") or [])
    # Заявка прежней версии, дошедшая до DNS без проверки имён
    return _names_to_publish(replacement)


def _start_names_verification(db_session, replacement) -> None:
    """Проверка КАЖДОГО имени на кандидате до публикации.

    Контрольное имя доказало, что адрес жив, но клиенты идут с другими
    именами, а у ТСПУ бывают правила на пару «адрес + имя» (2026-09-02:
    monkora и space падали на живом .198 при рабочих именах на .143).
    Публикуется только то, что на кандидате реально проходит. Имена,
    которые диагностика не смогла проверить (не было живого адреса), здесь
    проверяются впервые — раньше они считались чистыми и публиковались
    вслепую (xyz на .132). Полный набор зондов.
    """
    from engine import infra

    names = _names_to_publish(replacement)
    if not names:
        _rlog(
            replacement,
            "names_verified",
            "Публиковать нечего: все имена сервера под баном имени",
            publish=[], skipped=[], unverified=[],
        )
        replacement.status = "dns_add"
        return
    # Проверяется то, что уйдёт в ClientHello, а не имя A-записи; одинаковые
    # имена у разных доменов дают одну пробу. Лимита здесь НЕТ намеренно:
    # это последняя защита перед правкой боевых A-записей, а непроверенное
    # имя публикуется (см. ветку probe is None ниже) — именно так адрес уехал
    # под непроверенным именем 2026-09-02. Стоимость — один адрес × число
    # различных имён, самая дешёвая точка конвейера.
    snis = _domain_snis(replacement)
    probe_snis: list[str] = []
    for domain in names:
        for sni in snis.get(domain) or [domain]:
            if sni not in probe_snis:
                probe_snis.append(sni)
    try:
        started = infra.start_diagnosis_probes(
            db_session,
            [(replacement.new_ip, sni) for sni in probe_snis],
            reason="CANDIDATE_NAMES_VERIFY",
            light=False,
        )
    except Exception as e:
        log.exception("infra: names verification could not start")
        _rlog(
            replacement,
            "names_verify_skipped",
            f"Проверку имён на кандидате запустить не удалось ({e!r}) — "
            "публикуем без неё",
            publish=names,
        )
        replacement.status = "dns_add"
        return
    if not started["runs"]:
        _rlog(
            replacement,
            "names_verify_skipped",
            "Проверку имён на кандидате запустить не удалось ("
            + "; ".join(started["errors"]) + ") — публикуем без неё",
            publish=names,
        )
        replacement.status = "dns_add"
        return
    replacement.status = "verifying_names"
    _rlog(
        replacement,
        "verifying_names",
        f"Проверяем на кандидате {replacement.new_ip} имена: "
        + ", ".join(probe_snis),
        runs=started["runs"],
    )


def _replacement_step_verifying_names(db_session, replacement, server) -> None:
    """Итог проверки имён на кандидате: публикуются только прошедшие."""
    from engine import infra
    from engine import infra_diagnosis as diag

    names = _names_to_publish(replacement)
    entry = _rlog_last(replacement, "verifying_names")
    runs = (entry or {}).get("runs") or {}
    if not runs:
        _rlog(
            replacement,
            "names_verify_skipped",
            "Прогоны проверки имён потеряны — публикуем без неё",
            publish=names,
        )
        replacement.status = "dns_add"
        return
    probes, pending = infra.collect_probes(db_session, runs)
    if pending:
        return  # ждём зонды; зависание закроет общий stuck-таймаут

    ip = replacement.new_ip
    snis = _domain_snis(replacement)
    banned = _sni_banned_names(replacement)
    publish, skipped, unverified = [], [], []
    failing_names: dict = {}
    for domain in names:
        domain_names = snis.get(domain) or [domain]
        domain_banned = set(banned.get(domain) or [])
        passed, failed, unknown = [], [], []
        for sni in domain_names:
            probe = probes.get(infra.probe_key(ip, sni))
            label_name = domain if sni == domain else f"{domain} / {sni}"
            if probe is None:
                unknown.append(sni)
                _rlog(
                    replacement,
                    "name_unverified",
                    f"Имя {label_name} на кандидате {ip}: проверка не дала "
                    "результата",
                )
                continue
            label = (
                f"{diag.availability_pct(probe)}% зондов, "
                f"{probe['ok_probes']}/{probe['total_probes']}"
            )
            if diag.probe_passed(probe):
                passed.append(sni)
                _rlog(
                    replacement, "name_verified",
                    f"Имя {label_name} на кандидате {ip}: проходит ({label})",
                )
                continue
            failed.append(sni)
            stage = diag.dominant_stage(probe) or "нет ответа"
            _rlog(
                replacement,
                "name_skipped" if sni not in domain_banned else "name_verified",
                f"Имя {label_name} на кандидате {ip}: НЕ проходит ({label}, "
                f"стадия {stage})"
                + (
                    " — ожидаемо, имя забанено вердиктом"
                    if sni in domain_banned
                    else " — бан пары «адрес + имя» либо имени"
                ),
            )
        if failed:
            failing_names[domain] = failed
        # Домен публикуется ВСЕГДА (решение владельца 09.09.2026): адрес
        # кандидата уже доказан живым контрольным именем на шаге verifying, а
        # состояние имён на решение о DNS не влияет. Старый адрес мёртв —
        # оставить клиентов на нём хуже любого исхода здесь. Какие имена на
        # кандидате не прошли, видно в журнале и в итоговом алерте.
        if passed:
            publish.append(domain)
        elif not failed and unknown:
            unverified.append(domain)
        else:
            publish.append(domain)
            _rlog(
                replacement,
                "name_verified",
                f"Домен {domain} публикуется, хотя на кандидате {ip} не прошло "
                "ни одно его имя (" + ", ".join(failed + unknown) + "): адрес "
                "кандидата признан живым, а старый адрес мёртв. Имена чинятся "
                "в конфигурации ноды и хостах Remnawave",
            )
    summary = "Публикуем: " + ", ".join(publish + unverified)
    if failing_names:
        summary += "; не прошли на кандидате имена: " + ", ".join(
            f"{domain} ({', '.join(names)})"
            for domain, names in failing_names.items()
        )
    _rlog(
        replacement, "names_verified", summary,
        publish=publish, skipped=skipped, unverified=unverified,
        failing_names=failing_names,
    )
    replacement.status = "dns_add"


def _replacement_step_dns_add(db_session, replacement, server) -> None:
    from engine import cloudflare_dns

    names = _published_names(replacement)
    try:
        for domain in names:
            created = cloudflare_dns.ensure_a_record(domain, replacement.new_ip)
            _rlog(
                replacement,
                "dns_add",
                f"A-запись {domain} -> {replacement.new_ip} "
                + ("создана" if created else "уже существует"),
            )
    except cloudflare_dns.CloudflareError as e:
        _rlog(replacement, "dns_add", f"Cloudflare недоступен: {e}")
        return  # retry
    if not names:
        _rlog(
            replacement, "dns_add",
            "Новый адрес не опубликован ни под одним именем",
        )
    # Порядок принципиален: старую запись удаляем только после успешного ADD
    replacement.status = "dns_remove"


def _replacement_step_dns_remove(db_session, replacement, server, title) -> None:
    """Старый адрес убирается из ВСЕХ доменов заявки.

    Не только из тех, куда опубликован новый: адрес доказанно забанен, и под
    забаненным или не прошедшим на кандидате именем он так же мёртв
    (2026-09-02 под «забаненными» monkora и xyz остались .142 и .128).
    Единственную A-запись домена не трогаем: имя без записей хуже имени с
    мёртвой записью, такое решение принимает человек.
    """
    from engine import cloudflare_dns

    published = set(_published_names(replacement))
    old_ip = replacement.old_ip
    try:
        for domain in replacement.domains or []:
            if domain not in published:
                records = cloudflare_dns.list_a_records(domain)
                has_old = any(r["content"] == old_ip for r in records)
                others = [r for r in records if r["content"] != old_ip]
                if has_old and not others:
                    _rlog(
                        replacement,
                        "dns_remove",
                        f"A-запись {domain} -> {old_ip}: единственная у "
                        "домена, не удаляем (новый адрес под это имя не "
                        "опубликован)",
                    )
                    continue
            deleted = cloudflare_dns.delete_a_records(domain, old_ip)
            _rlog(
                replacement,
                "dns_remove",
                f"A-запись {domain} -> {old_ip}: удалено {deleted}",
            )
    except cloudflare_dns.CloudflareError as e:
        _rlog(replacement, "dns_remove", f"Cloudflare недоступен: {e}")
        return  # retry

    _mark_old_ip_blocked(db_session, replacement)
    _start_replacement_confirmation(db_session, replacement, title)


def _start_replacement_confirmation(db_session, replacement, title) -> None:
    """Постпроверка: заработали ли связки «новый адрес + КАЖДОЕ имя».

    Проверять контрольным именем здесь нельзя — оно доказывало чистоту
    адреса, а теперь важна работоспособность именно тех сочетаний, которыми
    будут пользоваться клиенты. Все опубликованные имена, а не первое по
    алфавиту: 2026-09-02 «связка работает» относилось к одному имени из
    четырёх. Полный набор зондов.
    """
    from engine import infra

    names = _published_names(replacement)
    if not names:
        _rlog(
            replacement, "confirm_skipped",
            "Опубликованных имён нет — постпроверять нечего",
        )
        _finish_replacement(db_session, replacement, title, confirmed=None)
        return
    snis = _domain_snis(replacement)
    probe_snis: list[str] = []
    for domain in names:
        for sni in snis.get(domain) or [domain]:
            if sni not in probe_snis:
                probe_snis.append(sni)
    try:
        started = infra.start_diagnosis_probes(
            db_session,
            [(replacement.new_ip, sni) for sni in probe_snis],
            reason="REPLACEMENT_CONFIRM",
            light=False,
        )
    except Exception as e:
        log.exception("infra: replacement confirmation could not start")
        _rlog(
            replacement,
            "confirm_skipped",
            f"Постпроверку запустить не удалось ({e!r})",
        )
        _finish_replacement(db_session, replacement, title, confirmed=None)
        return

    run_ids = list(started["runs"].values())
    if not run_ids:
        _rlog(
            replacement,
            "confirm_skipped",
            "Постпроверку запустить не удалось ("
            + "; ".join(started["errors"]) + ")",
        )
        _finish_replacement(db_session, replacement, title, confirmed=None)
        return

    replacement.verify_run_id = run_ids[0]
    replacement.status = "confirming"
    _rlog(
        replacement,
        "confirming",
        f"Проверяем, что связки {replacement.new_ip} + "
        + ", ".join(probe_snis) + " действительно работают",
        runs=started["runs"],
    )


def _replacement_step_confirming(db_session, replacement, server, title) -> None:
    """Итог постпроверки по каждому опубликованному имени."""
    from engine import infra
    from engine import infra_diagnosis as diag

    entry = _rlog_last(replacement, "confirming")
    runs = dict((entry or {}).get("runs") or {})
    if not runs and replacement.verify_run_id:
        # Заявка прежней версии: один прогон по первому имени
        first = (replacement.domains or [""])[0]
        runs = {
            infra.probe_key(replacement.new_ip, first): replacement.verify_run_id
        }
    if not runs:
        _finish_replacement(db_session, replacement, title, confirmed=None)
        return
    probes, pending = infra.collect_probes(db_session, runs)
    if pending:
        return

    ip = replacement.new_ip
    snis = _domain_snis(replacement)
    published = _published_names(replacement)
    # Итог нужен по доменам (их видит админ), а пробы шли по именам. ВНУТРИ
    # домена правило any-of: домен считается заработавшим, если работает хотя
    # бы одно его имя. Иначе при трёх именах падение одного протокольного
    # имени давало бы страшный алерт «замена не решила проблему» на КАЖДОЙ
    # замене. Между доменами по-прежнему all-of.
    checked = [(domain, snis.get(domain) or [domain]) for domain in published]
    if not checked:
        checked = [
            (key.partition("|")[2], [key.partition("|")[2]]) for key in runs
        ]
    results: dict = {}
    partial: dict = {}
    for domain, domain_names in checked:
        passed, failed, unknown = [], [], []
        for sni in domain_names:
            label_name = domain if sni == domain else f"{domain} / {sni}"
            probe = probes.get(infra.probe_key(ip, sni))
            if probe is None:
                unknown.append(sni)
                _rlog(
                    replacement,
                    "confirm_skipped",
                    f"Постпроверка {ip} + {label_name} не дала результата",
                )
                continue
            label = (
                f"{diag.availability_pct(probe)}% зондов, "
                f"{probe['ok_probes']}/{probe['total_probes']}"
            )
            if diag.probe_passed(probe):
                passed.append(sni)
                _rlog(
                    replacement, "confirmed",
                    f"Связка {ip} + {label_name} работает ({label})",
                )
                continue
            failed.append(sni)
            stage = diag.dominant_stage(probe) or "нет ответа"
            _rlog(
                replacement,
                "not_confirmed",
                f"Связка {ip} + {label_name} НЕ работает ({label}, стадия "
                f"{stage})",
            )
        if passed:
            results[domain] = True
            if failed:
                partial[domain] = failed
        elif failed:
            results[domain] = False
        else:
            results[domain] = None
    known = [value for value in results.values() if value is not None]
    if not known:
        confirmed = None
    elif all(known):
        confirmed = True
    else:
        confirmed = False
    _finish_replacement(
        db_session, replacement, title, confirmed=confirmed,
        name_results=results, partial_names=partial,
    )


def _finish_replacement(
    db_session, replacement, title, confirmed, name_results=None,
    partial_names=None,
) -> None:
    """Закрывает замену и шлёт итоговый алерт.

    confirmed: True — все опубликованные связки проверены и работают;
    False — хотя бы одна не работает (значит причина не только в адресе);
    None — проверить не удалось. В алерте по именам: что опубликовано, что
    нет и почему.
    """
    from engine import infra

    replacement.status = "done"
    replacement.finished_at = infra.utcnow()

    journal = "\n".join(
        f"· {item.get('message', '')}" for item in (replacement.log or [])[-10:]
    )
    published = _published_names(replacement)
    verified = _rlog_last(replacement, "names_verified") or {}
    skipped = list(verified.get("skipped") or [])
    banned = _banned_names(replacement)
    name_results = name_results or {}
    failing = [name for name, ok in name_results.items() if ok is False]

    names_block = "Опубликовано под именами:\n" + (
        "\n".join(f"<code>{d}</code>" for d in published) if published else "—"
    )
    if skipped:
        names_block += (
            "\nНе опубликовано, не проходит на кандидате "
            "(бан пары «адрес + имя»):\n"
            + "\n".join(f"<code>{d}</code>" for d in skipped)
        )
    if banned:
        names_block += (
            "\nНе опубликовано, имя забанено:\n"
            + "\n".join(f"<code>{d}</code>" for d in banned)
        )
    sni_banned = _sni_banned_names(replacement)
    if sni_banned:
        names_block += (
            "\nОпубликовано, но часть имён забанена — сменить в конфигах "
            "ноды и хостах Remnawave:\n"
            + "\n".join(
                f"<code>{d}</code>: "
                + ", ".join(f"<code>{n}</code>" for n in names)
                for d, names in sni_banned.items()
                if names
            )
        )
    partial_names = partial_names or {}
    if partial_names:
        names_block += (
            "\nРаботает частично (адрес жив, эти имена не проходят):\n"
            + "\n".join(
                f"<code>{d}</code>: "
                + ", ".join(f"<code>{n}</code>" for n in names)
                for d, names in partial_names.items()
            )
        )

    if confirmed is False:
        _send_alert(
            "🟠 <b>IP заменён, но проблема не решена</b>\n\n"
            f"Сервер: <b>{title}</b>\n"
            f"<code>{replacement.old_ip}</code> → "
            f"<code>{replacement.new_ip}</code>\n"
            + names_block
            + "\n\nНовый адрес чист, но клиентская связка «адрес + имя» "
            "не проходит у: "
            + ", ".join(f"<code>{d}</code>" for d in failing)
            + ". Ни одно имя этих доменов на новом адресе не проходит, "
            "значит дело не только в адресе: проверьте имена, конфигурацию "
            "ноды и клиентские конфиги.\n\n"
            + (
                "Для уже забаненных имён это ожидаемо: адрес заменён, имя "
                "меняется отдельно.\n\n"
                if sni_banned
                else ""
            )
            + "<b>Что было сделано:</b>\n" + journal
        )
        return

    tail = (
        "Связки «новый адрес + имя» проверены и работают.\n"
        if confirmed
        else "⚠️ Постпроверку провести не удалось — убедитесь вручную.\n"
    )
    _send_alert(
        "🟡 <b>IP автоматически заменён</b>\n\n"
        f"Сервер: <b>{title}</b>\n"
        + names_block
        + f"\n\n<code>{replacement.old_ip}</code> → "
        f"<code>{replacement.new_ip}</code>\n\n"
        "Причина: блокировка адреса подтверждена ТСПУ-зондами.\n"
        + tail
        + "\n<b>Что было сделано:</b>\n" + journal + "\n\n"
        "⚠️ Адрес добавлен через <code>ip addr add</code> и НЕ переживёт "
        "ребут сервера — пропишите его в постоянную сетевую конфигурацию "
        "(netplan/interfaces)."
    )


def _mark_ip_blocked(db_session, server_id, ip: str) -> None:
    """Помечает адрес заблокированным: он больше не выбирается кандидатом."""
    from engine import infra

    row = (
        db_session.query(infra.InfraServerIp)
        .filter(
            infra.InfraServerIp.server_id == server_id,
            infra.InfraServerIp.ip == ip,
        )
        .one_or_none()
    )
    if row is not None and row.blocked_at is None:
        row.blocked_at = infra.utcnow()


def _mark_old_ip_blocked(db_session, replacement) -> None:
    _mark_ip_blocked(db_session, replacement.server_id, replacement.old_ip)


# --- обслуживание -----------------------------------------------------------


def _infra_tables_ready() -> bool:
    """Код может приехать раньше alembic-миграции: без таблиц молча ждём,
    не заливая лог одинаковыми ошибками каждые 30 секунд."""
    from engine import infra

    db_session = session_factory()
    try:
        db_session.query(infra.InfraServer.id).limit(1).all()
        return True
    except Exception:
        db_session.rollback()
        if _tick_counter % _PRUNE_EVERY_TICKS == 1:
            log.warning(
                "infra worker: таблицы infra_* недоступны (миграция common "
                "ещё не применена?) — обслуживание пропускается"
            )
        return False
    finally:
        db_session.close()


def _step_statement_timeout_ms() -> int:
    from django.conf import settings

    seconds = int(
        getattr(settings, "INFRA_WORKER_STEP_TIMEOUT", _DEFAULT_STEP_TIMEOUT_SECONDS)
    )
    return max(0, seconds) * 1000


def _run_step(name, fn, *args) -> bool:
    """Один шаг тика: своя сессия, statement_timeout на транзакцию, commit.

    SET LOCAL живёт до commit/rollback этого шага и не утекает через пул в
    веб-запросы. Шаги внутри не коммитят, поэтому лимит действует на весь
    шаг. Возвращает True при успехе.
    """
    started = time.monotonic()
    succeeded = False
    db_session = session_factory()
    try:
        timeout_ms = _step_statement_timeout_ms()
        if timeout_ms > 0 and engine.dialect.name == "postgresql":
            db_session.execute(
                text(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            )
        fn(db_session, *args)
        db_session.commit()
        succeeded = True
        return True
    except Exception:
        db_session.rollback()
        log.exception("infra worker: %s failed", name)
        return False
    finally:
        db_session.close()
        log.info("maintenance_step name=%s success=%s duration_ms=%.1f", name, succeeded, (time.monotonic()-started)*1000)


def run_maintenance() -> bool:
    """Один тик обслуживания; каждая подзадача — своя сессия и commit.

    Порядок: OFFLINE-проверка всегда первая в тике (алерты о слепоте
    мониторинга важнее агрегатов), остальные шаги — по сроку: первым идёт
    тот, что просрочен дольше, поэтому медленный шаг не морит голодом
    следующие. Срок шага — конец его прошлого запуска + every * interval
    (детектор аномалий, DNS-вотчер и prune — раз в несколько интервалов).
    У OFFLINE срок тоже от конца запуска: проверка дольше бюджета иначе
    съедала бы каждое продолжение тика, и остальные шаги не шли бы вовсе.
    Строгой очерёдности «агрегация перед детектором» нет: детектор берёт
    текущее окно из сырой телеметрии.

    INFRA_MAINTENANCE_BUDGET_SECONDS запрещает начинать следующий шаг после
    бюджета; уже работающий шаг не прерывается, OFFLINE бюджетом не
    отсекается. Возвращает True, если тик обрезан бюджетом и остались
    просроченные шаги: цикл лидера тогда делает короткую паузу
    (_tick_sleep_seconds) и доделывает их, а не ждёт полный interval
    (INFRA-01: иначе под нагрузкой все шаги, включая OFFLINE, шли реже, чем
    без бюджета).
    """
    from engine import infra
    from engine import geoip_updater

    global _tick_counter
    _tick_counter += 1
    # Выполняется уже после захвата advisory lock лидером. Загрузка не зависит
    # от infra_* таблиц: новая установка сможет получить MMDB до миграции.
    try:
        geoip_updater.update_if_due()
    except Exception:
        # GeoIP — необязательное обогащение и никогда не должно останавливать
        # алерты, агрегацию телеметрии или автозамену IP.
        log.exception("infra worker: DB-IP City Lite update failed")
    if not _infra_tables_ready():
        return False

    interval = max(1, int(getattr(settings, "INFRA_WORKER_INTERVAL", _DEFAULT_INTERVAL)))
    started = time.monotonic()
    budget = max(1, float(getattr(settings, "INFRA_MAINTENANCE_BUDGET_SECONDS", 30)))
    steps = [
        ("offline", check_offline, 1),
        ("aggregate", lambda db: infra.aggregate_telemetry(db), 1),
        ("xray", check_xray, 1),
        ("capacity", check_capacity, 1),
        ("load", check_load, 1),
        ("anomaly-detect", detect_anomalies, _ANOMALY_EVERY_TICKS),
        ("anomaly-process", process_anomalies, 1),
        ("replacements", process_replacements, 1),
        ("dns-watch", watch_dns_changes, _DNS_WATCH_EVERY_TICKS),
        ("prune", lambda db: infra.prune_telemetry(db), _PRUNE_EVERY_TICKS),
    ]
    for name, _, every in steps:
        _maintenance_due.setdefault(name, started if every == 1 else started + every * interval)
    deferred = False
    # OFFLINE — всегда первым (INFRA-01: иначе он вставал в очередь за
    # шагами, пропущенными в прошлом тике, и бюджет срезал его целиком);
    # дальше самый давно просроченный шаг первым, чтобы дорогой шаг не морил
    # голодом следующие.
    for name, fn, every in sorted(
        steps, key=lambda step: (step[0] != "offline", _maintenance_due[step[0]])
    ):
        now = time.monotonic()
        if name != "offline" and now - started >= budget:
            # Просроченное осталось — доделать после короткой паузы, а не
            # через полный interval
            deferred = any(
                now >= _maintenance_due[step_name] for step_name, _, _ in steps
            )
            log.warning(
                "maintenance_budget_exhausted duration_ms=%.1f deferred=%s",
                (now - started) * 1000,
                deferred,
            )
            break
        if now < _maintenance_due[name]:
            continue
        _run_step(name, fn)
        _maintenance_due[name] = time.monotonic() + every * interval
    return deferred


def _tick_sleep_seconds(interval: int, deferred) -> int:
    """Пауза перед следующим тиком.

    Тик обрезан бюджетом и остались просроченные шаги — короткая пауза,
    чтобы доделать их сразу; иначе полный interval, как раньше. Сравнение
    строго с True: None или упавший тик — полный interval. Короткая пауза
    не зацикливает воркер: обрезка бывает только после ≥ бюджета работы.
    """
    return min(interval, 1) if deferred is True else interval


def _simple_loop(interval: int) -> None:
    log.info("infra worker: non-postgres backend, running without leader lock")
    while True:
        deferred = False
        try:
            deferred = run_maintenance()
        except Exception:
            log.exception("infra worker: maintenance iteration failed")
        time.sleep(_tick_sleep_seconds(interval, deferred))


def _leader_loop(interval: int) -> None:
    if engine.dialect.name != "postgresql":
        _simple_loop(interval)
        return

    failures = 0
    while True:
        conn = None
        try:
            conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            failures = 0
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            ).scalar()
            if not got:
                time.sleep(interval)
                continue

            log.info("infra worker: acquired leadership, running maintenance loop")
            while True:
                # SELECT 1 на lock-соединении — каждую итерацию, в том числе
                # после короткой паузы: мёртвое соединение = перевыборы
                conn.execute(text("SELECT 1"))
                deferred = False
                try:
                    deferred = run_maintenance()
                except Exception:
                    log.exception("infra worker: maintenance iteration failed")
                time.sleep(_tick_sleep_seconds(interval, deferred))
        except Exception:
            log.exception("infra worker: leader loop error, will re-elect")
            failures += 1
            delay = min(max(1, interval), 2 ** min(failures - 1, 6))
            time.sleep(delay + random.uniform(0, min(1, delay * 0.1)))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def start() -> None:
    """Запускает фоновый поток один раз на процесс (вызывается из wsgi.py)."""
    global _started
    from django.conf import settings

    if not getattr(settings, "INFRA_WORKER_ENABLED", True):
        return

    with _start_lock:
        if _started:
            return
        _started = True

    interval = int(getattr(settings, "INFRA_WORKER_INTERVAL", _DEFAULT_INTERVAL))
    thread = threading.Thread(
        target=_leader_loop,
        args=(interval,),
        name="infra-worker",
        daemon=True,
    )
    thread.start()
    log.info("infra worker: background thread started (interval=%ss)", interval)
