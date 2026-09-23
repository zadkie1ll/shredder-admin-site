"""Standalone, read-only Shredder administration views."""

import hmac
import logging
from functools import wraps

from django.conf import settings
from django.core.signing import salted_hmac
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from engine import shredder_admin_repository as repository

SESSION_KEY = "shredder_admin_authenticated"
SESSION_USER_KEY = "shredder_admin_username"
SESSION_AUTH_HASH_KEY = "shredder_admin_auth_hash"


def _auth_fingerprint(username, password):
    return salted_hmac(
        "shredder-admin-session",
        f"{username}\0{password}",
        secret=settings.SECRET_KEY,
    ).hexdigest()


def _clear_admin_session(request):
    for key in (SESSION_KEY, SESSION_USER_KEY, SESSION_AUTH_HASH_KEY):
        request.session.pop(key, None)


def _session_is_valid(request):
    if not request.session.get(SESSION_KEY):
        return False
    username = settings.SHREDDER_ADMIN_SITE_USERNAME
    password = settings.SUPPORT_ADMIN_PASSWORD
    expected = _auth_fingerprint(username, password)
    valid = hmac.compare_digest(
        request.session.get(SESSION_USER_KEY, ""), username
    ) and hmac.compare_digest(request.session.get(SESSION_AUTH_HASH_KEY, ""), expected)
    if not valid:
        _clear_admin_session(request)
    return valid


def require_admin(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not _session_is_valid(request):
            if request.path.startswith("/support-admin/api/"):
                return JsonResponse(
                    {"status": "error", "message": "authentication required"},
                    status=401,
                )
            return redirect("support_admin_login")
        return view(request, *args, **kwargs)

    return wrapped


def root(_request):
    return redirect("support_admin_dashboard")


def health(_request):
    return JsonResponse(
        {"status": "ok", "service": "shredder-admin-site", "mode": "read-only"}
    )


@require_http_methods(["GET", "POST"])
def login_view(request):
    error = None
    if request.method == "POST":
        username_ok = hmac.compare_digest(
            request.POST.get("login", ""), settings.SHREDDER_ADMIN_SITE_USERNAME
        )
        password = settings.SUPPORT_ADMIN_PASSWORD
        password_ok = bool(password) and hmac.compare_digest(
            request.POST.get("password", ""), password
        )
        if username_ok and password_ok:
            request.session.cycle_key()
            request.session[SESSION_KEY] = True
            request.session[SESSION_USER_KEY] = settings.SHREDDER_ADMIN_SITE_USERNAME
            request.session[SESSION_AUTH_HASH_KEY] = _auth_fingerprint(
                settings.SHREDDER_ADMIN_SITE_USERNAME, password
            )
            return redirect("support_admin_dashboard")
        error = "Неверный логин или пароль"
    return render(request, "shredder_admin/login.html", {"error": error})


@require_GET
def logout_view(request):
    request.session.flush()
    return redirect("support_admin_login")


@require_admin
@require_GET
def dashboard(request):
    return render(request, "shredder_admin/dashboard.html")


def _read_json(loader):
    try:
        return JsonResponse({"status": "ok", **loader()})
    except Exception:
        logging.exception("read-only admin query failed")
        return JsonResponse(
            {"status": "error", "message": "Данные временно недоступны"}, status=503
        )


@require_admin
@require_GET
def api_stats(_request):
    return _read_json(lambda: {"stats": repository.load_stats()})


@require_admin
@require_GET
def api_users(request):
    try:
        limit = max(1, min(int(request.GET.get("limit", 50)), 100))
    except ValueError:
        limit = 50
    return _read_json(
        lambda: {"users": repository.search_users(request.GET.get("q", ""), limit)}
    )


@require_admin
@require_GET
def api_user(_request, user_id):
    try:
        user = repository.load_user(user_id)
    except Exception:
        logging.exception("read-only admin user query failed")
        return JsonResponse(
            {"status": "error", "message": "Данные временно недоступны"}, status=503
        )
    if user is None:
        return JsonResponse(
            {"status": "error", "message": "Пользователь не найден"}, status=404
        )
    return JsonResponse({"status": "ok", "user": user})


@require_admin
@require_GET
def api_payments(request):
    try:
        limit = max(1, min(int(request.GET.get("limit", 50)), 100))
    except ValueError:
        limit = 50
    return _read_json(
        lambda: {
            "payments": repository.search_payments(
                query=request.GET.get("q", ""),
                status=request.GET.get("status", ""),
                limit=limit,
            )
        }
    )


@require_admin
@require_GET
def api_referrals(request):
    try:
        limit = max(1, min(int(request.GET.get("limit", 50)), 100))
    except ValueError:
        limit = 50
    return _read_json(
        lambda: {
            "referrers": repository.search_referrers(
                query=request.GET.get("q", ""), limit=limit
            )
        }
    )
