import uuid
import hmac
import hashlib
import logging
import base64
import json
import resend
import secrets
import httpx
from pathlib import Path
from datetime import datetime
from datetime import date
from datetime import time
from datetime import timedelta
from datetime import timezone
from urllib.parse import urlencode
from urllib.parse import urlsplit
from django.http import HttpResponse
from django.http import JsonResponse
from django.http import FileResponse
from django.http import Http404
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
from django.core import signing
from django.core.signing import BadSignature
from django.core.signing import SignatureExpired
from django.urls import reverse
from django.templatetags.static import static
from django.template.loader import render_to_string
from django.utils.text import get_valid_filename
from django.utils.html import strip_tags
from sqlalchemy import func
from sqlalchemy import Integer
from sqlalchemy import update
from sqlalchemy import delete as sa_delete
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from common.models.db import User
from common.models.db import EventLog
from common.models.db import ReferralBonus
from common.models.db import ReferralBonusType
from common.models.db import ReferralType
from common.models.db import UserTrafficProgress
from common.models.db import YkPayment
from common.models.db import YkRecurrentPayment
from common.models.db import WataInvoice
from common.models.db import WataTransaction
from common.models.db import MagicToken
from common.models.db import TelegramLoginToken
from common.models.db import PurchaseLoginToken
from common.models.db import CustomConfigTemplate
from common.models.db import SupportTicket
from common.models.db import SupportTicketMessage
from common.models.db import SupportTicketMessageSender
from common.models.db import SupportTicketStatus
from common.models.db import SupportTicketAttachment
from common.models.db import SupportReplyTemplate
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
TRACKING_COOKIE_MAX_AGE = 30 * 24 * 60 * 60
GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
YANDEX_OAUTH_AUTH_URL = "https://oauth.yandex.com/authorize"
YANDEX_OAUTH_TOKEN_URL = "https://oauth.yandex.com/token"
YANDEX_OAUTH_USERINFO_URL = "https://login.yandex.ru/info"

rwms_client = RwmsClientSync(settings.RWMS_HOST, settings.RWMS_PORT)
SUPPORT_ADMIN_SESSION_KEY = "support_admin_authenticated"
SUPPORT_ADMIN_ROLE_SESSION_KEY = "support_admin_role"
SUPPORT_ADMIN_ROLE_ADMIN = "admin"
SUPPORT_ADMIN_ROLE_SUPPORT = "support"
SUPPORT_ATTACHMENT_ALLOWED_PREFIXES = ("image/", "video/")
EMAIL_CONFIRMATION_SALT = "dashboard-email-confirmation"
EMAIL_CONFIRMATION_MAX_AGE_SECONDS = 15 * 60


def normalize_host(host):
    return host.split(":", 1)[0].lower()


def is_known_site_host(host):
    normalized_host = normalize_host(host)
    return (
        normalized_host in settings.CABINET_DOMAINS
        or normalized_host in settings.VPS_DOMAINS
        or normalized_host in settings.VPS_DIRECT_SALE_DOMAINS
        or normalized_host in settings.VPN_DOMAINS
    )


def get_site_role(request):
    host = normalize_host(request.get_host())
    if host in settings.CABINET_DOMAINS:
        return "cabinet"
    if host in settings.VPS_DOMAINS:
        return "vps"
    if host in settings.VPS_DIRECT_SALE_DOMAINS:
        return "vps_direct_sale"
    if host in settings.VPN_DOMAINS:
        return "vpn"
    return "vpn"


def get_current_base_url(request):
    if not is_known_site_host(request.get_host()):
        fallback_url = settings.DEFAULT_CABINET_DOMAIN.rstrip("/")
        if "://" not in fallback_url:
            fallback_url = f"https://{fallback_url}"
        return fallback_url

    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{request.get_host()}"


def get_telegram_auth_bot(host):
    normalized_host = normalize_host(host)
    configured_bot = settings.TELEGRAM_AUTH_BOTS.get(normalized_host)
    if configured_bot:
        return configured_bot

    if settings.TG_BOT_USERNAME and settings.TELEGRAM_AUTH_BOT_TOKEN:
        return {
            "username": settings.TG_BOT_USERNAME.lstrip("@"),
            "token": settings.TELEGRAM_AUTH_BOT_TOKEN,
        }

    return None


def get_telegram_bot_id(bot):
    return bot["token"].split(":", 1)[0]


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
    captured = {}
    for key in TRACKING_PARAM_KEYS:
        value = request.GET.get(key)
        if value:
            request.session[f"tracking_{key}"] = value
            captured[key] = value

    return captured


def set_tracking_cookies(request, response, tracking_params):
    for key, value in tracking_params.items():
        response.set_cookie(
            f"tracking_{key}",
            value,
            max_age=TRACKING_COOKIE_MAX_AGE,
            path="/",
            secure=request.is_secure(),
            httponly=True,
            samesite="Lax",
        )

    return response


def get_tracking_params(request):
    tracking_params = {}
    for key in TRACKING_PARAM_KEYS:
        value = get_tracking_value(request, key)
        if value:
            tracking_params[key] = value

    return tracking_params


def append_query_params(url, params):
    if not params:
        return url

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


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

    for key in keys:
        value = request.COOKIES.get(f"tracking_{key}")
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
            (EventLog.user_id == user.id) & (EventLog.event_type == event.event_type)
        )
        .first()
    )

    if exists:
        return False

    add_event_log(db_session, user, event)
    return True


def dashboard_support_redirect():
    return redirect("/dashboard/?tab=support")


def get_support_ticket_for_user(db_session, user_id, ticket_id):
    return (
        db_session.query(SupportTicket)
        .filter((SupportTicket.id == ticket_id) & (SupportTicket.user_id == user_id))
        .first()
    )


def add_support_message(db_session, ticket, sender_type, message):
    clean_message = message.strip()
    if not clean_message:
        return None

    ticket.updated_at = datetime.utcnow()
    support_message = SupportTicketMessage(
        ticket_id=ticket.id,
        sender_type=sender_type,
        message=clean_message,
    )
    db_session.add(support_message)
    return support_message


def attach_support_attachments(db_session, support_message, uploaded_files):
    saved_attachments = []
    if not uploaded_files:
        return saved_attachments

    db_session.flush()
    base_dir = Path(settings.MEDIA_ROOT) / "support_attachments"
    message_dir = base_dir / str(support_message.id)
    message_dir.mkdir(parents=True, exist_ok=True)

    for uploaded_file in uploaded_files:
        content_type = uploaded_file.content_type or "application/octet-stream"
        if not content_type.startswith(SUPPORT_ATTACHMENT_ALLOWED_PREFIXES):
            logging.warning(
                "unsupported support attachment content type %s", content_type
            )
            continue

        if uploaded_file.size > settings.SUPPORT_ATTACHMENT_MAX_BYTES:
            logging.warning("support attachment %s is too large", uploaded_file.name)
            continue

        safe_name = get_valid_filename(uploaded_file.name) or "attachment"
        storage_name = f"{uuid.uuid4().hex}_{safe_name}"
        absolute_path = message_dir / storage_name

        with absolute_path.open("wb") as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)

        relative_path = absolute_path.relative_to(settings.MEDIA_ROOT).as_posix()
        attachment = SupportTicketAttachment(
            message_id=support_message.id,
            file_name=safe_name[:512],
            content_type=content_type[:128],
            file_size=uploaded_file.size,
            storage_path=relative_path,
        )
        db_session.add(attachment)
        saved_attachments.append(attachment)

    return saved_attachments


def load_support_messages_with_attachments(db_session, ticket_id):
    support_messages = (
        db_session.query(SupportTicketMessage)
        .filter(SupportTicketMessage.ticket_id == ticket_id)
        .order_by(SupportTicketMessage.created_at.asc())
        .all()
    )
    message_ids = [message.id for message in support_messages]
    attachments_by_message_id = {message.id: [] for message in support_messages}

    if message_ids:
        attachments = (
            db_session.query(SupportTicketAttachment)
            .filter(SupportTicketAttachment.message_id.in_(message_ids))
            .order_by(SupportTicketAttachment.created_at.asc())
            .all()
        )
        for attachment in attachments:
            attachment.is_image = is_image_attachment(attachment)
            attachment.is_video = is_video_attachment(attachment)
            attachments_by_message_id.setdefault(attachment.message_id, []).append(
                attachment
            )

    for message in support_messages:
        message.attachments = attachments_by_message_id.get(message.id, [])

    return support_messages


def is_image_attachment(attachment):
    return attachment.content_type.startswith("image/")


def is_video_attachment(attachment):
    return attachment.content_type.startswith("video/")


def support_attachment_payload(attachment, admin=False):
    route_name = "support_admin_attachment" if admin else "support_attachment"
    return {
        "id": attachment.id,
        "file_name": attachment.file_name,
        "content_type": attachment.content_type,
        "file_size": attachment.file_size,
        "url": reverse(route_name, args=[attachment.id]),
        "is_image": bool(
            getattr(attachment, "is_image", is_image_attachment(attachment))
        ),
        "is_video": bool(
            getattr(attachment, "is_video", is_video_attachment(attachment))
        ),
    }


def support_message_payload(message, admin=False):
    return {
        "id": message.id,
        "sender_type": message.sender_type.value,
        "is_user": message.sender_type == SupportTicketMessageSender.USER,
        "message": message.message,
        "created_at": message.created_at.strftime("%d.%m %H:%M"),
        "created_at_full": message.created_at.strftime("%d.%m.%Y %H:%M"),
        "attachments": [
            support_attachment_payload(attachment, admin=admin)
            for attachment in getattr(message, "attachments", [])
        ],
    }


def support_messages_payload(db_session, ticket_id, admin=False):
    return [
        support_message_payload(message, admin=admin)
        for message in load_support_messages_with_attachments(db_session, ticket_id)
    ]


def is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def support_reply_template_payload(template):
    return {
        "id": template.id,
        "title": template.title,
        "body": template.body,
        "sort_order": template.sort_order,
        "is_active": template.is_active,
    }


def custom_config_template_payload(template):
    return {
        "id": template.id,
        "name": template.name,
        "template_json": template.template_json,
        "entry_name": template.entry_name or "",
        "announce_text": template.announce_text or "",
        "support_url": template.support_url or "",
        "profile_update_interval": template.profile_update_interval or "",
        "additional_headers": template.additional_headers or {},
        "is_active": template.is_active,
    }


def load_custom_config_templates(db_session):
    return (
        db_session.query(CustomConfigTemplate)
        .order_by(
            CustomConfigTemplate.is_active.desc(),
            CustomConfigTemplate.name.asc(),
            CustomConfigTemplate.id.asc(),
        )
        .all()
    )


def load_support_reply_templates(db_session, active_only=True):
    query = db_session.query(SupportReplyTemplate)
    if active_only:
        query = query.filter(SupportReplyTemplate.is_active.is_(True))
    return (
        query.order_by(
            SupportReplyTemplate.sort_order.asc(),
            SupportReplyTemplate.title.asc(),
            SupportReplyTemplate.id.asc(),
        )
        .all()
    )


def delete_support_ticket_with_files(db_session, ticket):
    messages = (
        db_session.query(SupportTicketMessage)
        .filter(SupportTicketMessage.ticket_id == ticket.id)
        .all()
    )
    message_ids = [message.id for message in messages]
    attachments = []

    if message_ids:
        attachments = (
            db_session.query(SupportTicketAttachment)
            .filter(SupportTicketAttachment.message_id.in_(message_ids))
            .all()
        )

    media_root = Path(settings.MEDIA_ROOT).resolve()
    for attachment in attachments:
        path = (Path(settings.MEDIA_ROOT) / attachment.storage_path).resolve()
        if media_root in path.parents and path.exists():
            try:
                path.unlink()
            except OSError as e:
                logging.warning("failed to delete support attachment %s: %s", path, e)
            else:
                try:
                    path.parent.rmdir()
                except OSError:
                    pass

    if message_ids:
        db_session.execute(
            sa_delete(SupportTicketAttachment).where(
                SupportTicketAttachment.message_id.in_(message_ids)
            )
        )
        db_session.execute(
            sa_delete(SupportTicketMessage).where(
                SupportTicketMessage.id.in_(message_ids)
            )
        )

    db_session.execute(sa_delete(SupportTicket).where(SupportTicket.id == ticket.id))


