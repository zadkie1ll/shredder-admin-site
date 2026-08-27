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
from engine import ripe_atlas

log = logging.getLogger("infra")


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
    "infra_dns_rebalance_window_hours": (
        168, int,
        "Окно перекалибровки baseline после новой A-записи у домена, часов",
        None),
}

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

REPLACEMENT_ACTIVE_STATUSES = ("pending", "installing", "dns_add", "dns_remove")
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
    if cast in (int, float) and value <= 0:
        raise InfraError("Значение должно быть больше нуля")
    if key.endswith("_ratio") and not (0 < value < 1):
        raise InfraError("Отношение должно быть в диапазоне (0, 1)")
    if key.endswith("_pct") and not (1 <= value <= 100):
        raise InfraError("Процент должен быть в диапазоне 1-100")
    if allowed and value not in allowed:
        raise InfraError(
            "Допустимые значения: " + ", ".join(str(v) for v in allowed)
        )
    return str(value)


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
                "anomaly_paused": (
                    server.anomaly_suppressed_until is not None
                    and server.anomaly_suppressed_until > now
                ),
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
        "commands": [_command_payload(row) for row in commands],
        "anomalies": [_anomaly_payload(row) for row in anomalies],
        "replacements": [_replacement_payload(row) for row in replacements],
        "who_connects": who_connects_payload(db_session, server.node_name, now),
        "cloudflare_enabled": _cloudflare_enabled(),
    }


def who_connects_payload(db_session, node_name: str, now: datetime) -> dict:
    """«Кто подключается» по данным ip-guard (ipguard_user_ips) этой ноды."""
    day_ago = now - timedelta(hours=24)
    hour_ago = now - timedelta(hours=1)

    unique_24h = (
        db_session.query(func.count(func.distinct(UserIpObservation.ip)))
        .filter(
            UserIpObservation.node == node_name,
            UserIpObservation.last_seen >= day_ago,
        )
        .scalar()
        or 0
    )
    unique_1h = (
        db_session.query(func.count(func.distinct(UserIpObservation.ip)))
        .filter(
            UserIpObservation.node == node_name,
            UserIpObservation.last_seen >= hour_ago,
        )
        .scalar()
        or 0
    )
    top_rows = (
        db_session.query(
            UserIpObservation.ip,
            func.sum(UserIpObservation.hits).label("hits"),
            func.count(func.distinct(UserIpObservation.username)).label("users"),
        )
        .filter(
            UserIpObservation.node == node_name,
            UserIpObservation.last_seen >= day_ago,
        )
        .group_by(UserIpObservation.ip)
        .order_by(func.sum(UserIpObservation.hits).desc())
        .limit(20)
        .all()
    )
    return {
        "unique_ips_24h": int(unique_24h),
        "unique_ips_1h": int(unique_1h),
        "top_addresses": [
            {"ip": row.ip, "hits": int(row.hits or 0), "users": int(row.users or 0)}
            for row in top_rows
        ],
    }


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


def add_domain(db_session, server_id, domain: str) -> InfraServerDomain:
    server = get_server(db_session, server_id)
    domain = (domain or "").strip().strip(".").lower()
    if not domain or "." not in domain or " " in domain or "/" in domain:
        raise InfraError("Некорректный домен")
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
    row = InfraServerDomain(server_id=server.id, domain=domain)
    db_session.add(row)
    db_session.flush()
    return row


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


def request_replacement(
    db_session, server_id, old_ip: str, created_by: str, anomaly_id=None
) -> InfraIpReplacement:
    """replaceFailedIp: заявка на замену IP (идемпотентная).

    Вызывается воркером после подтверждения ТСПУ или админом вручную.
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
                "message": f"Заявка на замену {old_ip} ({created_by})",
            }
        ],
    )
    db_session.add(replacement)
    db_session.flush()
    return replacement


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
    domains = [
        row.domain
        for row in db_session.query(InfraServerDomain)
        .filter(InfraServerDomain.server_id == server.id)
        .order_by(InfraServerDomain.domain)
        .all()
    ]

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

    updated = 0
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
        _upsert_agg(
            db_session,
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
        updated += 1
    return updated


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

    updated = 0
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
        _upsert_agg(
            db_session,
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
        updated += 1
    return updated


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
