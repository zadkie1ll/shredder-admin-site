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

from common.models.db import (
    EmailLoginCode,
    MobileAccessToken,
    MobileAuthCode,
    User,
)
from engine.sql_helpers import lock_registration_email
from engine.user_block import is_user_blocked

# One-time login code lifetime.
AUTH_CODE_TTL = timedelta(minutes=10)

# Email login-code lifetime and brute-force ceiling. The contract fixes these:
# a 6-digit numeric code is short, so we cap verify attempts and the TTL tightly.
EMAIL_CODE_TTL = timedelta(minutes=10)
EMAIL_CODE_MAX_ATTEMPTS = 5


def hash_auth_code(code):
    payload = f"mobile-auth-code:{code}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def hash_access_token(token):
    payload = f"mobile-access-token:{token}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def hash_email_code(code):
    # Distinct namespace from the telegram auth code so the two hash spaces never
    # collide even if the same string is used as both. Only the hash is stored.
    payload = f"mobile-email-code:{code}:{settings.SECRET_KEY}".encode()
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


def _issue_access_token(db_session, user, now=None):
    """Issue a fresh revocable access token for ``user``. Only the hash is stored.
    Shared by the telegram device-code exchange and the email-code verify path so
    token issuance lives in exactly one place. Returns the raw token."""
    raw_token = secrets.token_urlsafe(48)
    db_session.add(
        MobileAccessToken(
            user_id=user.id,
            token_hash=hash_access_token(raw_token),
            last_seen_at=now,
        )
    )
    return raw_token


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
    user = db_session.query(User).filter(User.id == row.user_id).one_or_none()
    if user is None:
        return None, None
    if is_user_blocked(db_session, user.id):
        return None, None
    raw_token = _issue_access_token(db_session, user, now=now)
    return user, raw_token


def register_email_code(db_session, email, code, source="mobile_email", now=None):
    """Persist ``hash(code)`` for ``email`` with a short TTL. The caller has already
    lowercased ``email`` and validated its shape. Only the hash is stored, never the
    raw code. Returns the created row."""
    now = now or datetime.utcnow()
    # Requesting a new code invalidates any prior unused codes for this email, so at
    # most one code is ever active (conventional "a new code voids the old one"). This
    # also prevents an older still-unexpired code from shadowing the newest at verify
    # time, and caps the brute-force surface to a single active code.
    (
        db_session.query(EmailLoginCode)
        .filter(
            EmailLoginCode.email == email,
            EmailLoginCode.used_at.is_(None),
        )
        .update({EmailLoginCode.used_at: now}, synchronize_session=False)
    )
    row = EmailLoginCode(
        email=email,
        code_hash=hash_email_code(code),
        source=source,
        created_at=now,
        expires_at=now + EMAIL_CODE_TTL,
    )
    db_session.add(row)
    return row


def verify_email_code(db_session, email, code, now=None):
    """Verify ``code`` against the newest unused, unexpired code for ``email``.

    Returns ``(True, row)`` on a match (row marked used), else ``(False, row|None)``.
    On a code mismatch the attempt counter is incremented so a code is locked after
    ``EMAIL_CODE_MAX_ATTEMPTS`` wrong tries. The caller has already lowercased
    ``email``."""
    now = now or datetime.utcnow()
    row = (
        db_session.query(EmailLoginCode)
        .filter(
            EmailLoginCode.email == email,
            EmailLoginCode.used_at.is_(None),
            EmailLoginCode.expires_at > now,
        )
        .order_by(EmailLoginCode.created_at.desc(), EmailLoginCode.id.desc())
        .first()
    )
    if row is None:
        return False, None
    if row.attempts >= EMAIL_CODE_MAX_ATTEMPTS:
        return False, row
    if row.code_hash != hash_email_code(code):
        row.attempts = row.attempts + 1
        return False, row

    row.used_at = now
    return True, row


def lock_email(db_session, email):
    """Serialize concurrent email-verify transactions for the same address with a
    Postgres transaction-level advisory lock (auto-released on commit/rollback).

    Without this, two simultaneous verifies for a brand-new email could each
    provision an RWMS trial subscription — the DB-row loser rolls back, but its
    gRPC ``AddUser`` already created a second (orphaned) subscription. Holding the
    lock for the whole find-or-provision section guarantees only one provisioner
    runs per email. No-op on non-Postgres backends (e.g. the test SQLite).

    Тонкая обёртка над общим ``lock_registration_email``: сайтовая регистрация
    берёт ТОТ ЖЕ лок по тому же ключу, поэтому мобильный и сайтовый flow для
    одного email сериализуются друг с другом, а не только сами с собой."""
    lock_registration_email(db_session, email)


def bearer_token(request):
    header = request.META.get("HTTP_AUTHORIZATION", "") or ""
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :].strip()
    return token or None


def authenticate(db_session, request, now=None):
    """Resolve a ``Authorization: Bearer <token>`` to ``(user, token_row)``, or
    ``(None, None)`` if missing/unknown/revoked/blocked. Updates ``last_seen_at``."""
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
    # Полностью заблокированный аккаунт (user_blocks) не аутентифицируется
    if user is not None and is_user_blocked(db_session, user.id):
        return None, None
    return user, row
