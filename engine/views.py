import uuid
import hashlib
import logging
from datetime import datetime
from datetime import timezone
from datetime import timedelta
from django.http import JsonResponse
from django.conf import settings
from django.shortcuts import render
from django.shortcuts import redirect
from django.contrib.auth import logout as auth_logout
from django.contrib.auth import SESSION_KEY
from django.contrib.auth import BACKEND_SESSION_KEY
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from common.models.db import User
from common.models.db import MagicToken
from common.models.tariff import Tariff
from common.models.tariff import OneMonthTariff
from common.models.tariff import OneYearTariff
from common.models.tariff import ThreeMonthsTariff
from common.rwms_client_sync import RwmsClientSync

from .encrypt_happ_url import encrypt_happ_url1
from database import session_factory
from .rwms_helpers import create_user

ACTUAL_TARIFFS: list[Tariff] = [
    OneMonthTariff(),
    ThreeMonthsTariff(),
    OneYearTariff(),
]

rwms_client = RwmsClientSync("127.0.0.1", 50052)


def send_magic_link(request):
    if request.method == "POST":
        email = request.POST.get("email")

        session = session_factory()

        try:
            with session.begin():
                user = session.query(User).filter(User.email == email).first()

                if not user:
                    username = str(uuid.uuid4().hex)

                    user = User(
                        email=email,
                        username=username,
                        expire_at=datetime.now(timezone.utc).replace(tzinfo=None)
                        + timedelta(days=1),
                    )
                    create_user(rwms_client=rwms_client, username=username)
                    session.add(user)
                    session.flush()
                    logging.info(f"User with username {user.username} and email {email} was successfully created")
                else:
                    logging.info(f"Found user with username {user.username} and email {email} to authorize")

                logging.info(f"Authorizing user with username {user.username} and email {email}")

                # Создаем токен
                magic = MagicToken(user_id=user.id)
                session.add(magic)

            # Формируем ссылку (в реальности замени на свой домен)
            link = f"http://localhost:8000/login/magic/{magic.token}/"
            link = f"https://7935-203-23-179-183.ngrok-free.app/login/magic/{magic.token}/"

            # Отправляем письмо
            send_mail(
                "Твой вход на Остров Свободы",
                f"Нажми сюда, чтобы войти: {link}",
                "monkeyislandservice@yandex.ru",
                [email],
                fail_silently=False,
            )

        except Exception as e:
            logging.error(f"Error during sign-up/login: {e}")

        finally:
            session.close()

        # Мы всегда возвращаем успех, чтобы не "палить" наличие email в базе (защита от парсинга)
        return JsonResponse({"status": "ok"})


def auth_by_magic_link(request, token):
    session = session_factory()
    try:
        magic = session.query(MagicToken).filter(MagicToken.token == token).first()

        if magic and magic.is_valid():
            user = session.query(User).filter(User.id == magic.user_id).first()

            if user:
                # Вручную авторизуем пользователя в сессии Django
                # (Это то, что делает login(), но без проверки _meta)
                request.session[SESSION_KEY] = str(user.id)  # ID пользователя
                request.session[BACKEND_SESSION_KEY] = (
                    "engine.auth_backend.SQLAlchemyBackend"
                )
                # Хэш пароля нам не нужен, так как вход по ссылке,
                # но если Django будет его требовать, можно поставить заглушку:
                request.session[HASH_SESSION_KEY] = ""

                # Помечаем токен использованным
                magic.is_used = True
                session.commit()

                # Важно: после ручного обновления сессии ее нужно сохранить
                request.session.modified = True

                return redirect("dashboard")

        return render(request, "login.html", {"error": "Ссылка истекла или неверна"})
    finally:
        session.close()


def index(request):
    return render(request, "index.html", {"tariffs": ACTUAL_TARIFFS})


@login_required(login_url="/login/")
def dashboard(request):
    user = request.user

    # Если зашел из ТГ (уже есть ID), но почты нет — просим почту
    if user.telegram_id and not user.email:
        return render(request, "dashboard_collect_email.html", {"user": user})

    # Если зашел по почте и ТГ еще не привязан — готовим ссылку для привязки
    tg_bind_link = None
    if not user.telegram_id:
        # Создаем короткую подпись на основе ID пользователя и SECRET_KEY
        token = hashlib.md5(f"{user.id}{settings.SECRET_KEY}".encode()).hexdigest()[:8]
        tg_bind_link = f"https://t.me/easybirdvpnbot?start=bind_{user.id}_{token}"

    subscription = rwms_client.get_user_by_username(user.username)

    plain_subscription_url = subscription.subscription_url if subscription else "Не удалось получить ключ доступа. Пожалуйста, свяжитесь с поддержкой."
    happ_subscription_url = encrypt_happ_url1(subscription.subscription_url + "/custom-json") if subscription else "Не удалось получить ключ доступа. Пожалуйста, свяжитесь с поддержкой."

    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "tg_bind_link": tg_bind_link,
            "plain_subscription_url": plain_subscription_url,
            "happ_subscription_url": happ_subscription_url,
        }
    )


def update_email(request):
    if request.method == "POST" and request.user.is_authenticated:
        new_email = request.POST.get("email").lower().strip()

        session = session_factory()
        try:
            db_user = session.query(User).filter(User.id == request.user.id).first()
            if db_user:
                db_user.email = new_email
                session.commit()

                # Обновляем email в текущем объекте пользователя в памяти
                request.user.email = new_email

            return redirect("dashboard")
        finally:
            session.close()

    return redirect("login")


def login(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "login.html")


def logout(request):
    auth_logout(request)
    return redirect("index")


def buy(request):
    if request.method == "POST":
        email = request.POST.get("email").lower().strip()
        tariff_id = request.POST.get("tariff_id")

        # Тут будет логика создания платежа ЮKassa, которую мы обсуждали.
        # Пока просто выведем в консоль для теста:
        print(f"Заказ от {email} на тариф {tariff_id}")

        return redirect("index")  # Временно редиректим обратно
