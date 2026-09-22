"""URL surface for the standalone Shredder administration service."""

from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import path

from engine.urls import urlpatterns as engine_urlpatterns


def admin_root(_request):
    return redirect("support_admin_tickets")


def health(_request):
    return JsonResponse({"status": "ok", "service": "shredder-admin-site"})


urlpatterns = [
    path("", admin_root, name="admin_root"),
    path("health/", health, name="health"),
    *[
        pattern
        for pattern in engine_urlpatterns
        if str(pattern.pattern).startswith("support-admin/")
    ],
]
