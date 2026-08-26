import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

import proto.rwmanager_pb2 as proto
from common.models.db import EmailLoginCode, User
from common.rwms_client import RwmsUnavailableError
from database import session_factory
from engine.rwms_helpers import RwmsSubscriptionOwnershipError

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

# Per-IP limit for POST auth/exchange (defense-in-depth against device-code
# brute force; the 128-bit code space is the primary defense). Sized for the
# app's legit polling (1 req / 3 s ≈ 20/min per device) plus several devices
# behind one NAT. Backed by Django's cache (per-process LocMemCache by default).
EXCHANGE_RATE_WINDOW_SECONDS = 60
EXCHANGE_RATE_LIMIT = 90


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
    seconds_left = (expire_dt - datetime.now(timezone.utc)).total_seconds()
    # Round UP, mirroring the cabinet (engine/views.py): 23h remaining is
    # "1 day", not 0 — timedelta.days truncates and would flip the app's card
    # to "истекла" on the last paid day while the panel is still ACTIVE.
    days_left = int((seconds_left + 86399) // 86400) if seconds_left > 0 else 0
    return expire_dt.isoformat(), days_left


def _status_name(rw):
    if rw is None or not rw.HasField("status"):
        return None
    return proto.UserStatus.Name(rw.status)


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _exchange_rate_limited(request):
    """Sliding-window-ish per-IP counter on Django's cache. Returns True when the
    IP exceeded EXCHANGE_RATE_LIMIT requests in the current window."""
    key = f"mobile_api:exchange:{_client_ip(request)}"
    if cache.add(key, 1, EXCHANGE_RATE_WINDOW_SECONDS):
        return False
    try:
        count = cache.incr(key)
    except ValueError:
        # The key expired between add() and incr() — start a new window.
        cache.add(key, 1, EXCHANGE_RATE_WINDOW_SECONDS)
        return False
    return count > EXCHANGE_RATE_LIMIT


@csrf_exempt
@require_http_methods(["POST"])
def auth_exchange(request):
    if _exchange_rate_limited(request):
        return JsonResponse({"error": "rate_limited"}, status=429)
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
        try:
            rw = get_rwms_user(user.username)
        except RwmsUnavailableError as error:
            # Блип RWMS/панели не должен валить успешный вход: токен выдаём,
            # subscription_url приложение добирает позже через /me.
            logging.warning(
                "mobile_api: RWMS unavailable during auth_exchange for %s: %s",
                user.username,
                error,
            )
            rw = None
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

            try:
                user = provision_trial_user(session, rwms_client(), email)
            except RwmsSubscriptionOwnershipError:
                # Найденная подписка ЯВНО принадлежит другому email — состояние
                # неоднозначное (ALERT уже залогирован). Панель не тронута,
                # аккаунт не создан: отдаём внятное «попробуйте позже», а не
                # чужой доступ и не «неверный код».
                session.rollback()
                return JsonResponse(
                    {
                        "error": "temporarily_unavailable",
                        "message": "Не удалось подготовить аккаунт. "
                        "Попробуйте позже или напишите в поддержку.",
                    },
                    status=503,
                )
            if user is None:
                session.rollback()
                return JsonResponse({"error": "invalid_or_expired_code"}, status=401)
        # Existing user: DO NOT touch RWMS, DO NOT re-provision a trial.

        raw_token = _issue_access_token(session, user, now=now)
        try:
            rw = get_rwms_user(user.username)
        except RwmsUnavailableError as error:
            # Вход уже состоялся — не показываем «нет подписки» из-за блипа
            # панели; subscription_url приложение добирает через /me.
            logging.warning(
                "mobile_api: RWMS unavailable during email verify for %s: %s",
                user.username,
                error,
            )
            rw = None
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
        try:
            rw = get_rwms_user(user.username)
        except RwmsUnavailableError as error:
            # Панель недоступна — статус подписки выяснить нельзя. Отвечаем
            # «временно недоступно» (503), а НЕ null-полями, которые
            # приложение показало бы как «нет подписки/истекла».
            logging.warning(
                "mobile_api: RWMS unavailable during /me for %s: %s",
                user.username,
                error,
            )
            return JsonResponse(
                {
                    "error": "temporarily_unavailable",
                    "message": "Данные временно недоступны, попробуйте позже",
                },
                status=503,
            )
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
