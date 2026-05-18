from django.urls import path
from django.views.generic import TemplateView
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("vps-direct-sale/", views.vps_direct_sale, name="vps_direct_sale"),
    path("offer/", views.offer, name="offer"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("update-email/", views.update_email, name="update_email"),
    path("confirm-email/<path:token>/", views.confirm_email, name="confirm_email"),
    path(
        "support/tickets/create/",
        views.create_support_ticket,
        name="create_support_ticket",
    ),
    path(
        "support/tickets/<int:ticket_id>/messages/",
        views.create_support_ticket_message,
        name="create_support_ticket_message",
    ),
    path(
        "support/tickets/<int:ticket_id>/messages-json/",
        views.support_ticket_messages_json,
        name="support_ticket_messages_json",
    ),
    path(
        "support/tickets/<int:ticket_id>/close/",
        views.close_support_ticket,
        name="close_support_ticket",
    ),
    path(
        "support/attachments/<int:attachment_id>/",
        views.support_attachment,
        name="support_attachment",
    ),
    path("support-admin/login/", views.support_admin_login, name="support_admin_login"),
    path(
        "support-admin/logout/", views.support_admin_logout, name="support_admin_logout"
    ),
    path("support-admin/", views.support_admin_tickets, name="support_admin_tickets"),
    path(
        "support-admin/tickets-json/",
        views.support_admin_tickets_json,
        name="support_admin_tickets_json",
    ),
    path(
        "support-admin/api/stats/",
        views.support_admin_api_stats,
        name="support_admin_api_stats",
    ),
    path(
        "support-admin/api/stats/source-users/",
        views.support_admin_api_stats_source_users,
        name="support_admin_api_stats_source_users",
    ),
    path(
        "support-admin/api/user-payments/",
        views.support_admin_api_user_payments,
        name="support_admin_api_user_payments",
    ),
    path(
        "support-admin/api/referrals/",
        views.support_admin_api_referrals,
        name="support_admin_api_referrals",
    ),
    path(
        "support-admin/api/payment-info/",
        views.support_admin_api_payment_info,
        name="support_admin_api_payment_info",
    ),
    path(
        "support-admin/api/subscription-manage/",
        views.support_admin_api_subscription_manage,
        name="support_admin_api_subscription_manage",
    ),
    path(
        "support-admin/api/reply-templates/",
        views.support_admin_api_reply_templates,
        name="support_admin_api_reply_templates",
    ),
    path(
        "support-admin/tickets/<int:ticket_id>/",
        views.support_admin_ticket_detail,
        name="support_admin_ticket_detail",
    ),
    path(
        "support-admin/tickets/<int:ticket_id>/messages/",
        views.support_admin_create_message,
        name="support_admin_create_message",
    ),
    path(
        "support-admin/tickets/<int:ticket_id>/messages-json/",
        views.support_admin_ticket_messages_json,
        name="support_admin_ticket_messages_json",
    ),
    path(
        "support-admin/tickets/<int:ticket_id>/close/",
        views.support_admin_close_ticket,
        name="support_admin_close_ticket",
    ),
    path(
        "support-admin/tickets/<int:ticket_id>/reopen/",
        views.support_admin_reopen_ticket,
        name="support_admin_reopen_ticket",
    ),
    path(
        "support-admin/tickets/<int:ticket_id>/delete/",
        views.support_admin_delete_ticket,
        name="support_admin_delete_ticket",
    ),
    path(
        "support-admin/attachments/<int:attachment_id>/",
        views.support_admin_attachment,
        name="support_admin_attachment",
    ),
    path("pay/", views.pay, name="pay"),
    path("login/", views.login, name="login"),
    path("login/send-link/", views.send_magic_link, name="send_magic_link"),
    path("login/google/", views.login_with_google, name="google_login"),
    path("login/google/callback/", views.auth_by_google_callback, name="google_auth"),
    path("login/yandex/", views.login_with_yandex, name="yandex_login"),
    path("login/yandex/callback/", views.auth_by_yandex_callback, name="yandex_auth"),
    path(
        "login/telegram-auth/",
        views.auth_by_telegram_widget,
        name="telegram_widget_auth",
    ),
    path("login/magic/<uuid:token>/", views.auth_by_magic_link, name="magic_auth"),
    path(
        "login/purchase/<str:token>/",
        views.auth_by_purchase_link,
        name="purchase_auth",
    ),
    path(
        "login/telegram/<str:token>/", views.auth_by_telegram_link, name="telegram_auth"
    ),
    path("logout/", views.logout, name="logout"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("manifest.json", views.dynamic_manifest),
    # Путь к сервис-воркеру
    path(
        "sw.js",
        TemplateView.as_view(
            template_name="pwa/sw.js", content_type="application/javascript"
        ),
        name="sw",
    ),
]
