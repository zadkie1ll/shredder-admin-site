"""Mobile-app device-code auth.

Mirrors the existing telegram-login token pattern (only hashes are stored, never
raw secrets). The hash uses ``settings.SECRET_KEY`` — the SAME value the bot uses
as ``config.django_site_sk`` — so a code hashed by the bot matches here.

Flow: app generates a random ``code`` and opens ``/start app_<code>`` in the bot;
the bot stores ``hash(code) -> user`` (``register_auth_code``); the app then calls
``/api/mobile/v1/auth/exchange`` which swaps a valid, unused, unexpired code for a
revocable access token (``exchange_code``).
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from django.conf import settings

from common.models.db import MobileAccessToken, MobileAuthCode, User

# One-time login code lifetime.
AUTH_CODE_TTL = timedelta(minutes=10)


def hash_auth_code(code):
    payload = f"mobile-auth-code:{code}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def hash_access_token(token):
    payload = f"mobile-access-token:{token}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def register_auth_code(db_session, *, user_id, code, source="bot_start", now=None):
    """Persist ``hash(code) -> user_id`` with a short TTL. Used by the bot when it
    receives ``/start app_<code>``. Only the hash is stored."""
    now = now or datetime.utcnow()
    db_session.add(
        MobileAuthCode(
            user_id=user_id,
            code_hash=hash_auth_code(code),
            source=source,
            expires_at=now + AUTH_CODE_TTL,
        )
    )


def exchange_code(db_session, code, now=None):
    """One-time exchange: the code must exist, be unused and unexpired. Marks it
    used and issues a fresh access token. Returns ``(user, raw_access_token)`` or
    ``(None, None)``."""
    now = now or datetime.utcnow()
    row = (
        db_session.query(MobileAuthCode)
        .filter(MobileAuthCode.code_hash == hash_auth_code(code))
        .one_or_none()
    )
    if row is None or row.used_at is not None or row.expires_at <= now:
        return None, None

    row.used_at = now
    raw_token = secrets.token_urlsafe(48)
    db_session.add(
        MobileAccessToken(user_id=row.user_id, token_hash=hash_access_token(raw_token))
    )
    user = db_session.query(User).filter(User.id == row.user_id).one_or_none()
    return user, raw_token


def bearer_token(request):
    header = request.META.get("HTTP_AUTHORIZATION", "") or ""
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :].strip()
    return token or None


def authenticate(db_session, request, now=None):
    """Resolve a ``Authorization: Bearer <token>`` to ``(user, token_row)``, or
    ``(None, None)`` if missing/unknown/revoked. Updates ``last_seen_at``."""
    raw_token = bearer_token(request)
    if not raw_token:
        return None, None
    row = (
        db_session.query(MobileAccessToken)
        .filter(MobileAccessToken.token_hash == hash_access_token(raw_token))
        .one_or_none()
    )
    if row is None or row.revoked_at is not None:
        return None, None
    row.last_seen_at = now or datetime.utcnow()
    user = db_session.query(User).filter(User.id == row.user_id).one_or_none()
    return user, row
