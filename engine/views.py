import uuid
import hashlib
import logging
import resend
from datetime import datetime
from datetime import timedelta
from django.http import JsonResponse
from django.conf import settings
from django.shortcuts import render
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.contrib.auth import SESSION_KEY
from django.contrib.auth import BACKEND_SESSION_KEY
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from sqlalchemy import func
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from common.models.db import User
from common.models.db import EventLog
from common.models.db import ReferralBonus
from common.models.db import ReferralBonusType
from common.models.db import ReferralType
from common.models.db import UserTrafficProgress
from common.models.db import YkRecurrentPayment
from common.models.db import MagicToken
from common.models import analytics_event
from common.models.tariff import Tariff
from common.models.tariff import OneMonthTariff
from common.models.tariff import OneYearTariff
from common.models.tariff import ThreeMonthsTariff
from common.rwms_client_sync import RwmsClientSync

from .rwms_helpers import create_user
from .encrypt_happ_url import encrypt_happ_url1
from .sql_helpers import save_wata_invoice

from database import session_factory
from engine.payments import create_yk_payment_sync
from engine.payments import create_wata_payment_sync
import proto.rwmanager_pb2 as proto

ACTUAL_TARIFFS: list[Tariff] = [
    OneMonthTariff(),
    ThreeMonthsTariff(),
    OneYearTariff(),
]

rwms_client = RwmsClientSync(settings.RWMS_HOST, settings.RWMS_PORT)


def normalize_host(host):
    return host.split(":", 1)[0].lower()


def is_known_site_host(host):
    normalized_host = normalize_host(host)
    return (
        normalized_host in settings.CABINET_DOMAINS
        or normalized_host in settings.NEUTRAL_DOMAINS
        or normalized_host in settings.PROMO_DOMAINS
    )


def get_site_role(request):
    host = normalize_host(request.get_host())
    if host in settings.CABINET_DOMAINS:
        return "cabinet"
    if host in settings.NEUTRAL_DOMAINS:
        return "neutral"
    if host in settings.PROMO_DOMAINS:
        return "promo"
    return "promo"


def get_current_base_url(request):
    if not is_known_site_host(request.get_host()):
        fallback_url = settings.DEFAULT_CABINET_DOMAIN.rstrip("/")
        if "://" not in fallback_url:
            fallback_url = f"https://{fallback_url}"
        return fallback_url

    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{request.get_host()}"


def get_pwa_context():
    return {
        "pwa_mirror_source_url": settings.PWA_MIRROR_SOURCE_URL,
    }


def parse_int(value):
    if value in (None, ""):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        logging.warning(f"failed to parse integer tracking value {value}")
        return None


def capture_tracking_params(request):
    for key in ("ymid", "ts", "a"):
        value = request.GET.get(key)
        if value:
            request.session[f"tracking_{key}"] = value


def get_tracking_value(request, *keys):
    for source in (request.POST, request.GET):
        for key in keys:
            value = source.get(key)
            if value:
                return value

    for key in keys:
        value = request.session.get(f"tracking_{key}")
        if value:
            return value

    return None


def get_registration_context(request, db_session):
    referrer_username = get_tracking_value(request, "a")
    referrer = None

    if referrer_username:
        referrer = (
            db_session.query(User).filter(User.username == referrer_username).first()
        )

    return {
        "referrer": referrer,
        "traffic_source": parse_int(get_tracking_value(request, "ts")),
        "ymid": parse_int(get_tracking_value(request, "ymid")),
    }


def add_user_to_traffic_progress(db_session, user):
    exists = (
        db_session.query(UserTrafficProgress.id)
        .filter(UserTrafficProgress.user_id == user.id)
        .first()
    )

    if not exists:
        db_session.add(UserTrafficProgress(user_id=user.id))


def add_event_log(db_session, user, event):
    db_session.add(
        EventLog(
            user_id=user.id,
            event_type=event.event_type,
            event_payload=event.model_dump(),
        )
    )


def get_last_traffic_source(db_session, user):
    event = (
        db_session.query(EventLog)
        .filter(
            (EventLog.user_id == user.id)
            & (
                EventLog.event_type.in_(
                    ["subscription_created", "traffic_source_changed"]
                )
            )
        )
        .order_by(EventLog.timestamp.desc())
        .first()
    )

    if not event:
        return False, None

    return True, event.event_payload.get("traffic_source")


