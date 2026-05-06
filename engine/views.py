import uuid
import hmac
import hashlib
import logging
import resend
from datetime import datetime
from datetime import timedelta
from urllib.parse import urlsplit
from django.http import HttpResponse
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
from django.templatetags.static import static
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
from common.models.db import TelegramLoginToken
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
TRACKING_PARAM_KEYS = ("ymid", "ts", "a")

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


def format_days_ru(days):
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} день"
    if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        return f"{days} дня"
    return f"{days} дней"


def get_display_trial_period_days_for_request(request):
    return (
        settings.SITE_REFERRAL_TRIAL_PERIOD_DAYS
        if get_tracking_value(request, "a")
        else settings.SITE_TRIAL_PERIOD_DAYS
    )


def parse_int(value):
    if value in (None, ""):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        logging.warning(f"failed to parse integer tracking value {value}")
        return None


def capture_tracking_params(request):
    for key in TRACKING_PARAM_KEYS:
        value = request.GET.get(key)
        if value:
            request.session[f"tracking_{key}"] = value


def get_tracking_params(request):
    tracking_params = {}
    for key in TRACKING_PARAM_KEYS:
        value = get_tracking_value(request, key)
        if value:
            tracking_params[key] = value

    return tracking_params


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


def add_event_log_once(db_session, user, event):
    exists = (
        db_session.query(EventLog.id)
        .filter(
            (EventLog.user_id == user.id)
            & (EventLog.event_type == event.event_type)
        )
        .first()
    )

    if exists:
        return False

    add_event_log(db_session, user, event)
    return True


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
            traffic_source=context["traffic_source"],
            creation_channel="site",
        ),
    )

    logging.info(
        f"User with username {user.username} and email {email} was successfully created"
    )

    return user


def render_login(request, context=None, status=200):
    capture_tracking_params(request)
    payload = get_pwa_context()
    payload["tracking_params"] = get_tracking_params(request)
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


def authorize_user_session(request, user):
    # Вручную авторизуем пользователя в сессии Django
    # (Это то, что делает login(), но без проверки _meta)
    request.session[SESSION_KEY] = str(user.id)
    request.session[BACKEND_SESSION_KEY] = "engine.auth_backend.SQLAlchemyBackend"

    # Хэш пароля нам не нужен, так как вход по ссылке,
    # но если Django будет его требовать, можно поставить заглушку:
    request.session[HASH_SESSION_KEY] = ""

    # Важно: после ручного обновления сессии ее нужно сохранить
    request.session.modified = True


def hash_telegram_login_token(token):
    payload = f"telegram-login:{token}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def send_magic_link_email(email, link, *, subject=None, template_context=None):
    context = {"link": link}
    if template_context:
        context.update(template_context)

    if "icon_url" not in context:
        parsed_link = urlsplit(link)
        if parsed_link.scheme and parsed_link.netloc:
            context["icon_url"] = (
                f"{parsed_link.scheme}://{parsed_link.netloc}"
                f"{static('icons/icon-192x192.png')}"
            )

    html_message = render_to_string("emails/magic_link.html", context)
    plain_message = strip_tags(html_message)
    subject = subject or "Ссылка для входа в личный кабинет"

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
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
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

            send_magic_link_email(email, link)

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

            add_event_log_once(
                session,
                user,
                analytics_event.FirstSuccessfulLogin(login_method="magic_link"),
            )
            session.commit()

            authorize_user_session(request, user)
            return redirect("dashboard")

        session.rollback()

        return render_login(request, {"error": "Ссылка истекла или неверна"})
    finally:
        session.close()


def auth_by_telegram_link(request, token):
    session = session_factory()
    try:
        token_hash = hash_telegram_login_token(token)
        login_token = (
            session.query(TelegramLoginToken)
            .filter(
                TelegramLoginToken.token_hash == token_hash,
                TelegramLoginToken.revoked_at.is_(None),
            )
            .first()
        )
        if not login_token or not hmac.compare_digest(
            login_token.token_hash,
            token_hash,
        ):
            logging.warning("invalid telegram login token was used")
            return render_login(request, {"error": "Ссылка истекла или неверна"})

        user = session.query(User).filter(User.id == login_token.user_id).first()
        if not user or not user.telegram_id:
            logging.warning("telegram login token points to missing telegram user")
            return render_login(request, {"error": "Ссылка истекла или неверна"})

        login_token.last_used_at = datetime.utcnow()
        add_event_log_once(
            session,
            user,
            analytics_event.FirstSuccessfulLogin(login_method="telegram_link"),
        )
        session.commit()

        authorize_user_session(request, user)
        return redirect("dashboard")
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
        trial_period_days = get_display_trial_period_days_for_request(request)
        return render(
            request,
            "index_neutral.html",
            {
                "tariffs": ACTUAL_TARIFFS,
                "tracking_params": get_tracking_params(request),
                "trial_period_days_label": format_days_ru(trial_period_days),
            },
        )

    return render(
        request,
        "index.html",
        {"tariffs": ACTUAL_TARIFFS, "tracking_params": get_tracking_params(request)},
    )