def support_admin_is_authenticated(request):
    return bool(request.session.get(SUPPORT_ADMIN_SESSION_KEY))


def support_admin_role(request):
    if not support_admin_is_authenticated(request):
        return None
    return request.session.get(
        SUPPORT_ADMIN_ROLE_SESSION_KEY,
        SUPPORT_ADMIN_ROLE_ADMIN,
    )


def support_admin_is_full_admin(request):
    return support_admin_role(request) == SUPPORT_ADMIN_ROLE_ADMIN


def require_support_admin(request):
    if not settings.SUPPORT_ADMIN_PASSWORD and not settings.SUPPORT_STAFF_PASSWORD:
        logging.warning("support admin requested but no support password is configured")
        return render(
            request,
            "support_admin_login.html",
            {
                "error": "Админка поддержки не настроена: задайте SUPPORT_ADMIN_PASSWORD или SUPPORT_STAFF_PASSWORD.",
            },
            status=503,
        )

    if not support_admin_is_authenticated(request):
        return redirect("support_admin_login")

    return None


def require_support_admin_role(request, role):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    if support_admin_role(request) != role:
        return JsonResponse(
            {
                "status": "forbidden",
                "message": "Недостаточно прав для этого раздела.",
            },
            status=403,
        )

    return None


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


def create_site_user(
    db_session,
    email,
    request,
    telegram_id=None,
    creation_channel="site",
):
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
        telegram_id=telegram_id,
    )

    user_label = email or f"telegram_id={telegram_id}"
    if rw_user is None:
        raise RuntimeError(
            f"creating subscription for site user {user_label} was failed"
        )

    expire_at = None
    if rw_user.HasField("expire_at"):
        expire_at = rw_user.expire_at.ToDatetime().replace(tzinfo=None)

    user = User(
        email=email,
        telegram_id=telegram_id,
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
            creation_channel=creation_channel,
        ),
    )

    logging.info(
        f"User with username {user.username} and {user_label} was successfully created"
    )

    return user


def render_login(request, context=None, status=200):
    captured_tracking_params = capture_tracking_params(request)
    payload = get_pwa_context()
    telegram_bot = get_telegram_auth_bot(request.get_host())
    telegram_bot_id = ""
    telegram_bot_username = ""
    if telegram_bot:
        telegram_bot_id = get_telegram_bot_id(telegram_bot)
        telegram_bot_username = telegram_bot["username"]

    payload["tracking_params"] = get_tracking_params(request)
    google_oauth_enabled = bool(
        settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET
    )
    yandex_oauth_enabled = bool(
        settings.YANDEX_OAUTH_CLIENT_ID and settings.YANDEX_OAUTH_CLIENT_SECRET
    )
    telegram_auth_enabled = bool(telegram_bot_username and telegram_bot_id)

    payload["google_oauth_enabled"] = google_oauth_enabled
    payload["yandex_oauth_enabled"] = yandex_oauth_enabled
    payload["telegram_auth_enabled"] = telegram_auth_enabled
    payload["social_login_enabled"] = any(
        [google_oauth_enabled, yandex_oauth_enabled, telegram_auth_enabled]
    )
    payload["telegram_bot_username"] = telegram_bot_username
    payload["telegram_bot_id"] = telegram_bot_id
    payload["telegram_auth_url"] = append_query_params(
        f"{get_current_base_url(request)}{reverse('telegram_widget_auth')}",
        payload["tracking_params"],
    )
    payload["telegram_return_url"] = append_query_params(
        f"{get_current_base_url(request)}{reverse('login')}",
        payload["tracking_params"],
    )
    payload["telegram_direct_auth_url"] = (
        "https://oauth.telegram.org/auth?"
        + urlencode(
            {
                "bot_id": telegram_bot_id,
                "origin": get_current_base_url(request),
                "return_to": payload["telegram_return_url"],
                "request_access": "write",
            }
        )
        if telegram_auth_enabled
        else ""
    )
    if context:
        payload.update(context)
    response = render(request, "login.html", payload, status=status)
    return set_tracking_cookies(request, response, captured_tracking_params)


