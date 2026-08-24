"""Служебные пуши из админки сайта в Redis-очереди ботов.

Тот же канал, что у user-notify: сообщение (pydantic-модель из
``common/models/messages.py``) кладётся RPUSH в очередь КАЖДОГО бота из
``BOT_REDIS_QUEUES`` (vpn/vps) — боты читают свои очереди и сами решают по
``telegram_delivery_blocks``, кто доставит. Сайт ничего не шлёт в Telegram
напрямую. Пустой ``BOT_REDIS_HOST`` — канал отключён: функции возвращают
False, само действие в админке (бан и т.п.) всё равно выполняется.
"""

import logging

import orjson
from django.conf import settings

from common.models.messages import AdminTemporaryBanNotify

REDIS_TIMEOUT_SECONDS = 3


def bot_queues() -> list[str]:
    raw = getattr(settings, "BOT_REDIS_QUEUES", "") or ""
    return [queue.strip() for queue in raw.split(",") if queue.strip()]


def is_enabled() -> bool:
    return bool(getattr(settings, "BOT_REDIS_HOST", "")) and bool(bot_queues())


def _redis_client():
    # Лениво: сайт без Redis (локальная разработка, тесты) должен работать —
    # пуши при этом просто отключены.
    import redis

    return redis.Redis(
        host=settings.BOT_REDIS_HOST,
        port=int(getattr(settings, "BOT_REDIS_PORT", 6379) or 6379),
        password=getattr(settings, "BOT_REDIS_PASSWORD", "") or None,
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
    )


def push_to_bots(message) -> bool:
    """RPUSH сообщения в очередь каждого бота. True — положили во все очереди."""
    if not is_enabled():
        logging.info(
            "bot push: Redis is not configured, skipping %s",
            getattr(message, "type", type(message).__name__),
        )
        return False
    payload = orjson.dumps(message.model_dump()).decode("utf-8")
    queues = bot_queues()
    try:
        client = _redis_client()
        for queue in queues:
            client.rpush(queue, payload)
    except Exception:
        logging.exception("bot push: failed to queue %s to %s", message.type, queues)
        return False
    logging.info("bot push: queued %s to %s", message.type, queues)
    return True


def push_admin_temporary_ban(telegram_id, ban_minutes) -> bool:
    """Уведомление о временной блокировке (NOTIFY_TEMPORARY_BAN шлёт бот).
    Без telegram_id (пользователь только с email) уведомлять некого."""
    if not telegram_id:
        return False
    message = AdminTemporaryBanNotify(
        telegram_id=int(telegram_id), ban_minutes=max(int(ban_minutes), 1)
    )
    return push_to_bots(message)