def sync_existing_user_tracking(db_session, user, traffic_source, ymid):
    if ymid is not None:
        user.ymid = ymid

    found_event, prev_traffic_source = get_last_traffic_source(db_session, user)
    if found_event and traffic_source != prev_traffic_source:
        logging.info(
            f"for site user {user.username} detected change of traffic source, "
            f"previous {'Direct' if prev_traffic_source is None else prev_traffic_source} "
            f"new {'Direct' if traffic_source is None else traffic_source}"
        )
        add_event_log(
            db_session,
            user,
            analytics_event.TrafficSourceChanged(traffic_source=traffic_source),
        )


def create_site_user(db_session, email, request):
    context = get_registration_context(request, db_session)
    referrer = context["referrer"]
    username = str(uuid.uuid4().hex)
    trial_period_days = (
        settings.SITE_REFERRAL_TRIAL_PERIOD_DAYS
        if referrer
        else settings.SITE_TRIAL_PERIOD_DAYS
    )

    rw_user = create_user(
        rwms_client=rwms_client,
        username=username,
        trial_period_days=trial_period_days,
        from_referrer=referrer is not None,
        email=email,
    )

    if rw_user is None:
        raise RuntimeError(f"creating subscription for site user {email} was failed")

    expire_at = None
    if rw_user.HasField("expire_at"):
        expire_at = rw_user.expire_at.ToDatetime().replace(tzinfo=None)

    user = User(
        email=email,
        username=username,
        expire_at=expire_at,
        ymid=context["ymid"],
        referred_by_id=referrer.id if referrer else None,
        referral_type=ReferralType.STANDARD if referrer else None,
    )
    db_session.add(user)
    db_session.flush()

    add_user_to_traffic_progress(db_session, user)
    add_event_log(
        db_session,
        user,
        analytics_event.SubscriptionCreated(
            traffic_source=context["traffic_source"]
        ),
    )

    logging.info(
        f"User with username {user.username} and email {email} was successfully created"
    )

    return user


def render_login(request, context=None, status=200):
    capture_tracking_params(request)
    payload = get_pwa_context()
    if context:
        payload.update(context)
    return render(request, "login.html", payload, status=status)


def render_collect_email(request, user, error=None, email=""):
    return render(
        request,
        "dashboard_collect_email.html",
        {
            "user": user,
            "error": error,
            "email": email,
        },
    )


def send_magic_link(request):
    if request.method == "POST":
        capture_tracking_params(request)
        email_raw = request.POST.get("email", "")
        email = email_raw.lower().strip()
        auth_base_url = get_current_base_url(request)
        entry_host = normalize_host(request.get_host())

        if not email:
            return JsonResponse({"status": "ok"})

        db_session = session_factory()

        try:
            with db_session.begin():
                user = db_session.query(User).filter(User.email == email).first()

                if not user:
                    user = create_site_user(db_session, email, request)
                else:
                    registration_context = get_registration_context(request, db_session)
                    sync_existing_user_tracking(
                        db_session,
                        user,
                        registration_context["traffic_source"],
                        registration_context["ymid"],
                    )
                    logging.info(
                        f"Found user with username {user.username} and email {email} to authorize"
                    )

                logging.info(
                    f"Authorizing user with username {user.username} and email {email}"
                )

                # Создаем токен
                magic = MagicToken(user_id=user.id)
                db_session.add(magic)

            # Возвращаем пользователя в кабинет на том же домене, где он начал вход.
            link = f"{auth_base_url}/login/magic/{magic.token}/"

            logging.info(
                "Magic link requested from host %s, target auth host is %s for %s",
                entry_host,
                auth_base_url,
                email,
            )

            # Формируем контекст для шаблона
            context = {
                'link': link,
            }

            # Рендерим HTML
            html_message = render_to_string('emails/magic_link.html', context)
            # Создаем текстовую версию (на случай, если клиент не поддерживает HTML)
            plain_message = strip_tags(html_message)

            subject = "Ссылка для входа в личный кабинет"

            if settings.EMAIL_PROVIDER.lower() == "resend":
                resend.api_key = settings.RESEND_API_KEY
                resend.Emails.send(
                    {
                        "from": settings.RESEND_FROM_EMAIL,
                        "to": [email],
                        "subject": subject,
                        "html": html_message,
                        "text": plain_message,
                    }
                )
            else:
                # Отправляем письмо через SMTP
                send_mail(
                    subject=subject,
                    message=plain_message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[email],
                    html_message=html_message,
                    fail_silently=False,
                )

        except Exception as e:
            logging.error(f"Error during sign-up/login: {e}")

        finally:
            db_session.close()

        # Мы всегда возвращаем успех, чтобы не "палить" наличие email в базе (защита от парсинга)
        return JsonResponse({"status": "ok"})