def render_collect_email(request, user, error=None, email="", info=None):
    return render(
        request,
        "dashboard_collect_email.html",
        {
            "user": user,
            "error": error,
            "email": email,
            "info": info,
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


def hash_purchase_login_token(token):
    payload = f"purchase-login:{token}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def create_purchase_login_token(db_session, user):
    raw_token = secrets.token_urlsafe(48)
    db_session.add(
        PurchaseLoginToken(
            user_id=user.id,
            token_hash=hash_purchase_login_token(raw_token),
        )
    )
    return raw_token


def build_purchase_login_link(request, token):
    return f"{get_current_base_url(request)}{reverse('purchase_auth', args=[token])}"


def build_payment_status_url(request, token):
    return f"{get_current_base_url(request)}{reverse('payment_status', args=[token])}"


def create_purchase_login_link(db_session, request, user):
    raw_token = create_purchase_login_token(db_session, user)
    return build_purchase_login_link(request, raw_token)


def verify_telegram_widget_auth(auth_data, bot_token):
    received_hash = auth_data.get("hash")
    auth_date = auth_data.get("auth_date")

    if not received_hash or not auth_date:
        return False

    try:
        auth_date_int = int(auth_date)
    except (TypeError, ValueError):
        return False

    if datetime.utcnow().timestamp() - auth_date_int > 86400:
        return False

    check_data = {
        key: value
        for key, value in auth_data.items()
        if key != "hash" and value not in (None, "")
    }
    data_check_string = "\n".join(
        f"{key}={check_data[key]}" for key in sorted(check_data)
    )
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(calculated_hash, received_hash)


def decode_telegram_auth_result(raw_result):
    if not raw_result:
        return None

    try:
        normalized = raw_result.replace("-", "+").replace("_", "/")
        normalized += "=" * (-len(normalized) % 4)
        decoded = base64.b64decode(normalized).decode()
        result = json.loads(decoded)
        if isinstance(result, dict):
            return {str(key): str(value) for key, value in result.items()}
    except Exception as e:
        logging.warning("failed to decode telegram auth result: %s", e)

    return None


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


def build_email_confirmation_token(user_id, email):
    return signing.dumps(
        {
            "user_id": user_id,
            "email": email,
        },
        salt=EMAIL_CONFIRMATION_SALT,
    )


def load_email_confirmation_token(token):
    return signing.loads(
        token,
        salt=EMAIL_CONFIRMATION_SALT,
        max_age=EMAIL_CONFIRMATION_MAX_AGE_SECONDS,
    )


def send_email_confirmation_email(email, link):
    send_magic_link_email(
        email,
        link,
        subject="Подтвердите email в Monkey Island",
        template_context={
            "title": "Подтвердите email",
            "intro": "Вы привязываете этот email к личному кабинету Monkey Island.",
            "note": "Нажмите кнопку ниже, чтобы подтвердить почту. Ссылка действует 15 минут.",
            "button_text": "Подтвердить почту",
            "footer": "Если вы не привязывали почту, просто проигнорируйте это письмо.",
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
                    user = create_site_user(
                        db_session,
                        email,
                        request,
                        creation_channel="site_magic_link",
                    )
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


def get_google_oauth_redirect_uri(request):
    if settings.GOOGLE_OAUTH_REDIRECT_URI:
        return settings.GOOGLE_OAUTH_REDIRECT_URI

    return f"{get_current_base_url(request)}/login/google/callback/"


def login_with_google(request):
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        logging.warning("google oauth login requested but credentials are missing")
        return render_login(
            request,
            {"error": "Вход через Google временно недоступен"},
            status=503,
        )

    captured_tracking_params = capture_tracking_params(request)
    state = secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state
    request.session.modified = True

    auth_url = (
        GOOGLE_OAUTH_AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "redirect_uri": get_google_oauth_redirect_uri(request),
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "access_type": "online",
                "prompt": "select_account",
            }
        )
    )

    response = redirect(auth_url)
    return set_tracking_cookies(request, response, captured_tracking_params)


def auth_by_google_callback(request):
    error = request.GET.get("error")
    if error:
        logging.warning(f"google oauth returned error: {error}")
        return render_login(request, {"error": "Вход через Google отменен"})

    code = request.GET.get("code")
    state = request.GET.get("state")
    expected_state = request.session.pop("google_oauth_state", None)
    request.session.modified = True

    if not code or not state or not hmac.compare_digest(state, expected_state or ""):
        logging.warning("invalid google oauth callback state")
        return render_login(
            request, {"error": "Сессия входа истекла. Попробуйте еще раз."}
        )

    redirect_uri = get_google_oauth_redirect_uri(request)

    try:
        with httpx.Client(timeout=10) as client:
            token_response = client.post(
                GOOGLE_OAUTH_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")

            if not access_token:
                raise RuntimeError("google oauth token response has no access_token")

            userinfo_response = client.get(
                GOOGLE_OAUTH_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
    except Exception as e:
        logging.exception(f"google oauth login failed: {e}")
        return render_login(
            request,
            {"error": "Не удалось войти через Google. Попробуйте еще раз."},
        )

    if not userinfo.get("email_verified"):
        logging.warning("google oauth user email is not verified")
        return render_login(request, {"error": "Google не подтвердил этот email."})

    email = (userinfo.get("email") or "").lower().strip()
    if not email:
        logging.warning("google oauth userinfo has no email")
        return render_login(request, {"error": "Google не вернул email аккаунта."})

    db_session = session_factory()
    try:
        with db_session.begin():
            user = db_session.query(User).filter(User.email == email).first()

            if not user:
                user = create_site_user(
                    db_session,
                    email,
                    request,
                    creation_channel="site_google_oauth",
                )
            else:
                registration_context = get_registration_context(request, db_session)
                sync_existing_user_tracking(
                    db_session,
                    user,
                    registration_context["traffic_source"],
                    registration_context["ymid"],
                )

            add_event_log_once(
                db_session,
                user,
                analytics_event.FirstSuccessfulLogin(login_method="google_oauth"),
            )

        authorize_user_session(request, user)
        return redirect("dashboard")
    except Exception as e:
        logging.exception(f"google oauth user authorization failed for {email}: {e}")
        return render_login(
            request,
            {"error": "Не удалось подготовить личный кабинет. Напишите в поддержку."},
        )
    finally:
        db_session.close()


def get_yandex_oauth_redirect_uri(request):
    if settings.YANDEX_OAUTH_REDIRECT_URI:
        return settings.YANDEX_OAUTH_REDIRECT_URI

    return f"{get_current_base_url(request)}/login/yandex/callback/"


def login_with_yandex(request):
    if not settings.YANDEX_OAUTH_CLIENT_ID or not settings.YANDEX_OAUTH_CLIENT_SECRET:
        logging.warning("yandex oauth login requested but credentials are missing")
        return render_login(
            request,
            {"error": "Вход через Яндекс временно недоступен"},
            status=503,
        )

    captured_tracking_params = capture_tracking_params(request)
    state = secrets.token_urlsafe(32)
    request.session["yandex_oauth_state"] = state
    request.session.modified = True

    auth_url = (
        YANDEX_OAUTH_AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": settings.YANDEX_OAUTH_CLIENT_ID,
                "redirect_uri": get_yandex_oauth_redirect_uri(request),
                "response_type": "code",
                "state": state,
                "force_confirm": "yes",
            }
        )
    )

    response = redirect(auth_url)
    return set_tracking_cookies(request, response, captured_tracking_params)


def auth_by_yandex_callback(request):
    error = request.GET.get("error")
    if error:
        logging.warning(f"yandex oauth returned error: {error}")
        return render_login(request, {"error": "Вход через Яндекс отменен"})

    code = request.GET.get("code")
    state = request.GET.get("state")
    expected_state = request.session.pop("yandex_oauth_state", None)
    request.session.modified = True

    if not code or not state or not hmac.compare_digest(state, expected_state or ""):
        logging.warning("invalid yandex oauth callback state")
        return render_login(
            request, {"error": "Сессия входа истекла. Попробуйте еще раз."}
        )

    redirect_uri = get_yandex_oauth_redirect_uri(request)

    try:
        with httpx.Client(timeout=10) as client:
            token_response = client.post(
                YANDEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.YANDEX_OAUTH_CLIENT_ID,
                    "client_secret": settings.YANDEX_OAUTH_CLIENT_SECRET,
                    "redirect_uri": redirect_uri,
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")

            if not access_token:
                raise RuntimeError("yandex oauth token response has no access_token")

            userinfo_response = client.get(
                YANDEX_OAUTH_USERINFO_URL,
                params={"format": "json"},
                headers={"Authorization": f"OAuth {access_token}"},
            )
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
    except Exception as e:
        logging.exception(f"yandex oauth login failed: {e}")
        return render_login(
            request,
            {"error": "Не удалось войти через Яндекс. Попробуйте еще раз."},
        )

    email = (
        (userinfo.get("default_email") or (userinfo.get("emails") or [None])[0] or "")
        .lower()
        .strip()
    )
    if not email:
        logging.warning("yandex oauth userinfo has no email")
        return render_login(
            request,
            {"error": "Яндекс не вернул email аккаунта. Проверьте права приложения."},
        )

    db_session = session_factory()
    try:
        with db_session.begin():
            user = db_session.query(User).filter(User.email == email).first()

            if not user:
                user = create_site_user(
                    db_session,
                    email,
                    request,
                    creation_channel="site_yandex_oauth",
                )
            else:
                registration_context = get_registration_context(request, db_session)
                sync_existing_user_tracking(
                    db_session,
                    user,
                    registration_context["traffic_source"],
                    registration_context["ymid"],
                )

            add_event_log_once(
                db_session,
                user,
                analytics_event.FirstSuccessfulLogin(login_method="yandex_oauth"),
            )

        authorize_user_session(request, user)
        return redirect("dashboard")
    except Exception as e:
        logging.exception(f"yandex oauth user authorization failed for {email}: {e}")
        return render_login(
            request,
            {"error": "Не удалось подготовить личный кабинет. Напишите в поддержку."},
        )
    finally:
        db_session.close()


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


def auth_by_purchase_link(request, token):
    session = session_factory()
    try:
        token_hash = hash_purchase_login_token(token)
        login_token = (
            session.query(PurchaseLoginToken)
            .filter(
                PurchaseLoginToken.token_hash == token_hash,
                PurchaseLoginToken.revoked_at.is_(None),
            )
            .first()
        )
        if not login_token or not hmac.compare_digest(
            login_token.token_hash,
            token_hash,
        ):
            logging.warning("invalid purchase login token was used")
            return render_login(request, {"error": "Ссылка истекла или неверна"})

        user = session.query(User).filter(User.id == login_token.user_id).first()
        if not user:
            logging.warning("purchase login token points to missing user")
            return render_login(request, {"error": "Ссылка истекла или неверна"})

        login_token.last_used_at = datetime.utcnow()
        add_event_log_once(
            session,
            user,
            analytics_event.FirstSuccessfulLogin(login_method="purchase_link"),
        )
        session.commit()

        authorize_user_session(request, user)
        return redirect("dashboard")
    finally:
        session.close()


def get_purchase_login_token(db_session, token):
    token_hash = hash_purchase_login_token(token)
    login_token = (
        db_session.query(PurchaseLoginToken)
        .filter(
            PurchaseLoginToken.token_hash == token_hash,
            PurchaseLoginToken.revoked_at.is_(None),
        )
        .first()
    )
    if not login_token or not hmac.compare_digest(login_token.token_hash, token_hash):
        return None
    return login_token


def is_wata_invoice_expired(invoice):
    if not invoice or not invoice.expiration_datetime:
        return False

    expires_at = invoice.expiration_datetime
    if expires_at.tzinfo is None:
        return datetime.utcnow() > expires_at
    return datetime.now(timezone.utc) > expires_at


def get_purchase_payment_status(db_session, login_token):
    started_at = login_token.created_at - timedelta(minutes=5)

    yk_filters = [YkPayment.user_id == login_token.user_id]
    if login_token.payment_gateway == "yookassa" and login_token.payment_reference:
        yk_filters.append(YkPayment.payment_id == login_token.payment_reference)
    else:
        yk_filters.append(YkPayment.created_at >= started_at)

    yk_payment = (
        db_session.query(YkPayment)
        .filter(*yk_filters)
        .order_by(YkPayment.created_at.desc())
        .first()
    )
    if yk_payment:
        if yk_payment.status == "succeeded":
            return "succeeded", "Платеж прошел успешно"
        if yk_payment.status == "canceled":
            return "failed", "Платеж не прошел"

    wata_filters = [WataInvoice.user_id == login_token.user_id]
    if login_token.payment_gateway == "wata" and login_token.payment_reference:
        wata_filters.append(WataInvoice.order_id == login_token.payment_reference)
    else:
        wata_filters.append(WataInvoice.creation_time >= started_at)

    wata_invoice = (
        db_session.query(WataInvoice)
        .filter(*wata_filters)
        .order_by(WataInvoice.creation_time.desc())
        .first()
    )
    if wata_invoice:
        wata_transaction = (
            db_session.query(WataTransaction)
            .filter(WataTransaction.order_id == wata_invoice.order_id)
            .order_by(WataTransaction.payment_time.desc())
            .first()
        )
        if wata_transaction:
            if wata_transaction.transaction_status == "Paid":
                return "succeeded", "Платеж прошел успешно"
            return "failed", "Платеж не прошел"
        if is_wata_invoice_expired(wata_invoice):
            return "failed", "Время оплаты истекло"

    return "pending", "Ждем подтверждения платежа"


def payment_status_payload(request, token):
    if request.GET.get("result") == "failed":
        return {
            "status": "failed",
            "message": "Платеж не прошел",
            "login_url": "",
        }

    db_session = session_factory()
    try:
        login_token = get_purchase_login_token(db_session, token)
        if not login_token:
            return {
                "status": "failed",
                "message": "Ссылка проверки платежа истекла или неверна",
                "login_url": "",
            }

        status, message = get_purchase_payment_status(db_session, login_token)
        return {
            "status": status,
            "message": message,
            "login_url": build_purchase_login_link(request, token)
            if status == "succeeded"
            else "",
        }
    finally:
        db_session.close()


def payment_status(request, token):
    payload = payment_status_payload(request, token)
    return render(
        request,
        "payment_status.html",
        {
            "initial_status": payload["status"],
            "initial_message": payload["message"],
            "login_url": payload["login_url"],
            "status_api_url": reverse("payment_status_json", args=[token]),
            "support_telegram_url": settings.SUPPORT_TELEGRAM_URL,
        },
    )


def payment_status_json(request, token):
    return JsonResponse(payment_status_payload(request, token))


def auth_by_telegram_widget(request):
    telegram_bot = get_telegram_auth_bot(request.get_host())
    if not telegram_bot:
        logging.warning("telegram widget auth requested but bot token is missing")
        return render_login(
            request,
            {"error": "Вход через Telegram временно недоступен"},
            status=503,
        )

    auth_data = request.GET.dict()
    telegram_auth_result = auth_data.get("tgAuthResult")
    decoded_auth_data = decode_telegram_auth_result(telegram_auth_result)
    if decoded_auth_data:
        auth_data = decoded_auth_data

    if not auth_data:
        return render_login(request)

    if not verify_telegram_widget_auth(auth_data, telegram_bot["token"]):
        logging.warning(
            "telegram widget auth failed signature check, keys=%s",
            sorted(auth_data.keys()),
        )
        return render_login(
            request, {"error": "Не удалось подтвердить вход через Telegram."}
        )

    try:
        telegram_id = int(auth_data["id"])
    except (KeyError, TypeError, ValueError):
        logging.warning("telegram widget auth returned invalid telegram id")
        return render_login(request, {"error": "Telegram не вернул ID аккаунта."})

    db_session = session_factory()
    try:
        with db_session.begin():
            user = (
                db_session.query(User).filter(User.telegram_id == telegram_id).first()
            )

            if not user:
                logging.info(
                    "creating site subscription from telegram widget auth for telegram id %s",
                    telegram_id,
                )
                user = create_site_user(
                    db_session,
                    None,
                    request,
                    telegram_id=telegram_id,
                    creation_channel="site_telegram_widget",
                )
            else:
                registration_context = get_registration_context(request, db_session)
                sync_existing_user_tracking(
                    db_session,
                    user,
                    registration_context["traffic_source"],
                    registration_context["ymid"],
                )

            add_event_log_once(
                db_session,
                user,
                analytics_event.FirstSuccessfulLogin(login_method="telegram_widget"),
            )

        authorize_user_session(request, user)
        return redirect("dashboard")
    except Exception as e:
        logging.exception(f"telegram widget auth failed for {telegram_id}: {e}")
        return render_login(
            request,
            {"error": "Не удалось войти через Telegram. Напишите в поддержку."},
        )
    finally:
        db_session.close()


def index(request):
    captured_tracking_params = capture_tracking_params(request)
    site_role = get_site_role(request)

    if site_role == "cabinet":
        if request.user.is_authenticated:
            return redirect("dashboard")
        return render_login(request)

    if site_role == "vps":
        trial_period_days = get_display_trial_period_days_for_request(request)
        response = render(
            request,
            "index_vps.html",
            {
                "tariffs": ACTUAL_TARIFFS,
                "tracking_params": get_tracking_params(request),
                "trial_period_days_label": format_days_ru(trial_period_days),
            },
        )
        return set_tracking_cookies(request, response, captured_tracking_params)

    if site_role == "vps_direct_sale":
        response = render_vps_direct_sale(request)
        return set_tracking_cookies(request, response, captured_tracking_params)

    trial_period_days = get_display_trial_period_days_for_request(request)
    response = render(
        request,
        "index_vpn.html",
        {
            "tariffs": ACTUAL_TARIFFS,
            "tracking_params": get_tracking_params(request),
            "trial_period_days_label": format_days_ru(trial_period_days),
        },
    )
    return set_tracking_cookies(request, response, captured_tracking_params)


def vps_direct_sale(request):
    captured_tracking_params = capture_tracking_params(request)
    response = render_vps_direct_sale(request)
    return set_tracking_cookies(request, response, captured_tracking_params)


def render_vps_direct_sale(request):
    return render(
        request,
        "index_vps_direct_sale.html",
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

        support_open_ticket = (
            session.query(SupportTicket)
            .filter(
                (SupportTicket.user_id == user.id)
                & (SupportTicket.status == SupportTicketStatus.OPEN)
            )
            .order_by(SupportTicket.updated_at.desc())
            .first()
        )
        support_ticket = support_open_ticket or (
            session.query(SupportTicket)
            .filter(SupportTicket.user_id == user.id)
            .order_by(SupportTicket.updated_at.desc())
            .first()
        )
        support_messages = []
        if support_ticket:
            support_messages = load_support_messages_with_attachments(
                session,
                support_ticket.id,
            )

        support_open_count = (
            session.query(func.count(SupportTicket.id))
            .filter(
                (SupportTicket.user_id == user.id)
                & (SupportTicket.status == SupportTicketStatus.OPEN)
            )
            .scalar()
        )
    finally:
        session.close()

    bonus_days = ref_connected_count * 10 + ref_purchased_count * 30
    subscription = rwms_client.get_user_by_username(user.username)
    seconds_left = (
        user.time_until_expiration.total_seconds() if user.time_until_expiration else -1
    )
    days_left = int((seconds_left + 86399) // 86400) if seconds_left > 0 else 0
    expiring_banner_threshold_seconds = 3 * 24 * 60 * 60
    show_expiring_banner = 0 < seconds_left <= expiring_banner_threshold_seconds
    show_telegram_bind_banner = not user.telegram_id
    show_email_bind_banner = not user.email
    show_not_connected_banner = False

    if seconds_left > 0 and traffic_progress and not traffic_progress.passed_0:
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
        show_email_bind_banner = (
            request.GET.get("debug_no_email") == "1" or show_email_bind_banner
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

    email_bind_modal = request.session.pop("email_bind_modal", None)
    if email_bind_modal:
        request.session.modified = True

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

    dashboard_template = (
        "dashboard.html" if request.GET.get("ui") == "v1" else "dashboard_v2.html"
    )

    return render(
        request,
        dashboard_template,
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
            "show_email_bind_banner": show_email_bind_banner,
            "show_expiring_banner": show_expiring_banner,
            "show_not_connected_banner": show_not_connected_banner,
            "email_bind_modal": email_bind_modal,
            "use_new_setup_flow": settings.USE_NEW_SETUP_FLOW,
            "support_ticket": support_ticket,
            "support_messages": support_messages,
            "support_open_count": support_open_count,
            "support_status_open": SupportTicketStatus.OPEN,
            "support_sender_user": SupportTicketMessageSender.USER,
            "support_telegram_url": settings.SUPPORT_TELEGRAM_URL,
            "referral_link": f"https://t.me/{tg_bot}?start=a{user.username}",
            "site_referral_link": f"{get_current_base_url(request)}/?a={user.username}",
        },
    )


def update_email(request):
    if request.method != "POST" or not request.user.is_authenticated:
        return redirect("login")

    new_email = request.POST.get("email", "").lower().strip()
    if not new_email:
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Введите email.",
            "email": new_email,
        }
        request.session.modified = True
        return redirect("dashboard")

    session = session_factory()
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
            request.session["email_bind_modal"] = {
                "open": True,
                "error": "Этот email уже привязан к другому аккаунту.",
                "email": new_email,
            }
            request.session.modified = True
            return redirect("dashboard")

        token = build_email_confirmation_token(db_user.id, new_email)
        link = f"{get_current_base_url(request)}{reverse('confirm_email', args=[token])}"
        send_email_confirmation_email(new_email, link)

        request.session["email_bind_modal"] = {
            "open": True,
            "email": new_email,
            "info": (
                "Мы отправили письмо с подтверждением. "
                "Откройте ссылку из письма, чтобы завершить привязку."
            ),
        }
        request.session.modified = True
        return redirect("dashboard")
    except Exception as e:
        session.rollback()
        logging.exception(
            f"failed to send email confirmation for user {request.user.id}: {e}"
        )
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Не удалось отправить письмо подтверждения. Попробуйте еще раз.",
            "email": new_email,
        }
        request.session.modified = True
        return redirect("dashboard")
    finally:
        session.close()


def confirm_email(request, token):
    try:
        payload = load_email_confirmation_token(token)
    except SignatureExpired:
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Ссылка подтверждения истекла. Введите email еще раз.",
        }
        request.session.modified = True
        return redirect("dashboard" if request.user.is_authenticated else "login")
    except BadSignature:
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Ссылка подтверждения неверна. Введите email еще раз.",
        }
        request.session.modified = True
        return redirect("dashboard" if request.user.is_authenticated else "login")

    user_id = payload.get("user_id")
    new_email = (payload.get("email") or "").lower().strip()
    if not user_id or not new_email:
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Ссылка подтверждения неверна. Введите email еще раз.",
        }
        request.session.modified = True
        return redirect("dashboard" if request.user.is_authenticated else "login")

    session = session_factory()
    username = None
    try:
        db_user = session.query(User).filter(User.id == user_id).first()
        if not db_user:
            logging.warning(f"missing user {user_id} while confirming email")
            return redirect("login")

        existing_user = (
            session.query(User)
            .filter(User.email == new_email, User.id != db_user.id)
            .first()
        )
        if existing_user:
            request.session["email_bind_modal"] = {
                "open": True,
                "error": "Этот email уже привязан к другому аккаунту.",
                "email": new_email,
            }
            request.session.modified = True
            return redirect("dashboard" if request.user.is_authenticated else "login")

        db_user.email = new_email
        username = db_user.username
        session.commit()

        if request.user.is_authenticated and request.user.id == db_user.id:
            request.user.email = new_email
        else:
            authorize_user_session(request, db_user)
    except IntegrityError:
        session.rollback()
        logging.warning(
            f"email {new_email} already exists while confirming user {user_id}"
        )
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Этот email уже привязан к другому аккаунту.",
            "email": new_email,
        }
        request.session.modified = True
        return redirect("dashboard" if request.user.is_authenticated else "login")
    except Exception as e:
        session.rollback()
        logging.exception(f"failed to confirm email for user {user_id}: {e}")
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Не удалось подтвердить email. Попробуйте еще раз.",
            "email": new_email,
        }
        request.session.modified = True
        return redirect("dashboard" if request.user.is_authenticated else "login")
    finally:
        session.close()

    try:
        # После привязки email нельзя терять активные internal squads в RWMS.
        subscription = rwms_client.get_user_by_username(username)
        if subscription:
            response = rwms_client.update_user(
                proto.UpdateUserRequest(
                    uuid=subscription.uuid,
                    email=new_email,
                    active_internal_squads=subscription.active_internal_squads,
                )
            )
            if response is None:
                logging.warning(f"failed to update rwms email for user {user_id}")
    except Exception as e:
        logging.exception(f"failed to sync rwms email for user {user_id}: {e}")

    return redirect("dashboard")


