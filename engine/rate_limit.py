"""Лимиты частоты запросов на кэше Django (фиксированное окно).

Модуль зависит от Django и нужен только сайту, поэтому живёт в engine, а не в
общем submodule common (там Django нет у бота и payment).

Ключ бакета: ``rate-limit:<scope>:<bucket>:<window_id>:<sha256>``. Идентификатор
(email, логин, IP) хешируется вместе с SECRET_KEY и в ключ кэша не попадает.
``window_id = int(now // window_seconds)``: даже ключ, оставшийся без TTL, со
следующего окна перестаёт влиять на лимит.

Бакет задаётся кортежем ``(bucket_name, identifier, limit, window_seconds)``.
``limit`` None или <= 0 (как и окно <= 0) выключает бакет. Любая ошибка кэша
пропускает запрос (fail-open) и пишется в лог: недоступный кэш не должен
запирать снаружи всех клиентов и сотрудников.

Бакет ``"ip"`` пропускается (не считается и не блокирует), если настоящий IP
клиента за прокси не определён: см. ``ip_rate_limit_skipped``. Бакеты email,
global и account-ip от этого не меняются.
"""
import hashlib
import hmac
import logging
import threading
import time

from django.conf import settings
from django.core.cache import cache

from engine.request_ip import is_unresolved_client_ip


# Отдельная ссылка на часы: тесты сдвигают окно, не трогая time.time
# (LocMemCache считает по нему истечение ключей).
_now = time.time

IP_BUCKET_NAME = "ip"
UNRESOLVED_IP_WARNING_INTERVAL_SECONDS = 600

# Часы для частоты предупреждения; тесты подменяют их отдельно от _now.
_monotonic = time.monotonic
_unresolved_ip_warning_lock = threading.Lock()
_unresolved_ip_warned_at = None


def _warn_unresolved_client_ip(scope, identifier):
    """WARNING не чаще раза в UNRESOLVED_IP_WARNING_INTERVAL_SECONDS на процесс."""
    global _unresolved_ip_warned_at
    now = _monotonic()
    with _unresolved_ip_warning_lock:
        if (
            _unresolved_ip_warned_at is not None
            and now - _unresolved_ip_warned_at < UNRESOLVED_IP_WARNING_INTERVAL_SECONDS
        ):
            return
        _unresolved_ip_warned_at = now
    logging.warning(
        "client IP unresolved (proxy chain fully trusted): IP rate limit skipped "
        "— check edge IPv6/docker-proxy (scope=%s ip=%s)",
        scope,
        str(identifier)[:64] if identifier else "unknown",
    )


def ip_rate_limit_skipped(scope, identifier):
    """True, если лимит по IP для этого идентификатора надо пропустить.

    Идентификатор None/"unknown", частный, loopback, link-local, unspecified
    или из TRUSTED_PROXY_NETWORKS значит, что вся цепочка прокси доверенная и
    настоящий адрес клиента не определён. Пример: IPv6-клиенты edge за docker
    userland-proxy приходят с адреса шлюза bridge 172.x.0.1. Один бакет на всех
    таких клиентов запер бы их разом, поэтому IP-лимит не считается и не
    блокирует, а в лог уходит WARNING с ограничением частоты. Клиент с
    публичным адресом в цепочке сразу за доверенными прокси получает свой
    бакет, как и раньше.
    """
    if not is_unresolved_client_ip(identifier):
        return False
    _warn_unresolved_client_ip(scope, identifier)
    return True


def _skip_bucket(scope, bucket_name, identifier):
    return bucket_name == IP_BUCKET_NAME and ip_rate_limit_skipped(scope, identifier)


def _bucket_enabled(limit, window_seconds):
    try:
        return (
            limit is not None
            and limit > 0
            and window_seconds is not None
            and window_seconds > 0
        )
    except TypeError:
        return False


def _bucket_key(scope, bucket_name, identifier, window_seconds, now):
    digest = hashlib.sha256(
        f"{settings.SECRET_KEY}:{scope}:{bucket_name}:{identifier}".encode()
    ).hexdigest()
    window_id = int(now // window_seconds)
    return f"rate-limit:{scope}:{bucket_name}:{window_id}:{digest}"


def _increment(key, window_seconds):
    cache.add(key, 0, timeout=window_seconds)
    count = cache.incr(key)
    if count == 1:
        # Redis: cache.incr — это EXISTS и INCR двумя запросами. Если ключ истёк
        # между ними, INCR создаёт его со значением 1 и без TTL, а следующие
        # add (SET NX EX) TTL уже не поставят. Возвращаем TTL явно; в штатном
        # первом запросе окна это лишь продлевает TTL на доли секунды.
        cache.touch(key, window_seconds)
    return count


def rate_limit_exceeded(scope, buckets):
    """Посчитать запрос и вернуть ``(limited, retry_after)``.

    Бакеты проверяются по порядку, проверка останавливается на первом
    превышенном: отбитый запрос следующие бакеты не расходует (иначе запросы,
    уже отклонённые по IP, выбивали бы общий лимит и лимит чужого email).
    """
    try:
        now = _now()
        for bucket_name, identifier, limit, window_seconds in buckets:
            if not _bucket_enabled(limit, window_seconds):
                continue
            if _skip_bucket(scope, bucket_name, identifier):
                continue
            key = _bucket_key(scope, bucket_name, identifier, window_seconds, now)
            if _increment(key, window_seconds) > limit:
                return True, window_seconds
    except Exception:
        logging.exception("rate limit cache failed for scope=%s", scope)
        return False, 0
    return False, 0


def rate_limit_peek(scope, buckets):
    """Только проверить, исчерпан ли какой-то бакет; ничего не расходует.

    Возвращает ``(limited, retry_after)``. Бакет исчерпан, когда его счётчик
    уже достиг лимита, то есть следующая попытка была бы лишней.
    """
    try:
        now = _now()
        for bucket_name, identifier, limit, window_seconds in buckets:
            if not _bucket_enabled(limit, window_seconds):
                continue
            if _skip_bucket(scope, bucket_name, identifier):
                continue
            key = _bucket_key(scope, bucket_name, identifier, window_seconds, now)
            if int(cache.get(key, 0) or 0) >= limit:
                return True, window_seconds
    except Exception:
        logging.exception("rate limit cache peek failed for scope=%s", scope)
        return False, 0
    return False, 0


def rate_limit_hit(scope, buckets):
    """Увеличить счётчики всех включённых бакетов, ничего не блокируя.

    Возвращает ``{bucket_name: count}`` для посчитанных бакетов (при ошибке
    кэша — то, что успели посчитать, ошибка пишется в лог).
    """
    counts = {}
    try:
        now = _now()
        for bucket_name, identifier, limit, window_seconds in buckets:
            if not _bucket_enabled(limit, window_seconds):
                continue
            if _skip_bucket(scope, bucket_name, identifier):
                continue
            key = _bucket_key(scope, bucket_name, identifier, window_seconds, now)
            counts[bucket_name] = _increment(key, window_seconds)
    except Exception:
        logging.exception("rate limit cache hit failed for scope=%s", scope)
    return counts


def rate_limit_log_digest(identifier):
    """Короткий ключевой отпечаток идентификатора для логов.

    Email и логины сотрудников в открытом виде в лог не пишутся; отпечаток
    позволяет сопоставить записи об одном и том же адресе/аккаунте.
    """
    return hmac.new(
        str(settings.SECRET_KEY).encode(),
        f"rate-limit-log:{identifier}".encode(),
        hashlib.sha256,
    ).hexdigest()[:12]
