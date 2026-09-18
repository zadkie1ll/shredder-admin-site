"""Бизнес-логика раздела «Инфраструктура → Серверы».

Данные поставляет node-agent (ip-guard) через коллектор: heartbeat пишет
infra_servers/infra_telemetry/infra_server_ips. Здесь живёт всё остальное:

- настройки порогов (system_settings, ключи infra_*);
- payload'ы для админки (список серверов, карточка, серии телеметрии);
- агрегация телеметрии (raw -> 1 мин -> 15 мин) и retention;
- ONLINE/OFFLINE и алерты о недоступности;
- алерты о длительной высокой нагрузке канала;
- adaptive anomaly detection (одновременное падение трафика и соединений
  против baseline того же времени суток за последние дни);
- принудительный запуск существующих «Замеров ТСПУ» (ripe_atlas) по аномалии;
- state machine замены заблокированного IP: node-agent ensure_ip ->
  Cloudflare ADD new -> Cloudflare REMOVE old -> Telegram.

Периодические функции вызывает лидер-поток engine/infra_worker.py.
Аномалия сама по себе никогда не меняет DNS/IP — только подтверждение
ТСПУ-замером запускает ротацию (см. InfraAnomaly в common/models/db.py).
"""

import ipaddress
import logging
import statistics
import threading
from sqlalchemy.dialects.postgresql import insert as pg_insert
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from django.conf import settings as django_settings
from sqlalchemy import delete as sa_delete
from sqlalchemy import func
from sqlalchemy import select

from common.models.db import CensorCheck
from common.models.db import CensorCheckRun
from common.models.db import InfraAgentCommand
from common.models.db import InfraAnomaly
from common.models.db import InfraIpReplacement
from common.models.db import InfraServer
from common.models.db import InfraServerDomain
from common.models.db import InfraServerIp
from common.models.db import InfraTelemetry
from common.models.db import InfraTelemetryAgg
from common.models.db import SystemSetting
from common.models.db import UserIpObservation
from engine import geoip_lookup
from engine import ripe_atlas

log = logging.getLogger("infra")

_geo_analytics_cache: dict[tuple, dict] = {}
_geo_analytics_lock = threading.Lock()
# Кэш всего блока «кто подключается» карточки сервера. Четыре агрегатных
# запроса по ipguard_user_ips — самое дорогое место карточки: без кэша
# 5-секундный автообновляющий поллинг админки выполнял их заново на каждый
# тик и при большой таблице занимал всех gunicorn-воркеров (сайт отвечал 504).
_who_connects_cache: dict[tuple, dict] = {}
_who_connects_lock = threading.Lock()
_who_connects_compute_locks = [threading.Lock() for _ in range(32)]


class InfraError(Exception):
    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- настройки (system_settings) -------------------------------------------

# Ключ -> (default, тип, описание, допустимые значения для UI или None)
INFRA_SETTINGS = {
    "infra_offline_after_seconds": (
        180, int, "OFFLINE: нет heartbeat дольше, сек", None),
    "infra_offline_alert_cooldown_minutes": (
        60, int, "OFFLINE: кулдаун повторного алерта, мин", None),
    "infra_mass_offline_threshold": (
        5, int,
        "OFFLINE: массовая потеря heartbeat — один алерт «мониторинг ослеп» "
        "вместо N, серверов за проход (0 = выкл)", None),
    "infra_load_threshold_pct": (
        85, int, "Нагрузка канала: порог алерта, %", None),
    "infra_load_duration_minutes": (
        15, int, "Нагрузка канала: длительность превышения, мин", [10, 15, 30]),
    "infra_load_recover_pct": (
        75, int, "Нагрузка канала: порог восстановления, %", None),
    "infra_load_alert_cooldown_minutes": (
        120, int, "Нагрузка канала: кулдаун повторного алерта, мин", None),
    "infra_anomaly_traffic_ratio": (
        0.35, float, "Аномалия: порог отношения трафика к baseline", None),
    "infra_anomaly_conn_ratio": (
        0.35, float, "Аномалия: порог отношения соединений к baseline", None),
    "infra_anomaly_duration_minutes": (
        5, int, "Аномалия: длительность падения, мин", None),
    "infra_anomaly_baseline_days": (
        14, int, "Аномалия: глубина baseline, дней", None),
    "infra_anomaly_min_history_days": (
        3, int, "Аномалия: минимум дней истории для baseline", None),
    "infra_anomaly_warmup_hours": (
        48, int, "Аномалия: warm-up нового сервера, часов", None),
    "infra_anomaly_cooldown_minutes": (
        120, int, "Аномалия: пауза между аномалиями сервера, мин", None),
    "infra_anomaly_min_baseline_mbps": (
        20, int, "Аномалия: минимальный baseline-трафик, Mbit/s", None),
    "infra_auto_replace_enabled": (
        True, bool, "Автозамена IP после подтверждения ТСПУ", None),
    "infra_control_names": (
        "ya.ru,www.microsoft.com", str,
        "Диагностика: контрольные посторонние имена (через запятую)", None),
    "infra_tspu_max_names": (
        8, int,
        "Диагностика: сколько имён проверять за одну аномалию (каждое имя — "
        "полный прогон 33 зондов на каждом живом адресе, ~660 кредитов Atlas)",
        None),
    "infra_control_names_burned": (
        "", str,
        "Диагностика: выгоревшие контрольные имена (через запятую) — "
        "заполняется автоматически, очистите, чтобы вернуть имя в работу",
        None),
    "infra_capacity_warn_pct": (
        70, int, "Лимиты ноды: порог предупреждения, %", None),
    "infra_capacity_alert_cooldown_minutes": (
        180, int, "Лимиты ноды: кулдаун повторного алерта, мин", None),
    "infra_xray_down_confirm_minutes": (
        3, int, "XRAY: подтверждение падения перед алертом, мин", None),
    "infra_xray_alert_cooldown_minutes": (
        60, int, "XRAY: кулдаун повторного алерта, мин", None),
    "infra_dns_rebalance_window_hours": (
        168, int,
        "Окно перекалибровки baseline после новой A-записи у домена, часов",
        None),
}

# Настройки, где 0 означает «выключено» (validate_setting пропускает ноль)
ZERO_DISABLES_SETTINGS = ("infra_mass_offline_threshold",)

# Сколько живёт сырая телеметрия и агрегаты
RAW_RETENTION_HOURS = 26
AGG60_RETENTION_DAYS = 14
AGG900_RETENTION_DAYS = 90

# Окно baseline вокруг текущего времени суток (±минут)
BASELINE_WINDOW_MINUTES = 30
# Свежий ручной/автозапуск ТСПУ не дублируем, если прогон был недавно
TSPU_FRESH_RUN_MINUTES = 5
# Максимум IP-целей одной принудительной проверки: каждый прогон стоит
# реальных кредитов RIPE Atlas, а heartbeat-данные (список адресов) приходят
# с ноды и не должны уметь запускать десятки замеров
TSPU_MAX_TARGETS = 4
# Дефолт лимита на число ИМЁН одной диагностики; настраивается ключом
# infra_tspu_max_names. Отдельный от лимита адресов: раньше один срез резал
# и адреса, и имена, и при списке имён один домен съедал бы весь лимит.
# Имена идут полным набором зондов на каждом из SNI_PROBE_MAX_IPS адресов,
# поэтому потолок фазы 2 — имена × SNI_PROBE_MAX_IPS полных прогонов.
# Срезанные имена НЕ считаются чистыми: они попадают в unknown_snis и
# проверяются на кандидате перед публикацией.
TSPU_MAX_NAMES = 8
# Потолок числа имён на одной связке «сервер + домен»: защита от вставки
# мусора в поле ввода, а не бизнес-ограничение (протоколов бывает больше
# трёх).
MAX_DOMAIN_SNIS = 12
# Подавление аномалий после рестарта агента/ребута сервера
ANOMALY_AGENT_RESTART_MINUTES = 15
ANOMALY_REBOOT_MINUTES = 30
# Ожидание завершения ТСПУ-прогонов аномалии, потом status=error
ANOMALY_CHECK_TIMEOUT_MINUTES = 30
# Замена, не завершившаяся за это время с момента создания — failed + алерт
# (покрывает и ожидание команды агентом с ретраями, и сбои Cloudflare)
REPLACEMENT_STUCK_MINUTES = 45
# TTL команды ensure_ip
COMMAND_TTL_MINUTES = 15

REPLACEMENT_ACTIVE_STATUSES = (
    "pending", "installing", "verifying", "verifying_names", "dns_add",
    "dns_remove", "confirming")
# Фаза имён идёт на нескольких живых адресах сразу (не больше стольких):
# только так бан имени отличим от бана пары «адрес + имя»
SNI_PROBE_MAX_IPS = 2
# Волна диагностик: результаты проб имени на живых адресах соседних
# серверов не старше этого окна входят в вердикт по имени
NAME_WAVE_WINDOW_MINUTES = 90
# Сколько диагноз считается описывающим ТЕКУЩЕЕ состояние. Дальше он
# остаётся в карточке как история, но пометки «имя в бане» на доменах
# гаснут: вердикт — снимок момента, а не свойство домена. Бан пары
# «адрес + имя» вообще снимался сам за пару часов (2026-09-02), и
# недельной давности вердикт, показанный как текущий, дезинформирует.
DIAGNOSIS_FRESH_MINUTES = 24 * 60
REPLACEMENT_TERMINAL_STATUSES = (
    "done", "dns_cleanup", "manual_required", "failed")


def get_settings(db_session) -> dict:
    """Настройки инфраструктуры: дефолты + переопределения из system_settings."""
    keys = list(INFRA_SETTINGS.keys())
    rows = (
        db_session.query(SystemSetting)
        .filter(SystemSetting.key.in_(keys))
        .all()
    )
    overrides = {row.key: row.value for row in rows}
    result = {}
    for key, (default, cast, _desc, _allowed) in INFRA_SETTINGS.items():
        raw = overrides.get(key)
        if raw is None:
            result[key] = default
            continue
        try:
            if cast is bool:
                result[key] = str(raw).strip().lower() in ("1", "true", "yes", "on")
            else:
                result[key] = cast(str(raw).strip())
        except (TypeError, ValueError):
            log.warning("infra setting %s has bad value %r, using default", key, raw)
            result[key] = default
    return result


def validate_setting(key: str, raw_value: str) -> str:
    """Проверка значения настройки; возвращает нормализованную строку."""
    if key not in INFRA_SETTINGS:
        raise InfraError("Неизвестная настройка")
    default, cast, _desc, allowed = INFRA_SETTINGS[key]
    raw_value = (raw_value or "").strip()
    try:
        if cast is bool:
            value = raw_value.lower() in ("1", "true", "yes", "on")
            return "true" if value else "false"
        value = cast(raw_value)
    except (TypeError, ValueError):
        raise InfraError("Некорректное значение")
    if cast in (int, float) and value <= 0 and not (
        key in ZERO_DISABLES_SETTINGS and value == 0
    ):
        raise InfraError("Значение должно быть больше нуля")
    if key.endswith("_ratio") and not (0 < value < 1):
        raise InfraError("Отношение должно быть в диапазоне (0, 1)")
    if key.endswith("_pct") and not (1 <= value <= 100):
        raise InfraError("Процент должен быть в диапазоне 1-100")
    if allowed and value not in allowed:
        raise InfraError(
            "Допустимые значения: " + ", ".join(str(v) for v in allowed)
        )
    if key == "infra_control_names" and not parse_control_names(value):
        raise InfraError(
            "Нужно хотя бы одно контрольное имя (домены через запятую)"
        )
    return str(value)


def parse_control_names(raw: str) -> list[str]:
    """Контрольные имена из настройки: строка через запятую -> список.

    Контрольное имя — посторонний домен, заведомо не находящийся под
    фильтром; проба им по нашему адресу проверяет сам адрес, потому что имя
    вне подозрений. Список нужен на случай, если контрольное имя всё же
    выгорит: тогда берётся следующее (см. control_burn_state и
    active_control_names).
    """
    seen = set()
    names = []
    for item in str(raw or "").split(","):
        name = item.strip().strip(".").lower()
        # Пробел внутри — не домен; защищаемся от мусора в настройке
        if not name or " " in name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def active_control_names(cfg: dict) -> list[str]:
    """Контрольные имена за вычетом выгоревших.

    Выгоревшее имя попадает под фильтр само и делает вывод «адрес забанен»
    ложным, поэтому диагностика переходит на следующее имя из списка. Если
    выгорели все, работаем прежним списком: остаться совсем без контроля
    хуже, чем мерить сомнительным именем — вердикт всё равно перепроверяется
    клиентскими именами.
    """
    names = parse_control_names(cfg.get("infra_control_names"))
    burned = set(parse_control_names(cfg.get("infra_control_names_burned")))
    alive = [name for name in names if name not in burned]
    return alive or names


def mark_control_name_burned(db_session, name: str) -> bool:
    """Помечает контрольное имя выгоревшим. True, если пометка новая.

    Пишется в system_settings, а не в отдельную таблицу: список короткий,
    виден в админке и снимается там же одним движением, когда имя
    разблокируют.
    """
    name = (name or "").strip().strip(".").lower()
    if not name:
        return False
    cfg = get_settings(db_session)
    burned = parse_control_names(cfg.get("infra_control_names_burned"))
    if name in burned:
        return False
    burned.append(name)
    value = ",".join(burned)
    row = db_session.get(SystemSetting, "infra_control_names_burned")
    if row is None:
        db_session.add(
            SystemSetting(key="infra_control_names_burned", value=value)
        )
    else:
        row.value = value
    db_session.flush()
    log.warning("infra: control name %s marked burned (all: %s)", name, value)
    return True


# --- вычисления -------------------------------------------------------------


