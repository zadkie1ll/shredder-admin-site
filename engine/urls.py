from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("update-email/", views.update_email, name="update_email"),
    path("pay/", views.pay, name="pay"),
    path("login/", views.login, name="login"),
    path("login/send-link/", views.send_magic_link, name="send_magic_link"),
    path("login/magic/<uuid:token>/", views.auth_by_magic_link, name="magic_auth"),
    path("logout/", views.logout, name="logout"),
]
