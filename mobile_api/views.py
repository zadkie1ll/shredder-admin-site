import json
from datetime import datetime, timezone
from urllib.parse import quote

from django.http import HttpResponse, JsonResponse
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

import proto.rwmanager_pb2 as proto
from database import session_factory

from .auth import authenticate, exchange_code
from .subscription import get_rwms_user
from .tariffs import MOBILE_TARIFFS, serialize_tariff


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
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Monkey Island VPN</title>"
        f"<script>window.location.replace({json.dumps(deep_link)});</script>"
        "</head>"
        "<body style=\"background:#090a0f;color:#fff;font-family:-apple-system,"
        "Segoe UI,Roboto,sans-serif;text-align:center;padding:48px 24px\">"
        "<h2>Возвращаемся в приложение…</h2>"
        "<p style=\"color:#9ca3af\">Если приложение не открылось автоматически:</p>"
        f"<p><a href=\"{escape(deep_link)}\" style=\"display:inline-block;"
        "background:#ffc700;color:#000;font-weight:700;padding:14px 28px;"
        "border-radius:28px;text-decoration:none\">Открыть приложение</a></p>"
        "</body></html>"
    )
    return HttpResponse(html)