# ISO 3166-1 alpha-2: флаг в UI рендерится только для валидного кода —
# иначе wl-1 (whitelist-ноды) получала бы «флаг» из букв WL
ISO_COUNTRY_CODES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ
BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR
CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU
ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ
LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ
MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF
PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI
SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR
TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())
# Неисошные префиксы имён нод -> ISO (uk-1 это Великобритания)
COUNTRY_CODE_ALIASES = {"UK": "GB"}


def server_country_code(server: InfraServer) -> str | None:
    """Страна сервера для флага: явное поле, иначе префикс node_name.

    de-1 -> DE, uk-1 -> GB; wl-1 -> None (не страна). Только валидные
    ISO-коды — «флаг» из случайных букв хуже отсутствия флага.
    """
    if server.country_code:
        code = server.country_code.upper()
    else:
        prefix = (server.node_name or "").split("-", 1)[0].split(".", 1)[0]
        if len(prefix) != 2 or not prefix.isalpha():
            return None
        code = prefix.upper()
    code = COUNTRY_CODE_ALIASES.get(code, code)
    return code if code in ISO_COUNTRY_CODES else None


def effective_bandwidth_mbps(server: InfraServer) -> int | None:
    """Лимит канала: ручной override, иначе определённая скорость линка."""
    if server.bandwidth_limit_mbps:
        return server.bandwidth_limit_mbps
    return server.detected_link_speed_mbps


def utilization_pct(
    rx_bps: int | None, tx_bps: int | None, limit_mbps: int | None
) -> float | None:
    """Загрузка full-duplex канала: max(rx, tx) относительно лимита."""
    if not limit_mbps:
        return None
    limit_bps = limit_mbps * 1_000_000
    used = max(rx_bps or 0, tx_bps or 0)
    return round(used * 100.0 / limit_bps, 1)


def is_online(server: InfraServer, cfg: dict, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return server.last_seen_at is not None and (
        now - server.last_seen_at
    ) <= timedelta(seconds=cfg["infra_offline_after_seconds"])


def server_title(server: InfraServer) -> str:
    return server.display_name or server.node_name


# --- payload'ы для админки --------------------------------------------------


def _dt(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds()))


def server_list_payload(db_session, include_archived: bool = False) -> dict:
    cfg = get_settings(db_session)
    now = utcnow()
    query = db_session.query(InfraServer)
    if not include_archived:
        query = query.filter(InfraServer.is_archived.is_(False))
    servers = query.order_by(InfraServer.node_name).all()

    ip_counts: dict[int, dict] = {}
    for row in db_session.query(InfraServerIp).all():
        entry = ip_counts.setdefault(
            row.server_id, {"active": 0, "reserve": 0, "blocked": 0}
        )
        if row.blocked_at:
            entry["blocked"] += 1
        elif row.on_interface:
            entry["active"] += 1
        else:
            entry["reserve"] += 1

    domains: dict[int, list] = {}
    for row in (
        db_session.query(InfraServerDomain)
        .order_by(InfraServerDomain.domain)
        .all()
    ):
        domains.setdefault(row.server_id, []).append(row.domain)

    checking = {
        row.server_id
        for row in db_session.query(InfraAnomaly)
        .filter(InfraAnomaly.status == "checking")
        .all()
    }
    replacing = {
        row.server_id
        for row in db_session.query(InfraIpReplacement)
        .filter(InfraIpReplacement.status.in_(REPLACEMENT_ACTIVE_STATUSES))
        .all()
    }

    items = []
    online_count = 0
    for server in servers:
        online = is_online(server, cfg, now)
        online_count += 1 if online else 0
        limit = effective_bandwidth_mbps(server)
        items.append(
            {
                "id": server.id,
                "name": server_title(server),
                "node_name": server.node_name,
                "country_code": server_country_code(server),
                "hostname": server.hostname,
                "online": online,
                "agent_version": server.agent_version,
                "last_seen_at": _dt(server.last_seen_at),
                "last_seen_age": _age_seconds(server.last_seen_at, now),
                "rx_bps": server.cur_rx_bps if online else None,
                "tx_bps": server.cur_tx_bps if online else None,
                "tcp_connections": (
                    server.cur_tcp_connections if online else None
                ),
                "cpu_load": (
                    float(server.cur_cpu_load)
                    if online and server.cur_cpu_load is not None
                    else None
                ),
                "utilization_pct": (
                    utilization_pct(server.cur_rx_bps, server.cur_tx_bps, limit)
                    if online
                    else None
                ),
                "effective_limit_mbps": limit,
                "bandwidth_limit_mbps": server.bandwidth_limit_mbps,
                "detected_link_speed_mbps": server.detected_link_speed_mbps,
                "wan_interface": server.wan_interface,
                "ips": ip_counts.get(server.id, {"active": 0, "reserve": 0, "blocked": 0}),
                "domains": domains.get(server.id, []),
                "checking": server.id in checking,
                "replacing": server.id in replacing,
                "xray_down": (
                    online
                    and (
                        server.xray_process_running is False
                        or server.xray_crash_loop is True
                    )
                ),
                "anomaly_paused": (
                    server.anomaly_suppressed_until is not None
                    and server.anomaly_suppressed_until > now
                ),
                "tspu_checks_enabled": bool(server.tspu_checks_enabled),
                "baseline_scale": (
                    float(server.baseline_scale)
                    if server.baseline_scale is not None
                    and server.baseline_scale_until is not None
                    and server.baseline_scale_until > now
                    else None
                ),
                "is_archived": server.is_archived,
            }
        )

    return {
        "servers": items,
        "totals": {
            "count": len(items),
            "online": online_count,
            "offline": len(items) - online_count,
        },
        "settings": {
            key: cfg[key] for key in ("infra_load_threshold_pct",)
        },
        "cloudflare_enabled": _cloudflare_enabled(),
        "now": _dt(now),
    }


def _cloudflare_enabled() -> bool:
    from engine import cloudflare_dns

    return cloudflare_dns.is_enabled()


def _ip_payload(row: InfraServerIp) -> dict:
    return {
        "id": row.id,
        "ip": row.ip,
        "prefix_len": row.prefix_len,
        "interface": row.interface,
        "source": row.source,
        "on_interface": row.on_interface,
        "blocked_at": _dt(row.blocked_at),
        "comment": row.comment,
        "last_seen_on_interface_at": _dt(row.last_seen_on_interface_at),
    }


def _command_payload(row: InfraAgentCommand) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "payload": row.payload or {},
        "status": row.status,
        "attempts": row.attempts,
        "error": row.error,
        "created_by": row.created_by,
        "created_at": _dt(row.created_at),
        "finished_at": _dt(row.finished_at),
    }


def _anomaly_payload(row: InfraAnomaly) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "traffic_ratio": float(row.traffic_ratio) if row.traffic_ratio is not None else None,
        "connection_ratio": (
            float(row.connection_ratio) if row.connection_ratio is not None else None
        ),
        "details": row.details or {},
        "censor_run_ids": row.censor_run_ids or [],
        "created_at": _dt(row.created_at),
        "resolved_at": _dt(row.resolved_at),
    }


def _replacement_payload(row: InfraIpReplacement) -> dict:
    return {
        "id": row.id,
        "old_ip": row.old_ip,
        "new_ip": row.new_ip,
        "domains": row.domains or [],
        "status": row.status,
        "log": row.log or [],
        "error": row.error,
        "created_by": row.created_by,
        "created_at": _dt(row.created_at),
        "finished_at": _dt(row.finished_at),
    }


def get_server(db_session, server_id) -> InfraServer:
    server = db_session.get(InfraServer, int(server_id or 0))
    if server is None:
        raise InfraError("Сервер не найден", 404)
    return server