def offer(request):
    return render(
        request,
        "offer.html",
        {
            "tariffs": ACTUAL_TARIFFS,
            "trial_period_days_label": format_days_ru(settings.SITE_TRIAL_PERIOD_DAYS),
            "referral_trial_period_days_label": format_days_ru(
                settings.SITE_REFERRAL_TRIAL_PERIOD_DAYS
            ),
        },
    )


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
        traffic_progress = (
            session.query(UserTrafficProgress)
            .filter(UserTrafficProgress.user_id == user.id)
            .first()
        )

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
    seconds_left = (
        user.time_until_expiration.total_seconds()
        if user.time_until_expiration
        else -1
    )
    days_left = int((seconds_left + 86399) // 86400) if seconds_left > 0 else 0
    expiring_banner_threshold_seconds = 3 * 24 * 60 * 60
    show_expiring_banner = 0 < seconds_left <= expiring_banner_threshold_seconds
    show_telegram_bind_banner = not user.telegram_id
    show_not_connected_banner = False

    if (
        seconds_left > 0
        and traffic_progress
        and not traffic_progress.passed_0
    ):
        show_not_connected_banner = True

    if settings.DEBUG:
        debug_expiring_days = parse_int(request.GET.get("debug_expiring"))
        if debug_expiring_days is not None:
            seconds_left = debug_expiring_days * 24 * 60 * 60

        debug_seconds_left = parse_int(request.GET.get("debug_seconds_left"))
        if debug_seconds_left is not None:
            seconds_left = debug_seconds_left

        show_telegram_bind_banner = (
            request.GET.get("debug_no_tg") == "1" or show_telegram_bind_banner
        )
        show_not_connected_banner = (
            request.GET.get("debug_nc") == "1" or show_not_connected_banner
        )

        if request.GET.get("debug_expired") == "1":
            seconds_left = -1

        show_expiring_banner = 0 < seconds_left <= expiring_banner_threshold_seconds
        days_left = int((seconds_left + 86399) // 86400) if seconds_left > 0 else 0

    if seconds_left <= 0:
        show_expiring_banner = False
        show_not_connected_banner = False
    elif show_not_connected_banner:
        show_expiring_banner = False

    if seconds_left <= 0:
        time_left_value = 0
        time_left_label = "дней осталось"
    elif seconds_left < 24 * 60 * 60:
        time_left_value = max(1, int((seconds_left + 3599) // 3600))
        time_left_label = "часов осталось"
    else:
        time_left_value = days_left
        time_left_label = "дней осталось"

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
            "seconds_left": seconds_left,
            "days_left": days_left,
            "time_left_value": time_left_value,
            "time_left_label": time_left_label,
            "show_telegram_bind_banner": show_telegram_bind_banner,
            "show_expiring_banner": show_expiring_banner,
            "show_not_connected_banner": show_not_connected_banner,
            "use_new_setup_flow": settings.USE_NEW_SETUP_FLOW,
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
            return HttpResponse("Email обязателен", status=400)

        email = email_raw.lower().strip()
        tariff_id = request.POST.get("tariff_id")

        if not email or not tariff_id:
            return HttpResponse("Не указан email или тариф", status=400)

        tariff = next((t for t in ACTUAL_TARIFFS if t.db_tariff_id == tariff_id), None)
        if not tariff:
            return HttpResponse("Выбранный тариф не найден", status=400)

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

            magic = MagicToken(user_id=user.id)
            db_session.add(magic)
            db_session.flush()
            magic_link = f"{get_current_base_url(request)}/login/magic/{magic.token}/"

            db_session.commit()

            try:
                send_magic_link_email(
                    email,
                    magic_link,
                    subject="Ссылка на личный кабинет Monkey Island",
                    template_context={
                        "title": "Кабинет уже готов",
                        "intro": "Мы создали для вас личный кабинет Monkey Island.",
                        "note": (
                            "После оплаты зайдите по кнопке ниже: ссылка действует "
                            "15 минут и откроет VPN-подписку, инструкции для "
                            "устройств и поддержку."
                        ),
                        "button_text": "Открыть кабинет",
                        "footer": (
                            "Если вы не оформляли VPN Monkey Island, просто "
                            "проигнорируйте это письмо."
                        ),
                    },
                )
            except Exception as e:
                logging.exception(f"failed to send payment magic link to {email}: {e}")

            return redirect(confirmation_url)

        except Exception as e:
            db_session.rollback()
            logging.error(f"Pay error: {e}")
            messages.error(request, "Ошибка платежной системы")
            return redirect("dashboard")
        finally:
            db_session.close()

    return redirect("index")


def robots_txt(request):
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /dashboard/",
        "Disallow: /pay/",
        "Disallow: /login/magic/",
        "Disallow: /login/telegram/",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


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