@login_required(login_url="/login/")
def create_support_ticket(request):
    if request.method != "POST":
        return dashboard_support_redirect()

    subject = request.POST.get("subject", "").strip()
    message = request.POST.get("message", "").strip()

    if not message:
        return dashboard_support_redirect()

    if not subject:
        subject = message[:80]

    db_session = session_factory()
    try:
        with db_session.begin():
            ticket = SupportTicket(
                user_id=request.user.id,
                status=SupportTicketStatus.OPEN,
                subject=subject[:256],
                updated_at=datetime.utcnow(),
            )
            db_session.add(ticket)
            db_session.flush()
            support_message = add_support_message(
                db_session,
                ticket,
                SupportTicketMessageSender.USER,
                message,
            )
            attach_support_attachments(
                db_session,
                support_message,
                request.FILES.getlist("attachments"),
            )
            ticket_id = ticket.id
    finally:
        db_session.close()

    if is_ajax(request):
        return JsonResponse({"status": "ok", "ticket_id": ticket_id})

    return dashboard_support_redirect()


@login_required(login_url="/login/")
def create_support_ticket_message(request, ticket_id):
    if request.method != "POST":
        return dashboard_support_redirect()

    message = request.POST.get("message", "").strip()
    if not message:
        return dashboard_support_redirect()

    db_session = session_factory()
    try:
        with db_session.begin():
            ticket = get_support_ticket_for_user(db_session, request.user.id, ticket_id)
            if not ticket:
                if is_ajax(request):
                    return JsonResponse({"status": "not_found"}, status=404)
                return dashboard_support_redirect()

            if ticket.status == SupportTicketStatus.CLOSED:
                ticket.status = SupportTicketStatus.OPEN
                ticket.closed_at = None

            support_message = add_support_message(
                db_session,
                ticket,
                SupportTicketMessageSender.USER,
                message,
            )
            attach_support_attachments(
                db_session,
                support_message,
                request.FILES.getlist("attachments"),
            )
    finally:
        db_session.close()

    if is_ajax(request):
        return JsonResponse({"status": "ok"})

    return dashboard_support_redirect()


@login_required(login_url="/login/")
def close_support_ticket(request, ticket_id):
    if request.method != "POST":
        return dashboard_support_redirect()

    db_session = session_factory()
    try:
        with db_session.begin():
            ticket = get_support_ticket_for_user(db_session, request.user.id, ticket_id)
            if ticket and ticket.status == SupportTicketStatus.OPEN:
                ticket.status = SupportTicketStatus.CLOSED
                ticket.closed_at = datetime.utcnow()
                ticket.updated_at = datetime.utcnow()
    finally:
        db_session.close()

    return dashboard_support_redirect()


@login_required(login_url="/login/")
def support_ticket_messages_json(request, ticket_id):
    db_session = session_factory()
    try:
        ticket = get_support_ticket_for_user(db_session, request.user.id, ticket_id)
        if not ticket:
            return JsonResponse({"status": "not_found"}, status=404)

        return JsonResponse(
            {
                "status": "ok",
                "ticket_status": ticket.status.value,
                "messages": support_messages_payload(db_session, ticket.id),
            }
        )
    finally:
        db_session.close()


@login_required(login_url="/login/")
def support_attachment(request, attachment_id):
    db_session = session_factory()
    try:
        row = (
            db_session.query(
                SupportTicketAttachment, SupportTicketMessage, SupportTicket
            )
            .join(
                SupportTicketMessage,
                SupportTicketAttachment.message_id == SupportTicketMessage.id,
            )
            .join(SupportTicket, SupportTicketMessage.ticket_id == SupportTicket.id)
            .filter(
                (SupportTicketAttachment.id == attachment_id)
                & (SupportTicket.user_id == request.user.id)
            )
            .first()
        )
        if not row:
            raise Http404("Attachment not found")

        attachment = row[0]
        path = (Path(settings.MEDIA_ROOT) / attachment.storage_path).resolve()
        media_root = Path(settings.MEDIA_ROOT).resolve()
        if media_root not in path.parents or not path.exists():
            raise Http404("Attachment not found")

        return FileResponse(
            path.open("rb"),
            content_type=attachment.content_type,
            filename=attachment.file_name,
        )
    finally:
        db_session.close()


def support_admin_login(request):
    if support_admin_is_authenticated(request):
        return redirect("support_admin_tickets")

    if request.method == "POST":
        password = request.POST.get("password", "")
        if settings.SUPPORT_ADMIN_PASSWORD and hmac.compare_digest(
            password,
            settings.SUPPORT_ADMIN_PASSWORD,
        ):
            request.session[SUPPORT_ADMIN_SESSION_KEY] = True
            request.session[SUPPORT_ADMIN_ROLE_SESSION_KEY] = SUPPORT_ADMIN_ROLE_ADMIN
            request.session.modified = True
            return redirect("support_admin_tickets")

        if settings.SUPPORT_STAFF_PASSWORD and hmac.compare_digest(
            password,
            settings.SUPPORT_STAFF_PASSWORD,
        ):
            request.session[SUPPORT_ADMIN_SESSION_KEY] = True
            request.session[SUPPORT_ADMIN_ROLE_SESSION_KEY] = SUPPORT_ADMIN_ROLE_SUPPORT
            request.session.modified = True
            return redirect("support_admin_tickets")

        logging.warning(
            "Support admin login failed: admin_password_configured=%s, "
            "staff_password_configured=%s",
            bool(settings.SUPPORT_ADMIN_PASSWORD),
            bool(settings.SUPPORT_STAFF_PASSWORD),
        )
        return render(
            request,
            "support_admin_login.html",
            {"error": "Неверный пароль."},
            status=403,
        )

    return render(request, "support_admin_login.html")


def support_admin_logout(request):
    request.session.pop(SUPPORT_ADMIN_SESSION_KEY, None)
    request.session.pop(SUPPORT_ADMIN_ROLE_SESSION_KEY, None)
    request.session.modified = True
    return redirect("support_admin_login")


