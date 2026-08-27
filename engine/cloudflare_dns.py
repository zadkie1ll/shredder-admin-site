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

import logging

import httpx
from django.conf import settings

API_BASE = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 15

log = logging.getLogger("cloudflare-dns")

# Кэш domain -> zone_id на процесс: зоны не меняются, а каждый lookup —
# отдельный API-запрос.
_zone_cache: dict[str, str] = {}


class CloudflareError(Exception):
    pass


def is_enabled() -> bool:
    return bool(getattr(settings, "CLOUDFLARE_API_TOKEN", ""))


def _request(method: str, path: str, **kwargs) -> dict:
    token = getattr(settings, "CLOUDFLARE_API_TOKEN", "")
    if not token:
        raise CloudflareError("CLOUDFLARE_API_TOKEN не настроен")
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.request(
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
    labels = domain.split(".")
    # До двух меток включительно: голый TLD зоной быть не может.
    # Кандидаты проверяются строго от самого специфичного к родителю —
    # и кэш, и API в одном проходе: иначе закэшированная родительская зона
    # перекрывала бы собственную зону поддомена
    candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    for candidate in candidates:
        if candidate in _zone_cache:
            return _zone_cache[candidate]
        payload = _request(
            "GET", "/zones", params={"name": candidate, "status": "active"}
        )
        result = payload.get("result") or []
        if result:
            zone_id = result[0]["id"]
            _zone_cache[candidate] = zone_id
            return zone_id
    raise CloudflareError(f"Зона для домена {domain} не найдена в Cloudflare")


def list_a_records(domain: str) -> list[dict]:
    """A-записи домена: [{"id", "content", "proxied", "ttl"}]."""
    zone_id = find_zone_id(domain)
    payload = _request(
        "GET",
        f"/zones/{zone_id}/dns_records",
        params={"type": "A", "name": domain.strip(".").lower(), "per_page": 100},
    )
    return [
        {
            "id": record["id"],
            "content": record.get("content"),
            "proxied": record.get("proxied", False),
            "ttl": record.get("ttl"),
        }
        for record in payload.get("result") or []
    ]


def ensure_a_record(domain: str, ip: str, ttl: int = 60) -> bool:
    """Создаёт A-запись domain -> ip, если её ещё нет.

    Возвращает True, если запись создана, False — уже существовала.
    """
    domain = domain.strip(".").lower()
    existing = list_a_records(domain)
    if any(record["content"] == ip for record in existing):
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
