"""Агрегация потребления трафика подписками по нодам Remnawave.

Данные для вкладки «Трафик нод» в админке: помогает находить расшаренные
("утекшие") подписки по аномально высокому потреблению. Все данные получаются
через rwms (read-only RPC GetNodes / GetNodeUsersUsage / GetUserByUuid),
напрямую в панель Remnawave сайт не ходит, ничего не изменяет и не удаляет.
"""

import os
import logging

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from google.protobuf.timestamp_pb2 import Timestamp

import proto.rwmanager_pb2 as proto
from common.rwms_client_sync import RwmsClientSync

# Служебные подписки, не являющиеся реальными пользователями — исключаются из
# выборки (и из totals), чтобы не искажать статистику. Расширяется переменной
# окружения TRAFFIC_STATS_EXCLUDE_USERS (username через запятую).
DEFAULT_EXCLUDED_USERNAMES = frozenset({"SYS-REROUTE"})

GIB = 1024**3

# Панель тяжело переживает параллельные запросы статистики (HTTP 500 на
# длинных периодах), поэтому параллельность намеренно низкая.
NODES_FETCH_CONCURRENCY = 3
DETAILS_FETCH_CONCURRENCY = 8

# Потолок строк в ответе API: защита от запроса "top=0" на десятках тысяч
# пользователей — обогащение карточками стало бы неприемлемо долгим.
MAX_TOP = 500


def excluded_usernames() -> set[str]:
    excluded = set(DEFAULT_EXCLUDED_USERNAMES)
    raw = os.getenv("TRAFFIC_STATS_EXCLUDE_USERS", "")
    excluded.update(name.strip() for name in raw.split(",") if name.strip())
    return excluded


@dataclass
class UserTraffic:
    user_uuid: str
    username: str
    total_bytes: int = 0
    # трафик по нодам: имя ноды -> bytes (для режима "все ноды")
    per_node: dict[str, int] = field(default_factory=dict)
    details: Optional[proto.UserResponse] = None

    def top_node(self) -> tuple[Optional[str], int]:
        if not self.per_node:
            return None, 0
        name = max(self.per_node, key=self.per_node.get)
        return name, self.per_node[name]


def _to_ts(dt: datetime) -> Timestamp:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def list_nodes(rwms_client: RwmsClientSync) -> Optional[list[proto.Node]]:
    """Список нод панели; None при недоступности rwms."""
    response = rwms_client.get_nodes()
    if response is None:
        return None
    return list(response.nodes)


def _fetch_node_usage(
    rwms_client: RwmsClientSync,
    node_uuid: str,
    start: datetime,
    end: datetime,
) -> Optional[list[proto.NodeUserUsage]]:
    response = rwms_client.get_node_users_usage(
        proto.GetNodeUsersUsageRequest(
            node_uuid=node_uuid,
            start=_to_ts(start),
            end=_to_ts(end),
        )
    )
    if response is None:
        return None
    return list(response.items)


def collect_usage(
    rwms_client: RwmsClientSync,
    nodes: list[proto.Node],
    start: datetime,
    end: datetime,
) -> tuple[dict[str, UserTraffic], list[str]]:
    """Собирает трафик пользователей на нодах и агрегирует по пользователю.

    Возвращает (usage по user_uuid, имена нод, по которым статистику получить
    не удалось). Ноды опрашиваются параллельно, но с низкой конкурентностью,
    чтобы не перегружать панель.
    """
    usage: dict[str, UserTraffic] = {}
    failed_nodes: list[str] = []

    def fetch(node: proto.Node):
        return node, _fetch_node_usage(rwms_client, node.uuid, start, end)

    with ThreadPoolExecutor(max_workers=NODES_FETCH_CONCURRENCY) as executor:
        results = list(executor.map(fetch, nodes))

    for node, rows in results:
        if rows is None:
            logging.warning("node traffic: failed to fetch usage for %s", node.name)
            failed_nodes.append(node.name)
            continue
        for row in rows:
            entry = usage.get(row.user_uuid)
            if entry is None:
                entry = usage[row.user_uuid] = UserTraffic(
                    user_uuid=row.user_uuid, username=row.username
                )
            entry.total_bytes += row.total_bytes
            entry.per_node[node.name] = (
                entry.per_node.get(node.name, 0) + row.total_bytes
            )

    return usage, failed_nodes


