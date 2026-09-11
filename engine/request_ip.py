"""IP клиента за цепочкой доверенных прокси.

Модуль зависит от Django settings и нужен только сайту, поэтому живёт в engine,
а не в общем submodule common.
"""
import ipaddress
import logging
from functools import lru_cache

from django.conf import settings


# Частные сети (RFC 1918 и IPv6 ULA): адрес из них не может быть адресом
# посетителя из интернета. Документационные сети (192.0.2.0/24,
# 198.51.100.0/24, 203.0.113.0/24), которые ipaddress.is_private тоже считает
# частными, сюда намеренно не входят.
_PRIVATE_CLIENT_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)

# Невалидные записи TRUSTED_PROXY_NETWORKS, о которых уже написали в лог:
# ошибка пишется один раз на значение за жизнь процесса.
_reported_invalid_proxy_networks = set()


def _report_invalid_proxy_network(value):
    shown = str(value)[:64]
    if shown in _reported_invalid_proxy_networks:
        return
    _reported_invalid_proxy_networks.add(shown)
    logging.error(
        "invalid TRUSTED_PROXY_NETWORKS/ORIGIN_ALLOWED_PROXY_CIDRS entry %r "
        "ignored: this proxy is not trusted and all its clients share its IP "
        "in rate limits and audit",
        shown,
    )


@lru_cache(maxsize=16)
def _trusted_proxy_networks(values):
    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            # nginx allow принимает записи, которые ipaddress отвергает
            # (например, ведущий ноль в октете): edge проходит на origin, но
            # не доверен. Молча такую запись не отбрасываем.
            _report_invalid_proxy_network(value)
    return tuple(networks)


def _address(value):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _is_trusted(address):
    trusted = _trusted_proxy_networks(tuple(settings.TRUSTED_PROXY_NETWORKS))
    return any(address in network for network in trusted)


def is_trusted_proxy_address(value):
    """True, если адрес входит в TRUSTED_PROXY_NETWORKS (edge, nginx, docker)."""
    address = _address(value)
    return address is not None and _is_trusted(address)


def is_unresolved_client_ip(value):
    """True, если по значению нельзя различать посетителей.

    None, "unknown" или не IP; частный (RFC 1918, IPv6 ULA), loopback,
    link-local или unspecified адрес; адрес из TRUSTED_PROXY_NETWORKS. Так
    выглядит результат client_ip, когда вся цепочка прокси доверенная:
    например, IPv6-клиенты edge за docker userland-proxy приходят с адреса
    шлюза bridge 172.x.0.1, и по такому IP все они слились бы в одного.
    """
    if value is None:
        return True
    address = _address(str(value))
    if address is None:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    for candidate in (address,) if mapped is None else (address, mapped):
        if (
            candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_unspecified
            or any(candidate in network for network in _PRIVATE_CLIENT_NETWORKS)
            or _is_trusted(candidate)
        ):
            return True
    return False


def client_ip(request):
    """Return the nearest untrusted address in a trusted proxy chain.

    Forwarded headers are ignored when the direct peer is not a configured
    proxy. Walking the chain from the application outwards also prevents a
    client-prepended value from becoming the rate-limit or audit identity.
    """
    peer = _address(request.META.get("REMOTE_ADDR"))
    if peer is None:
        return None

    if not _is_trusted(peer):
        return str(peer)

    forwarded = []
    for raw_address in request.META.get("HTTP_X_FORWARDED_FOR", "").split(","):
        address = _address(raw_address)
        if address is not None:
            forwarded.append(address)

    chain = forwarded + [peer]
    for address in reversed(chain):
        if not _is_trusted(address):
            return str(address)

    return str(chain[0]) if chain else str(peer)
