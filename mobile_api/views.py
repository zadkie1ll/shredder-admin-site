import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

import proto.rwmanager_pb2 as proto
from common.models.db import EmailLoginCode, User
from database import session_factory

from .auth import (
    EMAIL_CODE_TTL,
    _issue_access_token,
    authenticate,
    exchange_code,
    lock_email,
    register_email_code,
    verify_email_code,
)
from .subscription import get_rwms_user, rwms_client
from .tariffs import MOBILE_TARIFFS, serialize_tariff

# Pragmatic email shape check (we do not verify deliverability here; the email
# service is the source of truth for that). Mirrors common "good enough" patterns.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Per-email send limits (contract): at least 60s between codes, at most 5/hour.
EMAIL_RESEND_INTERVAL = timedelta(seconds=60)
EMAIL_HOURLY_WINDOW = timedelta(hours=1)
EMAIL_HOURLY_LIMIT = 5


def _normalize_email(value):
    return (value or "").strip().lower()


def _is_valid_email(email):
    return bool(email) and len(email) <= 256 and _EMAIL_RE.match(email) is not None


def _serialize_user(user):
    return {
        "id": user.id,
        "username": user.username,
        "telegram_id": user.telegram_id,
    }


def _expire_fields(rw):
    if rw is None or not rw.HasField("expire_at"):
        return None, None
    expire_dt = rw.expire_at.ToDatetime().replace(tzinfo=timezone.utc)
    days_left = max(0, (expire_dt - datetime.now(timezone.utc)).days)
    return expire_dt.isoformat(), days_left


def _status_name(rw):
    if rw is None or not rw.HasField("status"):
        return None
    return proto.UserStatus.Name(rw.status)


@csrf_exempt
@require_http_methods(["POST"])
def auth_exchange(request):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    code = (body.get("code") or "").strip()
    if not code:
        return JsonResponse({"error": "missing_code"}, status=400)

    session = session_factory()
    try:
        user, raw_token = exchange_code(session, code)
        if user is None:
            session.rollback()
            return JsonResponse({"error": "invalid_or_expired_code"}, status=401)
        rw = get_rwms_user(user.username)
        subscription_url = rw.subscription_url if rw is not None else None
        session.commit()
        return JsonResponse(
            {
                "access_token": raw_token,
                "subscription_url": subscription_url,
                "user": _serialize_user(user),
            }
        )
    finally:
        session.close()


def _email_rate_limited(session, email, now):
    """Return True if ``email`` has sent a code in the last 60s, or 5+ in the last
    hour. Implemented by inspecting recent ``email_login_codes`` rows (no extra
    state). The caller has already normalized ``email``."""
    last = (
        session.query(EmailLoginCode)
        .filter(EmailLoginCode.email == email)
        .order_by(EmailLoginCode.created_at.desc(), EmailLoginCode.id.desc())
        .first()
    )
    if last is not None and (now - last.created_at) < EMAIL_RESEND_INTERVAL:
        return True

    hour_ago = now - EMAIL_HOURLY_WINDOW
    recent_count = (
        session.query(EmailLoginCode)
        .filter(
            EmailLoginCode.email == email,
            EmailLoginCode.created_at >= hour_ago,
        )
        .count()
    )
    return recent_count >= EMAIL_HOURLY_LIMIT


