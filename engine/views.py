from django.http import JsonResponse
from django.shortcuts import render
from django.shortcuts import redirect
from django.shortcuts import get_object_or_404
from django.contrib.auth import login
from django.core.mail import send_mail
from common.models.db import User
from common.models.tariff import Tariff
from common.models.tariff import ALL_TARIFFS
from common.models.tariff import OneMonthTariff
from common.models.tariff import OneYearTariff
from common.models.tariff import ThreeMonthsTariff

from database import session_factory
from .models import MagicToken

ACTUAL_TARIFFS: list[Tariff] = [
    OneMonthTariff(),
    ThreeMonthsTariff(),
    OneYearTariff(),
]

def send_magic_link(request):
    if request.method == 'POST':
        email = request.POST.get('email')

        session = session_factory()

        try:
            user = session.query(User).filter(User.email == email).first()

            if user:
                # Создаем токен
                magic = MagicToken.objects.create(user_id=user.id)
                
                # Формируем ссылку (в реальности замени на свой домен)
                link = f"http://localhost:8000/login/magic/{magic.token}/"
                
                # Отправляем письмо
                send_mail(
                    'Твой вход на Остров Свободы',
                    f'Нажми сюда, чтобы войти: {link}',
                    'noreply@monkey-island-vpn.com',
                    [email],
                    fail_silently=False,
                )

            # Мы всегда возвращаем успех, чтобы не "палить" наличие email в базе (защита от парсинга)
            return JsonResponse({'status': 'ok'})

        finally:
            session.close()
    
def auth_by_magic_link(request, token):
    session = session_factory()
    try:
        # Ищем токен и сразу джойним юзера
        magic = session.query(MagicToken).filter(MagicToken.token == token).first()
        
        if magic and magic.is_valid():
            # Авторизуем
            user_id = magic.user_id
            magic.is_used = True
            session.commit()
            
            # Сохраняем в сессию Django
            request.session['user_id'] = user_id 
            return redirect('dashboard')
        else:
            return render(request, 'login.html', {'error': 'Ссылка истекла или неверна'})
    finally:
        session.close()

def index(request):
    return render(request, 'index.html', {'tariffs': ACTUAL_TARIFFS})

def dashboard(request):
    return render(request, 'dashboard.html')

def login(request):
    return render(request, 'login.html')

def second(request):
    return render(request, 'second.html', {'tariffs': ALL_TARIFFS})

def buy(request):
    if request.method == "POST":
        email = request.POST.get("email").lower().strip()
        tariff_id = request.POST.get("tariff_id")
        
        # Тут будет логика создания платежа ЮKassa, которую мы обсуждали.
        # Пока просто выведем в консоль для теста:
        print(f"Заказ от {email} на тариф {tariff_id}")
        
        return redirect('index') # Временно редиректим обратно