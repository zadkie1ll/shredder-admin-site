from django.urls import path

from . import views

app_name = "mobile_api"

# Mounted at /api/mobile/v1/ (see web_app/urls.py).
urlpatterns = [
    path("auth/exchange", views.auth_exchange, name="auth_exchange"),
    path("me", views.me, name="me"),
    path("tariffs", views.tariffs, name="tariffs"),
    path("auth/logout", views.logout, name="logout"),
]
