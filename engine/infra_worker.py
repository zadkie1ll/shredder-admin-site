"""Фоновый воркер «Инфраструктуры»: крутится внутри процесса сайта.

Стартует из web_app/wsgi.py (как censor_worker) и раз в
INFRA_WORKER_INTERVAL секунд выполняет обслуживание:

- агрегация телеметрии (raw -> 1 мин -> 15 мин) и retention;
- ONLINE/OFFLINE серверов + Telegram-алерты с recovery;
- алерты о длительной высокой нагрузке канала (порог/длительность из
  system_settings, гистерезис + кулдаун);
- anomaly detection: одновременное падение трафика и TCP-соединений
  против baseline того же времени суток -> принудительный «Замер ТСПУ»;
- обработка аномалий: ТСПУ подтвердил блокировку -> заявка на замену IP;
- state machine замены IP: node-agent ensure_ip -> Cloudflare ADD new ->
  Cloudflare REMOVE old -> Telegram (порядок принципиален).

Лидерство между gunicorn-воркерами — Postgres advisory lock (свой ключ,
отличный от censor_worker). Аномалия никогда не меняет DNS сама: только
подтверждение существующим механизмом «Замеров ТСПУ» запускает ротацию.
"""

import logging
import threading
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

_started = False
_start_lock = threading.Lock()
_tick_counter = 0

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


def check_offline(db_session) -> None:
    from engine import infra

    cfg = infra.get_settings(db_session)
    now = infra.utcnow()
    cooldown = timedelta(minutes=cfg["infra_offline_alert_cooldown_minutes"])
    servers = (
        db_session.query(infra.InfraServer)
        .filter(infra.InfraServer.is_archived.is_(False))
        .all()
    )
    for server in servers:
        online = infra.is_online(server, cfg, now)
        title = infra.server_title(server)
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
        else:
            if not incident_alerted:
                continue
            if now - server.offline_alerted_at < timedelta(minutes=1):
                # Мгновенный флап: даём состоянию устаканиться
                continue
            delivered = _send_alert(
                "🟢 <b>Сервер снова онлайн</b>\n\n"
                f"Сервер: <b>{title}</b>\n"
                f"Heartbeat восстановился."
            )
            if delivered:
                server.recovered_alerted_at = now
                log.info("infra: server %s back ONLINE", title)


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

        result = infra.force_tspu_check(
            db_session, server, reason="NETWORK_TRAFFIC_ANOMALY"
        )
        anomaly.censor_run_ids = result["run_ids"]
        log.warning(
            "infra: anomaly detected server=%s traffic_ratio=%s conn_ratio=%s "
            "runs=%s errors=%s",
            title,
            verdict["traffic_ratio"],
            verdict["connection_ratio"],
            result["run_ids"],
            result["errors"],
        )
        if not result["run_ids"]:
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


def process_anomalies(db_session) -> None:
    from engine import infra
    from engine import ripe_atlas

    cfg = infra.get_settings(db_session)
    now = infra.utcnow()
    anomalies = (
        db_session.query(infra.InfraAnomaly)
        .filter(infra.InfraAnomaly.status == "checking")
        .all()
    )
    for anomaly in anomalies:
        run_ids = [int(run_id) for run_id in anomaly.censor_run_ids or []]
        runs = (
            db_session.query(infra.CensorCheckRun)
            .filter(infra.CensorCheckRun.id.in_(run_ids))
            .all()
            if run_ids
            else []
        )
        server = db_session.get(infra.InfraServer, anomaly.server_id)
        title = infra.server_title(server) if server else f"#{anomaly.server_id}"

        pending = [run for run in runs if run.status == "pending"]
        if pending:
            if anomaly.created_at and now - anomaly.created_at > timedelta(
                minutes=infra.ANOMALY_CHECK_TIMEOUT_MINUTES
            ):
                anomaly.status = "error"
                anomaly.resolved_at = now
                _send_alert(
                    "🟠 <b>Аномалия нагрузки: замеры ТСПУ не завершились</b>\n\n"
                    f"Сервер: <b>{title}</b>\n"
                    "Проверьте сервер и замеры вручную."
                )
            continue

        blocked_ips = []
        completed = 0
        for run in runs:
            if run.status != "complete":
                continue
            completed += 1
            availability = ripe_atlas.run_availability_percent(run)
            if (
                availability is not None
                and availability <= ripe_atlas.ALERT_BLOCKED_MAX
            ):
                check = db_session.get(infra.CensorCheck, run.check_id)
                if check is not None and check.target_ip not in blocked_ips:
                    blocked_ips.append(check.target_ip)

        if blocked_ips:
            anomaly.status = "confirmed"
            anomaly.resolved_at = now
            log.warning(
                "infra: anomaly confirmed server=%s blocked_ips=%s",
                title,
                blocked_ips,
            )
            if not cfg["infra_auto_replace_enabled"]:
                _send_alert(
                    "🔴 <b>ТСПУ подтвердил блокировку, автозамена выключена</b>\n\n"
                    f"Сервер: <b>{title}</b>\n"
                    "IP: " + ", ".join(f"<code>{ip}</code>" for ip in blocked_ips)
                    + "\n\nЗамените IP вручную "
                    "(настройка infra_auto_replace_enabled)."
                )
                continue
            for blocked_ip in blocked_ips:
                try:
                    infra.request_replacement(
                        db_session,
                        anomaly.server_id,
                        blocked_ip,
                        created_by="auto:anomaly",
                        anomaly_id=anomaly.id,
                    )
                except infra.InfraError as e:
                    log.warning(
                        "infra: cannot create replacement for %s: %s",
                        blocked_ip,
                        e.message,
                    )
        elif completed:
            anomaly.status = "dismissed"
            anomaly.resolved_at = now
            log.info(
                "infra: anomaly dismissed server=%s (TSPU ok, traffic drop "
                "is not a block)",
                title,
            )
        else:
            anomaly.status = "error"
            anomaly.resolved_at = now
            _send_alert(
                "🟠 <b>Аномалия нагрузки: замеры ТСПУ завершились ошибкой</b>\n\n"
                f"Сервер: <b>{title}</b>\n"
                "Проверьте сервер и замеры вручную."
            )