def support_admin_tickets(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    status_filter = request.GET.get("status", "open")
    tickets_data = load_support_admin_tickets(status_filter)

    return render(
        request,
        "admin_dashboard.html",
        {
            "tickets": tickets_data["tickets"],
            "status_filter": tickets_data["status_filter"],
            "open_count": tickets_data["open_count"],
            "closed_count": tickets_data["closed_count"],
            "support_status_open": SupportTicketStatus.OPEN,
            "support_admin_role": support_admin_role(request),
            "support_admin_is_full_admin": support_admin_is_full_admin(request),
        },
    )


def support_admin_api_reply_templates(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "GET":
            templates = load_support_reply_templates(db_session, active_only=False)
            return JsonResponse(
                {
                    "status": "ok",
                    "templates": [
                        support_reply_template_payload(template)
                        for template in templates
                    ],
                }
            )

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        action = request.POST.get("action", "save")
        template_id = request.POST.get("template_id")

        if action == "delete":
            template = db_session.get(SupportReplyTemplate, int(template_id or 0))
            if not template:
                return JsonResponse({"status": "not_found"}, status=404)
            db_session.delete(template)
            db_session.commit()
            return JsonResponse({"status": "ok"})

        title = request.POST.get("title", "").strip()
        body = request.POST.get("body", "").strip()
        is_active = request.POST.get("is_active", "1") == "1"
        try:
            sort_order = int(request.POST.get("sort_order", "100"))
        except ValueError:
            sort_order = 100

        if not title or not body:
            return JsonResponse(
                {"status": "error", "message": "Заполните название и текст ответа"},
                status=400,
            )

        if template_id:
            template = db_session.get(SupportReplyTemplate, int(template_id))
            if not template:
                return JsonResponse({"status": "not_found"}, status=404)
        else:
            template = SupportReplyTemplate()
            db_session.add(template)

        template.title = title[:160]
        template.body = body
        template.sort_order = sort_order
        template.is_active = is_active
        template.updated_at = datetime.utcnow()
        db_session.commit()
        return JsonResponse(
            {
                "status": "ok",
                "template": support_reply_template_payload(template),
            }
        )
    finally:
        db_session.close()


def support_admin_api_config_templates(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "GET":
            templates = load_custom_config_templates(db_session)
            return JsonResponse(
                {
                    "status": "ok",
                    "templates": [
                        custom_config_template_payload(template)
                        for template in templates
                    ],
                }
            )

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        action = request.POST.get("action", "save")
        template_id = request.POST.get("template_id")

        if action == "delete":
            template = db_session.get(CustomConfigTemplate, int(template_id or 0))
            if not template:
                return JsonResponse({"status": "not_found"}, status=404)
            db_session.delete(template)
            db_session.commit()
            return JsonResponse({"status": "ok"})

        name = request.POST.get("name", "").strip()
        template_json = request.POST.get("template_json", "").strip()
        entry_name = request.POST.get("entry_name", "").strip() or None
        announce_text = request.POST.get("announce_text", "").strip() or None
        support_url = request.POST.get("support_url", "").strip() or None
        profile_update_interval = (
            request.POST.get("profile_update_interval", "").strip() or None
        )
        is_active = request.POST.get("is_active", "1") == "1"

        if not name or not template_json:
            return JsonResponse(
                {"status": "error", "message": "Заполните имя и JSON шаблона"},
                status=400,
            )

        try:
            json.loads(template_json)
        except json.JSONDecodeError as error:
            return JsonResponse(
                {
                    "status": "error",
                    "message": f"Ошибка JSON шаблона: строка {error.lineno}, колонка {error.colno}: {error.msg}",
                },
                status=400,
            )

        raw_headers = request.POST.get("additional_headers", "").strip()
        if raw_headers:
            try:
                additional_headers = json.loads(raw_headers)
            except json.JSONDecodeError as error:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": f"Ошибка JSON заголовков: строка {error.lineno}, колонка {error.colno}: {error.msg}",
                    },
                    status=400,
                )
            if not isinstance(additional_headers, dict):
                return JsonResponse(
                    {
                        "status": "error",
                        "message": "Дополнительные заголовки должны быть JSON-объектом",
                    },
                    status=400,
                )
        else:
            additional_headers = {}

        is_new_template = not template_id
        if template_id:
            template = db_session.get(CustomConfigTemplate, int(template_id))
            if not template:
                return JsonResponse({"status": "not_found"}, status=404)
        else:
            template = CustomConfigTemplate()

        if is_active:
            active_templates_query = db_session.query(CustomConfigTemplate).filter(
                CustomConfigTemplate.is_active.is_(True)
            )
            if template_id:
                active_templates_query = active_templates_query.filter(
                    CustomConfigTemplate.id != int(template_id)
                )
            active_templates_query.update(
                {CustomConfigTemplate.is_active: False},
                synchronize_session=False,
            )

        template.name = name[:160]
        template.template_json = template_json
        template.entry_name = entry_name[:256] if entry_name else None
        template.announce_text = announce_text
        template.support_url = support_url[:512] if support_url else None
        template.profile_update_interval = (
            profile_update_interval[:32] if profile_update_interval else None
        )
        template.additional_headers = additional_headers
        template.is_active = is_active
        template.updated_at = datetime.utcnow()
        if is_new_template:
            db_session.add(template)
        try:
            db_session.commit()
        except IntegrityError:
            db_session.rollback()
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Не удалось сохранить: проверьте уникальность имени конфига",
                },
                status=400,
            )

        return JsonResponse(
            {
                "status": "ok",
                "template": custom_config_template_payload(template),
            }
        )
    finally:
        db_session.close()


def support_admin_ticket_payload(ticket, user):
    return {
        "id": ticket.id,
        "status": ticket.status.value,
        "is_open": ticket.status == SupportTicketStatus.OPEN,
        "subject": ticket.subject,
        "updated_at": ticket.updated_at.strftime("%d.%m.%Y %H:%M"),
        "updated_at_iso": ticket.updated_at.isoformat() if ticket.updated_at else "",
        "user_id": user.id,
        "email": user.email or "",
        "telegram_id": str(user.telegram_id or ""),
        "url": reverse("support_admin_ticket_detail", args=[ticket.id]),
    }


def load_support_admin_tickets(status_filter):
    db_session = session_factory()
    try:
        query = db_session.query(SupportTicket, User).join(
            User,
            SupportTicket.user_id == User.id,
        )
        if status_filter == "closed":
            query = query.filter(SupportTicket.status == SupportTicketStatus.CLOSED)
        elif status_filter != "all":
            status_filter = "open"
            query = query.filter(SupportTicket.status == SupportTicketStatus.OPEN)

        tickets = query.order_by(SupportTicket.updated_at.desc()).all()
        open_count = (
            db_session.query(func.count(SupportTicket.id))
            .filter(SupportTicket.status == SupportTicketStatus.OPEN)
            .scalar()
        )
        closed_count = (
            db_session.query(func.count(SupportTicket.id))
            .filter(SupportTicket.status == SupportTicketStatus.CLOSED)
            .scalar()
        )

        return {
            "tickets": tickets,
            "ticket_payloads": [
                support_admin_ticket_payload(ticket, user) for ticket, user in tickets
            ],
            "status_filter": status_filter,
            "open_count": open_count,
            "closed_count": closed_count,
        }
    finally:
        db_session.close()


def support_admin_tickets_json(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    tickets_data = load_support_admin_tickets(request.GET.get("status", "open"))
    return JsonResponse(
        {
            "status": "ok",
            "status_filter": tickets_data["status_filter"],
            "open_count": tickets_data["open_count"],
            "closed_count": tickets_data["closed_count"],
            "tickets": tickets_data["ticket_payloads"],
        }
    )


def admin_parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def admin_money(value):
    return int(value or 0)


def admin_dt(value):
    if not value:
        return None
    if value.tzinfo:
        value = value.replace(tzinfo=None)
    return value


def admin_date_label(value, with_time=True):
    value = admin_dt(value)
    if not value:
        return "Нет данных"
    return value.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def get_tariff_display_name(tariff_id):
    tariff_names = {
        "threedays": "3 дня",
        "oneday": "1 день",
        "oneweek": "1 неделя",
        "month": "1 месяц",
        "threemonths": "3 месяца",
        "sixmonths": "6 месяцев",
        "year": "1 год",
    }
    return tariff_names.get(tariff_id, tariff_id or "Без тарифа")


def get_tariff_order(tariff_name):
    order = {
        "3 дня": 1,
        "1 день": 2,
        "1 неделя": 3,
        "1 месяц": 4,
        "3 месяца": 5,
        "6 месяцев": 6,
        "1 год": 7,
    }
    return order.get(tariff_name, 99)


def admin_find_user(db_session, value):
    value = (value or "").strip()
    if not value:
        return None

    normalized = value.lstrip("@")
    filters = [User.username == normalized, User.email == value]
    if normalized.isdigit():
        filters.append(User.telegram_id == int(normalized))

    return db_session.query(User).filter(or_(*filters)).first()


def admin_user_payload(user):
    if not user:
        return None

    now = datetime.utcnow()
    expire_at = admin_dt(user.expire_at)
    days_left = (expire_at - now).days if expire_at else None
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email or "",
        "telegram_id": str(user.telegram_id or ""),
        "expire_at": admin_date_label(user.expire_at, with_time=False),
        "days_left": days_left,
        "is_active": bool(expire_at and expire_at > now),
        "autopay_allow": bool(user.autopay_allow),
    }


def admin_traffic_status_payload(traffic):
    if not traffic:
        return "Нет данных"
    if traffic.passed_100mb:
        return "Активный: 100 MB+"
    if traffic.passed_5mb:
        return "Подключен: 5 MB+"
    if traffic.passed_0:
        return "Конфиг скачан"
    return "Не подключался"


def admin_payment_history(db_session, user):
    yk_payments = (
        db_session.query(YkPayment)
        .filter(YkPayment.user_id == user.id)
        .order_by(YkPayment.created_at.desc())
        .all()
    )
    wata_payments = (
        db_session.query(WataTransaction, WataInvoice)
        .join(WataInvoice, WataInvoice.order_id == WataTransaction.order_id)
        .filter(WataInvoice.user_id == user.id)
        .order_by(WataTransaction.payment_time.desc())
        .all()
    )

    history = []
    yk_ltv = 0
    wata_ltv = 0

    for payment in yk_payments:
        if payment.status == "succeeded":
            yk_ltv += admin_money(payment.amount)
        history.append(
            {
                "system": "YooKassa",
                "id": payment.payment_id,
                "date": admin_date_label(payment.created_at),
                "date_sort": admin_dt(payment.created_at) or datetime.min,
                "status": payment.status,
                "success": payment.status == "succeeded",
                "amount": admin_money(payment.amount),
                "currency": payment.currency,
                "tariff": get_tariff_display_name(payment.subscription_period),
                "trial": bool(payment.is_trial_promotion),
            }
        )

    for payment, invoice in wata_payments:
        if payment.transaction_status == "Paid":
            wata_ltv += admin_money(payment.amount)
        history.append(
            {
                "system": "Wata",
                "id": payment.transaction_id,
                "date": admin_date_label(payment.payment_time),
                "date_sort": admin_dt(payment.payment_time) or datetime.min,
                "status": payment.transaction_status,
                "success": payment.transaction_status == "Paid",
                "amount": admin_money(payment.amount),
                "currency": payment.currency,
                "tariff": invoice.tariff_id
                and get_tariff_display_name(invoice.tariff_id)
                or payment.order_description,
                "trial": False,
            }
        )

    history.sort(key=lambda item: item["date_sort"], reverse=True)
    for item in history:
        item.pop("date_sort", None)

    recurrent = (
        db_session.query(YkRecurrentPayment)
        .filter(YkRecurrentPayment.user_id == user.id)
        .first()
    )
    traffic = (
        db_session.query(UserTrafficProgress)
        .filter(UserTrafficProgress.user_id == user.id)
        .first()
    )
    first_seen = (
        db_session.query(func.min(EventLog.timestamp))
        .filter(EventLog.user_id == user.id)
        .scalar()
    )

    return {
        "user": admin_user_payload(user),
        "ltv": yk_ltv + wata_ltv,
        "autopay": {
            "yk": bool(recurrent),
            "wata": bool(user.autopay_allow),
            "yk_tariff": get_tariff_display_name(recurrent.subscription_period)
            if recurrent
            else "",
            "yk_amount": recurrent.amount if recurrent else None,
            "yk_currency": recurrent.currency if recurrent else "",
        },
        "traffic": admin_traffic_status_payload(traffic),
        "first_seen": admin_date_label(first_seen, with_time=False)
        if first_seen
        else "Нет данных",
        "history": history,
    }