def fetch_details(rwms_client: RwmsClientSync, entries: list[UserTraffic]) -> None:
    """Подтягивает карточки пользователей (статус, expire, hwid, tg id)."""

    def fetch(entry: UserTraffic) -> None:
        entry.details = rwms_client.get_user_by_uuid(entry.user_uuid)

    with ThreadPoolExecutor(max_workers=DETAILS_FETCH_CONCURRENCY) as executor:
        list(executor.map(fetch, entries))


def build_report(
    rwms_client: RwmsClientSync,
    nodes: list[proto.Node],
    start: datetime,
    end: datetime,
    top: int,
    min_gib: float,
    with_details: bool = True,
) -> dict:
    """Готовый payload отчета для JSON-ответа админки."""
    # Панель хранит трафик одной строкой на (нода, пользователь, сутки UTC) с
    # created_at = 00:00 дня, а выборка фильтрует created_at >= start. Начало
    # внутри дня отбрасывает ВЕСЬ этот день, поэтому выравниваем вниз до
    # полуночи — иначе отчёт молча теряет данные (инцидент 2026-07-10).
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)

    usage, failed_nodes = collect_usage(rwms_client, nodes, start, end)

    excluded = excluded_usernames()
    excluded_entries = [e for e in usage.values() if e.username in excluded]
    for entry in excluded_entries:
        del usage[entry.user_uuid]

    total_bytes = sum(e.total_bytes for e in usage.values())

    entries = sorted(usage.values(), key=lambda e: e.total_bytes, reverse=True)
    if min_gib > 0:
        entries = [e for e in entries if e.total_bytes >= min_gib * GIB]
    top = min(top, MAX_TOP) if top > 0 else MAX_TOP
    entries = entries[:top]

    if with_details and entries:
        fetch_details(rwms_client, entries)

    period_hours = max((end - start).total_seconds() / 3600, 1 / 60)
    multi_node = len(nodes) > 1

    users_payload = []
    for entry in entries:
        row = {
            "username": entry.username,
            "user_uuid": entry.user_uuid,
            "total_bytes": entry.total_bytes,
            "share_percent": (
                entry.total_bytes / total_bytes * 100 if total_bytes else 0.0
            ),
            "avg_bytes_per_hour": entry.total_bytes / period_hours,
        }
        if multi_node:
            top_name, top_bytes = entry.top_node()
            row["top_node"] = top_name
            row["top_node_share_percent"] = (
                top_bytes / entry.total_bytes * 100 if entry.total_bytes else 0.0
            )
        d = entry.details
        if d is not None:
            row["status"] = (
                proto.UserStatus.Name(d.status) if d.HasField("status") else None
            )
            row["expire_at"] = (
                d.expire_at.ToDatetime().strftime("%Y-%m-%d")
                if d.HasField("expire_at")
                else None
            )
            row["hwid_device_limit"] = (
                d.hwid_device_limit if d.HasField("hwid_device_limit") else None
            )
            row["telegram_id"] = d.telegram_id if d.HasField("telegram_id") else None
        users_payload.append(row)

    return {
        "start": start.strftime("%Y-%m-%d %H:%M"),
        "end": end.strftime("%Y-%m-%d %H:%M"),
        "period_hours": round(period_hours, 2),
        "nodes_total": len(nodes),
        "failed_nodes": failed_nodes,
        "total_bytes": total_bytes,
        "users_with_traffic": len(usage),
        "excluded_users": [
            {"username": e.username, "total_bytes": e.total_bytes}
            for e in excluded_entries
        ],
        "users": users_payload,
    }
