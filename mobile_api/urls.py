from django.urls import path

from . import views

app_name = "mobile_api"

# Mounted at /api/mobile/v1/ (see web_app/urls.py).
urlpatterns = [
    path("auth/exchange", views.auth_exchange, name="auth_exchange"),
    path("auth/email/request", views.auth_email_request, name="auth_email_request"),
    path("auth/email/verify", views.auth_email_verify, name="auth_email_verify"),
    path("me", views.me, name="me"),
    path("tariffs", views.tariffs, name="tariffs"),
    path("auth/logout", views.logout, name="logout"),
]
