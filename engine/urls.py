from django.urls import path
from django.views.generic import TemplateView
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("offer/", views.offer, name="offer"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("update-email/", views.update_email, name="update_email"),
    path("pay/", views.pay, name="pay"),
    path("login/", views.login, name="login"),
    path("login/send-link/", views.send_magic_link, name="send_magic_link"),
    path("login/magic/<uuid:token>/", views.auth_by_magic_link, name="magic_auth"),
    path("login/telegram/<str:token>/", views.auth_by_telegram_link, name="telegram_auth"),
    path("logout/", views.logout, name="logout"),
    path('robots.txt', views.robots_txt, name='robots_txt'),
    path('manifest.json', views.dynamic_manifest),
    # Путь к сервис-воркеру
    path('sw.js', TemplateView.as_view(
        template_name='pwa/sw.js', 
        content_type='application/javascript'
    ), name='sw'),
]
