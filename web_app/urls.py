"""
URL configuration for web_app project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path

from mobile_api import views as mobile_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/mobile/v1/", include("mobile_api.urls")),
    # App bridge page (https -> monkeyisland:// deep link). Telegram inline buttons
    # can't use custom schemes, so the bot's "Войти" button points here.
    path("app/auth-redirect", mobile_views.auth_redirect, name="app_auth_redirect"),
    path("", include("engine.urls")),
]