def support_admin_api_user_payments(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        user = admin_find_user(db_session, request.GET.get("q"))
        if not user:
            return JsonResponse({"status": "not_found"}, status=404)
        return JsonResponse({"status": "ok", "result": admin_payment_history(db_session, user)})
    finally:
        db_session.close()


def build_admin_interval_stats(db_session, start_date, end_date):
    start_datetime = datetime.combine(start_date, time.min)
    end_datetime = datetime.combine(end_date, time.max)
    events = (
        db_session.query(EventLog.user_id, EventLog.event_payload)
        .filter(EventLog.event_type == "subscription_created")
        .filter(EventLog.timestamp >= start_datetime)
        .filter(EventLog.timestamp <= end_datetime)
        .all()
    )
    user_ids_by_traffic = {}
    all_user_ids = set()
    for user_id, payload in events:
        payload = payload or {}
        traffic_source = payload.get("traffic_source")
        user_ids_by_traffic.setdefault(traffic_source, set()).add(user_id)
        all_user_ids.add(user_id)

    referral_user_ids = [
        row[0]
        for row in db_session.query(User.id)
        .filter(User.referred_by_id.isnot(None))
        .filter(User.id.in_(all_user_ids or {-1}))
        .all()
    ]
    referral_count = len(referral_user_ids)
    bonus_rows = (
        db_session.query(
            ReferralBonus.bonus_type,
            func.count(ReferralBonus.id),
        )
        .filter(ReferralBonus.created_at >= start_datetime)
        .filter(ReferralBonus.created_at <= end_datetime)
        .filter(ReferralBonus.referral_id.in_(referral_user_ids or {-1}))
        .group_by(ReferralBonus.bonus_type)
        .all()
    )
    referral_bonus_counts = {bonus_type: count for bonus_type, count in bonus_rows}

    sources = []
    total_tariffs = {}
    totals = {
        "subscriptions": 0,
        "connections": 0,
        "payments": 0,
        "unique_paying_users": 0,
    }

    for traffic_source, user_ids in user_ids_by_traffic.items():
        if not user_ids:
            continue

        yk_rows = (
            db_session.query(YkPayment.subscription_period, func.count(YkPayment.id))
            .filter(YkPayment.status == "succeeded")
            .filter(YkPayment.user_id.in_(user_ids))
            .group_by(YkPayment.subscription_period)
            .all()
        )
        wata_rows = (
            db_session.query(WataInvoice.tariff_id, func.count(WataTransaction.id))
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(WataInvoice.user_id.in_(user_ids))
            .group_by(WataInvoice.tariff_id)
            .all()
        )

        tariff_stats = {}
        for tariff_id, count in [*yk_rows, *wata_rows]:
            tariff_name = get_tariff_display_name(tariff_id)
            tariff_stats[tariff_name] = tariff_stats.get(tariff_name, 0) + count
            total_tariffs[tariff_name] = total_tariffs.get(tariff_name, 0) + count

        yk_payers = {
            row[0]
            for row in db_session.query(YkPayment.user_id)
            .filter(YkPayment.status == "succeeded")
            .filter(YkPayment.user_id.in_(user_ids))
            .distinct()
            .all()
        }
        wata_payers = {
            row[0]
            for row in db_session.query(WataInvoice.user_id)
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(WataInvoice.user_id.in_(user_ids))
            .distinct()
            .all()
        }
        unique_payers = len(yk_payers | wata_payers)
        connections = (
            db_session.query(func.count(func.distinct(EventLog.user_id)))
            .filter(EventLog.event_type == "traffic_threshold_reached")
            .filter(EventLog.user_id.in_(user_ids))
            .filter(EventLog.event_payload["threshold"].astext.cast(Integer) == 0)
            .scalar()
            or 0
        )
        subscriptions = len(user_ids)
        payments = sum(tariff_stats.values())
        source = {
            "traffic_source": traffic_source,
            "label": "Direct" if traffic_source is None else f"TS_{traffic_source}",
            "subscriptions": subscriptions,
            "connections": connections,
            "unique_paying_users": unique_payers,
            "payments": payments,
            "connection_conversion": (connections / subscriptions * 100)
            if subscriptions
            else 0,
            "payment_conversion": (unique_payers / subscriptions * 100)
            if subscriptions
            else 0,
            "tariffs": [
                {"name": name, "count": count}
                for name, count in sorted(
                    tariff_stats.items(), key=lambda item: get_tariff_order(item[0])
                )
            ],
        }
        sources.append(source)
        totals["subscriptions"] += subscriptions
        totals["connections"] += connections
        totals["payments"] += payments
        totals["unique_paying_users"] += unique_payers

    sources.sort(key=lambda item: item["subscriptions"], reverse=True)
    totals["referrals"] = referral_count
    totals["referral_traffic"] = referral_bonus_counts.get(ReferralBonusType.TRAFFIC, 0)
    totals["referral_purchase"] = referral_bonus_counts.get(
        ReferralBonusType.PURCHASE, 0
    )
    totals["connection_conversion"] = (
        totals["connections"] / totals["subscriptions"] * 100
        if totals["subscriptions"]
        else 0
    )
    totals["payment_conversion"] = (
        totals["unique_paying_users"] / totals["subscriptions"] * 100
        if totals["subscriptions"]
        else 0
    )

    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "totals": totals,
        "sources": sources,
        "tariffs": [
            {"name": name, "count": count}
            for name, count in sorted(
                total_tariffs.items(), key=lambda item: get_tariff_order(item[0])
            )
        ],
    }


