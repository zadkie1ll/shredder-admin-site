from django.http import JsonResponse
from django.shortcuts import render
from django.shortcuts import redirect
from django.shortcuts import get_object_or_404
from django.contrib.auth import login as auth_login
from django.contrib.auth import SESSION_KEY
from django.contrib.auth import BACKEND_SESSION_KEY
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from common.models.db import User
from common.models.db import MagicToken
from common.models.tariff import Tariff
from common.models.tariff import ALL_TARIFFS
from common.models.tariff import OneMonthTariff
from common.models.tariff import OneYearTariff
from common.models.tariff import ThreeMonthsTariff

from database import session_factory

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
                magic = MagicToken(user_id=user.id)
                session.add(magic)
                session.commit()
                
                # Формируем ссылку (в реальности замени на свой домен)
                link = f"http://localhost:8000/login/magic/{magic.token}/"
                
                # Отправляем письмо
                send_mail(
                    'Твой вход на Остров Свободы',
                    f'Нажми сюда, чтобы войти: {link}',
                    'monkeyislandservice@yandex.ru',
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
        magic = session.query(MagicToken).filter(MagicToken.token == token).first()
        
        if magic and magic.is_valid():
            user = session.query(User).filter(User.id == magic.user_id).first()
            
            if user:
                # Вручную авторизуем пользователя в сессии Django
                # (Это то, что делает login(), но без проверки _meta)
                request.session[SESSION_KEY] = str(user.id) # ID пользователя
                request.session[BACKEND_SESSION_KEY] = 'engine.auth_backend.SQLAlchemyBackend'
                # Хэш пароля нам не нужен, так как вход по ссылке, 
                # но если Django будет его требовать, можно поставить заглушку:
                request.session[HASH_SESSION_KEY] = "" 
                
                # Помечаем токен использованным
                magic.is_used = True
                session.commit()
                
                # Важно: после ручного обновления сессии ее нужно сохранить
                request.session.modified = True
                
                return redirect('dashboard')
        
        return render(request, 'login.html', {'error': 'Ссылка истекла или неверна'})
    finally:
        session.close()

def index(request):
    return render(request, 'index.html', {'tariffs': ACTUAL_TARIFFS})

@login_required(login_url='/login/') # Выкинет анонима на логин
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