def auth_by_magic_link(request, token):
    session = session_factory()
    try:
        expires_after = datetime.utcnow() - timedelta(minutes=15)
        user_id = session.execute(
            update(MagicToken)
            .where(
                MagicToken.token == token,
                MagicToken.is_used.is_(False),
                MagicToken.created_at > expires_after,
            )
            .values(is_used=True)
            .returning(MagicToken.user_id)
        ).scalar_one_or_none()

        if user_id:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                session.rollback()
                logging.warning(f"magic token {token} points to missing user {user_id}")
                return render_login(request, {"error": "Ссылка истекла или неверна"})

            session.commit()

            # Вручную авторизуем пользователя в сессии Django
            # (Это то, что делает login(), но без проверки _meta)
            request.session[SESSION_KEY] = str(user.id)  # ID пользователя
            request.session[BACKEND_SESSION_KEY] = (
                "engine.auth_backend.SQLAlchemyBackend"
            )
            # Хэш пароля нам не нужен, так как вход по ссылке,
            # но если Django будет его требовать, можно поставить заглушку:
            request.session[HASH_SESSION_KEY] = ""

            # Важно: после ручного обновления сессии ее нужно сохранить
            request.session.modified = True

            return redirect("dashboard")

        session.rollback()

        return render_login(request, {"error": "Ссылка истекла или неверна"})
    finally:
        session.close()


def index(request):
    capture_tracking_params(request)
    site_role = get_site_role(request)

    if site_role == "cabinet":
        if request.user.is_authenticated:
            return redirect("dashboard")
        return render_login(request)

    if site_role == "neutral":
        return render(request, "index_neutral.html", {"tariffs": ACTUAL_TARIFFS})

    return render(request, "index.html", {"tariffs": ACTUAL_TARIFFS})


@login_required(login_url="/login/")
def dashboard(request):
    user = request.user

    tg_bot = settings.TG_BOT_USERNAME

    # Если зашел из ТГ (уже есть ID), но почты нет — просим почту
    if user.telegram_id and not user.email:
        return render_collect_email(request, user)

    # Если зашел по почте и ТГ еще не привязан — готовим ссылку для привязки
    tg_bind_link = None
    if not user.telegram_id:
        # Создаем короткую подпись на основе ID пользователя и SECRET_KEY
        token = hashlib.md5(f"{user.id}{settings.SECRET_KEY}".encode()).hexdigest()[:8]
        tg_bind_link = f"https://t.me/{tg_bot}?start=bind_{user.id}_{token}"

    session = session_factory()
    try:
        has_recurrent = (
            session.query(func.count(YkRecurrentPayment.id))
            .filter(YkRecurrentPayment.user_id == user.id)
            .scalar()
        )

        ref_invited_count = (
            session.query(func.count(User.id))
            .filter(User.referred_by_id == user.id)
            .scalar()
        )

        ref_connected_count = (
            session.query(func.count(ReferralBonus.id))
            .filter(
                (ReferralBonus.referrer_id == user.id)
                & (ReferralBonus.bonus_type == ReferralBonusType.TRAFFIC)
            )
            .scalar()
        )

        ref_purchased_count = (
            session.query(func.count(ReferralBonus.id))
            .filter(
                (ReferralBonus.referrer_id == user.id)
                & (ReferralBonus.bonus_type == ReferralBonusType.PURCHASE)
            )
            .scalar()
        )
    finally:
        session.close()

    bonus_days = ref_connected_count * 10 + ref_purchased_count * 30
    subscription = rwms_client.get_user_by_username(user.username)

    plain_subscription_url = (
        subscription.subscription_url
        if subscription
        else "Не удалось получить ключ доступа. Пожалуйста, свяжитесь с поддержкой."
    )
    happ_subscription_url = (
        encrypt_happ_url1(subscription.subscription_url + "/custom-json")
        if subscription
        else "Не удалось получить ключ доступа. Пожалуйста, свяжитесь с поддержкой."
    )

    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "tg_bind_link": tg_bind_link,
            "plain_subscription_url": plain_subscription_url,
            "happ_subscription_url": happ_subscription_url,
            "ref_invited_count": ref_invited_count,
            "ref_connected_count": ref_connected_count,
            "ref_purchased_count": ref_purchased_count,
            "bonus_days": bonus_days,
            "tariffs": ACTUAL_TARIFFS,
            "has_recurrent": has_recurrent,
            "seconds_left": (
                user.time_until_expiration.total_seconds()
                if user.time_until_expiration
                else -1
            ),
            "referral_link": f"https://t.me/{tg_bot}?start=a{user.username}",
            "site_referral_link": f"{get_current_base_url(request)}/?a={user.username}",
        },
    )


