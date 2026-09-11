"""Клиент Cloudflare DNS API для автоматической ротации IP нод.

Используется инфраструктурным воркером (engine/infra_worker.py): при
подтверждённой ТСПУ-замерами блокировке IP добавляет A-запись резервного
адреса и удаляет A-запись заблокированного. Токен — settings.CLOUDFLARE_API_TOKEN
(права Zone:DNS:Edit + Zone:Zone:Read на зоны клиентских доменов; тот же
скоуп, что у CF_DNS_API_TOKEN edge-хоста). Пустой токен = интеграция
выключена, ротация останавливается с алертом.

Все операции идемпотентны: ensure_a_record ничего не делает, если запись
уже есть; delete_a_records молча завершает работу, если записи нет.
A-записи создаются DNS-only (proxied=false): за Cloudflare-прокси VPN
не работает.
"""

import atexit
import logging
import threading
from time import monotonic

import httpx
from django.conf import settings

API_BASE = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 15

log = logging.getLogger("cloudflare-dns")

# Кэш domain -> zone_id на процесс: зоны не меняются, а каждый lookup —
# отдельный API-запрос.
_zone_cache: dict[str, tuple[str, float]] = {}
_resolved_zone_cache: dict[str, tuple[str, float]] = {}
_missing_zone_cache: dict[str, float] = {}
_cache_token: str | None = None
_cache_lock = threading.Lock()
_client: httpx.Client | None = None
_client_lock = threading.Lock()

ZONE_CACHE_SECONDS = 300
MISSING_ZONE_CACHE_SECONDS = 60


class CloudflareError(Exception):
    pass


def is_enabled() -> bool:
    return bool(getattr(settings, "CLOUDFLARE_API_TOKEN", ""))


def _http_client() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(timeout=TIMEOUT_SECONDS)
    return _client


