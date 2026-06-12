"""Lazy gRPC client for fetching a user's Remnawave subscription (subscription_url,
expire_at, status). Created on first use so importing this module doesn't require
RWMS settings (keeps unit tests of the auth layer independent)."""

import logging

from django.conf import settings

from common.rwms_client_sync import RwmsClientSync

_client = None


def rwms_client():
    global _client
    if _client is None:
        _client = RwmsClientSync(settings.RWMS_HOST, settings.RWMS_PORT)
    return _client


def get_rwms_user(username):
    """Return the RWMS UserResponse for ``username`` (subscription_url/expire/status),
    or ``None`` on any error — the caller degrades gracefully."""
    try:
        return rwms_client().get_user_by_username(username)
    except Exception as error:  # noqa: BLE001 - never let RWMS errors 500 the API
        logging.warning("mobile_api: RWMS lookup failed for %s: %s", username, error)
        return None