def update_email(request):
    if request.method != "POST" or not request.user.is_authenticated:
        return redirect("login")

    new_email = request.POST.get("email", "").lower().strip()
    if not new_email:
        return render_collect_email(
            request,
            request.user,
            error="Введите email.",
            email=new_email,
        )

    session = session_factory()
    db_user = None
    try:
        db_user = session.query(User).filter(User.id == request.user.id).first()
        if not db_user:
            logging.warning(f"user {request.user.id} not found while updating email")
            auth_logout(request)
            return redirect("login")

        existing_user = (
            session.query(User)
            .filter(User.email == new_email, User.id != db_user.id)
            .first()
        )
        if existing_user:
            return render_collect_email(
                request,
                request.user,
                error="Этот email уже привязан к другому аккаунту.",
                email=new_email,
            )

        db_user.email = new_email
        session.commit()

        # Обновляем email в текущем объекте пользователя в памяти
        request.user.email = new_email
        username = db_user.username

    except IntegrityError:
        session.rollback()
        logging.warning(
            f"email {new_email} already exists while updating user {request.user.id}"
        )
        return render_collect_email(
            request,
            request.user,
            error="Этот email уже привязан к другому аккаунту.",
            email=new_email,
        )
    except Exception as e:
        session.rollback()
        logging.exception(f"failed to update email for user {request.user.id}: {e}")
        return render_collect_email(
            request,
            request.user,
            error="Не удалось привязать email. Попробуйте еще раз.",
            email=new_email,
        )
    finally:
        session.close()

    try:
        subscription = rwms_client.get_user_by_username(username)
        if subscription:
            response = rwms_client.update_user(
                proto.UpdateUserRequest(uuid=subscription.uuid, email=new_email)
            )
            if response is None:
                logging.warning(
                    f"failed to update rwms email for user {request.user.id}"
                )
    except Exception as e:
        logging.exception(f"failed to sync rwms email for user {request.user.id}: {e}")

    return redirect("dashboard")


def login(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    return render_login(request)


def logout(request):
    auth_logout(request)
    return redirect("index")


def pay(request):
    if request.method == "POST":
        capture_tracking_params(request)
        email_raw = request.POST.get("email")
        if not email_raw:
            messages.error(request, "Email обязателен")
            return redirect("dashboard")

        email = email_raw.lower().strip()
        tariff_id = request.POST.get("tariff_id")

        if not email or not tariff_id:
            messages.error(request, "Не указан email или тариф")
            return redirect("dashboard")

        tariff = next((t for t in ACTUAL_TARIFFS if t.db_tariff_id == tariff_id), None)
        if not tariff:
            messages.error(request, "Выбранный тариф не найден")
            return redirect("dashboard")

        db_session = session_factory()
        try:
            # Ищем или создаем пользователя
            user = db_session.query(User).filter(User.email == email).first()
            if not user:
                user = create_site_user(db_session, email, request)
            else:
                registration_context = get_registration_context(request, db_session)
                sync_existing_user_tracking(
                    db_session,
                    user,
                    registration_context["traffic_source"],
                    registration_context["ymid"],
                )

            if settings.PAYMENT_GATEWAY.lower() == "wata":
                json = create_wata_payment_sync(
                    wata_host=settings.WATA_HOST,
                    wata_token=settings.WATA_TOKEN,
                    tariff=tariff,
                )

                confirmation_url = json["url"]

                save_wata_invoice(
                    session=db_session,
                    invoice_json=json,
                    tariff_id=tariff.db_tariff_id,
                    email=email,
                )

                logging.info(
                    f"an invoice for the {tariff.db_tariff_id} tariff has been created for "
                    f"{email}, confirmation url: {confirmation_url}"
                )
            else:
                confirmation_url = create_yk_payment_sync(
                    shop_id=settings.YOOKASSA_SHOP_ID,
                    secret=settings.YOOKASSA_SECRET_KEY,
                    tariff=tariff,
                    username=user.username,
                    telegram_id=user.telegram_id or 0,
                )

            db_session.commit()
            return redirect(confirmation_url)

        except Exception as e:
            db_session.rollback()
            logging.error(f"Pay error: {e}")
            messages.error(request, "Ошибка платежной системы")
            return redirect("dashboard")
        finally:
            db_session.close()

    return redirect("index")

def dynamic_manifest(request):
    data = {
        "name": "VPN Monkey Island",
        "short_name": "VPN Monkey Island",
        "id": "/",
        "start_url": "/dashboard/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#1a1a1a",
        "theme_color": "#ff9900",
        "icons": [
            {"src": "/static/icons/icon-192x192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512x512.png", "sizes": "512x512", "type": "image/png"}
        ]
    }
    return JsonResponse(data)