def close_http_client() -> None:
    """Close the process-wide pool during worker shutdown."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


atexit.register(close_http_client)


def _request(method: str, path: str, **kwargs) -> dict:
    token = getattr(settings, "CLOUDFLARE_API_TOKEN", "")
    if not token:
        raise CloudflareError("CLOUDFLARE_API_TOKEN не настроен")
    try:
        response = _http_client().request(
            method,
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
    except httpx.HTTPError as e:
        raise CloudflareError(f"Cloudflare API недоступен: {e!r}") from e

    try:
        payload = response.json()
    except ValueError:
        raise CloudflareError(
            f"Cloudflare API: не-JSON ответ HTTP {response.status_code}"
        )
    if not payload.get("success"):
        errors = "; ".join(
            f"{err.get('code')}: {err.get('message')}"
            for err in payload.get("errors") or []
        )
        raise CloudflareError(
            f"Cloudflare API {method} {path}: {errors or 'HTTP %s' % response.status_code}"
        )
    return payload


def find_zone_id(domain: str) -> str:
    """zone_id для домена: пробуем сам домен, потом родительские зоны.

    de.monkeyisland.xyz -> зона de.monkeyisland.xyz? -> monkeyisland.xyz.
    """
    domain = domain.strip(".").lower()
    token = getattr(settings, "CLOUDFLARE_API_TOKEN", "")
    now = monotonic()
    global _cache_token
    with _cache_lock:
        if token != _cache_token:
            _zone_cache.clear()
            _resolved_zone_cache.clear()
            _missing_zone_cache.clear()
            _cache_token = token
        resolved = _resolved_zone_cache.get(domain)
        if resolved is not None and resolved[1] > now:
            return resolved[0]
        if _missing_zone_cache.get(domain, 0) > now:
            raise CloudflareError(f"Зона для домена {domain} не найдена в Cloudflare")

    labels = domain.split(".")
    # До двух меток включительно: голый TLD зоной быть не может.
    # Кандидаты проверяются строго от самого специфичного к родителю —
    # и кэш, и API в одном проходе: иначе закэшированная родительская зона
    # перекрывала бы собственную зону поддомена
    candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    for candidate in candidates:
        with _cache_lock:
            cached_zone = _zone_cache.get(candidate)
            if cached_zone is not None and cached_zone[1] <= now:
                _zone_cache.pop(candidate, None)
                cached_zone = None
        if cached_zone is not None:
            zone_id = cached_zone[0]
            with _cache_lock:
                _resolved_zone_cache[domain] = (
                    zone_id,
                    now + ZONE_CACHE_SECONDS,
                )
            return zone_id
        payload = _request(
            "GET", "/zones", params={"name": candidate, "status": "active"}
        )
        result = payload.get("result") or []
        if result:
            zone_id = result[0]["id"]
            with _cache_lock:
                _zone_cache[candidate] = (zone_id, now + ZONE_CACHE_SECONDS)
                _resolved_zone_cache[domain] = (
                    zone_id,
                    now + ZONE_CACHE_SECONDS,
                )
            return zone_id
    with _cache_lock:
        _missing_zone_cache[domain] = now + MISSING_ZONE_CACHE_SECONDS
    raise CloudflareError(f"Зона для домена {domain} не найдена в Cloudflare")


def list_a_records(domain: str) -> list[dict]:
    """A-записи домена: [{"id", "content", "proxied", "ttl"}]."""
    zone_id = find_zone_id(domain)
    page = 1
    records = []
    while True:
        payload = _request(
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={
                "type": "A",
                "name": domain.strip(".").lower(),
                "per_page": 100,
                "page": page,
            },
        )
        page_records = payload.get("result") or []
        records.extend(page_records)
        result_info = payload.get("result_info") or {}
        total_pages = int(result_info.get("total_pages") or 0)
        if len(page_records) < 100 or (total_pages and page >= total_pages):
            break
        if page >= 20:
            raise CloudflareError(
                f"Cloudflare API вернул более 2000 A-записей для {domain}"
            )
        page += 1
    return [
        {
            "id": record["id"],
            "content": record.get("content"),
            "proxied": record.get("proxied", False),
            "ttl": record.get("ttl"),
        }
        for record in records
    ]


def ensure_a_record(domain: str, ip: str, ttl: int = 60) -> bool:
    """Создаёт A-запись domain -> ip, если её ещё нет.

    Возвращает True, если запись создана, False — уже существовала.
    """
    domain = domain.strip(".").lower()
    existing = list_a_records(domain)
    matching = [record for record in existing if record["content"] == ip]
    if matching:
        record = matching[0]
        if record["proxied"] or record["ttl"] != ttl:
            zone_id = find_zone_id(domain)
            _request(
                "PATCH",
                f"/zones/{zone_id}/dns_records/{record['id']}",
                json={"ttl": ttl, "proxied": False},
            )
            log.info(
                "ensure_a_record: исправлены параметры A %s -> %s (ttl=%s, DNS-only)",
                domain,
                ip,
                ttl,
            )
        else:
            log.info("ensure_a_record: %s -> %s уже существует", domain, ip)
        return False
    zone_id = find_zone_id(domain)
    _request(
        "POST",
        f"/zones/{zone_id}/dns_records",
        json={
            "type": "A",
            "name": domain,
            "content": ip,
            "ttl": ttl,
            "proxied": False,
        },
    )
    log.info("ensure_a_record: создана A %s -> %s (ttl=%s)", domain, ip, ttl)
    return True


def delete_a_records(domain: str, ip: str) -> int:
    """Удаляет A-записи domain -> ip. Возвращает число удалённых (0 — не было)."""
    domain = domain.strip(".").lower()
    zone_id = find_zone_id(domain)
    deleted = 0
    for record in list_a_records(domain):
        if record["content"] != ip:
            continue
        _request("DELETE", f"/zones/{zone_id}/dns_records/{record['id']}")
        deleted += 1
    log.info("delete_a_records: %s -> %s удалено %s записей", domain, ip, deleted)
    return deleted