def diagnosis_payload(anomaly_rows) -> dict | None:
    """Последний вынесенный диагноз для карточки сервера.

    Состояния адресов и имён отдельными полями не хранятся: источником
    истины остаётся сам диагноз, привязанный ко времени. Иначе пришлось бы
    поддерживать копию, которая рано или поздно разойдётся с фактом.
    """
    for row in anomaly_rows:
        verdict = (row.details or {}).get("verdict")
        if not verdict:
            continue
        at = row.resolved_at or row.created_at
        age_minutes = (
            int((utcnow() - at).total_seconds() // 60) if at else None
        )
        return {
            "anomaly_id": row.id,
            "status": row.status,
            "at": _dt(at),
            "age_minutes": age_minutes,
            "stale": (
                age_minutes is None or age_minutes > DIAGNOSIS_FRESH_MINUTES
            ),
            "control_name": (row.details or {}).get("control_name") or "",
            "blocked_ips": verdict.get("blocked_ips") or [],
            "blocked_snis": verdict.get("blocked_snis") or [],
            "confidence": verdict.get("confidence") or "",
            "actionable": bool(verdict.get("actionable")),
            "evidence": (row.details or {}).get("evidence") or [],
        }
    return None


def capacity_payload(server, cfg: dict) -> dict | None:
    """Снимок лимитов ноды с готовой оценкой.

    Пороговую логику держим на сервере, чтобы карточка и алерты не разошлись
    в трактовке одних и тех же чисел.
    """
    if not server.capacity:
        return None
    verdict = evaluate_capacity(server.capacity, cfg["infra_capacity_warn_pct"])
    return {
        "level": verdict["level"],
        "problems": verdict["problems"],
        "details": verdict["details"],
        "collision_only": verdict.get("collision_only", False),
        "not_applied": verdict.get("not_applied", False),
        "raw": server.capacity,
    }


def server_entry_payload(db_session, server, ip_rows, domain_rows) -> dict:
    """Точка входа сервера для карточки админки.

    Точкой входа считаются IP, которые стоят на интерфейсе, не
    заблокированы, публичные IPv4 и присутствуют в свежих A-записях
    привязанных доменов — те же критерии, по которым force_tspu_check
    выбирает цели замеров. domains — только домены, чьи A-записи реально
    указывают на эти IP (а не весь привязанный список).

    status:
      ok         — активные IP подтверждены свежими слепками DNS;
      not_in_dns — слепки свежие, но ни один активный IP в DNS не стоит
                   (нода выведена из DNS: dns_cleanup, слив трафика);
      dns_stale  — домены привязаны, но слепки отсутствуют/протухли:
                   показываем активные IP без подтверждения DNS;
      no_domains — доменов нет, показываем активные IP интерфейса;
      none       — активных публичных IPv4 нет вовсе.
    """
    active_rows = []
    for row in ip_rows:
        if not row.on_interface or row.blocked_at is not None:
            continue
        try:
            address = ipaddress.ip_address(row.ip)
        except ValueError:
            continue
        if address.version != 4 or not address.is_global:
            continue
        active_rows.append(row)
    # WAN-интерфейс вперёд: при fallback без DNS-данных точка входа — он
    active_rows.sort(
        key=lambda row: (row.interface != server.wan_interface, row.ip)
    )
    active_ips = [row.ip for row in active_rows]

    if not active_ips:
        return {"status": "none", "ips": [], "domains": []}

    snapshot_ips = fresh_dns_snapshot_ips(db_session, server)
    if snapshot_ips is None:
        status = "dns_stale" if domain_rows else "no_domains"
        return {"status": status, "ips": active_ips, "domains": []}

    in_dns = [ip for ip in active_ips if ip in snapshot_ips]
    if not in_dns:
        return {"status": "not_in_dns", "ips": active_ips, "domains": []}
    entry_set = set(in_dns)
    entry_domains = [
        row.domain
        for row in domain_rows
        if entry_set & set(row.last_a_ips or [])
    ]
    return {"status": "ok", "ips": in_dns, "domains": entry_domains}


def server_detail_payload(db_session, server_id) -> dict:
    cfg = get_settings(db_session)
    now = utcnow()
    server = get_server(db_session, server_id)

    ips = (
        db_session.query(InfraServerIp)
        .filter(InfraServerIp.server_id == server.id)
        .order_by(InfraServerIp.on_interface.desc(), InfraServerIp.ip)
        .all()
    )
    domains = (
        db_session.query(InfraServerDomain)
        .filter(InfraServerDomain.server_id == server.id)
        .order_by(InfraServerDomain.domain)
        .all()
    )
    commands = (
        db_session.query(InfraAgentCommand)
        .filter(InfraAgentCommand.server_id == server.id)
        .order_by(InfraAgentCommand.id.desc())
        .limit(20)
        .all()
    )
    anomalies = (
        db_session.query(InfraAnomaly)
        .filter(InfraAnomaly.server_id == server.id)
        .order_by(InfraAnomaly.id.desc())
        .limit(20)
        .all()
    )
    replacements = (
        db_session.query(InfraIpReplacement)
        .filter(InfraIpReplacement.server_id == server.id)
        .order_by(InfraIpReplacement.id.desc())
        .limit(20)
        .all()
    )

    online = is_online(server, cfg, now)
    limit = effective_bandwidth_mbps(server)
    uptime_seconds = None
    if server.boot_time:
        uptime_seconds = max(0, int((now - server.boot_time).total_seconds()))

    return {
        "server": {
            "id": server.id,
            "name": server_title(server),
            "display_name": server.display_name,
            "node_name": server.node_name,
            "country_code": server_country_code(server),
            "country_code_explicit": server.country_code,
            "machine_uid": server.machine_uid,
            "hostname": server.hostname,
            "os": server.os_name,
            "kernel": server.kernel,
            "agent_version": server.agent_version,
            "online": online,
            "wan_interface": server.wan_interface,
            "interfaces": server.interfaces or [],
            "detected_link_speed_mbps": server.detected_link_speed_mbps,
            "bandwidth_limit_mbps": server.bandwidth_limit_mbps,
            "effective_limit_mbps": limit,
            "boot_time": _dt(server.boot_time),
            "uptime_seconds": uptime_seconds,
            "agent_started_at": _dt(server.agent_started_at),
            "first_seen_at": _dt(server.first_seen_at),
            "last_seen_at": _dt(server.last_seen_at),
            "last_seen_age": _age_seconds(server.last_seen_at, now),
            "last_heartbeat_ip": server.last_heartbeat_ip,
            "rx_bps": server.cur_rx_bps if online else None,
            "tx_bps": server.cur_tx_bps if online else None,
            "tcp_connections": server.cur_tcp_connections if online else None,
            "conntrack": server.cur_conntrack if online else None,
            "xray_process_running": (
                server.xray_process_running if online else None
            ),
            "xray_crash_loop": server.xray_crash_loop if online else None,
            "xray_process_uptime_seconds": (
                server.xray_process_uptime_seconds if online else None
            ),
            "xray_access_log_age_seconds": (
                server.xray_access_log_age_seconds if online else None
            ),
            "cpu_load": (
                float(server.cur_cpu_load)
                if server.cur_cpu_load is not None
                else None
            ),
            "utilization_pct": (
                utilization_pct(server.cur_rx_bps, server.cur_tx_bps, limit)
                if online
                else None
            ),
            "anomaly_suppressed_until": _dt(server.anomaly_suppressed_until),
            "anomaly_paused": (
                server.anomaly_suppressed_until is not None
                and server.anomaly_suppressed_until > now
            ),
            "tspu_checks_enabled": bool(server.tspu_checks_enabled),
            "baseline_scale": (
                float(server.baseline_scale)
                if server.baseline_scale is not None
                and server.baseline_scale_until is not None
                and server.baseline_scale_until > now
                else None
            ),
            "baseline_scale_until": _dt(server.baseline_scale_until),
            "is_archived": server.is_archived,
            "notes": server.notes,
        },
        "ips": [_ip_payload(row) for row in ips],
        "domains": [row.domain for row in domains],
        # Список имён сервера (вкладка «SNI») и то, с чем реально идёт
        # проверка адресов: без списка — имена доменов (старая схема)
        "client_snis": server_client_snis(server),
        "probe_snis": server_wire_names(server, domains),
        "snis_source": "server" if server_client_snis(server) else "domains",
        "domains_detail": [
            {
                "domain": row.domain,
                "client_snis": list(row.client_snis or []),
                "snis": _domain_wire_names(server, row),
                "own_name": row.domain in _domain_wire_names(server, row),
                "custom_sni": bool(server_client_snis(server) or row.client_snis),
            }
            for row in domains
        ],
        "entry": server_entry_payload(db_session, server, ips, domains),
        "commands": [_command_payload(row) for row in commands],
        "anomalies": [_anomaly_payload(row) for row in anomalies],
        "diagnosis": diagnosis_payload(anomalies),
        "capacity": capacity_payload(server, cfg),
        "replacements": [_replacement_payload(row) for row in replacements],
        "who_connects": who_connects_payload(db_session, server.node_name, now),
        "cloudflare_enabled": _cloudflare_enabled(),
    }


def _analytics_cache_seconds() -> int:
    """TTL кэшей аналитики карточки (60с по умолчанию, см. settings)."""
    try:
        return max(
            10,
            min(
                3600,
                int(getattr(django_settings, "INFRA_GEOIP_CACHE_SECONDS", 60)),
            ),
        )
    except (TypeError, ValueError):
        return 60


def who_connects_payload(db_session, node_name: str, now: datetime) -> dict:
    """«Кто подключается» по данным ip-guard (ipguard_user_ips) этой ноды.

    Результат кэшируется на короткое окно целиком: это сводка за 24 часа,
    и пересчитывать её на каждый 5-секундный тик автообновления карточки
    незачем — а по стоимости именно она определяет время ответа карточки.
    """
    cache_seconds = _analytics_cache_seconds()
    epoch_seconds = int(now.replace(tzinfo=timezone.utc).timestamp())
    cache_key = (node_name, epoch_seconds // cache_seconds)
    with _who_connects_lock:
        cached = _who_connects_cache.get(cache_key)
    if cached is not None:
        return cached

    # Equal requests share a computation; unrelated nodes use different stripes.
    with _who_connects_compute_locks[hash(node_name) % len(_who_connects_compute_locks)]:
        with _who_connects_lock:
            cached = _who_connects_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = _who_connects_payload_uncached(db_session, node_name, now)
        with _who_connects_lock:
            stale_keys = [key for key in _who_connects_cache if key[0] == node_name]
            for stale_key in stale_keys:
                _who_connects_cache.pop(stale_key, None)
            _who_connects_cache[cache_key] = payload
        return payload


def node_interface_ips(db_session) -> frozenset[str]:
    """Публичные адреса нод, стоящие сейчас на интерфейсе.

    Берутся из `infra_server_ips` с `on_interface=true` по ВСЕМ серверам —
    включая мосты/релеи с белым IP, которые пробрасывают трафик на ноду и
    в наблюдениях ip-guard выглядят как один «пользователь» с сотнями
    подключений. Резервы (`on_interface=false`) не исключаются: с них никто
    не подключается, а если адрес встал на интерфейс — heartbeat поднимет
    флаг. Тот же принцип, что фильтр адресов нод в детекторе ip-guard.

    Адреса нормализуются через `ipaddress` (обе стороны пишут
    `str(ip_address(...))`, но сырая строка тоже остаётся в множестве).
    Ошибка чтения — warning и пустое множество: карточка считает как раньше.
    """

    try:
        rows = (
            db_session.execute(
                select(InfraServerIp.ip).where(InfraServerIp.on_interface.is_(True))
            )
            .scalars()
            .all()
        )
    except Exception as exc:  # noqa: BLE001 — любой сбой БД не должен ронять карточку
        log.warning(
            "stage=who_connects status=node_ips_unavailable error=%s",
            exc,
        )
        return frozenset()

    result: set[str] = set()
    for raw in rows:
        value = (raw or "").strip()
        if not value:
            continue
        result.add(value)
        try:
            result.add(str(ipaddress.ip_address(value)))
        except ValueError:
            continue
    return frozenset(result)


def _who_connects_criteria(node_name: str, since: datetime, excluded) -> list:
    """Общий фильтр запросов блока: нода + окно + минус адреса нод.

    `NOT IN` добавляется только при непустом множестве, чтобы план запроса
    без адресов нод остался прежним (индекс `(node, last_seen)`).
    """

    criteria = [
        UserIpObservation.node == node_name,
        UserIpObservation.last_seen >= since,
    ]
    if excluded:
        criteria.append(UserIpObservation.ip.not_in(sorted(excluded)))
    return criteria


def _who_connects_payload_uncached(
    db_session, node_name: str, now: datetime
) -> dict:
    day_ago = now - timedelta(hours=24)
    hour_ago = now - timedelta(hours=1)
    excluded = node_interface_ips(db_session)

    unique_24h = (
        db_session.query(func.count(func.distinct(UserIpObservation.ip)))
        .filter(*_who_connects_criteria(node_name, day_ago, excluded))
        .scalar()
        or 0
    )
    unique_1h = (
        db_session.query(func.count(func.distinct(UserIpObservation.ip)))
        .filter(*_who_connects_criteria(node_name, hour_ago, excluded))
        .scalar()
        or 0
    )
    top_rows = (
        db_session.query(
            UserIpObservation.ip,
            func.sum(UserIpObservation.hits).label("hits"),
            func.count(func.distinct(UserIpObservation.username)).label("users"),
        )
        .filter(*_who_connects_criteria(node_name, day_ago, excluded))
        .group_by(UserIpObservation.ip)
        .order_by(func.sum(UserIpObservation.hits).desc())
        .limit(20)
        .all()
    )
    # Сколько уникальных адресов нод реально было отброшено на этой ноде за
    # 24 часа — админ видит, что фильтр сработал, а не что данных нет
    excluded_node_ips = 0
    if excluded:
        excluded_node_ips = (
            db_session.query(func.count(func.distinct(UserIpObservation.ip)))
            .filter(
                UserIpObservation.node == node_name,
                UserIpObservation.last_seen >= day_ago,
                UserIpObservation.ip.in_(sorted(excluded)),
            )
            .scalar()
            or 0
        )
    return {
        "unique_ips_24h": int(unique_24h),
        "unique_ips_1h": int(unique_1h),
        "excluded_node_ips": int(excluded_node_ips),
        "top_addresses": [
            {"ip": row.ip, "hits": int(row.hits or 0), "users": int(row.users or 0)}
            for row in top_rows
        ],
        "geo": _who_connects_geo_payload(
            db_session, node_name, day_ago, now, excluded=excluded
        ),
    }


def _who_connects_geo_payload(
    db_session,
    node_name: str,
    day_ago: datetime,
    now: datetime,
    excluded=frozenset(),
) -> dict:
    """Агрегирует страны и субъекты РФ по всем IP ноды за 24 часа.

    Lookup выполняется только по локальной MMDB. Результат кэшируется на
    короткое окно, чтобы пятиисекундный refresh карточки не повторял GROUP BY
    по тысячам адресов и тысячи GeoIP lookup'ов.
    """

    database = geoip_lookup.configured_database()
    if database is None:
        return {
            "enabled": False,
            "reason": "not_configured",
            "countries": [],
            "russian_regions": [],
        }

    cache_seconds = _analytics_cache_seconds()
    epoch_seconds = int(now.replace(tzinfo=timezone.utc).timestamp())
    cache_key = (
        node_name,
        epoch_seconds // cache_seconds,
        database.path,
        database.mtime_ns,
    )
    with _geo_analytics_lock:
        cached = _geo_analytics_cache.get(cache_key)
    if cached is not None:
        return cached

    rows = (
        db_session.query(
            UserIpObservation.ip,
            func.sum(UserIpObservation.hits).label("hits"),
        )
        .filter(*_who_connects_criteria(node_name, day_ago, excluded))
        .group_by(UserIpObservation.ip)
        .all()
    )
    total_hits = sum(int(row.hits or 0) for row in rows)
    countries: dict[str, dict] = {}
    russian_regions: dict[str, dict] = {}
    located_ips = 0
    russia_hits = 0

    def add(
        bucket: dict[str, dict],
        key: str,
        name: str,
        code: str | None,
        hits: int,
    ):
        item = bucket.setdefault(
            key,
            {"name": name, "code": code, "hits": 0, "addresses": 0},
        )
        item["hits"] += hits
        item["addresses"] += 1

    for row in rows:
        hits = int(row.hits or 0)
        location = geoip_lookup.lookup_ip(row.ip, database)
        if location is None:
            add(countries, "__unknown__", "Не определилось", None, hits)
            continue

        located_ips += 1
        add(
            countries,
            location.country_code,
            location.country_name,
            location.country_code,
            hits,
        )
        if location.country_code != "RU":
            continue

        russia_hits += hits
        # DB-IP City Lite определяет subdivision по имени, но обычно не
        # заполняет iso_code. Имя остаётся полноценным ключом и не должно
        # сливаться с другими субъектами в общий __unknown__ bucket.
        region_key = location.region_code or (
            f"name:{location.region_name.casefold()}"
            if location.region_name
            else "__unknown__"
        )
        add(
            russian_regions,
            region_key,
            location.region_name or "Регион не определился",
            location.region_code,
            hits,
        )

    def ranking(bucket: dict[str, dict]) -> list[dict]:
        result = []
        for item in bucket.values():
            result.append(
                {
                    **item,
                    "share_pct": (
                        round(item["hits"] * 100 / total_hits, 1) if total_hits else 0.0
                    ),
                }
            )
        return sorted(result, key=lambda item: (-item["hits"], item["name"]))

    payload = {
        "enabled": True,
        "source": "DB-IP City Lite",
        "total_ips": len(rows),
        "located_ips": located_ips,
        "unlocated_ips": len(rows) - located_ips,
        "total_hits": total_hits,
        "russia_share_pct": (
            round(russia_hits * 100 / total_hits, 1) if total_hits else 0.0
        ),
        "countries": ranking(countries),
        "russian_regions": ranking(russian_regions),
    }
    with _geo_analytics_lock:
        # Храним только свежий bucket каждой ноды, иначе process-local кэш рос
        # бы на один элемент каждую минуту.
        stale_keys = [key for key in _geo_analytics_cache if key[0] == node_name]
        for stale_key in stale_keys:
            _geo_analytics_cache.pop(stale_key, None)
        _geo_analytics_cache[cache_key] = payload
    return payload


def reset_geo_analytics_cache() -> None:
    """Сбрасывает короткие process-local кэши карточки (тесты/обслуживание)."""

    with _geo_analytics_lock:
        _geo_analytics_cache.clear()
    with _who_connects_lock:
        _who_connects_cache.clear()


# --- серии телеметрии для графиков ------------------------------------------

TELEMETRY_PERIODS = {
    # period -> (timedelta, источник, шаг группировки в секундах для ответа)
    "3h": (timedelta(hours=3), "raw", None),
    "24h": (timedelta(hours=24), "agg60", 120),
    "7d": (timedelta(days=7), "agg900", 900),
    "30d": (timedelta(days=30), "agg900", 3600),
}


def telemetry_series_payload(db_session, server_id, period: str) -> dict:
    if period not in TELEMETRY_PERIODS:
        raise InfraError("Неизвестный период")
    server = get_server(db_session, server_id)
    now = utcnow()
    span, source, group_seconds = TELEMETRY_PERIODS[period]
    since = now - span

    points = []
    if source == "raw":
        rows = (
            db_session.query(InfraTelemetry)
            .filter(
                InfraTelemetry.server_id == server.id,
                InfraTelemetry.ts >= since,
            )
            .order_by(InfraTelemetry.ts)
            .all()
        )
        for row in rows:
            points.append(
                {
                    "ts": _dt(row.ts),
                    "rx_bps": row.rx_bps,
                    "tx_bps": row.tx_bps,
                    "tcp": row.tcp_connections,
                    "conntrack": row.conntrack,
                }
            )
    else:
        bucket_seconds = 60 if source == "agg60" else 900
        rows = (
            db_session.query(InfraTelemetryAgg)
            .filter(
                InfraTelemetryAgg.server_id == server.id,
                InfraTelemetryAgg.bucket_seconds == bucket_seconds,
                InfraTelemetryAgg.bucket_start >= since,
            )
            .order_by(InfraTelemetryAgg.bucket_start)
            .all()
        )
        grouped: dict[datetime, list] = {}
        step = group_seconds or bucket_seconds
        for row in rows:
            grouped.setdefault(_floor_dt(row.bucket_start, step), []).append(row)
        for key in sorted(grouped):
            bucket_rows = grouped[key]
            def avg(values):
                values = [v for v in values if v is not None]
                return int(sum(values) / len(values)) if values else None
            points.append(
                {
                    "ts": _dt(key),
                    "rx_bps": avg([r.rx_bps_avg for r in bucket_rows]),
                    "tx_bps": avg([r.tx_bps_avg for r in bucket_rows]),
                    "rx_bps_max": max(
                        (r.rx_bps_max or 0 for r in bucket_rows), default=None
                    ),
                    "tx_bps_max": max(
                        (r.tx_bps_max or 0 for r in bucket_rows), default=None
                    ),
                    "tcp": avg([r.tcp_avg for r in bucket_rows]),
                    "conntrack": avg([r.conntrack_avg for r in bucket_rows]),
                }
            )

    summary = _series_summary(db_session, server, since, now, period)
    return {
        "period": period,
        "points": points,
        "summary": summary,
    }


def _series_summary(
    db_session, server: InfraServer, since: datetime, now: datetime, period: str
) -> dict:
    """Карточки над графиком: объём, пики, лимит — в духе portshaper."""
    limit = effective_bandwidth_mbps(server)

    # Объём: по минутным агрегатам (rx_bytes/tx_bytes за бакет)
    volume = db_session.query(
        func.sum(InfraTelemetryAgg.rx_bytes),
        func.sum(InfraTelemetryAgg.tx_bytes),
    ).filter(
        InfraTelemetryAgg.server_id == server.id,
        InfraTelemetryAgg.bucket_seconds == 60,
        InfraTelemetryAgg.bucket_start >= since,
    ).one()
    rx_volume, tx_volume = int(volume[0] or 0), int(volume[1] or 0)
    if period in ("7d", "30d"):
        volume = db_session.query(
            func.sum(InfraTelemetryAgg.rx_bytes),
            func.sum(InfraTelemetryAgg.tx_bytes),
        ).filter(
            InfraTelemetryAgg.server_id == server.id,
            InfraTelemetryAgg.bucket_seconds == 900,
            InfraTelemetryAgg.bucket_start >= since,
        ).one()
        rx_volume, tx_volume = int(volume[0] or 0), int(volume[1] or 0)

    # Пики: для коротких периодов по сырым сэмплам (без усреднения),
    # для длинных — по максимумам агрегатов
    if period in ("3h", "24h"):
        peaks = db_session.query(
            func.max(InfraTelemetry.rx_bps), func.max(InfraTelemetry.tx_bps)
        ).filter(
            InfraTelemetry.server_id == server.id,
            InfraTelemetry.ts >= since,
        ).one()
        peak_source = "raw"
    else:
        peaks = db_session.query(
            func.max(InfraTelemetryAgg.rx_bps_max),
            func.max(InfraTelemetryAgg.tx_bps_max),
        ).filter(
            InfraTelemetryAgg.server_id == server.id,
            InfraTelemetryAgg.bucket_seconds == 900,
            InfraTelemetryAgg.bucket_start >= since,
        ).one()
        peak_source = "agg"

    return {
        "rx_volume_bytes": rx_volume,
        "tx_volume_bytes": tx_volume,
        "rx_peak_bps": int(peaks[0] or 0),
        "tx_peak_bps": int(peaks[1] or 0),
        "peak_source": peak_source,
        "effective_limit_mbps": limit,
        "cur_rx_bps": server.cur_rx_bps,
        "cur_tx_bps": server.cur_tx_bps,
        "cur_tcp_connections": server.cur_tcp_connections,
    }


# --- мутации из админки -----------------------------------------------------


def update_server(db_session, server_id, fields: dict) -> InfraServer:
    server = get_server(db_session, server_id)
    if "display_name" in fields:
        server.display_name = (fields["display_name"] or "").strip()[:128] or None
    if "notes" in fields:
        server.notes = (fields["notes"] or "").strip() or None
    if "bandwidth_limit_mbps" in fields:
        raw = str(fields["bandwidth_limit_mbps"] or "").strip()
        if not raw:
            server.bandwidth_limit_mbps = None
        else:
            try:
                value = int(raw)
            except ValueError:
                raise InfraError("Лимит канала: введите число Mbit/s")
            if not (1 <= value <= 1_000_000):
                raise InfraError("Лимит канала: значение вне диапазона")
            server.bandwidth_limit_mbps = value
    if "country_code" in fields:
        raw = (fields["country_code"] or "").strip().upper()
        if not raw:
            server.country_code = None
        else:
            raw = COUNTRY_CODE_ALIASES.get(raw, raw)
            if raw not in ISO_COUNTRY_CODES:
                raise InfraError("Страна: нужен код ISO 3166-1 alpha-2 (DE, NL...)")
            server.country_code = raw
    if "is_archived" in fields:
        server.is_archived = bool(fields["is_archived"])
    return server


def add_manual_ip(
    db_session, server_id, ip_value: str, prefix_len, comment: str = ""
) -> InfraServerIp:
    server = get_server(db_session, server_id)
    try:
        address = ipaddress.ip_address((ip_value or "").strip())
    except ValueError:
        raise InfraError("Некорректный IP-адрес")
    if not address.is_global:
        raise InfraError(
            "Нужен публичный IP: приватный адрес не может быть резервом "
            "для DNS"
        )
    try:
        prefix = int(prefix_len or (32 if address.version == 4 else 128))
    except (TypeError, ValueError):
        raise InfraError("Некорректный префикс")
    max_prefix = 32 if address.version == 4 else 128
    if not (0 < prefix <= max_prefix):
        raise InfraError("Некорректный префикс")

    existing = (
        db_session.query(InfraServerIp)
        .filter(
            InfraServerIp.server_id == server.id,
            InfraServerIp.ip == str(address),
        )
        .one_or_none()
    )
    if existing is not None:
        raise InfraError("Такой IP уже есть у сервера")
    row = InfraServerIp(
        server_id=server.id,
        ip=str(address),
        prefix_len=prefix,
        source="manual",
        on_interface=False,
        comment=(comment or "").strip()[:256] or None,
    )
    db_session.add(row)
    db_session.flush()
    return row


def delete_ip(db_session, server_id, ip_id) -> None:
    server = get_server(db_session, server_id)
    row = db_session.get(InfraServerIp, int(ip_id or 0))
    if row is None or row.server_id != server.id:
        raise InfraError("IP не найден", 404)
    if row.on_interface:
        raise InfraError("Нельзя удалить IP, находящийся на интерфейсе")
    db_session.delete(row)


def set_ip_blocked(db_session, server_id, ip_id, blocked: bool) -> InfraServerIp:
    server = get_server(db_session, server_id)
    row = db_session.get(InfraServerIp, int(ip_id or 0))
    if row is None or row.server_id != server.id:
        raise InfraError("IP не найден", 404)
    row.blocked_at = utcnow() if blocked else None
    return row


def normalize_client_snis(raw) -> list[str]:
    """Клиентские имена домена: нормализация и проверка. Пусто -> [].

    Принимает список или строку через запятую/пробел/перевод строки: в
    админке это одно поле ввода, а имён у связки столько, сколько протоколов
    развели на ноде через haproxy.

    В отличие от домена A-записи, имя из ClientHello не обязано быть
    настоящим доменом: под протоколы заводятся example.com/net/org, а иногда
    и localhost. Точка поэтому не требуется — запрещены только разделители
    (пробел, слеш, запятая: запятая внутри значения означала бы, что список
    не разобрали, и в Atlas уехало бы 'a.com,b.com' одним именем, сжигая
    полный прогон) и длина сверх колонки.
    """
    if raw is None:
        items = []
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = str(raw).replace("\n", ",").replace(" ", ",").split(",")
    names: list[str] = []
    for item in items:
        name = str(item or "").strip().strip(".").lower()
        if not name:
            continue
        if " " in name or "/" in name or "," in name or len(name) > 255:
            raise InfraError(f"Некорректный SNI: {name[:64]}")
        if name not in names:
            names.append(name)
    if len(names) > MAX_DOMAIN_SNIS:
        raise InfraError(
            f"Слишком много имён (максимум {MAX_DOMAIN_SNIS})"
        )
    return names


def domain_client_snis(row) -> list[str]:
    """Что уходит в ClientHello для этой связки «сервер + домен».

    НИКОГДА не пустой список: без заданных имён это сам домен (старая
    схема). Пустой список означал бы фазу 2 без целей, то есть вердикт по
    адресу вслепую по одному контрольному имени — ровно тот путь, которым
    2026-09-02 адрес уехал без единой проверки имён.
    """
    names = list(getattr(row, "client_snis", None) or [])
    normalized = [str(n).strip().lower() for n in names if str(n).strip()]
    return normalized or [row.domain]


def domain_own_name_on_wire(row) -> bool:
    """Уходит ли сам домен A-записи в эфир (старая схема или смешанная)."""
    return row.domain in domain_client_snis(row)


def add_domain(
    db_session, server_id, domain: str, client_snis="" 
) -> InfraServerDomain:
    server = get_server(db_session, server_id)
    domain = (domain or "").strip().strip(".").lower()
    if not domain or "." not in domain or " " in domain or "/" in domain:
        raise InfraError("Некорректный домен")
    snis = normalize_client_snis(client_snis)
    # Один домен может стоять на нескольких серверах (round-robin из
    # нескольких A-записей); запрещён только дубль в рамках одного сервера
    existing = (
        db_session.query(InfraServerDomain)
        .filter(
            InfraServerDomain.server_id == server.id,
            InfraServerDomain.domain == domain,
        )
        .one_or_none()
    )
    if existing is not None:
        raise InfraError("Домен уже привязан к этому серверу")
    row = InfraServerDomain(
        server_id=server.id,
        domain=domain,
        client_snis=_stored_snis(snis, domain),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _stored_snis(snis: list, domain: str):
    """Что писать в колонку: None для старой схемы «единственное имя = домен».

    Так строка, которую админ не трогал, и строка, где он явно вписал сам
    домен, читаются одинаково, а NULL остаётся однозначным признаком
    «имена не настраивали».
    """
    if not snis or snis == [domain]:
        return None
    return list(snis)


def set_domain_snis(
    db_session, server_id, domain: str, client_snis, apply_all: bool = False
) -> list:
    """Задаёт (или снимает) клиентские имена домена.

    apply_all — записать тот же набор ВСЕМ доменам сервера. Имена разводит
    haproxy по req.ssl_sni на общем :443, то есть набор относится скорее к
    ноде, чем к отдельному домену; при нескольких A-записях у одной ноды
    вбивать один и тот же список руками в каждый домен — заведомая
    возможность опечататься.

    Пустое значение и список из одного домена дают NULL: это одна и та же
    старая схема «имя = домен».
    """
    server = get_server(db_session, server_id)
    domain = (domain or "").strip().strip(".").lower()
    snis = normalize_client_snis(client_snis)

    query = db_session.query(InfraServerDomain).filter(
        InfraServerDomain.server_id == server.id
    )
    if apply_all:
        rows = query.order_by(InfraServerDomain.domain).all()
        if not rows:
            raise InfraError("У сервера нет привязанных доменов", 404)
    else:
        row = query.filter(InfraServerDomain.domain == domain).one_or_none()
        if row is None:
            raise InfraError("Домен не найден", 404)
        rows = [row]

    for row in rows:
        row.client_snis = _stored_snis(snis, row.domain)
    db_session.flush()
    return rows


def server_client_snis(server) -> list[str]:
    """Явно заданный список имён сервера (вкладка «SNI»). Пусто -> []."""
    names = list(getattr(server, "client_snis", None) or [])
    return [str(n).strip().lower() for n in names if str(n).strip()]


def _domain_wire_names(server, row) -> list[str]:
    """Имена, которыми проверяется адрес для этого домена.

    Список сервера общий для всех его доменов: haproxy разводит
    протоколы по SNI на одном адресе, а не по доменам. Без списка —
    старая схема связки (client_snis домена или сам домен).
    """
    return server_client_snis(server) or domain_client_snis(row)


def server_wire_names(server, domain_rows) -> list[str]:
    """Все имена, с которыми диагностика проверяет адреса сервера.

    Пустой только у сервера без доменов: без списка «SNI» имена берутся
    из доменов, и пустой набор означал бы вердикт по адресу вслепую.
    """
    explicit = server_client_snis(server)
    if explicit:
        return list(explicit)
    ordered: list[str] = []
    for row in domain_rows:
        for name in domain_client_snis(row):
            if name not in ordered:
                ordered.append(name)
    return ordered


def set_server_snis(db_session, server_id, client_snis) -> InfraServer:
    """Задаёт (или снимает — пустым значением) список SNI сервера."""
    server = get_server(db_session, server_id)
    snis = normalize_client_snis(client_snis)
    server.client_snis = list(snis) or None
    db_session.flush()
    return server


def delete_domain(db_session, server_id, domain: str) -> None:
    server = get_server(db_session, server_id)
    row = (
        db_session.query(InfraServerDomain)
        .filter(
            InfraServerDomain.server_id == server.id,
            InfraServerDomain.domain == (domain or "").strip().lower(),
        )
        .one_or_none()
    )
    if row is None:
        raise InfraError("Домен не найден", 404)
    db_session.delete(row)


def create_ensure_ip_command(
    db_session, server: InfraServer, ip_row: InfraServerIp, created_by: str
) -> InfraAgentCommand:
    """Команда агенту поставить IP на WAN-интерфейс (идемпотентная)."""
    if not server.wan_interface:
        raise InfraError("У сервера не определён WAN-интерфейс")
    pending = (
        db_session.query(InfraAgentCommand)
        .filter(
            InfraAgentCommand.server_id == server.id,
            InfraAgentCommand.kind == "ensure_ip",
            InfraAgentCommand.status.in_(("pending", "sent")),
        )
        .all()
    )
    for command in pending:
        if (command.payload or {}).get("ip") == ip_row.ip:
            return command
    command = InfraAgentCommand(
        server_id=server.id,
        kind="ensure_ip",
        payload={
            "ip": ip_row.ip,
            "prefix": ip_row.prefix_len,
            "interface": ip_row.interface or server.wan_interface,
        },
        status="pending",
        created_by=created_by[:128],
        expires_at=utcnow() + timedelta(minutes=COMMAND_TTL_MINUTES),
    )
    db_session.add(command)
    db_session.flush()
    return command


def fresh_dns_snapshot_ips(db_session, server: InfraServer) -> set | None:
    """Объединение A-записей привязанных доменов по слепкам DNS-вотчера.

    None — решение принимать нельзя: доменов нет, либо хоть один слепок
    отсутствует/протух (вотчер умер, Cloudflare отвалился). Свежесть и
    полнота обязательны — по устаревшим данным нельзя ни выключать
    детектор, ни сужать цели замеров.
    """
    domains = (
        db_session.query(InfraServerDomain)
        .filter(InfraServerDomain.server_id == server.id)
        .all()
    )
    if not domains:
        return None
    fresh_cutoff = utcnow() - timedelta(hours=24)
    snapshots = [
        set(row.last_a_ips or [])
        for row in domains
        if row.dns_checked_at is not None and row.dns_checked_at >= fresh_cutoff
    ]
    if len(snapshots) < len(domains):
        return None
    return set().union(*snapshots)


def server_is_in_dns(db_session, server: InfraServer) -> bool:
    """Стоит ли сервер в DNS: хоть один его активный незаблокированный IP
    присутствует в A-записях привязанных доменов (по слепкам DNS-вотчера).

    Нода, выведенная из DNS (слив трафика на другую, dns_cleanup после бана
    без резерва), теряет клиентов «как при бане» — но это намеренно, и гонять
    по ней anomaly detector с ТСПУ-замерами бессмысленно. Если слепков ещё
    нет — считаем, что в DNS: консервативный fallback к старому поведению.
    """
    dns_ips = fresh_dns_snapshot_ips(db_session, server)
    if dns_ips is None:
        return True
    active_ips = {
        row.ip
        for row in db_session.query(InfraServerIp)
        .filter(
            InfraServerIp.server_id == server.id,
            InfraServerIp.on_interface.is_(True),
            InfraServerIp.blocked_at.is_(None),
        )
        .all()
    }
    return bool(dns_ips & active_ips)


def snooze_anomaly_detector(db_session, server_id, hours) -> InfraServer:
    """Пауза anomaly detector'а: плановые работы (перебалансировка DNS,
    миграция клиентов) роняют трафик без всякой блокировки."""
    server = get_server(db_session, server_id)
    try:
        hours_value = int(hours or 24)
    except (TypeError, ValueError):
        raise InfraError("Некорректное число часов")
    if not (1 <= hours_value <= 24 * 14):
        raise InfraError("Часы паузы: от 1 до 336")
    server.anomaly_suppressed_until = utcnow() + timedelta(hours=hours_value)
    return server


def unsnooze_anomaly_detector(db_session, server_id) -> InfraServer:
    server = get_server(db_session, server_id)
    server.anomaly_suppressed_until = None
    return server


def set_tspu_checks_enabled(db_session, server_id, enabled: bool) -> InfraServer:
    """Постоянное включение/выключение слежки за ТСПУ по серверу.

    Выключенный сервер не порождает аномалий и автоматических RIPE
    Atlas-замеров (кредиты не расходуются). Телеметрия, графики, алерты
    offline/нагрузки/лимитов продолжают работать. Ручной запуск диагностики
    кнопкой в карточке остаётся доступен — это явное действие админа.
    """
    server = get_server(db_session, server_id)
    server.tspu_checks_enabled = bool(enabled)
    return server


def request_replacement(
    db_session, server_id, old_ip: str, created_by: str, anomaly_id=None,
    domains: list = None, banned_names: list = None,
    domain_snis: dict = None, sni_banned: dict = None,
) -> InfraIpReplacement:
    """replaceFailedIp: заявка на замену IP (идемпотентная).

    Вызывается воркером после подтверждения ТСПУ или админом вручную.

    domains — из каких доменов убирать старый адрес. None означает «все
    домены сервера». banned_names — домены СТАРОЙ схемы (имя = домен), чьё
    имя забанено: под них новый адрес не публикуется (имя всё равно не
    работает, а чистый адрес под ним рискует уйти в бан следом), но мёртвый
    старый адрес из их A-записей убирается. Остальные имена перед
    публикацией проверяются на кандидате по одному.

    domain_snis — {домен: [клиентские имена]} для доменов заявки: пробы на
    кандидате идут именно этими именами, а не доменами A-записей.
    sni_banned — {домен: [забаненные имена этого домена]}: их отказ на
    кандидате ожидаем и публикацию не отменяет (имя забанено везде, а
    старый адрес мёртв); чинится сменой имени в конфигах.
    """
    server = get_server(db_session, server_id)
    old_ip = (old_ip or "").strip()
    ip_row = (
        db_session.query(InfraServerIp)
        .filter(
            InfraServerIp.server_id == server.id, InfraServerIp.ip == old_ip
        )
        .one_or_none()
    )
    if ip_row is None:
        raise InfraError("IP не принадлежит этому серверу")

    active = (
        db_session.query(InfraIpReplacement)
        .filter(
            InfraIpReplacement.server_id == server.id,
            InfraIpReplacement.old_ip == old_ip,
            InfraIpReplacement.status.in_(REPLACEMENT_ACTIVE_STATUSES),
        )
        .first()
    )
    if active is not None:
        return active

    # Уже успешно заменяли и IP помечен заблокированным — повторная заявка
    # не выбирает следующий резерв (идемпотентность replaceFailedIp)
    done = (
        db_session.query(InfraIpReplacement)
        .filter(
            InfraIpReplacement.server_id == server.id,
            InfraIpReplacement.old_ip == old_ip,
            InfraIpReplacement.status.in_(("done", "dns_cleanup")),
        )
        .order_by(InfraIpReplacement.id.desc())
        .first()
    )
    if done is not None and ip_row.blocked_at is not None:
        return done

    if domains is None:
        domains = [
            row.domain
            for row in db_session.query(InfraServerDomain)
            .filter(InfraServerDomain.server_id == server.id)
            .order_by(InfraServerDomain.domain)
            .all()
        ]
    replacement = InfraIpReplacement(
        server_id=server.id,
        anomaly_id=anomaly_id,
        old_ip=old_ip,
        domains=domains,
        status="pending",
        created_by=created_by[:128],
        log=[
            {
                "ts": _dt(utcnow()),
                "step": "created",
                "message": f"Заявка на замену {old_ip} ({created_by})"
                + (
                    "; имена под баном, не публикуются: "
                    + ", ".join(sorted(set(banned_names)))
                    if banned_names
                    else ""
                ),
                "banned_names": sorted(set(banned_names or [])),
                "domain_snis": {
                    str(domain): [str(name) for name in (names or [])]
                    for domain, names in (domain_snis or {}).items()
                    if names
                },
                "sni_banned": {
                    str(domain): sorted({str(name) for name in (names or [])})
                    for domain, names in (sni_banned or {}).items()
                    if names
                },
            }
        ],
    )
    db_session.add(replacement)
    db_session.flush()
    return replacement


# --- лимиты ноды (conntrack, nginx, дескрипторы) ----------------------------


# Заполнение conntrack, выше которого таблица реально близка к пределу.
# Ниже него отказы вставки объясняются не переполнением, а чем-то другим.
CONNTRACK_CRIT_USAGE_PCT = 90
# Отказы вставки при свободной таблице — гонки однотипных потоков: два пакета
# одновременно создают запись, один проигрывает. После раскатки NOTRACK на
# loopback и локального DNS-кэша на нодах (сентябрь 2026) фон таких гонок —
# десятки за интервал, в основном транзитный клиентский DNS. Тревога — только
# когда счёт идёт на сотни: это регресс одного из слоёв защиты, а не шум.
CONNTRACK_INSERT_FAILED_NOISE = 200


def _plural_records(count: int) -> str:
    """«N записей» с правильным окончанием: «1 записей» режет глаз."""
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} записей"
    last = count % 10
    if last == 1:
        return f"{count} запись"
    if last in (2, 3, 4):
        return f"{count} записи"
    return f"{count} записей"


def evaluate_capacity(capacity: dict, warn_pct: int) -> dict:
    """Оценка снимка лимитов: {"level", "problems", "details"}.

    level: "crit" — потолок исчерпан и нода теряет клиентов (изменения DNS
    запрещаются, диагноз NODE_CAPACITY); "warn" — пора поднимать потолок,
    но клиенты ещё обслуживаются; "ok" — запас есть.

    Приближение к порогу НЕ отключает диагностику блокировок: нагруженная,
    но исправная нода должна по-прежнему проверяться на баны.
    """
    if not capacity:
        return {"level": "ok", "problems": [], "details": []}

    problems: list[str] = []
    details: list[str] = []
    level = "ok"
    collision_warn = False
    not_applied_warn = False

    def escalate(new_level):
        nonlocal level
        order = {"ok": 0, "warn": 1, "crit": 2}
        if order[new_level] > order[level]:
            level = new_level

    conntrack = capacity.get("conntrack") or {}
    usage = conntrack.get("usage_pct")
    insert_failed = conntrack.get("insert_failed_delta") or 0
    live_max = conntrack.get("max")
    # Что оператор задал в /etc/sysctl.d. Присылает агент v0.4.2+; у старых
    # агентов ключа нет, и всё работает как раньше.
    configured_max = conntrack.get("configured_max")
    # Лимит задан, но ядро живёт с меньшим: настройка не пережила загрузку
    # (systemd-sysctl отрабатывает раньше, чем грузится модуль nf_conntrack,
    # и молча пропускает ветку /proc/sys/net/netfilter). Поднимать потолок
    # бесполезно — он уже поднят в конфиге, просто не доехал.
    limit_not_applied = bool(
        configured_max and live_max and configured_max > live_max
    )
    if usage is not None:
        details.append(
            f"conntrack: {conntrack.get('count')} / {conntrack.get('max')} "
            f"({usage}%)"
        )
        if configured_max and live_max and configured_max != live_max:
            details.append(f"в /etc/sysctl.d задано: {configured_max}")
        table_full = (
            insert_failed > 0 and usage >= CONNTRACK_CRIT_USAGE_PCT
        )
        many_failures = insert_failed >= CONNTRACK_INSERT_FAILED_NOISE
        if table_full:
            escalate("crit")
            problems.append(
                f"conntrack переполнен: заполнен на {usage}%, за последний "
                f"интервал не удалось создать {_plural_records(insert_failed)}"
                " — ядро отбрасывает пакеты новых соединений"
            )
        elif many_failures:
            # Таблица не забита, но отказов много — причина не в размере
            escalate("warn")
            collision_warn = True
            problems.append(
                f"conntrack: за последний интервал не удалось создать "
                f"{_plural_records(insert_failed)} при заполнении {usage}% — "
                "переполнением это не объясняется: это гонки вставки, "
                "поднимать лимиты бесполезно"
            )
        elif limit_not_applied:
            # Заполнение здесь вторично: лимит занижен против намерения
            # оператора, и знать об этом надо до того, как таблица забьётся.
            escalate("warn")
            not_applied_warn = True
            problems.append(
                f"nf_conntrack_max = {live_max}, хотя в /etc/sysctl.d задано "
                f"{configured_max} — настройка не применилась при загрузке "
                "(модуль nf_conntrack поднялся позже systemd-sysctl). "
                f"Заполнение {usage}%. Поднимать лимит не нужно, нужно "
                "закрепить его: fix-conntrack-persistence.sh"
            )
        elif usage >= warn_pct:
            escalate("warn")
            problems.append(
                f"conntrack заполнен на {usage}% — пора поднимать "
                "nf_conntrack_max"
            )

    nginx = capacity.get("nginx") or {}
    recent = nginx.get("recent_errors") or []
    if recent:
        escalate("crit")
        problems.append("nginx: " + "; ".join(recent))
    worker_connections = nginx.get("worker_connections")
    if worker_connections is not None:
        details.append(f"nginx worker_connections: {worker_connections}")
        if worker_connections <= 1024:
            escalate("warn")
            problems.append(
                f"nginx worker_connections={worker_connections} — значение по "
                "умолчанию, для VPN-ноды мало"
            )

    # Фактические лимиты живых worker-процессов: unit может декларировать
    # большой hard, а процессы, поднятые до его применения, работают со
    # старым soft — расхождение и есть диагноз
    configured_nofile = nginx.get("worker_rlimit_nofile")
    if configured_nofile:
        details.append(f"nginx worker_rlimit_nofile: {configured_nofile}")
    low_workers = []
    tight_workers = []
    for worker in nginx.get("workers") or []:
        # Лимит master'а не важен: worker_rlimit_nofile поднимает его только
        # worker-процессам, а клиентские соединения обслуживают именно они.
        # Роль присылает агент v0.4.1+; без неё судим по всем процессам,
        # как раньше.
        if worker.get("role") == "master":
            continue
        soft = worker.get("nofile_soft")
        hard = worker.get("nofile_hard")
        fd = worker.get("fd")
        if soft and hard and soft < hard and soft <= 1024:
            low_workers.append(f"{worker.get('pid')} (soft={soft}, hard={hard})")
        if soft and soft > 0 and fd and fd >= soft * warn_pct / 100:
            tight_workers.append(f"{worker.get('pid')} ({fd}/{soft})")
    if low_workers:
        escalate("warn")
        # Один и тот же симптом — два разных диагноза. Совет «перезапустить»
        # бесполезен, если директива вообще не задана: новые процессы
        # поднимутся с тем же системным дефолтом.
        if configured_nofile and configured_nofile > 1024:
            problems.append(
                "nginx-worker'ы не подхватили заданный лимит дескрипторов "
                f"({configured_nofile}): "
                + ", ".join(low_workers[:5])
                + " — после graceful reload старые процессы сохраняют прежний "
                "soft-лимит, нужен перезапуск nginx"
            )
        else:
            problems.append(
                "nginx-worker'ы работают с системным лимитом дескрипторов: "
                + ", ".join(low_workers[:5])
                + " — директива worker_rlimit_nofile не задана, перезапуск "
                "сам по себе не поможет"
            )
    if tight_workers:
        escalate("warn")
        problems.append(
            "nginx-worker'ы близки к своему лимиту дескрипторов: "
            + ", ".join(tight_workers[:5])
        )

    xray = capacity.get("xray") or {}
    xray_fd = xray.get("fd")
    xray_soft = xray.get("nofile_soft")
    if xray_fd and xray_soft and xray_soft > 0:
        details.append(f"xray дескрипторы: {xray_fd} / {xray_soft}")
        if xray_fd >= xray_soft * warn_pct / 100:
            escalate("warn")
            problems.append(
                f"xray занял {xray_fd} дескрипторов из {xray_soft}"
            )

    tcp = capacity.get("tcp") or {}
    overflows = tcp.get("listen_overflows_delta") or 0
    if overflows > 0:
        escalate("warn")
        problems.append(
            f"очередь принятия соединений переполнялась {overflows} раз за "
            "последний интервал"
        )
    if (tcp.get("abort_on_memory_delta") or 0) > 0:
        escalate("crit")
        problems.append("соединения обрываются из-за нехватки памяти")

    system = capacity.get("system") or {}
    if (system.get("oom_kills_delta") or 0) > 0:
        escalate("crit")
        problems.append("сработал OOM-killer")

    return {
        "level": level,
        "problems": problems,
        "details": details,
        # Единственная проблема — гонки вставки: алерту нужен чек-лист
        # NOTRACK/DNS-кэша, а не совет поднимать лимиты.
        "collision_only": (
            level == "warn" and collision_warn and len(problems) == 1
        ),
        # Лимит не доехал при загрузке: заголовок «пора поднять лимиты» здесь
        # врёт — поднимать нечего, нужно закрепить уже заданное значение.
        "not_applied": (
            level == "warn" and not_applied_warn and len(problems) == 1
        ),
    }


# --- диагностические пробы (адреса и имена отдельно) -------------------------

# Служебные строки замеров создаются с этим префиксом в имени: они не ходят
# по расписанию (interval_minutes=None) и не шлют собственных алертов —
# вердикт выносит классификатор по совокупности проб, а не каждая проба.
DIAG_CHECK_PREFIX = "DIAG"


def ensure_probe_check(db_session, target_ip: str, sni: str) -> CensorCheck:
    """Строка замера для конкретной пары «адрес + имя» (найти или создать).

    Ищем именно по паре: одна и та же пара переиспользуется между
    диагностиками, а разные пары — это разные проверки, потому что вся суть
    схемы в сравнении «тот же адрес, другое имя» и наоборот.
    """
    check = (
        db_session.query(CensorCheck)
        .filter(CensorCheck.target_ip == target_ip, CensorCheck.sni == sni)
        .order_by(CensorCheck.is_enabled.desc(), CensorCheck.id)
        .first()
    )
    if check is not None:
        return check
    check = CensorCheck(
        name=f"{DIAG_CHECK_PREFIX} {target_ip} / {sni}"[:160],
        target_ip=target_ip,
        sni=sni,
        port=443,
        interval_minutes=None,
        is_enabled=True,
        light_mode=True,
        alerts_enabled=False,
    )
    db_session.add(check)
    db_session.flush()
    return check


def probe_key(target_ip: str, sni: str) -> str:
    """Ключ пробы в JSONB: кортежи в JSON не сохранить."""
    return f"{target_ip}|{sni}"


def start_diagnosis_probes(
    db_session, pairs: list, probe_ids: list = None, reason: str = "",
    light: bool = True,
) -> dict:
    """Запускает пробы по парам (адрес, имя) ОДНИМ набором зондов.

    Общий набор принципиален: если пробы пойдут по разным зондам, разницу
    между ними нельзя будет приписать проверяемому параметру — она может
    объясняться разным составом зондов.

    light=True — лёгкий набор (10 зондов у семи операторов, отвечают ~6):
    годится для адресов, где вердикт дублируется контролем. Пробы ИМЁН и
    кандидатов идут полным набором (light=False, 31 зонд): имя общее для
    нескольких нод, решение по нему дорогое, а на шести зондах один зонд
    переворачивает вердикт (2026-09-02: .128 на 3/6 «забанен», .143 на
    4/6 «жив»).

    Возвращает {"runs": {ключ: run_id}, "errors": [...]}.
    """
    runs: dict[str, int] = {}
    errors: list[str] = []
    if not pairs:
        return {"runs": runs, "errors": ["Нечего проверять"]}

    if probe_ids is None:
        probe_ids = ripe_atlas.resolve_probe_ids(geo=False, light=light)

    for target_ip, sni in pairs:
        check = ensure_probe_check(db_session, target_ip, sni)
        api_key = ripe_atlas.resolve_api_key(
            db_session, check, django_settings.RIPE_ATLAS_API_KEY
        )
        if not api_key:
            errors.append(f"{target_ip} / {sni}: нет API-ключа RIPE Atlas")
            continue
        run = ripe_atlas.start_run(
            db_session,
            check,
            api_key,
            ripe_atlas.resolve_public_flag(db_session, check),
            bool(check.geo_mode),
            light,
            probe_ids,
        )
        if run.status == ripe_atlas.RUN_STATUS_ERROR:
            errors.append(
                f"{target_ip} / {sni}: замер не запустился "
                f"({run.error_message})"
            )
        runs[probe_key(target_ip, sni)] = run.id
        log.info(
            "infra: diagnosis probe ip=%s sni=%s run=%s reason=%s",
            target_ip, sni, run.id, reason,
        )
    return {"runs": runs, "errors": errors}


def probe_from_run(run) -> dict | None:
    """Прогон -> проба для классификатора; None пока прогон не завершён."""
    if run is None or run.status != ripe_atlas.RUN_STATUS_COMPLETE:
        return None
    stages: dict[str, int] = {}
    for item in run.results or []:
        stage = item.get("stage") or "unknown"
        stages[stage] = stages.get(stage, 0) + 1
    return {
        "ok_probes": run.ok_probes or 0,
        "total_probes": run.total_probes or 0,
        "stages": stages,
    }


def collect_probes(db_session, runs_map: dict) -> tuple:
    """{ключ: run_id} -> ({ключ: проба}, есть_ли_незавершённые)."""
    run_ids = [int(v) for v in (runs_map or {}).values()]
    if not run_ids:
        return {}, False
    rows = (
        db_session.query(CensorCheckRun)
        .filter(CensorCheckRun.id.in_(run_ids))
        .all()
    )
    by_id = {row.id: row for row in rows}
    probes: dict[str, dict] = {}
    pending = False
    for key, run_id in (runs_map or {}).items():
        run = by_id.get(int(run_id))
        if run is not None and run.status == ripe_atlas.RUN_STATUS_PENDING:
            pending = True
            continue
        probe = probe_from_run(run)
        if probe is not None:
            probes[key] = probe
    return probes, pending


def max_probe_names(db_session) -> int:
    """Сколько имён проверяем за одну аномалию (настройка, не константа)."""
    try:
        value = int(get_settings(db_session)["infra_tspu_max_names"])
    except (KeyError, TypeError, ValueError):
        return TSPU_MAX_NAMES
    return max(1, min(MAX_DOMAIN_SNIS * 4, value))


def server_probe_targets(db_session, server) -> tuple:
    """Что проверять у сервера: (адреса, имена).

    Адреса — активные публичные IPv4 на интерфейсе: приватные (docker/warp)
    точками входа не являются, а не поднятый на интерфейсе адрес дал бы
    такой же таймаут, как заблокированный, и был бы ошибочно объявлен
    забаненным.

    Имена — то, что клиенты реально шлют в рукопожатии: client_snis связки,
    а где они не заданы — сам домен (старая схема, где домен и был
    маскировочным именем). Имена дедуплицируются по всему серверу: haproxy
    разводит протоколы по SNI на общем адресе, поэтому один и тот же набор
    обычно стоит на всех доменах ноды — это одна проба на имя, а не по одной
    на каждый домен. Домен A-записи, которого нет в списке имён, не
    проверяется вовсе: в эфир он не уходит, фильтровать ТСПУ его нечему.

    Возвращает (адреса, имена, срезанные_имена). Срезанные возвращаются
    явно: молчаливое обрезание читалось бы как «проверено всё», хотя
    проверена только часть.
    """
    ips = []
    for row in (
        db_session.query(InfraServerIp)
        .filter(
            InfraServerIp.server_id == server.id,
            InfraServerIp.on_interface.is_(True),
            InfraServerIp.blocked_at.is_(None),
        )
        .order_by(InfraServerIp.ip)
        .all()
    ):
        try:
            address = ipaddress.ip_address(row.ip)
        except ValueError:
            continue
        if address.version == 4 and address.is_global:
            ips.append(row.ip)

    # Обход по доменам «в ширину», а не по одному домену целиком: срез
    # забирает первые имена каждого домена по кругу, поэтому лимит не
    # съедается одним доменом
    per_domain = [
        domain_client_snis(row)
        for row in db_session.query(InfraServerDomain)
        .filter(InfraServerDomain.server_id == server.id)
        .order_by(InfraServerDomain.domain)
        .all()
    ]
    # Список сервера (вкладка «SNI») — единственный источник, если задан:
    # имена относятся к ноде, а не к отдельному домену
    ordered: list[str] = list(server_client_snis(server))
    for index in range(
        0 if ordered else max((len(items) for items in per_domain), default=0)
    ):
        for items in per_domain:
            if index < len(items) and items[index] not in ordered:
                ordered.append(items[index])
    # Имена, которые уже наблюдались рабочими, идут первыми. Проба —
    # обычный ClientHello, а инбаунд Reality на него не отвечает (пересылает
    # в dest, и если там nginx с proxy_protocol — соединение рвётся). Такое
    # имя не пройдёт НИКОГДА, сколько кредитов в него ни вложи, и вердикта
    # по нему всё равно не будет: лимит должен тратиться на имена, которые
    # способны дать ответ.
    known_good = names_ever_seen_passing(db_session, ordered)
    ordered.sort(key=lambda name: name not in known_good)
    limit = max_probe_names(db_session)
    return ips[:TSPU_MAX_TARGETS], ordered[:limit], ordered[limit:]


def server_domain_targets(db_session, server_id) -> list[dict]:
    """Домены сервера с их именами: [{"domain", "snis": [...]}, ...].

    "snis" непустой всегда: у домена старой схемы это он сам. Потребители
    (классификатор, решение о смене DNS) отличают схемы по тому, входит ли
    домен в собственный список имён.
    """
    server = get_server(db_session, server_id)
    return [
        {"domain": row.domain, "snis": _domain_wire_names(server, row)}
        for row in db_session.query(InfraServerDomain)
        .filter(InfraServerDomain.server_id == server_id)
        .order_by(InfraServerDomain.domain)
        .all()
    ]


def names_ever_seen_passing(db_session, names: list) -> set:
    """Какие из имён хоть раз наблюдались прошедшими на любом адресе.

    Имя, которое НИ РАЗУ не проходило, отказывает не обязательно из-за ТСПУ:
    на ноде без default_backend неизвестный SNI даёт молчаливый обрыв после
    ClientHello, неотличимый от фильтрации, — а неизвестным его делает
    опечатка в карточке или имя, которого нода просто не обслуживает. Без
    этой проверки одна опечатка объявляла бы имя забаненным, а при
    выгоревшем контроле — ещё и живой адрес мёртвым, и запускала бы замену.
    """
    from engine import infra_diagnosis

    wanted = [str(name).strip().lower() for name in (names or []) if name]
    if not wanted:
        return set()
    rows = (
        db_session.query(CensorCheck.sni)
        .join(CensorCheckRun, CensorCheckRun.check_id == CensorCheck.id)
        .filter(CensorCheck.sni.in_(wanted))
        .filter(CensorCheckRun.status == ripe_atlas.RUN_STATUS_COMPLETE)
        .filter(CensorCheckRun.total_probes > 0)
        .filter(
            CensorCheckRun.ok_probes * 100
            > CensorCheckRun.total_probes * infra_diagnosis.PROBE_FAIL_MAX_PCT
        )
        .distinct()
        .all()
    )
    return {str(row[0]).strip().lower() for row in rows}


def start_ip_diagnosis(db_session, server, reason: str = "") -> dict:
    """Фаза 1: проверка АДРЕСОВ сервера контрольным посторонним именем.

    Контрольное имя заведомо вне фильтра, поэтому его отказ на адресе
    указывает на сам адрес. Пробы имён запускаются потом и только на
    адресах, признанных живыми: на мёртвом адресе падает всё, и вывод об
    имени был бы ложным.
    """
    cfg = get_settings(db_session)
    control_names = active_control_names(cfg)
    if not control_names:
        return {"runs": {}, "errors": ["Не задано контрольное имя"],
                "control_name": ""}
    control_name = control_names[0]

    ips, _names, _dropped = server_probe_targets(db_session, server)
    if not ips:
        return {"runs": {}, "errors": ["Нет активных публичных IPv4"],
                "control_name": control_name}

    started = start_diagnosis_probes(
        db_session,
        [(ip, control_name) for ip in ips],
        reason=reason or "IP_DIAGNOSIS",
    )
    started["control_name"] = control_name
    return started


def start_manual_diagnosis(db_session, server, actor: str = "") -> dict:
    """Полная диагностика по кнопке в карточке.

    Создаёт аномалию, чтобы дальше сработал обычный конвейер: фаза 1
    (адреса), фаза 2 (имена на живом адресе), классификация и алерт с
    журналом проверок. Ручной запуск отличается от автоматического только
    поводом — вердикт и действия те же.
    """
    running = (
        db_session.query(InfraAnomaly)
        .filter(
            InfraAnomaly.server_id == server.id,
            InfraAnomaly.status.in_(("checking", "checking_sni")),
        )
        .first()
    )
    if running is not None:
        # Параллельные диагностики одного сервера только жгли бы кредиты
        return {
            "runs": {}, "errors": ["Диагностика уже идёт"],
            "anomaly_id": running.id, "control_name": "",
        }

    result = start_ip_diagnosis(db_session, server, reason=f"manual:{actor}")
    if not result["runs"]:
        return dict(result, anomaly_id=None)

    anomaly = InfraAnomaly(
        server_id=server.id,
        kind="manual_check",
        status="checking",
        details={
            "control_name": result.get("control_name") or "",
            "ip_runs": result["runs"],
            "diagnosis_errors": result.get("errors") or [],
            "started_by": actor or "manual",
        },
        censor_run_ids=list(result["runs"].values()),
    )
    db_session.add(anomaly)
    db_session.flush()
    log.info(
        "infra: manual diagnosis server=%s anomaly=%s probes=%s",
        server.node_name, anomaly.id, list(result["runs"].keys()),
    )
    return dict(result, anomaly_id=anomaly.id)


def corroborating_names(
    control_names: list, client_names: list, known_good: set
) -> list[str]:
    """Чем перепроверять подозрительный адрес, кроме контрольного имени №1.

    Первый свидетель — СЛЕДУЮЩЕЕ контрольное имя. Оно чужое, а значит на
    ноде не попадает ни под один ACL haproxy и уходит в default_backend, то
    есть отвечает всегда, пока адрес жив. Клиентское имя такой гарантии не
    даёт: имя, ведущее на инбаунд Reality, не отвечает на обычный
    ClientHello зонда в принципе (Reality пересылает соединение в dest, а
    там nginx с proxy_protocol рвёт его) — по такому имени вердикт не
    вынести никогда, и свидетелем оно быть не может.

    Второй свидетель — клиентское имя, которое уже наблюдалось рабочим.
    Оно ценнее контрольного тем, что не является чужим доменом на нашем
    адресе: сочетание «чужое имя + наш адрес» ТСПУ фильтрует само по себе,
    и оба контрольных имени могут упасть по одной и той же причине.

    Порядок детерминирован: один и тот же инцидент, разобранный дважды,
    даёт одинаковые пробы.
    """
    witnesses: list[str] = []
    for name in (control_names or [])[1:2]:
        if name:
            witnesses.append(name)
    for name in client_names or []:
        if name in (known_good or set()) and name not in witnesses:
            witnesses.append(name)
            break
    return witnesses


def start_sni_diagnosis(
    db_session, server, live_ips, suspect_ips=None, reason: str = ""
) -> dict:
    """Фаза 2: проверка ИМЁН, на живых и на подозрительных адресах.

    На ЖИВОМ адресе проверяются все имена: адрес доказанно жив, значит отказ
    имени на нём говорит о фильтрации имени. Правило ТСПУ бывает и на пару
    «адрес + имя», поэтому имена идут сразу на нескольких живых адресах (до
    SNI_PROBE_MAX_IPS): имя, упавшее на одном и прошедшее на другом, — бан
    пары, а не имени.

    На ПОДОЗРИТЕЛЬНОМ адресе (контрольное имя не прошло) проверяется одно
    имя — этого достаточно, чтобы ответить на единственный вопрос «ходит ли
    через адрес хоть что-нибудь». Без этой пробы вердикт «адрес забанен»
    держался бы на одном свидетеле — контрольном имени, — а оно бывает и
    выгоревшим, и забаненным в паре именно с этим адресом. Ровно так живая
    нода уезжала на резервный адрес (эстонский инцидент 07.09.2026).
    Стоимость — один полный прогон на подозрительный адрес.

    Полный набор зондов: решения здесь дорогие.
    """
    if isinstance(live_ips, str):
        live_ips = [live_ips]
    live_ips = [ip for ip in (live_ips or []) if ip][:SNI_PROBE_MAX_IPS]
    suspect_ips = [
        ip for ip in (suspect_ips or []) if ip and ip not in live_ips
    ][:TSPU_MAX_TARGETS]
    _ips, names, dropped = server_probe_targets(db_session, server)
    if not names:
        return {"runs": {}, "errors": ["У сервера нет привязанных доменов"]}
    if not live_ips and not suspect_ips:
        return {"runs": {}, "errors": ["Нет адреса для проверки имён"]}
    pairs = [(ip, name) for ip in live_ips for name in names]
    witnesses: list[str] = []
    if suspect_ips:
        cfg = get_settings(db_session)
        witnesses = corroborating_names(
            active_control_names(cfg),
            names,
            names_ever_seen_passing(db_session, names),
        )
        pairs += [
            (ip, witness) for ip in suspect_ips for witness in witnesses
        ]
    started = start_diagnosis_probes(
        db_session,
        pairs,
        reason=reason or "SNI_DIAGNOSIS",
        light=False,
    )
    if dropped:
        # Молчаливое обрезание читалось бы как «проверено всё»
        started["errors"] = list(started.get("errors") or []) + [
            f"Имён больше {max_probe_names(db_session)} — не проверены: "
            + ", ".join(dropped)
            + " (защита кредитов RIPE Atlas); они останутся неизвестными и "
            "будут проверены на кандидате перед публикацией"
        ]
    started["dropped_names"] = dropped
    started["witness_names"] = witnesses
    if suspect_ips and not witnesses:
        started["errors"] = list(started.get("errors") or []) + [
            "Нечем перепроверить подозрительные адреса: в "
            "infra_control_names только одно имя, а клиентские имена ни разу "
            "не наблюдались рабочими. Вердикт по адресу вынесен не будет"
        ]
    return started


def recent_name_results(
    db_session, names: list, since: datetime, exclude_anomaly_id=None,
) -> dict:
    """Что известно об именах по диагностикам соседних серверов волны.

    Имя общее для нескольких нод, а диагностика идёт на каждой отдельно.
    Возвращает {(ip, sni): {"result": "pass"|"fail", "server": имя сервера,
    "at": "HH:MM"}} по пробам имён на ЖИВЫХ адресах из evidence аномалий,
    закрытых не раньше since. Для одной пары остаётся самый поздний
    результат.
    """
    wanted = set(names or [])
    if not wanted:
        return {}
    rows = (
        db_session.query(InfraAnomaly, InfraServer)
        .join(InfraServer, InfraServer.id == InfraAnomaly.server_id)
        .filter(InfraAnomaly.resolved_at.isnot(None))
        .filter(InfraAnomaly.resolved_at >= since)
        .order_by(InfraAnomaly.resolved_at, InfraAnomaly.id)
        .all()
    )
    results: dict = {}
    for anomaly, server in rows:
        if exclude_anomaly_id is not None and anomaly.id == exclude_anomaly_id:
            continue
        for item in (anomaly.details or {}).get("evidence") or []:
            if item.get("step") != "sni_probe":
                continue
            sni = item.get("sni")
            ip = item.get("ip")
            if sni not in wanted or not ip:
                continue
            if item.get("result") == "pass":
                result = "pass"
            elif item.get("result") in ("fail", "pair"):
                result = "fail"
            else:
                continue
            results[(ip, sni)] = {
                "result": result,
                "server": server_title(server),
                "at": anomaly.resolved_at.strftime("%H:%M"),
            }
    return results


# --- принудительный запуск «Замеров ТСПУ» -----------------------------------


def force_tspu_check(db_session, server: InfraServer, reason: str) -> dict:
    """Запускает существующий механизм замеров для точек входа сервера.

    Возвращает {"run_ids": [...], "errors": [...]}. Ничего не измеряет сам —
    только находит/создаёт строки CensorCheck по активным IP сервера и
    вызывает ripe_atlas.start_run (результаты доберёт censor-воркер).
    """
    ip_rows = (
        db_session.query(InfraServerIp)
        .filter(
            InfraServerIp.server_id == server.id,
            InfraServerIp.on_interface.is_(True),
            InfraServerIp.blocked_at.is_(None),
        )
        .all()
    )
    # Только публичные v4-адреса: замер приватного (docker0/warp) — впустую
    # сожжённые кредиты Atlas по заведомо недостижимой цели
    target_ips = []
    for row in ip_rows:
        try:
            address = ipaddress.ip_address(row.ip)
        except ValueError:
            continue
        if address.version == 4 and address.is_global:
            target_ips.append(row.ip)
    # Для авто-строки замера нужен SNI, а не домен: в ClientHello уходит
    # клиентское имя связки, и их у связки может быть несколько
    domains = server_wire_names(
        server,
        db_session.query(InfraServerDomain)
        .filter(InfraServerDomain.server_id == server.id)
        .order_by(InfraServerDomain.domain)
        .all(),
    )

    run_ids: list[int] = []
    errors: list[str] = []
    now = utcnow()
    fresh_cutoff = now - timedelta(minutes=TSPU_FRESH_RUN_MINUTES)

    if not target_ips:
        return {"run_ids": [], "errors": ["Нет активных IPv4-адресов на интерфейсе"]}

    # Если есть свежие DNS-слепки — меряем только адреса, реально стоящие
    # в A-записях: запасные IP на интерфейсе клиентов не обслуживают, их
    # замер жёг бы кредиты и мог плодить бессмысленные замены
    snapshot_ips = fresh_dns_snapshot_ips(db_session, server)
    if snapshot_ips is not None:
        in_dns = [ip for ip in target_ips if ip in snapshot_ips]
        if in_dns:
            target_ips = in_dns
        else:
            return {
                "run_ids": [],
                "errors": [
                    "Активные IP сервера не находятся в A-записях "
                    "привязанных доменов — точка входа не в DNS, замер "
                    "не нужен"
                ],
            }
    if len(target_ips) > TSPU_MAX_TARGETS:
        errors.append(
            f"IP больше {TSPU_MAX_TARGETS} — проверяются только первые "
            f"{TSPU_MAX_TARGETS} (защита кредитов RIPE Atlas)"
        )
        target_ips = target_ips[:TSPU_MAX_TARGETS]

    for target_ip in target_ips:
        checks = (
            db_session.query(CensorCheck)
            .filter(CensorCheck.target_ip == target_ip)
            .order_by(CensorCheck.is_enabled.desc(), CensorCheck.id)
            .all()
        )
        # Не больше двух прогонов на IP (кредиты Atlas); включённые проверки
        # предпочитаем выключенным, но при отсутствии включённых замер всё
        # равно нужен — подтверждение бана важнее флага is_enabled
        checks = checks[:2]
        if not checks:
            if not domains:
                errors.append(
                    f"{target_ip}: нет строки замера и нет привязанных доменов (SNI)"
                )
                continue
            check = CensorCheck(
                name=f"AUTO {server_title(server)} {target_ip}",
                target_ip=target_ip,
                sni=domains[0],
                port=443,
                interval_minutes=None,
                is_enabled=True,
                light_mode=True,
                alerts_enabled=True,
            )
            db_session.add(check)
            db_session.flush()
            checks = [check]

        for check in checks:
            pending_run = (
                db_session.query(CensorCheckRun)
                .filter(
                    CensorCheckRun.check_id == check.id,
                    CensorCheckRun.status == "pending",
                )
                .order_by(CensorCheckRun.id.desc())
                .first()
            )
            if pending_run is not None:
                run_ids.append(pending_run.id)
                continue
            if check.last_started_at and check.last_started_at >= fresh_cutoff:
                # Переиспользуем только действительно СВЕЖИЙ рабочий прогон
                # (по его created_at, не по last_started_at проверки): иначе
                # свежий error-прогон подсовывал бы аномалии старый complete
                # недельной давности как «доказательство»
                fresh_run = (
                    db_session.query(CensorCheckRun)
                    .filter(
                        CensorCheckRun.check_id == check.id,
                        CensorCheckRun.status.in_(("pending", "complete")),
                        CensorCheckRun.created_at >= fresh_cutoff,
                    )
                    .order_by(CensorCheckRun.id.desc())
                    .first()
                )
                if fresh_run is not None:
                    run_ids.append(fresh_run.id)
                    continue

            api_key = ripe_atlas.resolve_api_key(
                db_session, check, django_settings.RIPE_ATLAS_API_KEY
            )
            if not api_key:
                errors.append(f"{target_ip}: нет API-ключа RIPE Atlas")
                continue
            is_public = ripe_atlas.resolve_public_flag(db_session, check)
            run = ripe_atlas.start_run(
                db_session,
                check,
                api_key,
                is_public,
                bool(check.geo_mode),
                bool(check.light_mode),
            )
            if run.status == "error":
                errors.append(
                    f"{target_ip}: замер не запустился ({run.error_message})"
                )
            run_ids.append(run.id)
            log.info(
                "infra: forced TSPU check server=%s ip=%s check=%s run=%s reason=%s",
                server.node_name,
                target_ip,
                check.id,
                run.id,
                reason,
            )

    return {"run_ids": run_ids, "errors": errors}


# --- агрегация телеметрии и retention ---------------------------------------


def _floor_dt(value: datetime, seconds: int) -> datetime:
    # Все даты в БД — naive UTC; .timestamp() наивной даты трактует её как
    # локальную, поэтому явно проставляем UTC перед округлением
    epoch = int(value.replace(tzinfo=timezone.utc).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc).replace(
        tzinfo=None
    )


def _upsert_agg(db_session, row_values: dict) -> None:
    existing = (
        db_session.query(InfraTelemetryAgg)
        .filter(
            InfraTelemetryAgg.server_id == row_values["server_id"],
            InfraTelemetryAgg.bucket_seconds == row_values["bucket_seconds"],
            InfraTelemetryAgg.bucket_start == row_values["bucket_start"],
        )
        .one_or_none()
    )
    if existing is None:
        db_session.add(InfraTelemetryAgg(**row_values))
    else:
        for key, value in row_values.items():
            setattr(existing, key, value)


def _upsert_agg_batch(db_session, rows):
    if not rows:
        return
    if db_session.get_bind().dialect.name != "postgresql":
        for row in rows:
            _upsert_agg(db_session, row)
        db_session.flush()
        return
    keys = ("server_id", "bucket_seconds", "bucket_start")
    # Stay well below PostgreSQL parameter limits, including large backfills.
    for offset in range(0, len(rows), 500):
        batch = rows[offset:offset+500]
        statement = pg_insert(InfraTelemetryAgg).values(batch)
        statement = statement.on_conflict_do_update(
            index_elements=list(keys),
            set_={key: getattr(statement.excluded, key) for key in batch[0] if key not in keys},
        )
        db_session.execute(statement)


def aggregate_telemetry(db_session, now: datetime | None = None) -> int:
    """Сворачивает сырые сэмплы в минутные бакеты, минутные — в 15-минутные.

    Идемпотентно: бакет пересчитывается из всех своих сэмплов и upsert'ится.
    Возвращает число обновлённых бакетов (для логов).
    """
    now = now or utcnow()
    updated = 0
    server_ids = [row[0] for row in db_session.query(InfraServer.id).all()]
    for server_id in server_ids:
        updated += _aggregate_minutes_for_server(db_session, server_id, now)
        updated += _aggregate_quarters_for_server(db_session, server_id, now)
    return updated


def _aggregate_minutes_for_server(db_session, server_id: int, now: datetime) -> int:
    current_minute = _floor_dt(now, 60)
    last_bucket = (
        db_session.query(func.max(InfraTelemetryAgg.bucket_start))
        .filter(
            InfraTelemetryAgg.server_id == server_id,
            InfraTelemetryAgg.bucket_seconds == 60,
        )
        .scalar()
    )
    if last_bucket is not None:
        # Последний бакет пересчитываем: в момент прошлой агрегации он мог
        # быть неполным
        since = last_bucket
    else:
        since = now - timedelta(hours=RAW_RETENTION_HOURS)

    samples = (
        db_session.query(InfraTelemetry)
        .filter(
            InfraTelemetry.server_id == server_id,
            InfraTelemetry.ts >= since,
            InfraTelemetry.ts < current_minute,
        )
        .order_by(InfraTelemetry.ts)
        .all()
    )
    if not samples:
        return 0

    buckets: dict[datetime, list[InfraTelemetry]] = {}
    for sample in samples:
        buckets.setdefault(_floor_dt(sample.ts, 60), []).append(sample)

    batch_rows = []
    for bucket_start, rows in buckets.items():
        rx_values = [r.rx_bps for r in rows if r.rx_bps is not None]
        tx_values = [r.tx_bps for r in rows if r.tx_bps is not None]
        tcp_values = [
            r.tcp_connections for r in rows if r.tcp_connections is not None
        ]
        ct_values = [r.conntrack for r in rows if r.conntrack is not None]
        load_values = [
            float(r.cpu_load) for r in rows if r.cpu_load is not None
        ]
        rx_avg = int(sum(rx_values) / len(rx_values)) if rx_values else None
        tx_avg = int(sum(tx_values) / len(tx_values)) if tx_values else None
        batch_rows.append(
            {
                "server_id": server_id,
                "bucket_seconds": 60,
                "bucket_start": bucket_start,
                "rx_bps_avg": rx_avg,
                "rx_bps_max": max(rx_values) if rx_values else None,
                "tx_bps_avg": tx_avg,
                "tx_bps_max": max(tx_values) if tx_values else None,
                "tcp_avg": (
                    int(sum(tcp_values) / len(tcp_values)) if tcp_values else None
                ),
                "tcp_max": max(tcp_values) if tcp_values else None,
                "conntrack_avg": (
                    int(sum(ct_values) / len(ct_values)) if ct_values else None
                ),
                "cpu_load_avg": (
                    round(sum(load_values) / len(load_values), 2)
                    if load_values
                    else None
                ),
                # Объём за минуту из среднего bps: avg_bps * 60 / 8
                "rx_bytes": int(rx_avg * 60 / 8) if rx_avg is not None else None,
                "tx_bytes": int(tx_avg * 60 / 8) if tx_avg is not None else None,
                "sample_count": len(rows),
            },
        )
    _upsert_agg_batch(db_session, batch_rows)
    return len(batch_rows)


def _aggregate_quarters_for_server(db_session, server_id: int, now: datetime) -> int:
    current_quarter = _floor_dt(now, 900)
    last_bucket = (
        db_session.query(func.max(InfraTelemetryAgg.bucket_start))
        .filter(
            InfraTelemetryAgg.server_id == server_id,
            InfraTelemetryAgg.bucket_seconds == 900,
        )
        .scalar()
    )
    since = last_bucket or (now - timedelta(hours=RAW_RETENTION_HOURS))

    minute_rows = (
        db_session.query(InfraTelemetryAgg)
        .filter(
            InfraTelemetryAgg.server_id == server_id,
            InfraTelemetryAgg.bucket_seconds == 60,
            InfraTelemetryAgg.bucket_start >= since,
            InfraTelemetryAgg.bucket_start < current_quarter,
        )
        .order_by(InfraTelemetryAgg.bucket_start)
        .all()
    )
    if not minute_rows:
        return 0

    buckets: dict[datetime, list[InfraTelemetryAgg]] = {}
    for row in minute_rows:
        buckets.setdefault(_floor_dt(row.bucket_start, 900), []).append(row)

    batch_rows = []
    for bucket_start, rows in buckets.items():
        def avg(values):
            values = [v for v in values if v is not None]
            return int(sum(values) / len(values)) if values else None

        def peak(values):
            values = [v for v in values if v is not None]
            return max(values) if values else None

        load_values = [
            float(r.cpu_load_avg) for r in rows if r.cpu_load_avg is not None
        ]
        batch_rows.append(
            {
                "server_id": server_id,
                "bucket_seconds": 900,
                "bucket_start": bucket_start,
                "rx_bps_avg": avg([r.rx_bps_avg for r in rows]),
                "rx_bps_max": peak([r.rx_bps_max for r in rows]),
                "tx_bps_avg": avg([r.tx_bps_avg for r in rows]),
                "tx_bps_max": peak([r.tx_bps_max for r in rows]),
                "tcp_avg": avg([r.tcp_avg for r in rows]),
                "tcp_max": peak([r.tcp_max for r in rows]),
                "conntrack_avg": avg([r.conntrack_avg for r in rows]),
                "cpu_load_avg": (
                    round(sum(load_values) / len(load_values), 2)
                    if load_values
                    else None
                ),
                "rx_bytes": sum(r.rx_bytes or 0 for r in rows) or None,
                "tx_bytes": sum(r.tx_bytes or 0 for r in rows) or None,
                "sample_count": sum(r.sample_count or 0 for r in rows),
            },
        )
    _upsert_agg_batch(db_session, batch_rows)
    return len(batch_rows)


def prune_telemetry(db_session, now: datetime | None = None) -> None:
    now = now or utcnow()
    db_session.execute(
        sa_delete(InfraTelemetry).where(
            InfraTelemetry.ts < now - timedelta(hours=RAW_RETENTION_HOURS)
        )
    )
    db_session.execute(
        sa_delete(InfraTelemetryAgg).where(
            InfraTelemetryAgg.bucket_seconds == 60,
            InfraTelemetryAgg.bucket_start
            < now - timedelta(days=AGG60_RETENTION_DAYS),
        )
    )
    db_session.execute(
        sa_delete(InfraTelemetryAgg).where(
            InfraTelemetryAgg.bucket_seconds == 900,
            InfraTelemetryAgg.bucket_start
            < now - timedelta(days=AGG900_RETENTION_DAYS),
        )
    )


# --- baseline и anomaly detection -------------------------------------------


def compute_baseline(
    db_session, server_id: int, now: datetime, cfg: dict
) -> dict | None:
    """Ожидаемая нагрузка «в это время суток»: медиана по последним дням.

    Для каждого из последних baseline_days дней берём минутные агрегаты в
    окне [now - W, now + W] того же времени суток и усредняем; медиана по
    дням с данными. None — истории недостаточно (warm-up или новые серверы).
    """
    window = timedelta(minutes=BASELINE_WINDOW_MINUTES)
    day_traffic: list[float] = []
    day_conns: list[float] = []
    for day_offset in range(1, cfg["infra_anomaly_baseline_days"] + 1):
        center = now - timedelta(days=day_offset)
        rows = (
            db_session.query(InfraTelemetryAgg)
            .filter(
                InfraTelemetryAgg.server_id == server_id,
                InfraTelemetryAgg.bucket_seconds == 60,
                InfraTelemetryAgg.bucket_start >= center - window,
                InfraTelemetryAgg.bucket_start <= center + window,
            )
            .all()
        )
        traffic_values = [
            (row.rx_bps_avg or 0) + (row.tx_bps_avg or 0)
            for row in rows
            if row.rx_bps_avg is not None or row.tx_bps_avg is not None
        ]
        conn_values = [row.tcp_avg for row in rows if row.tcp_avg is not None]
        if traffic_values:
            day_traffic.append(sum(traffic_values) / len(traffic_values))
        if conn_values:
            day_conns.append(sum(conn_values) / len(conn_values))

    if (
        len(day_traffic) < cfg["infra_anomaly_min_history_days"]
        or len(day_conns) < cfg["infra_anomaly_min_history_days"]
    ):
        return None
    return {
        "traffic_bps": statistics.median(day_traffic),
        "connections": statistics.median(day_conns),
        "days_traffic": len(day_traffic),
        "days_connections": len(day_conns),
    }


def current_minute_metrics(
    db_session, server_id: int, now: datetime, minutes: int
) -> list[dict]:
    """Пер-минутные средние (traffic_bps, connections) за последние N минут.

    Только минуты, в которых есть сэмплы; телеметрия с пропусками даёт
    меньше точек, и детектор это учитывает.
    """
    since = _floor_dt(now - timedelta(minutes=minutes), 60)
    rows = (
        db_session.query(InfraTelemetry)
        .filter(
            InfraTelemetry.server_id == server_id,
            InfraTelemetry.ts >= since,
        )
        .order_by(InfraTelemetry.ts)
        .all()
    )
    buckets: dict[datetime, list[InfraTelemetry]] = {}
    for row in rows:
        buckets.setdefault(_floor_dt(row.ts, 60), []).append(row)

    result = []
    for bucket_start in sorted(buckets):
        bucket_rows = buckets[bucket_start]
        traffic_values = [
            (r.rx_bps or 0) + (r.tx_bps or 0)
            for r in bucket_rows
            if r.rx_bps is not None or r.tx_bps is not None
        ]
        conn_values = [
            r.tcp_connections
            for r in bucket_rows
            if r.tcp_connections is not None
        ]
        if not traffic_values or not conn_values:
            continue
        result.append(
            {
                "minute": bucket_start,
                "traffic_bps": sum(traffic_values) / len(traffic_values),
                "connections": sum(conn_values) / len(conn_values),
            }
        )
    return result


def evaluate_network_drop(
    minute_metrics: list[dict], baseline: dict, cfg: dict
) -> dict | None:
    """Чистая проверка: все минуты окна ниже порогов по обоим показателям.

    Возвращает {"traffic_ratio", "connection_ratio"} (по последней минуте)
    либо None, если аномалии нет или данных мало.
    """
    duration = cfg["infra_anomaly_duration_minutes"]
    # Требуем данных почти во всех минутах окна: пропуски телеметрии не
    # должны давать ложных срабатываний
    if len(minute_metrics) < max(2, duration - 1):
        return None
    baseline_traffic = baseline["traffic_bps"]
    baseline_conns = baseline["connections"]
    if baseline_traffic <= 0 or baseline_conns <= 0:
        return None
    # Минимальный baseline проверяется по НЕмасштабированному значению
    # (floor_traffic_bps): иначе перекалибровка после дробления DNS могла бы
    # утащить масштабированную норму под порог и молча выключить детекцию
    # реального бана на некрупных серверах
    floor_traffic = baseline.get("floor_traffic_bps", baseline_traffic)
    if floor_traffic < cfg["infra_anomaly_min_baseline_mbps"] * 1_000_000:
        return None

    for metric in minute_metrics:
        traffic_ratio = metric["traffic_bps"] / baseline_traffic
        conn_ratio = metric["connections"] / baseline_conns
        if (
            traffic_ratio >= cfg["infra_anomaly_traffic_ratio"]
            or conn_ratio >= cfg["infra_anomaly_conn_ratio"]
        ):
            return None

    last = minute_metrics[-1]
    return {
        "traffic_ratio": round(last["traffic_bps"] / baseline_traffic, 3),
        "connection_ratio": round(last["connections"] / baseline_conns, 3),
        "current_traffic_bps": int(last["traffic_bps"]),
        "current_connections": int(last["connections"]),
        "baseline_traffic_bps": int(baseline_traffic),
        "baseline_connections": int(baseline_conns),
    }