# --- замена IP --------------------------------------------------------------


def _rlog(replacement, step: str, message: str) -> None:
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
    replacement.log = existing + [entry]
    log.info("infra: replacement #%s %s: %s", replacement.id, step, message)


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
    elif replacement.status == "dns_add":
        _replacement_step_dns_add(db_session, replacement, server)
    elif replacement.status == "dns_remove":
        _replacement_step_dns_remove(db_session, replacement, server, title)


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
        replacement.status = "dns_add"
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
            return ipaddress_module.ip_address(row.ip).version == 4
        except ValueError:
            return False

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
        replacement.status = "dns_add"
        return
    _fail_replacement(
        db_session,
        replacement,
        server,
        f"Агент не смог установить IP {replacement.new_ip}: "
        f"{command.error or command.status}",
    )


def _replacement_step_dns_add(db_session, replacement, server) -> None:
    from engine import cloudflare_dns
    from engine import infra

    try:
        for domain in replacement.domains or []:
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
    # Порядок принципиален: старую запись удаляем только после успешного ADD
    replacement.status = "dns_remove"


def _replacement_step_dns_remove(db_session, replacement, server, title) -> None:
    from engine import cloudflare_dns
    from engine import infra

    try:
        for domain in replacement.domains or []:
            deleted = cloudflare_dns.delete_a_records(domain, replacement.old_ip)
            _rlog(
                replacement,
                "dns_remove",
                f"A-запись {domain} -> {replacement.old_ip}: "
                f"удалено {deleted}",
            )
    except cloudflare_dns.CloudflareError as e:
        _rlog(replacement, "dns_remove", f"Cloudflare недоступен: {e}")
        return  # retry

    replacement.status = "done"
    replacement.finished_at = infra.utcnow()
    _mark_old_ip_blocked(db_session, replacement)
    _send_alert(
        "🟡 <b>IP автоматически заменён</b>\n\n"
        f"Сервер: <b>{title}</b>\n"
        "Домены:\n"
        + "\n".join(f"<code>{d}</code>" for d in replacement.domains or [])
        + f"\n\n<code>{replacement.old_ip}</code> → "
        f"<code>{replacement.new_ip}</code>\n\n"
        "Причина: блокировка подтверждена ТСПУ-зондами.\n"
        "Новый IP установлен на сервер.\n"
        "Cloudflare DNS успешно обновлён.\n\n"
        "⚠️ Адрес добавлен через <code>ip addr add</code> и НЕ переживёт "
        "ребут сервера — пропишите его в постоянную сетевую конфигурацию "
        "(netplan/interfaces)."
    )


def _mark_old_ip_blocked(db_session, replacement) -> None:
    from engine import infra

    row = (
        db_session.query(infra.InfraServerIp)
        .filter(
            infra.InfraServerIp.server_id == replacement.server_id,
            infra.InfraServerIp.ip == replacement.old_ip,
        )
        .one_or_none()
    )
    if row is not None and row.blocked_at is None:
        row.blocked_at = infra.utcnow()


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


def run_maintenance() -> None:
    """Один тик обслуживания; каждая подзадача — своя сессия и commit."""
    from engine import infra

    global _tick_counter
    _tick_counter += 1
    if not _infra_tables_ready():
        return

    def _run(name, fn, *args):
        db_session = session_factory()
        try:
            fn(db_session, *args)
            db_session.commit()
        except Exception:
            db_session.rollback()
            log.exception("infra worker: %s failed", name)
        finally:
            db_session.close()

    _run("aggregate", lambda db: infra.aggregate_telemetry(db))
    _run("offline", check_offline)
    _run("load", check_load)
    if _tick_counter % _ANOMALY_EVERY_TICKS == 0:
        _run("anomaly-detect", detect_anomalies)
    _run("anomaly-process", process_anomalies)
    _run("replacements", process_replacements)
    if _tick_counter % _DNS_WATCH_EVERY_TICKS == 0:
        _run("dns-watch", watch_dns_changes)
    if _tick_counter % _PRUNE_EVERY_TICKS == 0:
        _run("prune", lambda db: infra.prune_telemetry(db))


def _simple_loop(interval: int) -> None:
    log.info("infra worker: non-postgres backend, running without leader lock")
    while True:
        try:
            run_maintenance()
        except Exception:
            log.exception("infra worker: maintenance iteration failed")
        time.sleep(interval)


def _leader_loop(interval: int) -> None:
    if engine.dialect.name != "postgresql":
        _simple_loop(interval)
        return

    while True:
        conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            ).scalar()
            if not got:
                conn.close()
                time.sleep(interval)
                continue

            log.info("infra worker: acquired leadership, running maintenance loop")
            while True:
                conn.execute(text("SELECT 1"))
                try:
                    run_maintenance()
                except Exception:
                    log.exception("infra worker: maintenance iteration failed")
                time.sleep(interval)
        except Exception:
            log.exception("infra worker: leader loop error, will re-elect")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(interval)


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