@csrf_exempt
@require_http_methods(["POST"])
def auth_email_request(request):
    """Step 1 of email login: email a 6-digit code. Always 200 for a validly
    formatted email (never leak whether the user exists)."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    email = _normalize_email(body.get("email"))
    if not _is_valid_email(email):
        return JsonResponse({"error": "invalid_email"}, status=400)

    now = datetime.utcnow()
    session = session_factory()
    try:
        if _email_rate_limited(session, email, now):
            return JsonResponse({"error": "rate_limited"}, status=429)

        # Zero-padded 6-digit numeric code (000000-999999).
        code = f"{secrets.randbelow(1_000_000):06d}"
        register_email_code(session, email, code, now=now)
        ttl_minutes = int(EMAIL_CODE_TTL.total_seconds() // 60)

        # Send synchronously via the SAME transport the site uses for magic-link
        # emails (Resend, with the Django send_mail fallback) — no queue/worker.
        # Lazy import keeps the auth-layer unit tests independent of engine.views.
        from engine.views import send_login_code_email

        try:
            send_login_code_email(email, code, ttl_minutes=ttl_minutes)
        except Exception:  # noqa: BLE001 - don't 500 the client on a provider hiccup
            import logging

            logging.exception(
                "mobile_api: failed to send login-code email for %s", email
            )
            # The send failed → don't keep the code we couldn't deliver.
            session.rollback()
            return JsonResponse({"error": "email_send_failed"}, status=502)

        session.commit()
        return JsonResponse(
            {"ok": True, "ttl_seconds": int(EMAIL_CODE_TTL.total_seconds())}
        )
    finally:
        session.close()


@csrf_exempt
@require_http_methods(["POST"])
def auth_email_verify(request):
    """Step 2 of email login: verify the code, then return a bearer token. For a
    brand-new email this provisions a real RWMS trial subscription (bot way)."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    email = _normalize_email(body.get("email"))
    code = (body.get("code") or "").strip()
    if not _is_valid_email(email) or not code:
        return JsonResponse({"error": "invalid_or_expired_code"}, status=400)

    now = datetime.utcnow()
    session = session_factory()
    try:
        # Serialize concurrent verifies for this email so a brand-new user can
        # never have two RWMS trial subscriptions provisioned in a race.
        lock_email(session, email)
        ok, _row = verify_email_code(session, email, code, now=now)
        if not ok:
            # Persist the incremented attempt counter before returning.
            session.commit()
            return JsonResponse({"error": "invalid_or_expired_code"}, status=401)

        user = session.query(User).filter(User.email == email).one_or_none()
        if user is None:
            # Brand-new user: provision a full trial the bot way (real RWMS sub).
            from .provisioning import provision_trial_user

            user = provision_trial_user(session, rwms_client(), email)
            if user is None:
                session.rollback()
                return JsonResponse({"error": "invalid_or_expired_code"}, status=401)
        # Existing user: DO NOT touch RWMS, DO NOT re-provision a trial.

        raw_token = _issue_access_token(session, user, now=now)
        rw = get_rwms_user(user.username)
        subscription_url = rw.subscription_url if rw is not None else None
        session.commit()
        return JsonResponse(
            {
                "access_token": raw_token,
                "subscription_url": subscription_url,
                "user": _serialize_user(user),
            }
        )
    finally:
        session.close()


@csrf_exempt
@require_http_methods(["GET"])
def me(request):
    session = session_factory()
    try:
        user, _token = authenticate(session, request)
        if user is None:
            return JsonResponse({"error": "unauthorized"}, status=401)
        rw = get_rwms_user(user.username)
        expire_iso, days_left = _expire_fields(rw)
        payload = {
            "user": _serialize_user(user),
            "status": _status_name(rw),
            "expire_at": expire_iso,
            "days_left": days_left,
            "subscription_url": rw.subscription_url if rw is not None else None,
        }
        session.commit()
        return JsonResponse(payload)
    finally:
        session.close()


@require_http_methods(["GET"])
def tariffs(request):
    return JsonResponse({"tariffs": [serialize_tariff(t) for t in MOBILE_TARIFFS]})


@csrf_exempt
@require_http_methods(["POST"])
def logout(request):
    session = session_factory()
    try:
        _user, token_row = authenticate(session, request)
        if token_row is None:
            return JsonResponse({"error": "unauthorized"}, status=401)
        token_row.revoked_at = datetime.utcnow()
        session.commit()
        return JsonResponse({"status": "ok"})
    finally:
        session.close()


@require_http_methods(["GET"])
def auth_redirect(request):
    """Bridge page: Telegram inline buttons cannot use custom schemes, so the bot's
    'Войти' button points here (https), and this 302/JS-redirects to
    ``monkeyisland://auth?code=...`` (with a manual fallback link)."""
    code = request.GET.get("code", "")
    deep_link = "monkeyisland://auth?code=" + quote(code, safe="")
    html = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Monkey Island VPN</title>"
        f"<script>window.location.replace({json.dumps(deep_link)});</script>"
        "</head>"
        '<body style="background:#090a0f;color:#fff;font-family:-apple-system,'
        'Segoe UI,Roboto,sans-serif;text-align:center;padding:48px 24px">'
        "<h2>Возвращаемся в приложение…</h2>"
        '<p style="color:#9ca3af">Если приложение не открылось автоматически:</p>'
        f'<p><a href="{escape(deep_link)}" style="display:inline-block;'
        "background:#ffc700;color:#000;font-weight:700;padding:14px 28px;"
        'border-radius:28px;text-decoration:none">Открыть приложение</a></p>'
        "</body></html>"
    )
    return HttpResponse(html)