def support_admin_api_stats(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    try:
        today = date.today()
        start_date = admin_parse_date(request.GET.get("start")) if request.GET.get("start") else today - timedelta(days=30)
        end_date = admin_parse_date(request.GET.get("end")) if request.GET.get("end") else today
        if start_date > end_date:
            return JsonResponse({"status": "error", "message": "Начальная дата больше конечной"}, status=400)
    except ValueError:
        return JsonResponse({"status": "error", "message": "Неверный формат даты"}, status=400)

    db_session = session_factory()
    try:
        return JsonResponse({"status": "ok", "result": build_admin_interval_stats(db_session, start_date, end_date)})
    finally:
        db_session.close()


def support_admin_api_stats_source_users(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    try:
        today = date.today()
        start_date = (
            admin_parse_date(request.GET.get("start"))
            if request.GET.get("start")
            else today - timedelta(days=30)
        )
        end_date = (
            admin_parse_date(request.GET.get("end"))
            if request.GET.get("end")
            else today
        )
        if start_date > end_date:
            return JsonResponse(
                {"status": "error", "message": "Начальная дата больше конечной"},
                status=400,
            )
    except ValueError:
        return JsonResponse(
            {"status": "error", "message": "Неверный формат даты"},
            status=400,
        )

    traffic_source = request.GET.get("traffic_source")
    if traffic_source == "__direct__":
        traffic_source = None

    try:
        page = max(1, int(request.GET.get("page") or "1"))
        per_page = min(50, max(5, int(request.GET.get("per_page") or "20")))
    except ValueError:
        return JsonResponse(
            {"status": "error", "message": "Неверные параметры пагинации"},
            status=400,
        )

    start_datetime = datetime.combine(start_date, time.min)
    end_datetime = datetime.combine(end_date, time.max)
    db_session = session_factory()
    try:
        query = (
            db_session.query(
                EventLog.user_id,
                func.max(EventLog.timestamp).label("last_seen"),
            )
            .filter(EventLog.event_type == "subscription_created")
            .filter(EventLog.timestamp >= start_datetime)
            .filter(EventLog.timestamp <= end_datetime)
        )
        if traffic_source is None:
            query = query.filter(EventLog.event_payload["traffic_source"].astext.is_(None))
        else:
            query = query.filter(
                EventLog.event_payload["traffic_source"].astext == str(traffic_source)
            )
        query = query.group_by(EventLog.user_id).order_by(func.max(EventLog.timestamp).desc())
        total = query.count()
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
        user_ids = [row.user_id for row in rows]
        users_by_id = {
            user.id: user
            for user in db_session.query(User).filter(User.id.in_(user_ids or {-1})).all()
        }
        yk_paying_user_ids = {
            row[0]
            for row in db_session.query(YkPayment.user_id)
            .filter(YkPayment.status == "succeeded")
            .filter(YkPayment.user_id.in_(user_ids or {-1}))
            .distinct()
            .all()
        }
        wata_paying_user_ids = {
            row[0]
            for row in db_session.query(WataInvoice.user_id)
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(WataInvoice.user_id.in_(user_ids or {-1}))
            .distinct()
            .all()
        }
        paying_user_ids = yk_paying_user_ids | wata_paying_user_ids
        connected_user_ids = {
            row[0]
            for row in db_session.query(UserTrafficProgress.user_id)
            .filter(UserTrafficProgress.user_id.in_(user_ids or {-1}))
            .filter(UserTrafficProgress.passed_0.is_(True))
            .all()
        }
        users_payload = []
        for user_id in user_ids:
            user = users_by_id.get(user_id)
            if not user:
                continue
            payload = admin_user_payload(user)
            payload["has_paid"] = user_id in paying_user_ids
            payload["has_connected"] = user_id in connected_user_ids
            users_payload.append(payload)
        return JsonResponse(
            {
                "status": "ok",
                "result": {
                    "users": users_payload,
                    "pagination": {
                        "page": page,
                        "per_page": per_page,
                        "total": total,
                        "total_pages": (total + per_page - 1) // per_page,
                    },
                },
            }
        )
    finally:
        db_session.close()


def support_admin_api_payment_info(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    payment_id = (request.GET.get("payment_id") or "").strip()
    if not payment_id:
        return JsonResponse({"status": "error", "message": "Введите ID платежа"}, status=400)

    db_session = session_factory()
    try:
        payment = db_session.query(YkPayment).filter(YkPayment.payment_id == payment_id).first()
        system = "YooKassa"
        invoice = None
        if not payment:
            payment = db_session.query(WataTransaction).filter(WataTransaction.transaction_id == payment_id).first()
            system = "Wata"
            if payment:
                invoice = db_session.query(WataInvoice).filter(WataInvoice.order_id == payment.order_id).first()
        if not payment:
            return JsonResponse({"status": "not_found"}, status=404)

        user_id = invoice.user_id if invoice else payment.user_id
        user = db_session.get(User, user_id)
        if not user:
            return JsonResponse({"status": "not_found", "message": "Пользователь не найден"}, status=404)

        payment_payload = admin_payment_info_payload(db_session, payment, user, system, invoice)
        return JsonResponse({"status": "ok", "result": payment_payload})
    finally:
        db_session.close()


def support_admin_api_payments(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    try:
        limit = min(50, max(5, int(request.GET.get("limit") or "10")))
    except ValueError:
        limit = 10

    db_session = session_factory()
    try:
        yk_payments = (
            db_session.query(YkPayment, User)
            .join(User, User.id == YkPayment.user_id)
            .order_by(YkPayment.created_at.desc())
            .limit(limit)
            .all()
        )
        wata_payments = (
            db_session.query(WataTransaction, WataInvoice, User)
            .join(WataInvoice, WataInvoice.order_id == WataTransaction.order_id)
            .join(User, User.id == WataInvoice.user_id)
            .order_by(WataTransaction.payment_time.desc())
            .limit(limit)
            .all()
        )

        payments = []
        for payment, user in yk_payments:
            payments.append(
                {
                    "id": payment.payment_id,
                    "date": admin_date_label(payment.created_at, with_time=False),
                    "date_sort": admin_dt(payment.created_at) or datetime.min,
                    "user": user.username or user.email or str(user.id),
                    "tariff": get_tariff_display_name(payment.subscription_period),
                    "amount": admin_money(payment.amount),
                    "currency": payment.currency,
                    "status": {
                        "succeeded": "Успешен",
                        "pending": "В обработке",
                        "canceled": "Ошибка",
                        "waiting_for_capture": "Ожидает",
                    }.get(payment.status, payment.status),
                    "success": payment.status == "succeeded",
                }
            )

        for payment, invoice, user in wata_payments:
            payments.append(
                {
                    "id": payment.transaction_id,
                    "date": admin_date_label(payment.payment_time, with_time=False),
                    "date_sort": admin_dt(payment.payment_time) or datetime.min,
                    "user": user.username or user.email or str(user.id),
                    "tariff": invoice.tariff_id
                    and get_tariff_display_name(invoice.tariff_id)
                    or payment.order_description,
                    "amount": admin_money(payment.amount),
                    "currency": payment.currency,
                    "status": "Успешен"
                    if payment.transaction_status == "Paid"
                    else "Ошибка",
                    "success": payment.transaction_status == "Paid",
                }
            )

        payments.sort(key=lambda item: item["date_sort"], reverse=True)
        payments = payments[:limit]
        for payment in payments:
            payment.pop("date_sort", None)

        return JsonResponse({"status": "ok", "payments": payments, "total": len(payments)})
    finally:
        db_session.close()


def admin_payment_info_payload(db_session, payment, user, system, invoice=None):
    recurrent = db_session.query(YkRecurrentPayment).filter(YkRecurrentPayment.user_id == user.id).first()
    yk_ltv = db_session.query(func.sum(YkPayment.amount)).filter(YkPayment.user_id == user.id, YkPayment.status == "succeeded").scalar() or 0
    wata_ltv = (
        db_session.query(func.sum(WataTransaction.amount))
        .join(WataInvoice, WataInvoice.order_id == WataTransaction.order_id)
        .filter(WataInvoice.user_id == user.id)
        .filter(WataTransaction.transaction_status == "Paid")
        .scalar()
        or 0
    )
    yk_count = db_session.query(func.count(YkPayment.id)).filter(YkPayment.user_id == user.id).scalar() or 0
    wata_count = (
        db_session.query(func.count(WataTransaction.id))
        .join(WataInvoice, WataInvoice.order_id == WataTransaction.order_id)
        .filter(WataInvoice.user_id == user.id)
        .scalar()
        or 0
    )

    if system == "YooKassa":
        status = payment.status
        is_success = status == "succeeded"
        payload = {
            "id": payment.payment_id,
            "date": admin_date_label(payment.created_at),
            "amount": admin_money(payment.amount),
            "currency": payment.currency,
            "tariff": get_tariff_display_name(payment.subscription_period),
            "type": "Пробный период" if payment.is_trial_promotion else "Обычный платеж",
            "status": {"succeeded": "Успешен", "pending": "В обработке", "canceled": "Отменен", "waiting_for_capture": "Ожидает подтверждения"}.get(status, status),
            "success": is_success,
        }
    else:
        status = payment.transaction_status
        payload = {
            "id": payment.transaction_id,
            "date": admin_date_label(payment.payment_time),
            "amount": admin_money(payment.amount),
            "currency": payment.currency,
            "tariff": (invoice and get_tariff_display_name(invoice.tariff_id)) or payment.order_description,
            "type": "Обычный платеж",
            "status": "Успешен" if status == "Paid" else status,
            "success": status == "Paid",
        }

    return {
        "system": system,
        "payment": payload,
        "user": admin_user_payload(user),
        "recurrent": {
            "active": bool(recurrent or user.autopay_allow),
            "label": (
                f"YooKassa: {get_tariff_display_name(recurrent.subscription_period)}, {recurrent.amount} {recurrent.currency}"
                if recurrent
                else ("Wata разрешен" if user.autopay_allow else "Не активен")
            ),
        },
        "ltv": admin_money(yk_ltv) + admin_money(wata_ltv),
        "payments_count": yk_count + wata_count,
    }


def support_admin_api_referrals(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        user = admin_find_user(db_session, request.GET.get("q"))
        top = admin_referral_top(db_session)
        if not user:
            return JsonResponse({"status": "not_found", "top": top}, status=404)
        return JsonResponse({"status": "ok", "result": admin_referral_payload(db_session, user), "top": top})
    finally:
        db_session.close()


def admin_referral_top(db_session):
    rows = (
        db_session.query(
            User.username,
            User.telegram_id,
            func.count(ReferralBonus.id).label("ref_count"),
            func.sum(ReferralBonus.days_added).label("total_days"),
        )
        .join(ReferralBonus, User.id == ReferralBonus.referrer_id)
        .group_by(User.id, User.username, User.telegram_id)
        .order_by(func.sum(ReferralBonus.days_added).desc())
        .limit(10)
        .all()
    )
    return [
        {
            "name": f"@{row.username}" if row.username else f"ID:{row.telegram_id}",
            "username": row.username or "",
            "telegram_id": str(row.telegram_id or ""),
            "ref_count": row.ref_count,
            "total_days": admin_money(row.total_days),
        }
        for row in rows
    ]


def admin_successful_payment_count(db_session, user_id):
    yk_count = db_session.query(func.count(YkPayment.id)).filter(YkPayment.user_id == user_id, YkPayment.status == "succeeded").scalar() or 0
    wata_count = (
        db_session.query(func.count(WataTransaction.id))
        .join(WataInvoice, WataInvoice.order_id == WataTransaction.order_id)
        .filter(WataInvoice.user_id == user_id)
        .filter(WataTransaction.transaction_status == "Paid")
        .scalar()
        or 0
    )
    return yk_count + wata_count


def admin_referral_payload(db_session, user):
    referrals = db_session.query(User).filter(User.referred_by_id == user.id).order_by(User.id.desc()).all()
    bonuses = db_session.query(ReferralBonus).filter(ReferralBonus.referrer_id == user.id).all()
    bonuses_by_referral = {}
    for bonus in bonuses:
        bonuses_by_referral.setdefault(bonus.referral_id, []).append(bonus)

    referral_rows = []
    nodes = [admin_referral_graph_node(user, root=True)]
    edges = []
    for referral in referrals:
        user_bonuses = bonuses_by_referral.get(referral.id, [])
        payment_count = admin_successful_payment_count(db_session, referral.id)
        referral_rows.append(
            {
                "user": admin_user_payload(referral),
                "bonus_days": sum(admin_money(b.days_added) for b in user_bonuses),
                "bonuses": [
                    {
                        "type": bonus.bonus_type.value,
                        "days": admin_money(bonus.days_added),
                        "created_at": admin_date_label(bonus.created_at),
                    }
                    for bonus in user_bonuses
                ],
                "payments_count": payment_count,
                "paid": payment_count > 0,
                "children_count": db_session.query(func.count(User.id)).filter(User.referred_by_id == referral.id).scalar() or 0,
            }
        )
        nodes.append(admin_referral_graph_node(referral))
        edges.append({"from": user.id, "to": referral.id})
        children = db_session.query(User).filter(User.referred_by_id == referral.id).order_by(User.id.desc()).limit(30).all()
        for child in children:
            nodes.append(admin_referral_graph_node(child))
            edges.append({"from": referral.id, "to": child.id})

    return {
        "user": admin_user_payload(user),
        "summary": {
            "referrals": len(referrals),
            "paid_referrals": sum(1 for row in referral_rows if row["paid"]),
            "bonus_days": sum(row["bonus_days"] for row in referral_rows),
        },
        "referrals": referral_rows,
        "graph": {"nodes": nodes, "edges": edges},
    }


def admin_referral_graph_node(user, root=False):
    return {
        "id": user.id,
        "label": f"@{user.username}" if user.username else f"ID {user.telegram_id or user.id}",
        "root": root,
    }


def support_admin_api_subscription_manage(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response
    if request.method != "POST":
        return JsonResponse({"status": "error"}, status=405)

    action = request.POST.get("action")
    query = request.POST.get("q")
    db_session = session_factory()
    try:
        user = admin_find_user(db_session, query)
        if not user:
            return JsonResponse({"status": "not_found"}, status=404)

        if action == "preview":
            return JsonResponse({"status": "ok", "result": {"user": admin_user_payload(user)}})

        if action == "set_trial_hour":
            target_expire = datetime.now(timezone.utc) + timedelta(hours=1)
        elif action == "extend":
            try:
                days = int(request.POST.get("days") or "0")
            except ValueError:
                return JsonResponse({"status": "error", "message": "Дни должны быть числом"}, status=400)
            if days < 1:
                return JsonResponse({"status": "error", "message": "Интервал должен быть больше нуля"}, status=400)
            current_expire = admin_dt(user.expire_at)
            base = max(current_expire or datetime.utcnow(), datetime.utcnow())
            target_expire = base.replace(tzinfo=timezone.utc) + timedelta(days=days)
        else:
            return JsonResponse({"status": "error", "message": "Неизвестное действие"}, status=400)

        old_expire = user.expire_at
        user.expire_at = target_expire.replace(tzinfo=None)
        db_session.commit()

        rwms_user = rwms_client.get_user_by_username(user.username)
        rwms_updated = False
        if rwms_user:
            user_email = rwms_user.email if rwms_user.email and "@" in rwms_user.email else None
            active_squads = [squad.uuid for squad in rwms_user.active_internal_squads]
            response = rwms_client.update_user(
                proto.UpdateUserRequest(
                    uuid=rwms_user.uuid,
                    email=user_email,
                    telegram_id=rwms_user.telegram_id,
                    expire_at=target_expire,
                    status=proto.UserStatus.ACTIVE,
                    traffic_limit_strategy=proto.TrafficLimitStrategy.NO_RESET,
                    active_internal_squads=active_squads,
                )
            )
            rwms_updated = response is not None

        return JsonResponse(
            {
                "status": "ok",
                "result": {
                    "user": admin_user_payload(user),
                    "old_expire_at": admin_date_label(old_expire),
                    "new_expire_at": admin_date_label(user.expire_at),
                    "rwms_updated": rwms_updated,
                },
            }
        )
    finally:
        db_session.close()


def support_admin_ticket_detail(request, ticket_id):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        row = (
            db_session.query(SupportTicket, User)
            .join(User, SupportTicket.user_id == User.id)
            .filter(SupportTicket.id == ticket_id)
            .first()
        )
        if not row:
            return redirect("support_admin_tickets")

        ticket, user = row
        support_messages = load_support_messages_with_attachments(
            db_session,
            ticket.id,
        )
        reply_templates = load_support_reply_templates(db_session, active_only=True)
    finally:
        db_session.close()

    return render(
        request,
        "support_admin_ticket_detail.html",
        {
            "ticket": ticket,
            "ticket_user": user,
            "support_messages": support_messages,
            "reply_templates": reply_templates,
            "support_status_open": SupportTicketStatus.OPEN,
            "support_sender_user": SupportTicketMessageSender.USER,
        },
    )


def support_admin_create_message(request, ticket_id):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    if request.method != "POST":
        return redirect("support_admin_ticket_detail", ticket_id=ticket_id)

    message = request.POST.get("message", "").strip()
    if not message:
        return redirect("support_admin_ticket_detail", ticket_id=ticket_id)

    db_session = session_factory()
    try:
        with db_session.begin():
            ticket = (
                db_session.query(SupportTicket)
                .filter(SupportTicket.id == ticket_id)
                .first()
            )
            if not ticket:
                return redirect("support_admin_tickets")

            if ticket.status == SupportTicketStatus.CLOSED:
                ticket.status = SupportTicketStatus.OPEN
                ticket.closed_at = None

            support_message = add_support_message(
                db_session,
                ticket,
                SupportTicketMessageSender.SUPPORT,
                message,
            )
            attach_support_attachments(
                db_session,
                support_message,
                request.FILES.getlist("attachments"),
            )
    finally:
        db_session.close()

    if is_ajax(request):
        return JsonResponse({"status": "ok"})

    return redirect("support_admin_ticket_detail", ticket_id=ticket_id)


def support_admin_ticket_messages_json(request, ticket_id):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        ticket = (
            db_session.query(SupportTicket)
            .filter(SupportTicket.id == ticket_id)
            .first()
        )
        if not ticket:
            return JsonResponse({"status": "not_found"}, status=404)

        return JsonResponse(
            {
                "status": "ok",
                "ticket_status": ticket.status.value,
                "messages": support_messages_payload(
                    db_session,
                    ticket.id,
                    admin=True,
                ),
            }
        )
    finally:
        db_session.close()


def support_admin_close_ticket(request, ticket_id):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    if request.method == "POST":
        db_session = session_factory()
        try:
            with db_session.begin():
                ticket = (
                    db_session.query(SupportTicket)
                    .filter(SupportTicket.id == ticket_id)
                    .first()
                )
                if ticket:
                    ticket.status = SupportTicketStatus.CLOSED
                    ticket.closed_at = datetime.utcnow()
                    ticket.updated_at = datetime.utcnow()
        finally:
            db_session.close()

    return redirect("support_admin_ticket_detail", ticket_id=ticket_id)


def support_admin_reopen_ticket(request, ticket_id):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    if request.method == "POST":
        db_session = session_factory()
        try:
            with db_session.begin():
                ticket = (
                    db_session.query(SupportTicket)
                    .filter(SupportTicket.id == ticket_id)
                    .first()
                )
                if ticket:
                    ticket.status = SupportTicketStatus.OPEN
                    ticket.closed_at = None
                    ticket.updated_at = datetime.utcnow()
        finally:
            db_session.close()

    return redirect("support_admin_ticket_detail", ticket_id=ticket_id)


def support_admin_delete_ticket(request, ticket_id):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    if request.method == "POST":
        db_session = session_factory()
        try:
            with db_session.begin():
                ticket = (
                    db_session.query(SupportTicket)
                    .filter(SupportTicket.id == ticket_id)
                    .first()
                )
                if ticket:
                    delete_support_ticket_with_files(db_session, ticket)
        finally:
            db_session.close()

    return redirect("support_admin_tickets")


def support_admin_attachment(request, attachment_id):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        attachment = (
            db_session.query(SupportTicketAttachment)
            .filter(SupportTicketAttachment.id == attachment_id)
            .first()
        )
        if not attachment:
            raise Http404("Attachment not found")

        path = (Path(settings.MEDIA_ROOT) / attachment.storage_path).resolve()
        media_root = Path(settings.MEDIA_ROOT).resolve()
        if media_root not in path.parents or not path.exists():
            raise Http404("Attachment not found")

        return FileResponse(
            path.open("rb"),
            content_type=attachment.content_type,
            filename=attachment.file_name,
        )
    finally:
        db_session.close()


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
        tariff_id = request.POST.get("tariff_id")
        raw_purchase_token = None
        use_permanent_purchase_link = False
        payment_status_url = None
        tracking_params = get_tracking_params(request)
        tracking_cookies = {
            key: request.COOKIES.get(f"tracking_{key}") for key in TRACKING_PARAM_KEYS
        }

        logging.info(
            "payment tracking diagnostics: post_ts=%s get_ts=%s session_ts=%s "
            "tracking_cookies=%s cookie_keys=%s",
            request.POST.get("ts"),
            request.GET.get("ts"),
            request.session.get("tracking_ts"),
            tracking_cookies,
            sorted(request.COOKIES.keys()),
        )

        logging.info(
            "payment request started: host=%s referer=%s tariff_id=%s "
            "email_present=%s gateway=%s tracking=%s permanent_link=%s",
            request.get_host(),
            request.headers.get("referer", ""),
            tariff_id,
            bool(email_raw),
            settings.PAYMENT_GATEWAY,
            tracking_params,
            request.POST.get("login_link_kind") == "purchase_permanent",
        )

        if not email_raw:
            logging.warning("payment request rejected: missing email, tariff_id=%s", tariff_id)
            return HttpResponse("Email обязателен", status=400)

        email = email_raw.lower().strip()

        if not email or not tariff_id:
            logging.warning(
                "payment request rejected: email_or_tariff_missing email_present=%s tariff_id=%s",
                bool(email),
                tariff_id,
            )
            return HttpResponse("Не указан email или тариф", status=400)

        tariff = next((t for t in ACTUAL_TARIFFS if t.db_tariff_id == tariff_id), None)
        if not tariff:
            logging.warning(
                "payment request rejected: tariff not found tariff_id=%s email=%s",
                tariff_id,
                email,
            )
            return HttpResponse("Выбранный тариф не найден", status=400)

        logging.info(
            "payment request accepted: email=%s tariff_id=%s price=%s",
            email,
            tariff.db_tariff_id,
            tariff.price,
        )

        db_session = session_factory()
        try:
            # Ищем или создаем пользователя
            user = db_session.query(User).filter(User.email == email).first()
            if not user:
                user = create_site_user(db_session, email, request)
                logging.info(
                    "payment user created: email=%s user_id=%s username=%s tariff_id=%s",
                    email,
                    user.id,
                    user.username,
                    tariff.db_tariff_id,
                )
            else:
                is_authenticated_payment = (
                    request.user.is_authenticated
                    and str(request.user.id) == str(user.id)
                )
                if is_authenticated_payment:
                    logging.info(
                        "payment existing authenticated user tracking preserved: "
                        "email=%s user_id=%s username=%s tariff_id=%s",
                        email,
                        user.id,
                        user.username,
                        tariff.db_tariff_id,
                    )
                else:
                    registration_context = get_registration_context(request, db_session)
                    sync_existing_user_tracking(
                        db_session,
                        user,
                        registration_context["traffic_source"],
                        registration_context["ymid"],
                    )
                logging.info(
                    "payment existing user found: email=%s user_id=%s username=%s tariff_id=%s",
                    email,
                    user.id,
                    user.username,
                    tariff.db_tariff_id,
                )

            use_permanent_purchase_link = (
                request.POST.get("login_link_kind") == "purchase_permanent"
            )
            base_url = get_current_base_url(request)
            payment_success_redirect_url = f"{base_url}/dashboard/"
            payment_fail_redirect_url = f"{base_url}/"
            login_link = None

            if use_permanent_purchase_link:
                raw_purchase_token = create_purchase_login_token(db_session, user)
                login_link = build_purchase_login_link(request, raw_purchase_token)
                payment_status_url = build_payment_status_url(request, raw_purchase_token)
                payment_success_redirect_url = payment_status_url
                payment_fail_redirect_url = append_query_params(
                    payment_status_url,
                    {"result": "failed"},
                )
                logging.info(
                    "created permanent purchase login link: email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )

            if settings.PAYMENT_GATEWAY.lower() == "wata":
                logging.info(
                    "creating wata invoice: email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )
                created_payment = create_wata_payment_sync(
                    wata_host=settings.WATA_HOST,
                    wata_token=settings.WATA_TOKEN,
                    tariff=tariff,
                    success_redirect_url=payment_success_redirect_url,
                    fail_redirect_url=payment_fail_redirect_url,
                )

                confirmation_url = created_payment.confirmation_url
                if use_permanent_purchase_link:
                    login_token = get_purchase_login_token(
                        db_session,
                        raw_purchase_token,
                    )
                    login_token.payment_gateway = "wata"
                    login_token.payment_reference = created_payment.reference

                save_wata_invoice(
                    session=db_session,
                    invoice_json=created_payment.payload,
                    tariff_id=tariff.db_tariff_id,
                    email=email,
                )

                logging.info(
                    f"an invoice for the {tariff.db_tariff_id} tariff has been created for "
                    f"{email}, confirmation url: {confirmation_url}"
                )
            else:
                logging.info(
                    "creating yookassa payment: email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )
                created_payment = create_yk_payment_sync(
                    shop_id=settings.YOOKASSA_SHOP_ID,
                    secret=settings.YOOKASSA_SECRET_KEY,
                    tariff=tariff,
                    username=user.username,
                    telegram_id=user.telegram_id or 0,
                    return_url=payment_success_redirect_url,
                )
                confirmation_url = created_payment.confirmation_url
                if use_permanent_purchase_link:
                    login_token = get_purchase_login_token(
                        db_session,
                        raw_purchase_token,
                    )
                    login_token.payment_gateway = "yookassa"
                    login_token.payment_reference = created_payment.reference

            if use_permanent_purchase_link:
                product_name = (
                    "Monkey Island VPS"
                    if get_site_role(request) in ("vps", "vps_direct_sale")
                    else "VPN Monkey Island"
                )
                email_subject = f"Ссылка доступа {product_name}"
                email_title = "Доступ готов"
                email_intro = f"Мы подготовили для вас доступ {product_name}."
                login_link_note = (
                    "После оплаты зайдите по кнопке ниже: ссылка постоянная и "
                    "откроет оплаченный доступ, инструкции для устройств и поддержку."
                )
                email_button_text = "Открыть доступ"
                email_footer = (
                    f"Если вы не оформляли {product_name}, просто "
                    "проигнорируйте это письмо."
                )
            else:
                magic = MagicToken(user_id=user.id)
                db_session.add(magic)
                db_session.flush()
                login_link = f"{get_current_base_url(request)}/login/magic/{magic.token}/"
                logging.info(
                    "created short payment magic link: email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )
                email_subject = "Ссылка на личный кабинет Monkey Island"
                email_title = "Кабинет уже готов"
                email_intro = "Мы создали для вас личный кабинет Monkey Island."
                login_link_note = (
                    "После оплаты зайдите по кнопке ниже: ссылка действует "
                    "15 минут и откроет VPN-подписку, инструкции для "
                    "устройств и поддержку."
                )
                email_button_text = "Открыть кабинет"
                email_footer = (
                    "Если вы не оформляли VPN Monkey Island, просто "
                    "проигнорируйте это письмо."
                )

            db_session.commit()
            logging.info(
                "payment db transaction committed: email=%s user_id=%s tariff_id=%s",
                email,
                user.id,
                tariff.db_tariff_id,
            )

            try:
                send_magic_link_email(
                    email,
                    login_link,
                    subject=email_subject,
                    template_context={
                        "title": email_title,
                        "intro": email_intro,
                        "note": login_link_note,
                        "button_text": email_button_text,
                        "footer": email_footer,
                    },
                )
                logging.info(
                    "payment login email sent: email=%s user_id=%s tariff_id=%s permanent_link=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                    use_permanent_purchase_link,
                )
            except Exception as e:
                logging.exception(f"failed to send payment magic link to {email}: {e}")

            logging.info(
                "payment redirecting to confirmation_url: email=%s user_id=%s tariff_id=%s",
                email,
                user.id,
                tariff.db_tariff_id,
            )
            return redirect(confirmation_url)

        except Exception as e:
            db_session.rollback()
            logging.exception("Pay error")
            messages.error(request, "Ошибка платежной системы")
            if use_permanent_purchase_link and payment_status_url:
                return redirect(
                    append_query_params(payment_status_url, {"result": "failed"})
                )
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
        "Disallow: /login/purchase/",
        "Disallow: /login/telegram/",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def dynamic_manifest(request):
    site_role = get_site_role(request)
    app_name = (
        "Monkey Island VPS" if site_role == "vps_direct_sale" else "VPN Monkey Island"
    )
    start_url = "/" if site_role == "vps_direct_sale" else "/dashboard/"
    data = {
        "name": app_name,
        "short_name": app_name,
        "id": "/",
        "start_url": start_url,
        "scope": "/",
        "display": "standalone",
        "background_color": "#1a1a1a",
        "theme_color": "#ff9900",
        "icons": [
            {
                "src": "/static/icons/icon-192x192.png",
                "sizes": "192x192",
                "type": "image/png",
            },
            {
                "src": "/static/icons/icon-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
            },
        ],
    }
    return JsonResponse(data)
