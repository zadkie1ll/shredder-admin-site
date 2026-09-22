"""Standalone, read-only Shredder administration views."""

import hmac
import logging
from functools import wraps
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods
from engine import shredder_admin_repository as repository

SESSION_KEY = "shredder_admin_authenticated"


def require_admin(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.session.get(SESSION_KEY):
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
