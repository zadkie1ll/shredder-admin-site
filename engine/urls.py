from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('second/', views.second, name='second'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('pay/', views.buy, name='buy'),
    path('login/', views.login, name='login'),
    path('login/send-link/', views.send_magic_link, name='send_magic_link'),
    path('login/magic/<uuid:token>/', views.auth_by_magic_link, name='magic_auth'),
]