"""Lazy gRPC client for fetching a user's Remnawave subscription (subscription_url,
expire_at, status). Created on first use so importing this module doesn't require
RWMS settings (keeps unit tests of the auth layer independent)."""

import logging

from django.conf import settings

from common.rwms_client import RwmsUnavailableError
from common.rwms_client_sync import RwmsClientSync

_client = None


def rwms_client():
    global _client
    if _client is None:
        # Общий дедлайн RPC сайта (RWMS_RPC_TIMEOUT_SECONDS, по умолчанию 8 с),
        # как у engine.views.rwms_client: без него зависшая панель держала бы
        # поток gunicorn бесконечно.
        _client = RwmsClientSync(
            settings.RWMS_HOST,
            settings.RWMS_PORT,
            timeout=settings.RWMS_RPC_TIMEOUT_SECONDS,
        )
    return _client


def get_rwms_user(username):
    """Return the RWMS UserResponse for ``username`` (subscription_url/expire/status).

    ``None`` — ТОЛЬКО достоверный NOT_FOUND (подписки нет в панели). При
    недоступности RWMS/панели (или любой неожиданной ошибке) бросает
    ``RwmsUnavailableError``: вызывающий код обязан отвечать «временно
    недоступно», а не показывать отсутствие подписки (Политика: БД — истина
    по времени, панель — истина по существованию ключа)."""
    try:
        return rwms_client().get_user_by_username_strict(username)
    except RwmsUnavailableError:
        raise
    except Exception as error:  # noqa: BLE001 - never let RWMS errors 500 the API
        logging.warning("mobile_api: RWMS lookup failed for %s: %s", username, error)
        raise RwmsUnavailableError(username, None, str(error)) from error
