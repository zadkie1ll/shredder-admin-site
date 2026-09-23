"""Read-only URL surface backed exclusively by shredder-common."""

from django.urls import path

from engine import shredder_admin_views as views

urlpatterns = [
    path("", views.root, name="admin_root"),
    path("health/", views.health, name="health"),
    path("support-admin/login/", views.login_view, name="support_admin_login"),
    path("support-admin/logout/", views.logout_view, name="support_admin_logout"),
    path("support-admin/", views.dashboard, name="support_admin_dashboard"),
    path("support-admin/api/stats/", views.api_stats, name="support_admin_api_stats"),
    path("support-admin/api/users/", views.api_users, name="support_admin_api_users"),
    path(
        "support-admin/api/users/<int:user_id>/",
        views.api_user,
        name="support_admin_api_user",
    ),
    path(
        "support-admin/api/payments/",
        views.api_payments,
        name="support_admin_api_payments",
    ),
    path(
        "support-admin/api/referrals/",
        views.api_referrals,
        name="support_admin_api_referrals",
    ),
]
