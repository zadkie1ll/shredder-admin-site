import os
import uuid
import hmac
import hashlib
import logging
import base64
import json
import resend
import secrets
import httpx
import jinja2
from time import monotonic
from pathlib import Path
from datetime import datetime
from datetime import date
from datetime import time
from datetime import timedelta
from datetime import timezone
from urllib.parse import parse_qsl
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
from sqlalchemy import union_all
from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import insert
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy import Text
from sqlalchemy import TIMESTAMP
from sqlalchemy.exc import IntegrityError
from common.models.db import User
from common.models.db import TemporarySquadBan
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
from common.models.db import ClientUaRule
from common.models.db import CustomConfigTemplate
from common.models.db import SupportTicket
from common.models.db import SupportTicketMessage
from common.models.db import SupportTicketMessageSender
from common.models.db import SupportTicketStatus
from common.models.db import SupportTicketAttachment
from common.models.db import SupportReplyTemplate
from common.models.db import SystemSetting
from django.contrib.auth.hashers import check_password
from django.contrib.auth.hashers import make_password
from common.models.db import AdminAccount
from common.models.db import AdminAuditLog
from common.models.db import Broadcast
from common.models.db import BroadcastDelivery
from common.models.segments import ADMIN_SEGMENTS
from common.models.segments import segment_count_sql
from common.models.db import AdminDirectMessage
from common.models.db import AdminDirectMessageDelivery
from common.models.db import RwmsSyncMismatch
from common.models.db import UserBlock
from common.models.db import UserDiscount
from common.models.db import PromoBatch
from common.models.db import PromoCode
from common.models.db import ReferralProgramBlock
from common.models.db import CensorCheck
from common.models.db import CensorCheckRun
from common.models.db import RipeApiKey
from engine.user_block import ACCOUNT_BLOCKED_MESSAGE
from engine.user_block import is_user_blocked
from common.models.settings import BOOL_RUNTIME_SETTINGS
from common.models.settings import BOT_APPLE_RECOMMENDED_APP_SETTING
from common.models.settings import BOT_JOIN_REFERRER_BONUS_DAYS_SETTING
from common.models.settings import BOT_PURCHASE_REFERRER_BONUS_DAYS_SETTING
from common.models.settings import BOT_REFERRAL_REGISTRATION_AUTOBLOCK_ENABLED_SETTING
from common.models.settings import BOT_REFERRAL_REGISTRATION_BURST_LIMIT_SETTING
from common.models.settings import (
    BOT_REFERRAL_REGISTRATION_BURST_WINDOW_MINUTES_SETTING,
)
from common.models.settings import BOT_TRAFFIC_REFERRER_BONUS_DAYS_SETTING
from common.models.settings import BOT_TRAFFIC_USAGE_ALERT_GB_SETTING
from common.models.settings import BOT_TRAFFIC_USAGE_SUSPICIOUS_GB_SETTING
from common.models.settings import CSV_INT_RUNTIME_SETTINGS
from common.models.settings import CSV_STR_RUNTIME_SETTINGS
from common.models.settings import ENUM_RUNTIME_SETTINGS
from common.models.settings import INT_RUNTIME_SETTINGS
from common.models.settings import NON_NEGATIVE_INT_RUNTIME_SETTINGS
from common.models.settings import POSITIVE_FLOAT_RUNTIME_SETTINGS
from common.models.settings import POSITIVE_INT_RUNTIME_SETTINGS
from common.models.settings import RUNTIME_SETTING_DESCRIPTIONS
from common.models.settings import RUNTIME_SETTING_KEYS
from common.models.settings import SENSITIVE_RUNTIME_SETTINGS
from common.models.settings import SITE_TRIAL_REGISTRATION_ENABLED_SETTING
from common.models.settings import TARIFF_PRICE_SETTINGS
from common.models import analytics_event
from common.models.tariff import Tariff
from common.models.tariff import TrialPromotionTariff
from common.models.tariff import OneDayTariff
from common.models.tariff import OneMonthTariff
from common.models.tariff import OneYearTariff
from common.models.tariff import ThreeMonthsTariff
from common.rwms_client_sync import RwmsClientSync

from . import node_traffic
from . import ripe_atlas
from .rwms_helpers import create_user
from .rwms_helpers import create_user_until
from .encrypt_happ_url import encrypt_happ_url1
from .incy import IncyEncoderError
from .incy import encrypt_incy_url
from .sql_helpers import save_wata_invoice

from database import session_factory
from engine.payments import create_yk_payment_sync
from engine.payments import create_wata_payment_sync
from engine.payments import fetch_wata_transaction_status
import proto.rwmanager_pb2 as proto

ACTUAL_TARIFFS: list[Tariff] = [
    OneMonthTariff(),
    ThreeMonthsTariff(),
    OneYearTariff(),
]
OFFER_TARIFFS: list[Tariff] = [
    TrialPromotionTariff(),
    OneDayTariff(),
    *ACTUAL_TARIFFS,
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
SUPPORT_ADMIN_ROLE_MARKETER = "marketer"
SUPPORT_ADMIN_ACCOUNT_SESSION_KEY = "support_admin_account"
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


def get_telegram_web_login_start_code(host):
    normalized_host = normalize_host(host)
    return settings.TELEGRAM_WEB_LOGIN_START_CODES.get(normalized_host, "web")


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
        if referrer and db_session.get(ReferralProgramBlock, referrer.id):
            logging.warning(
                "ignore blocked site referral link from referrer %s",
                referrer.username,
            )
            referrer = None

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


def create_invoice_event_for_tariff(tariff_id):
    event_by_tariff = {
        "oneday": analytics_event.CreateInvoiceOneDay,
        "threedays": analytics_event.CreateInvoiceThreeDays,
        "month": analytics_event.CreateInvoiceOneMonth,
        "threemonths": analytics_event.CreateInvoiceThreeMonths,
        "sixmonths": analytics_event.CreateInvoiceSixMonths,
        "year": analytics_event.CreateInvoiceOneYear,
    }
    event_class = event_by_tariff.get(tariff_id)
    return event_class() if event_class else None


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


def form_bool_enabled(value, default=True):
    if value is None:
        return default

    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def custom_config_template_payload(template):
    return {
        "id": template.id,
        "name": template.name,
        "template_json": template.template_json,
        "entry_name": template.entry_name or "",
        "enable_dialer_proxy": bool(getattr(template, "enable_dialer_proxy", True)),
        "dialer_proxy_name": getattr(template, "dialer_proxy_name", None) or "",
        "announce_text": template.announce_text or "",
        "support_url": template.support_url or "",
        "profile_update_interval": template.profile_update_interval or "",
        "additional_headers": template.additional_headers or {},
        "is_active": template.is_active,
    }


# Переменные, которые custom-config всегда передаёт в рендер шаблона.
# UA-правилам запрещено их переопределять.
CONFIG_TEMPLATE_RESERVED_VARIABLES = ("VLESS_USER", "REMARKS", "ENTRY_NAME")

CONFIG_TEMPLATE_SAMPLE_CONTEXT = {
    "VLESS_USER": "00000000-0000-0000-0000-000000000000",
    "REMARKS": "🇩🇪 Германия",
    "ENTRY_NAME": "proxy",
}


def validate_config_template_json(template_json, client_variable_scenarios=()):
    """Рендерит Jinja-шаблон конфига и проверяет, что каждая ветка — валидный JSON.

    Возвращает текст ошибки или None. Сценарий «без клиентских переменных»
    проверяется всегда: так шаблон рендерится для клиентов, не попавших ни под
    одно UA-правило (и старыми версиями custom-config, не знающими про правила).
    """
    try:
        template = jinja2.Environment().from_string(template_json)
    except jinja2.TemplateError as error:
        return f"Ошибка Jinja шаблона: {error}"

    scenarios = [("без клиентских переменных", {})]
    scenarios.extend(client_variable_scenarios)
    for label, variables in scenarios:
        try:
            rendered = template.render(**CONFIG_TEMPLATE_SAMPLE_CONTEXT, **variables)
        except jinja2.TemplateError as error:
            return f"Ошибка Jinja шаблона ({label}): {error}"
        try:
            json.loads(rendered)
        except json.JSONDecodeError as error:
            return (
                f"Ошибка JSON шаблона ({label}): строка {error.lineno}, "
                f"колонка {error.colno}: {error.msg}"
            )
    return None


def load_client_ua_rules(db_session, active_only=False):
    query = db_session.query(ClientUaRule)
    if active_only:
        query = query.filter(ClientUaRule.is_active.is_(True))
    return query.order_by(ClientUaRule.priority.asc(), ClientUaRule.id.asc()).all()


def client_ua_rule_scenarios(rules):
    # По сценарию на каждое правило: ветки шаблона проверяются поодиночке,
    # комбинации разных переменных не строим.
    return [
        (f"{rule.variable_name}={rule.value}", {rule.variable_name: rule.value})
        for rule in rules
    ]


def client_ua_rule_payload(rule):
    return {
        "id": rule.id,
        "match_substring": rule.match_substring,
        "variable_name": rule.variable_name,
        "value": rule.value,
        "priority": rule.priority,
        "is_active": rule.is_active,
    }


def validate_client_ua_rule_fields(match_substring, variable_name, value):
    if not match_substring:
        return "Заполните строку поиска в User-Agent"
    if not variable_name:
        return "Заполните имя переменной"
    if not variable_name.isidentifier():
        return "Имя переменной должно быть идентификатором: буквы, цифры и _"
    if variable_name in CONFIG_TEMPLATE_RESERVED_VARIABLES:
        return f"Имя переменной {variable_name} зарезервировано шаблоном"
    if not value:
        return "Заполните значение переменной"
    return None


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
    return query.order_by(
        SupportReplyTemplate.sort_order.asc(),
        SupportReplyTemplate.title.asc(),
        SupportReplyTemplate.id.asc(),
    ).all()


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


def support_admin_is_marketer(request):
    return support_admin_role(request) == SUPPORT_ADMIN_ROLE_MARKETER


def require_support_admin_any(request, roles):
    """Доступ для любого из перечисленных ролей (например admin+marketer)."""
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response
    if support_admin_role(request) not in roles:
        return JsonResponse(
            {
                "status": "forbidden",
                "message": "Недостаточно прав для этого раздела.",
            },
            status=403,
        )
    return None


ANALYTICS_ROLES = {SUPPORT_ADMIN_ROLE_ADMIN, SUPPORT_ADMIN_ROLE_MARKETER}


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


def runtime_bool_from_db(db_session, key, default_value):
    value = db_session.get(SystemSetting, key)
    if value is None:
        return default_value

    normalized = str(value.value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on", "да", "вкл"}:
        return True
    if normalized in {"0", "false", "no", "off", "нет", "выкл"}:
        return False

    logging.error("invalid boolean system setting %s=%r", key, value.value)
    return default_value


APPLE_RECOMMENDED_APP_HAPP = "happ"
APPLE_RECOMMENDED_APP_INCY = "incy"


def apple_recommended_app_from_db(db_session):
    """Рекомендуемое приложение для Apple-устройств из админки (happ|incy).

    Та же настройка system_settings, что читает бот
    (get_runtime_apple_recommended_app); неизвестные значения — happ.
    """
    setting = db_session.get(SystemSetting, BOT_APPLE_RECOMMENDED_APP_SETTING)
    if setting is None:
        return APPLE_RECOMMENDED_APP_HAPP

    normalized = str(setting.value or "").strip().lower()
    if normalized in {APPLE_RECOMMENDED_APP_HAPP, APPLE_RECOMMENDED_APP_INCY}:
        return normalized

    logging.error("invalid Apple recommended app system setting %r", setting.value)
    return APPLE_RECOMMENDED_APP_HAPP


def build_apple_subscription_link(db_session, subscription_url):
    """(приложение, ссылка добавления подписки) для iOS/macOS в кабинете.

    Как в боте: INCY получает шифрованную incy://crypt1/-ссылку на
    subscription_url + /custom-json. Если энкодер недоступен (нет node в
    образе и т.п.) — молча откатываемся на Happ, кабинет ломать нельзя.
    """
    recommended = apple_recommended_app_from_db(db_session)
    happ_link = encrypt_happ_url1(subscription_url + "/custom-json")
    if recommended != APPLE_RECOMMENDED_APP_INCY:
        return APPLE_RECOMMENDED_APP_HAPP, happ_link

    try:
        return (
            APPLE_RECOMMENDED_APP_INCY,
            encrypt_incy_url(subscription_url + "/custom-json"),
        )
    except IncyEncoderError:
        logging.exception("incy link encoding failed, falling back to Happ")
        return APPLE_RECOMMENDED_APP_HAPP, happ_link


def site_trial_registration_enabled(db_session):
    return runtime_bool_from_db(
        db_session,
        SITE_TRIAL_REGISTRATION_ENABLED_SETTING,
        settings.SITE_TRIAL_REGISTRATION_ENABLED,
    )


def should_create_trial_for_channel(db_session, creation_channel):
    return site_trial_registration_enabled(db_session)


def unknown_site_account_error():
    return (
        "Аккаунт не найден. Оплатите доступ на сайте или войдите через Telegram, "
        "если вы уже создавали подписку в боте."
    )


def get_proto_optional(message, field_name, default=None):
    try:
        if message.HasField(field_name):
            return getattr(message, field_name)
    except ValueError:
        value = getattr(message, field_name, default)
        return value if value not in ("", 0) else default

    return default


def rwms_expire_at(rw_user):
    expire_at = get_proto_optional(rw_user, "expire_at")
    if expire_at is None:
        return None

    return expire_at.ToDatetime().replace(tzinfo=None)


def find_rwms_user_by_identity(email=None, telegram_id=None):
    normalized_email = (email or "").lower().strip()
    users_reply = rwms_client.get_all_users()
    if users_reply is None:
        return None

    for rw_user in users_reply.users:
        rw_email = (get_proto_optional(rw_user, "email", "") or "").lower().strip()
        rw_telegram_id = get_proto_optional(rw_user, "telegram_id")

        if normalized_email and rw_email == normalized_email:
            return rw_user

        if telegram_id is not None and rw_telegram_id == telegram_id:
            return rw_user

    return None


def sync_local_user_from_rwms(
    db_session,
    rw_user,
    email,
    telegram_id,
    context,
    creation_channel,
):
    rw_email = get_proto_optional(rw_user, "email")
    rw_telegram_id = get_proto_optional(rw_user, "telegram_id")
    local_email = email or rw_email
    local_telegram_id = telegram_id or rw_telegram_id

    user = db_session.query(User).filter(User.username == rw_user.username).first()
    if user:
        if local_email and not user.email:
            user.email = local_email
        if local_telegram_id is not None and user.telegram_id is None:
            user.telegram_id = local_telegram_id
        user.expire_at = rwms_expire_at(rw_user)
        if context["ymid"] is not None:
            user.ymid = context["ymid"]
        db_session.flush()
        add_user_to_traffic_progress(db_session, user)
        logging.info(
            "local user %s synced from existing RWMS subscription",
            user.username,
        )
        return user

    referrer = context["referrer"]
    user = User(
        email=local_email,
        telegram_id=local_telegram_id,
        username=rw_user.username,
        expire_at=rwms_expire_at(rw_user),
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
        "local user %s restored from existing RWMS subscription",
        user.username,
    )
    return user


def create_local_site_user_without_rwms(
    db_session,
    email,
    telegram_id,
    username,
    context,
    creation_channel,
):
    referrer = context["referrer"]
    user = User(
        email=email,
        telegram_id=telegram_id,
        username=username,
        expire_at=None,
        ymid=context["ymid"],
        referred_by_id=referrer.id if referrer else None,
        referral_type=ReferralType.STANDARD if referrer else None,
    )
    db_session.add(user)
    db_session.flush()

    add_user_to_traffic_progress(db_session, user)

    logging.info(
        "local site account %s created without RWMS subscription, channel=%s",
        user.username,
        creation_channel,
    )
    return user


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
    user_label = email or f"telegram_id={telegram_id}"
    create_trial_subscription = should_create_trial_for_channel(
        db_session,
        creation_channel,
    )

    if not create_trial_subscription:
        logging.info(
            "site trial subscription disabled for channel=%s, "
            "creating local account without RWMS subscription",
            creation_channel,
        )
        try:
            rw_user = find_rwms_user_by_identity(email=email, telegram_id=telegram_id)
        except Exception:
            logging.exception(
                "failed to recover existing RWMS subscription for site user %s; "
                "continuing with local account",
                user_label,
            )
            rw_user = None

        if rw_user is not None:
            return sync_local_user_from_rwms(
                db_session,
                rw_user,
                email,
                telegram_id,
                context,
                creation_channel,
            )

        return create_local_site_user_without_rwms(
            db_session,
            email,
            telegram_id,
            username,
            context,
            creation_channel,
        )

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

    if rw_user is None:
        logging.warning(
            "creating RWMS subscription for site user %s failed, "
            "trying to recover existing RWMS subscription",
            user_label,
        )
        try:
            rw_user = find_rwms_user_by_identity(email=email, telegram_id=telegram_id)
        except Exception:
            logging.exception(
                "failed to recover existing RWMS subscription for site user %s",
                user_label,
            )
            rw_user = None

        if rw_user is None:
            logging.warning(
                "creating local account for site user %s after RWMS trial creation failed",
                user_label,
            )
            return create_local_site_user_without_rwms(
                db_session,
                email,
                telegram_id,
                username,
                context,
                creation_channel,
            )
        return sync_local_user_from_rwms(
            db_session,
            rw_user,
            email,
            telegram_id,
            context,
            creation_channel,
        )

    expire_at = rwms_expire_at(rw_user)

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
    telegram_bot_login_enabled = bool(telegram_bot_username)
    telegram_widget_auth_enabled = False

    payload["google_oauth_enabled"] = google_oauth_enabled
    payload["yandex_oauth_enabled"] = yandex_oauth_enabled
    payload["telegram_auth_enabled"] = telegram_widget_auth_enabled
    payload["telegram_bot_login_enabled"] = telegram_bot_login_enabled
    payload["social_login_enabled"] = any(
        [google_oauth_enabled, yandex_oauth_enabled, telegram_bot_login_enabled]
    )
    payload["telegram_bot_username"] = telegram_bot_username
    payload["telegram_bot_id"] = telegram_bot_id
    telegram_start_parts = [get_telegram_web_login_start_code(request.get_host())]
    tracking_params = payload["tracking_params"]
    if tracking_params.get("ymid"):
        telegram_start_parts.append(f"ymid{tracking_params['ymid']}")
    if tracking_params.get("ts"):
        telegram_start_parts.append(f"ts{tracking_params['ts']}")
    if tracking_params.get("a"):
        telegram_start_parts.append(f"a{tracking_params['a']}")
    telegram_start_payload = "-".join(telegram_start_parts)
    if len(telegram_start_payload) > 64:
        telegram_start_parts = [get_telegram_web_login_start_code(request.get_host())]
        if tracking_params.get("ts"):
            telegram_start_parts.append(f"ts{tracking_params['ts']}")
        if tracking_params.get("a"):
            telegram_start_parts.append(f"a{tracking_params['a']}")
        telegram_start_payload = "-".join(telegram_start_parts)
    if len(telegram_start_payload) > 64:
        telegram_start_payload = get_telegram_web_login_start_code(request.get_host())
    payload["telegram_bot_login_url"] = (
        f"https://t.me/{telegram_bot_username}?start={telegram_start_payload}"
        if telegram_bot_login_enabled
        else ""
    )
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
        if telegram_widget_auth_enabled
        else ""
    )
    payload["site_role"] = get_site_role(request)
    # Онбординг из 5 слайдов показываем на всех доменах, кроме cabinet:
    # на cabinet-доменах главная страница — это форма входа без онбординга.
    payload["login_onboarding_enabled"] = payload["site_role"] != "cabinet"
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


def build_payment_retry_url(request, token):
    return f"{get_current_base_url(request)}{reverse('payment_retry', args=[token])}"


def site_apply_first_purchase_discount(db_session, user, tariff):
    """Персональная промо-скидка (активируется в боте).

    Возвращает (tariff, applied). Скидка одноразовая: сгорает после первой
    успешной оплаты с момента активации, платежи до активации не мешают —
    код можно выдать и действующему клиенту. Рекуррент заводится по
    регулярной цене (metadata.promo).
    """
    try:
        discount = (
            db_session.query(UserDiscount)
            .filter(UserDiscount.user_id == user.id)
            .first()
        )
        if discount is None:
            return tariff, False
        if discount.valid_until is None or discount.valid_until <= datetime.utcnow():
            return tariff, False
        if not 1 <= (discount.percent or 0) <= 99:
            return tariff, False
        if discount.source_promo_id is not None:
            # Выключение промокода в админке гасит и выданные им скидки.
            promo_is_active = (
                db_session.query(PromoCode.is_active)
                .filter(PromoCode.id == discount.source_promo_id)
                .scalar()
            )
            if promo_is_active is False:
                return tariff, False

        activated_at = discount.created_at
        yk_paid_query = db_session.query(YkPayment.id).filter(
            YkPayment.user_id == user.id, YkPayment.status == "succeeded"
        )
        wata_paid_query = (
            db_session.query(WataTransaction.id)
            .join(WataInvoice, WataInvoice.order_id == WataTransaction.order_id)
            .filter(
                WataInvoice.user_id == user.id,
                WataTransaction.transaction_status == "Paid",
            )
        )
        if activated_at is not None:
            yk_paid_query = yk_paid_query.filter(YkPayment.created_at > activated_at)
            # payment_time хранится с таймзоной — сравниваем с aware-версией.
            wata_paid_query = wata_paid_query.filter(
                WataTransaction.payment_time
                > activated_at.replace(tzinfo=timezone.utc)
            )
        has_paid = (
            yk_paid_query.first() is not None or wata_paid_query.first() is not None
        )
        if has_paid:
            return tariff, False

        new_price = max(1, round(tariff.price * (100 - discount.percent) / 100))
        if new_price >= tariff.price:
            return tariff, False
        logging.info(
            "site checkout: applying %s%% first-purchase discount for user %s "
            "(%s -> %s)",
            discount.percent,
            user.id,
            tariff.price,
            new_price,
        )
        return tariff.model_copy(update={"price": new_price}), True
    except Exception:
        logging.exception("failed to apply first purchase discount")
        return tariff, False


def payment_session_url_key(token):
    return f"payment_url:{token}"


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

    # datetime.utcnow().timestamp() трактует naive-время как локальное и
    # сдвигает окно свежести на смещение таймзоны — нужен aware-вариант.
    if datetime.now(timezone.utc).timestamp() - auth_date_int > 86400:
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


def verify_telegram_webapp_init_data(init_data, bot_token):
    """Проверить initData Telegram Mini App (WebView-кабинет из бота).

    Алгоритм отличается от Login Widget: секретный ключ считается как
    HMAC-SHA256(key="WebAppData", msg=bot_token). Возвращает словарь полей
    initData без "hash" при успешной проверке, иначе None.
    """
    if not init_data or not bot_token:
        return None

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    try:
        auth_date_int = int(data.get("auth_date", ""))
    except (TypeError, ValueError):
        return None

    # datetime.utcnow().timestamp() трактует naive-время как локальное и
    # сдвигает окно свежести на смещение таймзоны — нужен aware-вариант.
    if datetime.now(timezone.utc).timestamp() - auth_date_int > 86400:
        return None

    data_check_string = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    return data


def get_telegram_webapp_user_id(init_data_fields):
    try:
        user_payload = json.loads(init_data_fields.get("user", ""))
        return int(user_payload["id"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
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


def send_login_code_email(email, code, *, ttl_minutes=10):
    """Send a one-time login code to ``email``, synchronously, via the same
    transport the site uses for magic-link emails (Resend, with the Django
    ``send_mail`` fallback). Used by the mobile email-login flow; raises on a
    send failure so the caller can surface it (and not persist the code)."""
    context = {"code": code, "ttl_minutes": ttl_minutes}
    html_message = render_to_string("emails/login_code.html", context)
    plain_message = (
        f"Ваш код для входа в Monkey Island: {code}\n"
        f"Код действует {ttl_minutes} мин. "
        "Если вы не запрашивали вход, просто проигнорируйте это письмо."
    )
    subject = "Код для входа в Monkey Island"

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


def get_purchase_wata_invoice(db_session, login_token):
    started_at = login_token.created_at - timedelta(minutes=5)
    wata_filters = [WataInvoice.user_id == login_token.user_id]
    if login_token.payment_gateway == "wata" and login_token.payment_reference:
        wata_filters.append(WataInvoice.order_id == login_token.payment_reference)
    else:
        wata_filters.append(WataInvoice.creation_time >= started_at)

    return (
        db_session.query(WataInvoice)
        .filter(*wata_filters)
        .order_by(WataInvoice.creation_time.desc())
        .first()
    )


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

    wata_invoice = get_purchase_wata_invoice(db_session, login_token)
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

    if login_token.payment_gateway == "wata" and login_token.payment_reference:
        wata_transaction = (
            db_session.query(WataTransaction)
            .filter(WataTransaction.order_id == login_token.payment_reference)
            .order_by(WataTransaction.payment_time.desc())
            .first()
        )
        if wata_transaction:
            if wata_transaction.transaction_status == "Paid":
                return "succeeded", "Платеж прошел успешно"
            return "failed", "Платеж не прошел"

    return "pending", "Ждем подтверждения платежа"


# Активная проверка статуса оплаты у Wata лимитирована (у Wata GET — 1 запрос
# в 30 секунд на объект), поэтому троттлим вызовы по order_id.
WATA_ACTIVE_CHECK_MIN_INTERVAL_SECONDS = 30
_wata_active_check_last_ts = {}


def active_wata_status_for_token(login_token):
    """Активно (через API Wata) определяет финальный статус оплаты для токена.

    Используется, чтобы не ждать вебхук и редирект самой Wata (её экран успеха
    держит пользователя ~10 секунд). Возвращает ("succeeded"|"failed", message)
    или None, если статус ещё не финальный/проверка недоступна/сработал троттлинг.
    Безопасно: успехом считаем ТОЛЬКО явный "Paid" от Wata.
    """
    if (
        getattr(login_token, "payment_gateway", None) != "wata"
        or not getattr(login_token, "payment_reference", None)
    ):
        return None

    order_id = login_token.payment_reference
    now = monotonic()

    # Подчищаем устаревшие записи (старше окна троттлинга они бесполезны),
    # чтобы кэш не рос бесконечно.
    if len(_wata_active_check_last_ts) > 5000:
        cutoff = now - WATA_ACTIVE_CHECK_MIN_INTERVAL_SECONDS
        for stale_key in [
            key
            for key, ts in _wata_active_check_last_ts.items()
            if ts < cutoff
        ]:
            _wata_active_check_last_ts.pop(stale_key, None)

    last = _wata_active_check_last_ts.get(order_id, 0.0)
    if now - last < WATA_ACTIVE_CHECK_MIN_INTERVAL_SECONDS:
        return None
    _wata_active_check_last_ts[order_id] = now

    try:
        status = fetch_wata_transaction_status(
            settings.WATA_HOST,
            settings.WATA_TOKEN,
            order_id,
        )
    except Exception:
        logging.exception("active wata status check failed for order %s", order_id)
        return None

    if status == "Paid":
        logging.info("active wata check: order %s is Paid", order_id)
        return "succeeded", "Платеж прошел успешно"
    if status == "Declined":
        return "failed", "Платеж не прошел"
    return None


def payment_status_payload(request, token, allow_active_check=False):
    if request.GET.get("result") == "failed":
        return {
            "status": "failed",
            "message": "Платеж не прошел",
            "login_url": "",
            "payment_url": "",
        }

    db_session = session_factory()
    try:
        login_token = get_purchase_login_token(db_session, token)
        if not login_token:
            return {
                "status": "failed",
                "message": "Ссылка проверки платежа истекла или неверна",
                "login_url": "",
                "payment_url": "",
            }

        status, message = get_purchase_payment_status(db_session, login_token)
        # Если в БД ещё нет подтверждения (вебхук не дошёл), но это разрешено
        # вызывающим — спрашиваем статус напрямую у Wata, чтобы не ждать её
        # 10-секундный экран успеха.
        if status == "pending" and allow_active_check:
            active = active_wata_status_for_token(login_token)
            if active:
                status, message = active
        wata_invoice = get_purchase_wata_invoice(db_session, login_token)
        session_payment_url = request.session.get(payment_session_url_key(token), "")
        return {
            "status": status,
            "message": message,
            "login_url": (
                build_purchase_login_link(request, token)
                if status == "succeeded"
                else ""
            ),
            "payment_url": (
                build_payment_retry_url(request, token)
                if status == "pending" and wata_invoice and wata_invoice.url
                else session_payment_url if status == "pending" else ""
            ),
        }
    finally:
        db_session.close()


def payment_retry(request, token):
    db_session = session_factory()
    try:
        login_token = get_purchase_login_token(db_session, token)
        if not login_token:
            return redirect("payment_status", token=token)

        wata_invoice = get_purchase_wata_invoice(db_session, login_token)
        if (
            not wata_invoice
            or not wata_invoice.url
            or is_wata_invoice_expired(wata_invoice)
        ):
            return redirect("payment_status", token=token)

        return redirect(wata_invoice.url)
    finally:
        db_session.close()


def payment_status(request, token):
    # Здесь НЕ используем активную проверку Wata: вход в кабинет должен
    # оставаться завязан на вебхук, который не только подтверждает оплату, но и
    # активирует подписку. Иначе можно увести пользователя в кабинет раньше,
    # чем подписка активирована («оплатил, но нет доступа»). Активная проверка
    # применяется только на странице оплаты (wata_payment), чтобы быстрее
    # увести пользователя с 10-секундного экрана успеха Wata на эту страницу.
    payload = payment_status_payload(request, token)
    return render(
        request,
        "payment_status.html",
        {
            "initial_status": payload["status"],
            "initial_message": payload["message"],
            "login_url": payload["login_url"],
            "payment_url": payload["payment_url"],
            "status_api_url": reverse("payment_status_json", args=[token]),
            "support_telegram_url": settings.SUPPORT_TELEGRAM_URL,
        },
    )


def payment_status_json(request, token):
    return JsonResponse(payment_status_payload(request, token))


def payment_status_active_json(request, token):
    # Эндпоинт с активной проверкой статуса у Wata. Вызывается фронтом РЕДКО
    # (после оплаты), чтобы не упереться в лимит Wata (1 GET / 30с на объект).
    return JsonResponse(
        payment_status_payload(request, token, allow_active_check=True)
    )


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


def telegram_webapp_entry(request):
    """Страница Telegram Mini App: подхватывает initData и логинит в кабинет.

    Открывается из кнопки меню бота. initData доступен только на клиенте
    (через telegram-web-app.js), поэтому страница отправляет его POST-ом
    на telegram_webapp_auth.
    """
    if not get_telegram_auth_bot(request.get_host()):
        logging.warning("telegram webapp entry requested but bot token is missing")
        return redirect("login")

    return render(request, "tg_webapp.html")


def auth_by_telegram_webapp(request):
    if request.method != "POST":
        return redirect("telegram_webapp_entry")

    telegram_bot = get_telegram_auth_bot(request.get_host())
    if not telegram_bot:
        logging.warning("telegram webapp auth requested but bot token is missing")
        return render_login(
            request,
            {"error": "Вход через Telegram временно недоступен"},
            status=503,
        )

    init_data_fields = verify_telegram_webapp_init_data(
        request.POST.get("init_data", ""), telegram_bot["token"]
    )
    if init_data_fields is None:
        logging.warning("telegram webapp auth failed init data signature check")
        return render_login(
            request, {"error": "Не удалось подтвердить вход через Telegram."}
        )

    telegram_id = get_telegram_webapp_user_id(init_data_fields)
    if telegram_id is None:
        logging.warning("telegram webapp auth returned invalid telegram id")
        return render_login(request, {"error": "Telegram не вернул ID аккаунта."})

    db_session = session_factory()
    try:
        with db_session.begin():
            user = (
                db_session.query(User).filter(User.telegram_id == telegram_id).first()
            )

            if not user:
                # Пользователь из бота всегда уже есть в users; создание —
                # запасной путь на случай открытия Mini App до /start.
                logging.info(
                    "creating site subscription from telegram webapp auth "
                    "for telegram id %s",
                    telegram_id,
                )
                user = create_site_user(
                    db_session,
                    None,
                    request,
                    telegram_id=telegram_id,
                    creation_channel="bot_webapp",
                )

            add_event_log_once(
                db_session,
                user,
                analytics_event.FirstSuccessfulLogin(login_method="telegram_webapp"),
            )

        authorize_user_session(request, user)
        # Кабинет открыт внутри Telegram: дальше dashboard рендерится в режиме
        # Mini App (телеграм-SDK, без кнопки выхода и PWA-подсказок).
        request.session["tg_webapp_mode"] = True
        return redirect("dashboard")
    except Exception as e:
        logging.exception(f"telegram webapp auth failed for {telegram_id}: {e}")
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
                "tariffs": get_runtime_actual_tariffs(),
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
            "tariffs": get_runtime_actual_tariffs(),
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
    tariffs = get_runtime_actual_tariffs()
    return render(
        request,
        "index_vps_direct_sale.html",
        {
            "tariffs": tariffs,
            "min_tariff_price": min(tariff.price for tariff in tariffs),
            "tracking_params": get_tracking_params(request),
        },
    )


def offer(request):
    offer_tariffs = get_runtime_offer_tariffs()
    return render(
        request,
        "offer.html",
        {
            "site_role": get_site_role(request),
            "tariffs": [
                offer_tariffs[tariff.db_tariff_id] for tariff in ACTUAL_TARIFFS
            ],
            "offer_tariffs": offer_tariffs,
            "trial_period_days_label": format_days_ru(settings.SITE_TRIAL_PERIOD_DAYS),
            "referral_trial_period_days_label": format_days_ru(
                settings.SITE_REFERRAL_TRIAL_PERIOD_DAYS
            ),
        },
    )


def privacy(request):
    return render(request, "privacy.html", {"site_role": get_site_role(request)})


def terms(request):
    return render(request, "terms.html", {"site_role": get_site_role(request)})


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

        join_referrer_bonus_days = runtime_int_from_db(
            session, BOT_JOIN_REFERRER_BONUS_DAYS_SETTING, 3
        )
        traffic_referrer_bonus_days = runtime_int_from_db(
            session, BOT_TRAFFIC_REFERRER_BONUS_DAYS_SETTING, 7
        )
        purchase_referrer_bonus_days = runtime_int_from_db(
            session, BOT_PURCHASE_REFERRER_BONUS_DAYS_SETTING, 30
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

    bonus_days = (
        session.query(func.coalesce(func.sum(ReferralBonus.days_added), 0))
        .filter(ReferralBonus.referrer_id == user.id)
        .scalar()
    )

    subscription = rwms_client.get_user_by_username(user.username)
    seconds_left = (
        user.time_until_expiration.total_seconds() if user.time_until_expiration else -1
    )
    if subscription is None:
        seconds_left = -1

    has_subscription_access = subscription is not None and seconds_left > 0
    days_left = int((seconds_left + 86399) // 86400) if seconds_left > 0 else 0
    expiring_banner_threshold_seconds = 3 * 24 * 60 * 60
    show_expiring_banner = 0 < seconds_left <= expiring_banner_threshold_seconds
    show_telegram_bind_banner = not user.telegram_id
    show_email_bind_banner = not user.email
    show_not_connected_banner = False

    if has_subscription_access and traffic_progress and not traffic_progress.passed_0:
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

        if request.GET.get("debug_no_rwms") == "1":
            subscription = None
            seconds_left = -1

        show_expiring_banner = 0 < seconds_left <= expiring_banner_threshold_seconds
        days_left = int((seconds_left + 86399) // 86400) if seconds_left > 0 else 0

    has_subscription_access = subscription is not None and seconds_left > 0

    if not has_subscription_access:
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
        time_left_unit = "дн."
    elif seconds_left < 24 * 60 * 60:
        time_left_value = max(1, int((seconds_left + 3599) // 3600))
        time_left_label = "часов осталось"
        time_left_unit = "ч."
    else:
        time_left_value = days_left
        time_left_label = "дней осталось"
        time_left_unit = "дн."

    plain_subscription_url = (
        subscription.subscription_url if has_subscription_access else ""
    )
    happ_subscription_url = (
        encrypt_happ_url1(subscription.subscription_url + "/custom-json")
        if has_subscription_access
        else ""
    )
    # Рекомендуемое приложение для iOS/macOS из админки: happ (по умолчанию)
    # или incy — для INCY ссылка добавления подписки шифруется отдельно.
    if has_subscription_access:
        apple_recommended_app, apple_subscription_url = build_apple_subscription_link(
            session, subscription.subscription_url
        )
    else:
        apple_recommended_app, apple_subscription_url = (
            APPLE_RECOMMENDED_APP_HAPP,
            "",
        )

    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "tg_bind_link": tg_bind_link,
            "plain_subscription_url": plain_subscription_url,
            "happ_subscription_url": happ_subscription_url,
            "apple_recommended_app": apple_recommended_app,
            "apple_subscription_url": apple_subscription_url,
            "ref_invited_count": ref_invited_count,
            "ref_connected_count": ref_connected_count,
            "ref_purchased_count": ref_purchased_count,
            "bonus_days": bonus_days,
            "join_referrer_bonus_days": join_referrer_bonus_days,
            "traffic_referrer_bonus_days": traffic_referrer_bonus_days,
            "purchase_referrer_bonus_days": purchase_referrer_bonus_days,
            "tariffs": get_runtime_actual_tariffs(),
            "has_recurrent": has_recurrent,
            "has_subscription_access": has_subscription_access,
            "seconds_left": seconds_left,
            "days_left": days_left,
            "time_left_value": time_left_value,
            "time_left_label": time_left_label,
            "time_left_unit": time_left_unit,
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
            "tg_webapp_mode": bool(request.session.get("tg_webapp_mode")),
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
        link = (
            f"{get_current_base_url(request)}{reverse('confirm_email', args=[token])}"
        )
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


def cancel_autopay(request):
    """Отключение автопродления из личного кабинета.

    Полностью зеркалит поведение команды бота ``/cancelautopay``:
    удаляет запись о рекуррентном платеже YooKassa (``yk_recurrent_payments``)
    и выставляет флаг ``users.autopay_allow = False`` (Wata). Подписка и данные
    в Remnawave при этом не трогаются — доступ продолжает работать до конца
    оплаченного периода, просто следующее списание не произойдёт.
    """
    if request.method != "POST" or not request.user.is_authenticated:
        return JsonResponse({"status": "error"}, status=403)

    session = session_factory()
    try:
        db_user = session.query(User).filter(User.id == request.user.id).first()
        if not db_user:
            logging.warning(
                f"user {request.user.id} not found while cancelling autopay"
            )
            return JsonResponse({"status": "error"}, status=404)

        removed_recurrents = (
            session.query(YkRecurrentPayment)
            .filter(YkRecurrentPayment.user_id == db_user.id)
            .delete(synchronize_session=False)
        )
        old_autopay_allow = bool(db_user.autopay_allow)
        db_user.autopay_allow = False
        session.commit()

        logging.info(
            "user %s cancelled autopay via cabinet "
            "(removed %s yk recurrents, autopay_allow %s -> False)",
            db_user.id,
            removed_recurrents,
            old_autopay_allow,
        )
        return JsonResponse(
            {"status": "ok", "removed_recurrents": removed_recurrents}
        )
    except Exception as e:
        session.rollback()
        logging.exception(
            f"failed to cancel autopay for user {request.user.id}: {e}"
        )
        return JsonResponse({"status": "error"}, status=500)
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
        login_name = (request.POST.get("login") or "").strip().lower()

        # Персональные аккаунты сотрудников (этап 6 плана админки): если
        # указан логин — проверяем только по admin_accounts.
        if login_name:
            db_session = session_factory()
            try:
                account = (
                    db_session.query(AdminAccount)
                    .filter(
                        AdminAccount.login == login_name,
                        AdminAccount.is_active.is_(True),
                    )
                    .first()
                )
                if account and check_password(password, account.password_hash):
                    role_map = {
                        "full": SUPPORT_ADMIN_ROLE_ADMIN,
                        "marketer": SUPPORT_ADMIN_ROLE_MARKETER,
                        "support": SUPPORT_ADMIN_ROLE_SUPPORT,
                    }
                    request.session[SUPPORT_ADMIN_SESSION_KEY] = True
                    request.session[SUPPORT_ADMIN_ROLE_SESSION_KEY] = role_map.get(
                        account.role, SUPPORT_ADMIN_ROLE_SUPPORT
                    )
                    request.session[SUPPORT_ADMIN_ACCOUNT_SESSION_KEY] = account.login
                    request.session.modified = True
                    account.last_login_at = datetime.utcnow()
                    db_session.add(
                        AdminAuditLog(
                            actor=account.login,
                            source="site",
                            action="login",
                            details={"role": account.role},
                            ip=admin_client_ip(request),
                        )
                    )
                    db_session.commit()
                    return redirect("support_admin_tickets")
            finally:
                db_session.close()
            logging.warning("admin account login failed for %s", login_name)
            return render(
                request,
                "support_admin_login.html",
                {"error": "Неверный логин или пароль."},
                status=403,
            )

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
            "support_admin_is_marketer": support_admin_is_marketer(request),
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
            deleted_template_id = template.id
            db_session.delete(template)
            # Убираем удалённый шаблон из пер-юзерных пинов, чтобы не оставлять
            # висячие id в system_settings (иначе админка покажет несуществующие
            # закреплённые конфиги; на выдачу это не влияет — там всё равно fallback).
            remove_config_template_id_from_pins(db_session, deleted_template_id)
            db_session.commit()
            return JsonResponse({"status": "ok"})

        if action == "validate":
            # Проверка без сохранения — для кнопки «Проверить JSON» в админке:
            # шаблон с Jinja-ветками нельзя провалидировать на клиенте.
            template_json = request.POST.get("template_json", "").strip()
            if not template_json:
                return JsonResponse(
                    {"status": "error", "message": "Пустой шаблон"}, status=400
                )
            ua_rules = load_client_ua_rules(db_session, active_only=True)
            validation_error = validate_config_template_json(
                template_json, client_ua_rule_scenarios(ua_rules)
            )
            if validation_error:
                return JsonResponse(
                    {"status": "error", "message": validation_error}, status=400
                )
            return JsonResponse({"status": "ok", "scenarios_checked": 1 + len(ua_rules)})

        name = request.POST.get("name", "").strip()
        template_json = request.POST.get("template_json", "").strip()
        entry_name = request.POST.get("entry_name", "").strip() or None
        enable_dialer_proxy = form_bool_enabled(
            request.POST.get("enable_dialer_proxy"), default=True
        )
        dialer_proxy_name = request.POST.get("dialer_proxy_name", "").strip() or None
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

        # Шаблон может содержать Jinja-ветки ({% if CLIENT == "happ" %} ...),
        # поэтому валидируем не сырой текст, а результат рендера по каждому
        # клиентскому сценарию из UA-правил.
        ua_rules = load_client_ua_rules(db_session, active_only=True)
        validation_error = validate_config_template_json(
            template_json, client_ua_rule_scenarios(ua_rules)
        )
        if validation_error:
            return JsonResponse(
                {"status": "error", "message": validation_error},
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

        template.name = name[:160]
        template.template_json = template_json
        template.entry_name = entry_name[:256] if entry_name else None
        template.enable_dialer_proxy = enable_dialer_proxy
        template.dialer_proxy_name = dialer_proxy_name[:256] if dialer_proxy_name else None
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
                    "message": "Не удалось сохранить: проверьте уникальность имени конфига и снятие старого ограничения на один активный шаблон",
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


def collect_ua_rule_template_warnings(db_session, ua_rules):
    """Проверяет активные шаблоны против сценариев UA-правил.

    Возвращает список предупреждений (не блокирует сохранение правила): ветка
    шаблона под новое правило могла ещё не проверяться при сохранении шаблона.
    """
    scenarios = client_ua_rule_scenarios(ua_rules)
    warnings = []
    for template in load_custom_config_templates(db_session):
        if not template.is_active:
            continue
        error = validate_config_template_json(template.template_json, scenarios)
        if error:
            warnings.append(f"Шаблон «{template.name}»: {error}")
    return warnings


def support_admin_api_ua_rules(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "GET":
            rules = load_client_ua_rules(db_session)
            return JsonResponse(
                {
                    "status": "ok",
                    "rules": [client_ua_rule_payload(rule) for rule in rules],
                }
            )

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        action = request.POST.get("action", "save")
        rule_id = request.POST.get("rule_id")

        if action == "delete":
            rule = db_session.get(ClientUaRule, int(rule_id or 0))
            if not rule:
                return JsonResponse({"status": "not_found"}, status=404)
            db_session.delete(rule)
            db_session.commit()
            return JsonResponse({"status": "ok"})

        match_substring = request.POST.get("match_substring", "").strip()
        variable_name = request.POST.get("variable_name", "").strip() or "CLIENT"
        value = request.POST.get("value", "").strip()
        priority_raw = request.POST.get("priority", "").strip()
        is_active = request.POST.get("is_active", "1") == "1"

        validation_error = validate_client_ua_rule_fields(
            match_substring, variable_name, value
        )
        if validation_error:
            return JsonResponse(
                {"status": "error", "message": validation_error}, status=400
            )

        try:
            priority = int(priority_raw) if priority_raw else 100
        except ValueError:
            return JsonResponse(
                {"status": "error", "message": "Приоритет должен быть целым числом"},
                status=400,
            )

        if rule_id:
            rule = db_session.get(ClientUaRule, int(rule_id))
            if not rule:
                return JsonResponse({"status": "not_found"}, status=404)
        else:
            rule = ClientUaRule()
            db_session.add(rule)

        rule.match_substring = match_substring[:160]
        rule.variable_name = variable_name[:64]
        rule.value = value[:160]
        rule.priority = priority
        rule.is_active = is_active
        rule.updated_at = datetime.utcnow()
        db_session.commit()

        # Ветка шаблона под новое правило могла не проверяться при сохранении
        # шаблона — предупреждаем, но правило не блокируем.
        warnings = (
            collect_ua_rule_template_warnings(
                db_session, load_client_ua_rules(db_session, active_only=True)
            )
            if rule.is_active
            else []
        )

        return JsonResponse(
            {
                "status": "ok",
                "rule": client_ua_rule_payload(rule),
                "warnings": warnings,
            }
        )
    finally:
        db_session.close()


# Пер-юзерный пиннинг конфигов. Ключ в system_settings — "cfg_pin:<username>",
# значение — CSV из id активных CustomConfigTemplate, которые разрешено выдавать
# этому пользователю. Читает и применяет этот пин сервис custom-config
# (monkey-island-custom-config/main.py: apply_user_config_pin). Хранение в
# system_settings выбрано намеренно, чтобы обойтись без миграции схемы.
CONFIG_PIN_KEY_PREFIX = "cfg_pin:"


def config_pin_setting_key(username):
    return f"{CONFIG_PIN_KEY_PREFIX}{username}"


def load_user_pinned_template_ids(db_session, username):
    setting = db_session.get(SystemSetting, config_pin_setting_key(username))
    if not setting or not setting.value:
        return []
    return [
        int(part.strip())
        for part in setting.value.split(",")
        if part.strip().isdigit()
    ]


def remove_config_template_id_from_pins(db_session, template_id):
    """Убирает удалённый шаблон из всех записей cfg_pin:*.

    Если после удаления у пользователя не осталось закреплённых конфигов —
    удаляет саму запись, чтобы в system_settings не копились висячие ключи.
    Вызывать внутри транзакции до commit.
    """
    settings = (
        db_session.query(SystemSetting)
        .filter(SystemSetting.key.like(f"{CONFIG_PIN_KEY_PREFIX}%"))
        .all()
    )
    for setting in settings:
        ids = [
            int(part.strip())
            for part in (setting.value or "").split(",")
            if part.strip().isdigit()
        ]
        if template_id not in ids:
            continue
        remaining = [i for i in ids if i != template_id]
        if remaining:
            setting.value = ",".join(str(i) for i in remaining)
        else:
            db_session.delete(setting)


def config_pins_payload(db_session, user, templates=None):
    if templates is None:
        templates = load_custom_config_templates(db_session)
    # Показываем только реально существующие закреплённые id — так админка не
    # покажет висячие пины (например, если шаблон удалили напрямую в БД).
    existing_ids = {template.id for template in templates}
    pinned_ids = [
        template_id
        for template_id in load_user_pinned_template_ids(db_session, user.username)
        if template_id in existing_ids
    ]
    pinned_set = set(pinned_ids)
    return {
        "user": admin_user_payload(user),
        "pinned_ids": pinned_ids,
        "templates": [
            {
                "id": template.id,
                "name": template.name,
                "is_active": bool(template.is_active),
                "pinned": template.id in pinned_set,
            }
            for template in templates
        ],
    }


def support_admin_api_config_pins(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    query = request.GET.get("q") if request.method == "GET" else request.POST.get("q")
    db_session = session_factory()
    try:
        user = admin_find_user(db_session, query)
        if not user:
            return JsonResponse({"status": "not_found"}, status=404)

        if request.method == "GET":
            return JsonResponse(
                {"status": "ok", "result": config_pins_payload(db_session, user)}
            )

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        action = request.POST.get("action", "save")
        templates = load_custom_config_templates(db_session)
        setting_key = config_pin_setting_key(user.username)

        if action == "clear":
            pinned_ids = []
        else:
            valid_ids = {template.id for template in templates}
            requested = request.POST.get("template_ids", "")
            # Сохраняем только существующие id, порядок и без дублей.
            seen = set()
            pinned_ids = []
            for part in requested.split(","):
                part = part.strip()
                if not part.isdigit():
                    continue
                template_id = int(part)
                if template_id in valid_ids and template_id not in seen:
                    seen.add(template_id)
                    pinned_ids.append(template_id)

        existing = db_session.get(SystemSetting, setting_key)
        if pinned_ids:
            admin_upsert_system_setting(
                db_session, setting_key, ",".join(str(i) for i in pinned_ids)
            )
        elif existing:
            db_session.delete(existing)
        db_session.commit()

        logging.info(
            "admin set config pin for user %s (username=%s): %s",
            user.id,
            user.username,
            pinned_ids or "cleared",
        )
        return JsonResponse(
            {
                "status": "ok",
                "result": config_pins_payload(db_session, user, templates),
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


def runtime_int_from_db(db_session, key, default_value, min_value=0):
    value = db_session.get(SystemSetting, key)
    if value is None:
        return default_value
    try:
        parsed_value = int(value.value)
    except (TypeError, ValueError):
        logging.error("invalid integer system setting %s=%r", key, value.value)
        return default_value
    if parsed_value < min_value:
        logging.error("invalid integer system setting %s=%r", key, value.value)
        return default_value
    return parsed_value


def get_runtime_actual_tariffs(db_session=None):
    return get_runtime_tariffs(ACTUAL_TARIFFS, db_session)


def get_runtime_offer_tariffs(db_session=None):
    return {
        tariff.db_tariff_id: tariff
        for tariff in get_runtime_tariffs(OFFER_TARIFFS, db_session)
    }


def get_runtime_tariffs(tariffs, db_session=None):
    should_close_session = db_session is None
    if should_close_session:
        db_session = session_factory()
    try:
        runtime_tariffs = []
        for tariff in tariffs:
            key = TARIFF_PRICE_SETTINGS.get(tariff.db_tariff_id)
            if key is None:
                runtime_tariffs.append(tariff)
                continue
            price = runtime_int_from_db(db_session, key, tariff.price, min_value=1)
            runtime_tariffs.append(tariff.model_copy(update={"price": price}))
        return runtime_tariffs
    finally:
        if should_close_session:
            db_session.close()


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


ADMIN_STATS_GRANULARITIES = {"auto", "day", "week", "month"}
ADMIN_STATS_MAX_DAILY_DAYS = 62


def admin_stats_normalize_granularity(requested, start_date, end_date):
    requested = (requested or "week").lower()
    if requested not in ADMIN_STATS_GRANULARITIES:
        requested = "week"

    days = (end_date - start_date).days + 1
    if requested == "auto":
        if days <= 31:
            return "day", requested, ""
        if days <= 180:
            return "week", requested, ""
        return "month", requested, ""

    if requested == "day" and days > ADMIN_STATS_MAX_DAILY_DAYS:
        return (
            "week",
            requested,
            "Диапазон длиннее двух месяцев, поэтому дневная детализация заменена недельной.",
        )

    return requested, requested, ""


def admin_stats_bucket_start(value, granularity):
    if granularity == "day":
        return value
    if granularity == "week":
        return value - timedelta(days=value.weekday())
    return date(value.year, value.month, 1)


def admin_stats_next_bucket_start(value, granularity):
    if granularity == "day":
        return value + timedelta(days=1)
    if granularity == "week":
        return value + timedelta(days=7)
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def admin_stats_bucket_label(bucket_start, bucket_end, granularity):
    if granularity == "day":
        return bucket_start.strftime("%d.%m")
    if granularity == "week":
        return f"{bucket_start.strftime('%d.%m')}–{bucket_end.strftime('%d.%m')}"
    return bucket_start.strftime("%m.%Y")


def admin_stats_bucket_sql(value, granularity):
    if granularity == "day":
        return func.date_trunc("day", value)
    if granularity == "week":
        return func.date_trunc("week", value)
    return func.date_trunc("month", value)


def admin_stats_row_bucket_key(value, granularity):
    value = admin_dt(value)
    if not value:
        return None
    return admin_stats_bucket_start(value.date(), granularity).isoformat()


def admin_stats_source_key(value):
    return None if value is None else str(value)


# Имя session-temp таблицы, в которую один раз материализуется когорта первых
# подписок выбранного периода. См. build_admin_cohort_table.
ADMIN_COHORT_TEMP_TABLE = "admin_cohort_events_tmp"


def build_admin_cohort_table(db_session, start_datetime, end_datetime):
    """Считает когорту "первая подписка пользователя" один раз и кладет во временную таблицу.

    Раньше оконная функция row_number() по всему event_logs (тип события
    subscription_created) пересчитывалась как inline-подзапрос в каждом из ~9
    запросов аналитики. Эта стоимость не зависит от длины диапазона, поэтому даже
    короткие периоды строились очень долго. Здесь когорта материализуется один раз,
    а все последующие запросы джойнятся к легкой временной таблице по user_id.

    Бизнес-логика идентична прежнему cohort_events:
    - берется только ПЕРВОЕ событие subscription_created на пользователя
      (row_number == 1) среди всех событий с timestamp <= end_datetime;
    - в когорту попадают пользователи, чья первая подписка лежит в [start, end].

    Возвращает SQLAlchemy Table со столбцами user_id, timestamp, traffic_source.
    """
    first_subscription_events = (
        db_session.query(
            EventLog.user_id.label("user_id"),
            EventLog.event_payload.label("event_payload"),
            EventLog.timestamp.label("timestamp"),
            func.row_number()
            .over(
                partition_by=EventLog.user_id,
                order_by=(EventLog.timestamp, EventLog.id),
            )
            .label("row_number"),
        )
        .filter(EventLog.event_type == "subscription_created")
        .filter(EventLog.timestamp <= end_datetime)
        .subquery()
    )
    cohort_select = (
        db_session.query(
            first_subscription_events.c.user_id.label("user_id"),
            first_subscription_events.c.timestamp.label("timestamp"),
            first_subscription_events.c.event_payload["traffic_source"].astext.label(
                "traffic_source"
            ),
        )
        .filter(first_subscription_events.c.row_number == 1)
        .filter(first_subscription_events.c.timestamp >= start_datetime)
        .filter(first_subscription_events.c.timestamp <= end_datetime)
    )

    cohort_table = Table(
        ADMIN_COHORT_TEMP_TABLE,
        MetaData(),
        Column("user_id", BigInteger, primary_key=True, autoincrement=False),
        Column("timestamp", TIMESTAMP),
        Column("traffic_source", Text),
        prefixes=["TEMPORARY"],
    )
    # Соединения берутся из пула и переиспользуются между запросами, а session-temp
    # таблица живет на соединении до его закрытия. Поэтому от прошлого запроса на том
    # же коннекте таблица может остаться — пересоздаем явно.
    db_session.execute(text(f"DROP TABLE IF EXISTS {ADMIN_COHORT_TEMP_TABLE}"))
    cohort_table.create(bind=db_session.connection())
    db_session.execute(
        insert(cohort_table).from_select(
            ["user_id", "timestamp", "traffic_source"],
            cohort_select.statement,
        )
    )
    # Временные таблицы не анализируются автовакуумом, без статистики планировщик
    # выбирает плохие планы для последующих джойнов — собираем статистику явно.
    db_session.execute(text(f"ANALYZE {ADMIN_COHORT_TEMP_TABLE}"))
    return cohort_table


def drop_admin_cohort_table(db_session):
    """Удаляет временную таблицу когорты, чтобы не держать ее на пуловом соединении."""
    db_session.execute(text(f"DROP TABLE IF EXISTS {ADMIN_COHORT_TEMP_TABLE}"))


def build_admin_sales_series(
    db_session,
    cohort_table,
    start_date,
    end_date,
    granularity,
    requested_granularity,
    granularity_note,
    sales_mode="cohort",
):
    start_datetime = datetime.combine(start_date, time.min)
    end_datetime = datetime.combine(end_date, time.max)
    sales_mode = "absolute" if sales_mode == "absolute" else "cohort"
    bucket_start = admin_stats_bucket_start(start_date, granularity)
    buckets = {}

    current = bucket_start
    while current <= end_date:
        next_start = admin_stats_next_bucket_start(current, granularity)
        bucket_end = min(next_start - timedelta(days=1), end_date)
        display_start = max(current, start_date)
        key = current.isoformat()
        buckets[key] = {
            "key": key,
            "label": admin_stats_bucket_label(display_start, bucket_end, granularity),
            "start": display_start.isoformat(),
            "end": bucket_end.isoformat(),
            "revenue": 0,
            "payments": 0,
            "unique_paying_users": 0,
            "payer_ids": set(),
            "tariffs": {},
        }
        current = next_start

    yk_payment_time = func.coalesce(YkPayment.captured_at, YkPayment.created_at)
    yk_bucket = admin_stats_bucket_sql(yk_payment_time, granularity)
    wata_bucket = admin_stats_bucket_sql(WataTransaction.payment_time, granularity)

    if sales_mode == "absolute":
        yk_rows = (
            db_session.query(
                yk_bucket.label("bucket_start"),
                YkPayment.subscription_period,
                func.count(YkPayment.id),
                func.coalesce(func.sum(YkPayment.amount), 0),
            )
            .select_from(YkPayment)
            .filter(YkPayment.status == "succeeded")
            .filter(yk_payment_time >= start_datetime)
            .filter(yk_payment_time <= end_datetime)
            .group_by(yk_bucket, YkPayment.subscription_period)
            .all()
        )
        wata_rows = (
            db_session.query(
                wata_bucket.label("bucket_start"),
                WataInvoice.tariff_id,
                func.count(WataTransaction.id),
                func.coalesce(func.sum(WataTransaction.amount), 0),
            )
            .select_from(WataInvoice)
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(WataTransaction.payment_time >= start_datetime)
            .filter(WataTransaction.payment_time <= end_datetime)
            .group_by(wata_bucket, WataInvoice.tariff_id)
            .all()
        )
        yk_payer_query = (
            db_session.query(
                yk_bucket.label("bucket_start"),
                YkPayment.user_id.label("user_id"),
            )
            .select_from(YkPayment)
            .filter(YkPayment.status == "succeeded")
            .filter(yk_payment_time >= start_datetime)
            .filter(yk_payment_time <= end_datetime)
        )
        wata_payer_query = (
            db_session.query(
                wata_bucket.label("bucket_start"),
                WataInvoice.user_id.label("user_id"),
            )
            .select_from(WataInvoice)
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(WataTransaction.payment_time >= start_datetime)
            .filter(WataTransaction.payment_time <= end_datetime)
        )
    else:
        # Когорта уже материализована в build_admin_cohort_table — переиспользуем
        # ту же временную таблицу вместо повторного пересчета оконной функции.
        cohort_events = cohort_table
        yk_rows = (
            db_session.query(
                yk_bucket.label("bucket_start"),
                YkPayment.subscription_period,
                func.count(YkPayment.id),
                func.coalesce(func.sum(YkPayment.amount), 0),
            )
            .select_from(YkPayment)
            .join(cohort_events, cohort_events.c.user_id == YkPayment.user_id)
            .filter(YkPayment.status == "succeeded")
            .filter(yk_payment_time >= cohort_events.c.timestamp)
            .filter(yk_payment_time >= start_datetime)
            .filter(yk_payment_time <= end_datetime)
            .group_by(yk_bucket, YkPayment.subscription_period)
            .all()
        )
        wata_rows = (
            db_session.query(
                wata_bucket.label("bucket_start"),
                WataInvoice.tariff_id,
                func.count(WataTransaction.id),
                func.coalesce(func.sum(WataTransaction.amount), 0),
            )
            .select_from(WataInvoice)
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .join(cohort_events, cohort_events.c.user_id == WataInvoice.user_id)
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(WataTransaction.payment_time >= cohort_events.c.timestamp)
            .filter(WataTransaction.payment_time >= start_datetime)
            .filter(WataTransaction.payment_time <= end_datetime)
            .group_by(wata_bucket, WataInvoice.tariff_id)
            .all()
        )
        yk_payer_query = (
            db_session.query(
                yk_bucket.label("bucket_start"),
                YkPayment.user_id.label("user_id"),
            )
            .select_from(YkPayment)
            .join(cohort_events, cohort_events.c.user_id == YkPayment.user_id)
            .filter(YkPayment.status == "succeeded")
            .filter(yk_payment_time >= cohort_events.c.timestamp)
            .filter(yk_payment_time >= start_datetime)
            .filter(yk_payment_time <= end_datetime)
        )
        wata_payer_query = (
            db_session.query(
                wata_bucket.label("bucket_start"),
                WataInvoice.user_id.label("user_id"),
            )
            .select_from(WataInvoice)
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .join(cohort_events, cohort_events.c.user_id == WataInvoice.user_id)
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(WataTransaction.payment_time >= cohort_events.c.timestamp)
            .filter(WataTransaction.payment_time >= start_datetime)
            .filter(WataTransaction.payment_time <= end_datetime)
        )

    payer_events = union_all(
        yk_payer_query.statement,
        wata_payer_query.statement,
    ).subquery()
    payer_rows = (
        db_session.query(
            payer_events.c.bucket_start,
            func.count(func.distinct(payer_events.c.user_id)),
        )
        .group_by(payer_events.c.bucket_start)
        .all()
    )
    for bucket_start_value, unique_payers in payer_rows:
        key = admin_stats_row_bucket_key(bucket_start_value, granularity)
        bucket = buckets.get(key)
        if bucket is not None:
            bucket["unique_paying_users"] = int(unique_payers or 0)

    tariff_names = set()
    for bucket_start_value, tariff_id, count, amount in [
        *yk_rows,
        *wata_rows,
    ]:
        key = admin_stats_row_bucket_key(bucket_start_value, granularity)
        bucket = buckets.get(key)
        if bucket is None:
            continue
        tariff_name = get_tariff_display_name(tariff_id)
        amount = admin_money(amount)
        count = int(count or 0)
        tariff_names.add(tariff_name)
        bucket["revenue"] += amount
        bucket["payments"] += count
        tariff = bucket["tariffs"].setdefault(
            tariff_name, {"name": tariff_name, "count": 0, "revenue": 0}
        )
        tariff["count"] += count
        tariff["revenue"] += amount

    output_buckets = []
    for bucket in buckets.values():
        bucket.pop("payer_ids", None)
        bucket["tariffs"] = [
            value
            for _, value in sorted(
                bucket["tariffs"].items(), key=lambda item: get_tariff_order(item[0])
            )
        ]
        output_buckets.append(bucket)

    return {
        "mode": sales_mode,
        "granularity": granularity,
        "requested_granularity": requested_granularity,
        "note": granularity_note,
        "tariff_names": sorted(tariff_names, key=get_tariff_order),
        "buckets": output_buckets,
    }


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


def admin_mask_setting_value(key, value):
    if value is None:
        return ""
    if key in SENSITIVE_RUNTIME_SETTINGS and str(value):
        return "***"
    return str(value)


def admin_runtime_setting_type(key):
    if key in BOOL_RUNTIME_SETTINGS:
        return "bool"
    if key in INT_RUNTIME_SETTINGS:
        return "int"
    if key in POSITIVE_FLOAT_RUNTIME_SETTINGS:
        return "float"
    if key in CSV_INT_RUNTIME_SETTINGS:
        return "csv_int"
    if key in CSV_STR_RUNTIME_SETTINGS:
        return "csv"
    if key in ENUM_RUNTIME_SETTINGS:
        return "enum"
    return "string"


# Часы окна отправки win-back — час суток (0-23). Ключи заданы строками намеренно:
# common сайта может ещё не содержать win-back констант, а сюда мы попадаем только
# после проверки key in RUNTIME_SETTING_KEYS (т.е. уже после пропагации common).
WINBACK_SEND_HOUR_SETTINGS = {"winback_send_hour_start", "winback_send_hour_end"}


def admin_validate_runtime_setting(key, value):
    key = (key or "").strip()
    value = (value or "").strip()
    if key not in RUNTIME_SETTING_KEYS:
        return None, "Неизвестная настройка"

    if key in BOOL_RUNTIME_SETTINGS:
        normalized = value.lower()
        if normalized in {"1", "true", "yes", "on", "да", "вкл"}:
            return "1", None
        if normalized in {"0", "false", "no", "off", "нет", "выкл"}:
            return "0", None
        return None, "Значение должно быть true/false или 1/0"

    if key in INT_RUNTIME_SETTINGS:
        try:
            int_value = int(value)
        except ValueError:
            return None, "Значение должно быть числом"
        if key in NON_NEGATIVE_INT_RUNTIME_SETTINGS and int_value < 0:
            return None, "Значение не может быть отрицательным"
        if key in POSITIVE_INT_RUNTIME_SETTINGS and int_value <= 0:
            return None, "Значение должно быть больше нуля"
        if key in WINBACK_SEND_HOUR_SETTINGS and not (0 <= int_value <= 23):
            return None, "Час должен быть в диапазоне 0-23"
        return str(int_value), None

    if key in POSITIVE_FLOAT_RUNTIME_SETTINGS:
        try:
            float_value = float(value.replace(",", "."))
        except ValueError:
            return None, "Значение должно быть числом"
        if float_value <= 0:
            return None, "Значение должно быть больше нуля"
        normalized = ("%f" % float_value).rstrip("0").rstrip(".")
        return normalized, None

    if key in CSV_INT_RUNTIME_SETTINGS:
        values = [item.strip() for item in value.split(",") if item.strip()]
        for item in values:
            if not item.lstrip("-").isdigit():
                return None, "Список должен содержать только числа через запятую"
        return ",".join(values), None

    if key in CSV_STR_RUNTIME_SETTINGS:
        return ",".join(item.strip() for item in value.split(",") if item.strip()), None

    if key in ENUM_RUNTIME_SETTINGS:
        normalized = value.lower()
        allowed_values = ENUM_RUNTIME_SETTINGS[key]
        if normalized not in allowed_values:
            return None, f"Допустимые значения: {', '.join(sorted(allowed_values))}"
        return normalized, None

    if len(value) > 512:
        return None, "Значение не должно быть длиннее 512 символов"
    return value, None


def admin_validate_traffic_usage_threshold_pair(db_session, key, normalized_value):
    if key not in {
        BOT_TRAFFIC_USAGE_SUSPICIOUS_GB_SETTING,
        BOT_TRAFFIC_USAGE_ALERT_GB_SETTING,
    }:
        return None

    values = {key: normalized_value}
    other_key = (
        BOT_TRAFFIC_USAGE_ALERT_GB_SETTING
        if key == BOT_TRAFFIC_USAGE_SUSPICIOUS_GB_SETTING
        else BOT_TRAFFIC_USAGE_SUSPICIOUS_GB_SETTING
    )
    other_setting = db_session.get(SystemSetting, other_key)
    if other_setting is not None:
        values[other_key] = other_setting.value

    if not {
        BOT_TRAFFIC_USAGE_SUSPICIOUS_GB_SETTING,
        BOT_TRAFFIC_USAGE_ALERT_GB_SETTING,
    }.issubset(values):
        return None

    try:
        suspicious_gb = float(values[BOT_TRAFFIC_USAGE_SUSPICIOUS_GB_SETTING])
        alert_gb = float(values[BOT_TRAFFIC_USAGE_ALERT_GB_SETTING])
    except (TypeError, ValueError):
        return "Текущая пара порогов в БД некорректна, исправьте оба значения"

    if alert_gb < suspicious_gb:
        return "Критический порог должен быть больше или равен подозрительному"

    return None


def admin_runtime_setting_payload(key, setting=None):
    raw_value = setting.value if setting else ""
    return {
        "key": key,
        "value": raw_value,
        "display_value": admin_mask_setting_value(key, raw_value),
        "is_set": setting is not None,
        "type": admin_runtime_setting_type(key),
        "sensitive": key in SENSITIVE_RUNTIME_SETTINGS,
        "description": RUNTIME_SETTING_DESCRIPTIONS.get(key, ""),
        "allowed_values": sorted(ENUM_RUNTIME_SETTINGS.get(key, [])),
        "updated_at": admin_date_label(setting.updated_at) if setting else "",
    }


def admin_upsert_system_setting(db_session, key, value):
    setting = db_session.get(SystemSetting, key)
    if setting:
        setting.value = value
    else:
        setting = SystemSetting(key=key, value=value)
        db_session.add(setting)
    db_session.flush()
    return setting


def admin_referral_block_payload(db_session, user):
    block = db_session.get(ReferralProgramBlock, user.id) if user else None
    return {
        "user": admin_user_payload(user),
        "blocked": bool(block),
        "reason": block.reason if block else "",
        "created_at": admin_date_label(block.created_at) if block else "",
        "updated_at": admin_date_label(block.updated_at) if block else "",
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
            "yk_tariff": (
                get_tariff_display_name(recurrent.subscription_period)
                if recurrent
                else ""
            ),
            "yk_amount": recurrent.amount if recurrent else None,
            "yk_currency": recurrent.currency if recurrent else "",
        },
        "traffic": admin_traffic_status_payload(traffic),
        "first_seen": (
            admin_date_label(first_seen, with_time=False)
            if first_seen
            else "Нет данных"
        ),
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
        return JsonResponse(
            {"status": "ok", "result": admin_payment_history(db_session, user)}
        )
    finally:
        db_session.close()


def build_admin_interval_stats(
    db_session, start_date, end_date, requested_granularity="week", sales_mode="cohort"
):
    start_datetime = datetime.combine(start_date, time.min)
    end_datetime = datetime.combine(end_date, time.max)
    granularity, requested_granularity, granularity_note = (
        admin_stats_normalize_granularity(requested_granularity, start_date, end_date)
    )

    # Берем только первое создание подписки на пользователя.
    # При merge/site/magic-link сценариях повторные subscription_created не должны
    # превращать старого пользователя в новую регистрацию выбранного периода.
    #
    # Когорта считается ОДИН раз и кладется во временную таблицу: дальше все запросы
    # джойнятся к ней по user_id, а не пересчитывают оконную функцию по всему
    # event_logs на каждый показатель.
    cohort_events = build_admin_cohort_table(db_session, start_datetime, end_datetime)

    source_stats = {}
    subscription_rows = (
        db_session.query(
            cohort_events.c.traffic_source,
            func.count(cohort_events.c.user_id),
        )
        .group_by(cohort_events.c.traffic_source)
        .all()
    )
    for traffic_source, subscriptions in subscription_rows:
        source_stats[admin_stats_source_key(traffic_source)] = {
            "traffic_source": admin_stats_source_key(traffic_source),
            "subscriptions": int(subscriptions or 0),
            "connections": 0,
            "unique_paying_users": 0,
            "payments": 0,
            "revenue": 0,
            "tariffs": {},
        }

    referral_count = (
        db_session.query(func.count(User.id))
        .join(cohort_events, cohort_events.c.user_id == User.id)
        .filter(User.referred_by_id.isnot(None))
        .scalar()
        or 0
    )
    bonus_rows = (
        db_session.query(
            ReferralBonus.bonus_type,
            func.count(ReferralBonus.id),
        )
        .join(cohort_events, cohort_events.c.user_id == ReferralBonus.referral_id)
        .join(User, User.id == cohort_events.c.user_id)
        .filter(ReferralBonus.created_at >= start_datetime)
        .filter(ReferralBonus.created_at <= end_datetime)
        .filter(User.referred_by_id.isnot(None))
        .group_by(ReferralBonus.bonus_type)
        .all()
    )
    referral_bonus_counts = {bonus_type: count for bonus_type, count in bonus_rows}

    total_tariffs = {}
    totals = {
        "subscriptions": 0,
        "connections": 0,
        "payments": 0,
        "revenue": 0,
        "unique_paying_users": 0,
    }
    yk_payment_time = func.coalesce(YkPayment.captured_at, YkPayment.created_at)

    connection_rows = (
        db_session.query(
            cohort_events.c.traffic_source,
            func.count(func.distinct(EventLog.user_id)),
        )
        .select_from(EventLog)
        .join(cohort_events, cohort_events.c.user_id == EventLog.user_id)
        .filter(EventLog.event_type == "traffic_threshold_reached")
        .filter(EventLog.event_payload["threshold"].astext.cast(Integer) == 0)
        .group_by(cohort_events.c.traffic_source)
        .all()
    )
    for traffic_source, connections in connection_rows:
        source = source_stats.get(admin_stats_source_key(traffic_source))
        if source is not None:
            source["connections"] = int(connections or 0)

    yk_rows = (
        db_session.query(
            cohort_events.c.traffic_source,
            YkPayment.subscription_period,
            func.count(YkPayment.id),
            func.coalesce(func.sum(YkPayment.amount), 0),
        )
        .select_from(YkPayment)
        .join(cohort_events, cohort_events.c.user_id == YkPayment.user_id)
        .filter(YkPayment.status == "succeeded")
        .filter(yk_payment_time >= cohort_events.c.timestamp)
        .filter(yk_payment_time <= end_datetime)
        .group_by(cohort_events.c.traffic_source, YkPayment.subscription_period)
        .all()
    )
    wata_rows = (
        db_session.query(
            cohort_events.c.traffic_source,
            WataInvoice.tariff_id,
            func.count(WataTransaction.id),
            func.coalesce(func.sum(WataTransaction.amount), 0),
        )
        .select_from(WataInvoice)
        .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
        .join(cohort_events, cohort_events.c.user_id == WataInvoice.user_id)
        .filter(WataTransaction.transaction_status == "Paid")
        .filter(WataTransaction.payment_time >= cohort_events.c.timestamp)
        .filter(WataTransaction.payment_time <= end_datetime)
        .group_by(cohort_events.c.traffic_source, WataInvoice.tariff_id)
        .all()
    )
    for traffic_source, tariff_id, count, amount in [*yk_rows, *wata_rows]:
        source = source_stats.get(admin_stats_source_key(traffic_source))
        if source is None:
            continue
        tariff_name = get_tariff_display_name(tariff_id)
        amount = admin_money(amount)
        count = int(count or 0)
        source["payments"] += count
        source["revenue"] += amount
        source["tariffs"][tariff_name] = source["tariffs"].get(tariff_name, 0) + count
        total_tariffs[tariff_name] = total_tariffs.get(tariff_name, 0) + count

    yk_payer_query = (
        db_session.query(
            cohort_events.c.traffic_source.label("traffic_source"),
            YkPayment.user_id.label("user_id"),
        )
        .select_from(YkPayment)
        .join(cohort_events, cohort_events.c.user_id == YkPayment.user_id)
        .filter(YkPayment.status == "succeeded")
        .filter(yk_payment_time >= cohort_events.c.timestamp)
        .filter(yk_payment_time <= end_datetime)
    )
    wata_payer_query = (
        db_session.query(
            cohort_events.c.traffic_source.label("traffic_source"),
            WataInvoice.user_id.label("user_id"),
        )
        .select_from(WataInvoice)
        .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
        .join(cohort_events, cohort_events.c.user_id == WataInvoice.user_id)
        .filter(WataTransaction.transaction_status == "Paid")
        .filter(WataTransaction.payment_time >= cohort_events.c.timestamp)
        .filter(WataTransaction.payment_time <= end_datetime)
    )
    payer_events = union_all(
        yk_payer_query.statement,
        wata_payer_query.statement,
    ).subquery()
    payer_rows = (
        db_session.query(
            payer_events.c.traffic_source,
            func.count(func.distinct(payer_events.c.user_id)),
        )
        .group_by(payer_events.c.traffic_source)
        .all()
    )
    for traffic_source, unique_payers in payer_rows:
        source = source_stats.get(admin_stats_source_key(traffic_source))
        if source is not None:
            source["unique_paying_users"] = int(unique_payers or 0)

    sources = []
    for source in source_stats.values():
        subscriptions = source["subscriptions"]
        unique_payers = source["unique_paying_users"]
        sources.append(
            {
                "traffic_source": source["traffic_source"],
                "label": (
                    "Direct"
                    if source["traffic_source"] is None
                    else f"TS_{source['traffic_source']}"
                ),
                "subscriptions": subscriptions,
                "connections": source["connections"],
                "unique_paying_users": unique_payers,
                "payments": source["payments"],
                "revenue": source["revenue"],
                "connection_conversion": (
                    (source["connections"] / subscriptions * 100)
                    if subscriptions
                    else 0
                ),
                "payment_conversion": (
                    (unique_payers / subscriptions * 100) if subscriptions else 0
                ),
                "tariffs": [
                    {"name": name, "count": count}
                    for name, count in sorted(
                        source["tariffs"].items(),
                        key=lambda item: get_tariff_order(item[0]),
                    )
                ],
            }
        )
        totals["subscriptions"] += subscriptions
        totals["connections"] += source["connections"]
        totals["payments"] += source["payments"]
        totals["revenue"] += source["revenue"]
        totals["unique_paying_users"] += unique_payers

    sales_series = build_admin_sales_series(
        db_session,
        cohort_events,
        start_date,
        end_date,
        granularity,
        requested_granularity,
        granularity_note,
        sales_mode,
    )
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

    # Не держим временную таблицу на пуловом соединении после ответа.
    drop_admin_cohort_table(db_session)

    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "totals": totals,
        "sources": sources,
        "sales_series": sales_series,
        "tariffs": [
            {"name": name, "count": count}
            for name, count in sorted(
                total_tariffs.items(), key=lambda item: get_tariff_order(item[0])
            )
        ],
    }


def admin_clamp_cohort_window(period_start, period_end, cohort_start, cohort_end):
    """Зажимает окно когорты в границы внешнего периода.

    Возвращает кортеж (clamped_start, clamped_end, error_message). Окно когорты
    обязано пересекаться с внешним периодом — иначе считать нечего, и возвращается
    текст ошибки (русский, как остальные admin-ответы). Чистая функция без БД —
    удобно покрывать юнит-тестами.
    """
    if cohort_start > cohort_end:
        return None, None, "Начало когорты больше её конца"

    clamped_start = max(cohort_start, period_start)
    clamped_end = min(cohort_end, period_end)
    if clamped_start > clamped_end:
        return None, None, "Окно когорты вне выбранного диапазона"

    return clamped_start, clamped_end, None


def build_admin_cohort_retention_stats(
    db_session,
    period_start,
    period_end,
    cohort_start,
    cohort_end,
    requested_granularity="month",
):
    """Когортная аналитика с РАЗДЕЛЁННЫМИ окнами когорты и графика.

    Отличие от build_admin_interval_stats: там окно когорты и окно графика — это один
    и тот же интервал. Здесь они разделены:
    - когорта фиксируется по ВНУТРЕННЕМУ окну [cohort_start, cohort_end];
    - платежи этой когорты строятся на ВСЁМ внешнем периоде [period_start, period_end].

    Это позволяет, например, взять весь 2025 год как период и январь как когорту, и
    посмотреть, что осталось от январских клиентов на всём горизонте года.

    Бизнес-логика выбора когорты идентична существующей (первое событие
    subscription_created на пользователя), поэтому переиспользуется build_admin_cohort_table,
    а сама серия графика — build_admin_sales_series в режиме "cohort". Никакой
    существующий код при этом не меняется.
    """
    period_start_dt = datetime.combine(period_start, time.min)
    period_end_dt = datetime.combine(period_end, time.max)
    cohort_start_dt = datetime.combine(cohort_start, time.min)
    cohort_end_dt = datetime.combine(cohort_end, time.max)

    granularity, requested_granularity, granularity_note = (
        admin_stats_normalize_granularity(
            requested_granularity, period_start, period_end
        )
    )

    yk_payment_time = func.coalesce(YkPayment.captured_at, YkPayment.created_at)

    # Когорта материализуется по ОКНУ КОГОРТЫ (а не по всему периоду).
    cohort_events = build_admin_cohort_table(db_session, cohort_start_dt, cohort_end_dt)
    try:
        source_stats = {}
        cohort_size = 0
        subscription_rows = (
            db_session.query(
                cohort_events.c.traffic_source,
                func.count(cohort_events.c.user_id),
            )
            .group_by(cohort_events.c.traffic_source)
            .all()
        )
        for traffic_source, subscriptions in subscription_rows:
            key = admin_stats_source_key(traffic_source)
            count = int(subscriptions or 0)
            cohort_size += count
            source_stats[key] = {
                "traffic_source": key,
                "label": "Direct" if traffic_source is None else f"TS_{traffic_source}",
                "subscriptions": count,
                "payments": 0,
                "revenue": 0,
                "unique_paying_users": 0,
            }

        # Платежи когорты за ВЕСЬ внешний период, агрегированные по источникам.
        # payment_time >= timestamp когорты гарантирует, что платежи раньше первой
        # подписки пользователя в когорту не попадают.
        yk_rows = (
            db_session.query(
                cohort_events.c.traffic_source,
                func.count(YkPayment.id),
                func.coalesce(func.sum(YkPayment.amount), 0),
            )
            .select_from(YkPayment)
            .join(cohort_events, cohort_events.c.user_id == YkPayment.user_id)
            .filter(YkPayment.status == "succeeded")
            .filter(yk_payment_time >= cohort_events.c.timestamp)
            .filter(yk_payment_time >= period_start_dt)
            .filter(yk_payment_time <= period_end_dt)
            .group_by(cohort_events.c.traffic_source)
            .all()
        )
        wata_rows = (
            db_session.query(
                cohort_events.c.traffic_source,
                func.count(WataTransaction.id),
                func.coalesce(func.sum(WataTransaction.amount), 0),
            )
            .select_from(WataInvoice)
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .join(cohort_events, cohort_events.c.user_id == WataInvoice.user_id)
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(WataTransaction.payment_time >= cohort_events.c.timestamp)
            .filter(WataTransaction.payment_time >= period_start_dt)
            .filter(WataTransaction.payment_time <= period_end_dt)
            .group_by(cohort_events.c.traffic_source)
            .all()
        )
        total_payments = 0
        total_revenue = 0
        for traffic_source, count, amount in [*yk_rows, *wata_rows]:
            count = int(count or 0)
            amount = admin_money(amount)
            total_payments += count
            total_revenue += amount
            source = source_stats.get(admin_stats_source_key(traffic_source))
            if source is not None:
                source["payments"] += count
                source["revenue"] += amount

        # Уникальные плательщики когорты (по источникам и всего). У каждого
        # пользователя ровно одна строка в когорте → одно значение traffic_source,
        # поэтому сумма distinct по источникам равна общему distinct по когорте.
        yk_payer_query = (
            db_session.query(
                cohort_events.c.traffic_source.label("traffic_source"),
                YkPayment.user_id.label("user_id"),
            )
            .select_from(YkPayment)
            .join(cohort_events, cohort_events.c.user_id == YkPayment.user_id)
            .filter(YkPayment.status == "succeeded")
            .filter(yk_payment_time >= cohort_events.c.timestamp)
            .filter(yk_payment_time >= period_start_dt)
            .filter(yk_payment_time <= period_end_dt)
        )
        wata_payer_query = (
            db_session.query(
                cohort_events.c.traffic_source.label("traffic_source"),
                WataInvoice.user_id.label("user_id"),
            )
            .select_from(WataInvoice)
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .join(cohort_events, cohort_events.c.user_id == WataInvoice.user_id)
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(WataTransaction.payment_time >= cohort_events.c.timestamp)
            .filter(WataTransaction.payment_time >= period_start_dt)
            .filter(WataTransaction.payment_time <= period_end_dt)
        )
        payer_events = union_all(
            yk_payer_query.statement,
            wata_payer_query.statement,
        ).subquery()
        payer_rows = (
            db_session.query(
                payer_events.c.traffic_source,
                func.count(func.distinct(payer_events.c.user_id)),
            )
            .group_by(payer_events.c.traffic_source)
            .all()
        )
        unique_paying_users = 0
        for traffic_source, payers in payer_rows:
            payers = int(payers or 0)
            unique_paying_users += payers
            source = source_stats.get(admin_stats_source_key(traffic_source))
            if source is not None:
                source["unique_paying_users"] = payers

        # Сколько рефералов ПРИВЕЛА когорта: пользователи, чей referred_by_id
        # указывает на члена когорты (join по лёгкой temp-таблице, дёшево). Это
        # обратное направление к build_admin_interval_stats, где считается, сколько
        # членов когорты сами пришли по реферальной ссылке.
        invited_referrals = (
            db_session.query(func.count(User.id))
            .join(cohort_events, cohort_events.c.user_id == User.referred_by_id)
            .scalar()
            or 0
        )
        # Из приведённых: сколько начали пользоваться (TRAFFIC) и сколько оплатили
        # (PURCHASE) — по бонусам, где пригласивший (referrer_id) состоит в когорте.
        invited_bonus_rows = (
            db_session.query(
                ReferralBonus.bonus_type,
                func.count(ReferralBonus.id),
            )
            .join(cohort_events, cohort_events.c.user_id == ReferralBonus.referrer_id)
            .group_by(ReferralBonus.bonus_type)
            .all()
        )
        invited_bonus_counts = {
            bonus_type: int(count or 0) for bonus_type, count in invited_bonus_rows
        }

        # График платежей когорты на ВСЁМ внешнем периоде — переиспользуем
        # существующий построитель серии в когортном режиме, передав внешний период.
        sales_series = build_admin_sales_series(
            db_session,
            cohort_events,
            period_start,
            period_end,
            granularity,
            requested_granularity,
            granularity_note,
            sales_mode="cohort",
        )
    finally:
        # Не держим временную таблицу на пуловом соединении после ответа.
        drop_admin_cohort_table(db_session)

    sources = []
    for source in sorted(
        source_stats.values(), key=lambda item: item["subscriptions"], reverse=True
    ):
        subscriptions = source["subscriptions"]
        payers = source["unique_paying_users"]
        sources.append(
            {
                **source,
                "payment_conversion": (
                    payers / subscriptions * 100 if subscriptions else 0
                ),
                "arpu": source["revenue"] / subscriptions if subscriptions else 0,
            }
        )

    totals = {
        "cohort_size": cohort_size,
        "payments": total_payments,
        "revenue": total_revenue,
        "unique_paying_users": unique_paying_users,
        "payment_conversion": (
            unique_paying_users / cohort_size * 100 if cohort_size else 0
        ),
        "arpu": total_revenue / cohort_size if cohort_size else 0,
        "arppu": total_revenue / unique_paying_users if unique_paying_users else 0,
        "invited_referrals": invited_referrals,
        "invited_referrals_active": invited_bonus_counts.get(
            ReferralBonusType.TRAFFIC, 0
        ),
        "invited_referrals_paid": invited_bonus_counts.get(
            ReferralBonusType.PURCHASE, 0
        ),
    }

    return {
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "cohort": {"start": cohort_start.isoformat(), "end": cohort_end.isoformat()},
        "totals": totals,
        "sources": sources,
        "sales_series": sales_series,
    }


def support_admin_api_stats(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
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
            {"status": "error", "message": "Неверный формат даты"}, status=400
        )

    db_session = session_factory()
    try:
        return JsonResponse(
            {
                "status": "ok",
                "result": build_admin_interval_stats(
                    db_session,
                    start_date,
                    end_date,
                    request.GET.get("granularity", "week"),
                    request.GET.get("sales_mode", "cohort"),
                ),
            }
        )
    finally:
        db_session.close()


def support_admin_api_stats_source_users(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
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
        first_subscription_events = (
            db_session.query(
                EventLog.user_id.label("user_id"),
                EventLog.event_payload.label("event_payload"),
                EventLog.timestamp.label("timestamp"),
                func.row_number()
                .over(
                    partition_by=EventLog.user_id,
                    order_by=(EventLog.timestamp, EventLog.id),
                )
                .label("row_number"),
            )
            .filter(EventLog.event_type == "subscription_created")
            .subquery()
        )

        query = (
            db_session.query(
                first_subscription_events.c.user_id,
                first_subscription_events.c.timestamp.label("last_seen"),
            )
            .filter(first_subscription_events.c.row_number == 1)
            .filter(first_subscription_events.c.timestamp >= start_datetime)
            .filter(first_subscription_events.c.timestamp <= end_datetime)
        )
        if traffic_source is None:
            query = query.filter(
                first_subscription_events.c.event_payload["traffic_source"].astext.is_(
                    None
                )
            )
        else:
            query = query.filter(
                first_subscription_events.c.event_payload["traffic_source"].astext
                == str(traffic_source)
            )
        query = query.order_by(first_subscription_events.c.timestamp.desc())
        total = query.count()
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
        user_ids = [row.user_id for row in rows]
        users_by_id = {
            user.id: user
            for user in db_session.query(User)
            .filter(User.id.in_(user_ids or {-1}))
            .all()
        }

        page_subscription_events = (
            db_session.query(
                first_subscription_events.c.user_id,
                first_subscription_events.c.timestamp,
            )
            .filter(first_subscription_events.c.row_number == 1)
            .filter(first_subscription_events.c.user_id.in_(user_ids or {-1}))
            .subquery()
        )
        # В списке пользователей источник должен совпадать со сводкой:
        # платежи старше первого subscription_created не считаются оплатой когорты.
        yk_payment_time = func.coalesce(YkPayment.captured_at, YkPayment.created_at)
        yk_paying_user_ids = {
            row[0]
            for row in db_session.query(YkPayment.user_id)
            .join(
                page_subscription_events,
                page_subscription_events.c.user_id == YkPayment.user_id,
            )
            .filter(YkPayment.status == "succeeded")
            .filter(yk_payment_time >= page_subscription_events.c.timestamp)
            .distinct()
            .all()
        }
        wata_paying_user_ids = {
            row[0]
            for row in db_session.query(WataInvoice.user_id)
            .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
            .join(
                page_subscription_events,
                page_subscription_events.c.user_id == WataInvoice.user_id,
            )
            .filter(WataTransaction.transaction_status == "Paid")
            .filter(
                WataTransaction.payment_time >= page_subscription_events.c.timestamp
            )
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


def support_admin_api_cohort_stats(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response

    try:
        today = date.today()
        # По умолчанию: внешний период — последние 12 месяцев, когорта — первый месяц.
        period_start = (
            admin_parse_date(request.GET.get("start"))
            if request.GET.get("start")
            else today - timedelta(days=364)
        )
        period_end = (
            admin_parse_date(request.GET.get("end"))
            if request.GET.get("end")
            else today
        )
        cohort_start = (
            admin_parse_date(request.GET.get("cohort_start"))
            if request.GET.get("cohort_start")
            else period_start
        )
        cohort_end = (
            admin_parse_date(request.GET.get("cohort_end"))
            if request.GET.get("cohort_end")
            else min(period_end, period_start + timedelta(days=29))
        )
    except ValueError:
        return JsonResponse(
            {"status": "error", "message": "Неверный формат даты"}, status=400
        )

    if period_start > period_end:
        return JsonResponse(
            {"status": "error", "message": "Начало диапазона больше его конца"},
            status=400,
        )

    cohort_start, cohort_end, cohort_error = admin_clamp_cohort_window(
        period_start, period_end, cohort_start, cohort_end
    )
    if cohort_error:
        return JsonResponse(
            {"status": "error", "message": cohort_error}, status=400
        )

    db_session = session_factory()
    try:
        return JsonResponse(
            {
                "status": "ok",
                "result": build_admin_cohort_retention_stats(
                    db_session,
                    period_start,
                    period_end,
                    cohort_start,
                    cohort_end,
                    request.GET.get("granularity", "month"),
                ),
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
        return JsonResponse(
            {"status": "error", "message": "Введите ID платежа"}, status=400
        )

    db_session = session_factory()
    try:
        payment = (
            db_session.query(YkPayment)
            .filter(YkPayment.payment_id == payment_id)
            .first()
        )
        system = "YooKassa"
        invoice = None
        if not payment:
            payment = (
                db_session.query(WataTransaction)
                .filter(WataTransaction.transaction_id == payment_id)
                .first()
            )
            system = "Wata"
            if payment:
                invoice = (
                    db_session.query(WataInvoice)
                    .filter(WataInvoice.order_id == payment.order_id)
                    .first()
                )
        if not payment:
            return JsonResponse({"status": "not_found"}, status=404)

        user_id = invoice.user_id if invoice else payment.user_id
        user = db_session.get(User, user_id)
        if not user:
            return JsonResponse(
                {"status": "not_found", "message": "Пользователь не найден"}, status=404
            )

        payment_payload = admin_payment_info_payload(
            db_session, payment, user, system, invoice
        )
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
                    "status": (
                        "Успешен" if payment.transaction_status == "Paid" else "Ошибка"
                    ),
                    "success": payment.transaction_status == "Paid",
                }
            )

        payments.sort(key=lambda item: item["date_sort"], reverse=True)
        payments = payments[:limit]
        for payment in payments:
            payment.pop("date_sort", None)

        return JsonResponse(
            {"status": "ok", "payments": payments, "total": len(payments)}
        )
    finally:
        db_session.close()


def admin_payment_info_payload(db_session, payment, user, system, invoice=None):
    recurrent = (
        db_session.query(YkRecurrentPayment)
        .filter(YkRecurrentPayment.user_id == user.id)
        .first()
    )
    yk_ltv = (
        db_session.query(func.sum(YkPayment.amount))
        .filter(YkPayment.user_id == user.id, YkPayment.status == "succeeded")
        .scalar()
        or 0
    )
    wata_ltv = (
        db_session.query(func.sum(WataTransaction.amount))
        .join(WataInvoice, WataInvoice.order_id == WataTransaction.order_id)
        .filter(WataInvoice.user_id == user.id)
        .filter(WataTransaction.transaction_status == "Paid")
        .scalar()
        or 0
    )
    yk_count = (
        db_session.query(func.count(YkPayment.id))
        .filter(YkPayment.user_id == user.id)
        .scalar()
        or 0
    )
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
            "type": (
                "Пробный период"
                if payment.is_trial_promotion
                else ("Автосписание" if payment.is_autopay else "Обычный платеж")
            ),
            "status": {
                "succeeded": "Успешен",
                "pending": "В обработке",
                "canceled": "Отменен",
                "waiting_for_capture": "Ожидает подтверждения",
            }.get(status, status),
            "success": is_success,
            # Сырая причина отмены из вебхука ЮКассы (insufficient_funds,
            # recurring_permission_revoked, ...); у старых платежей пусто.
            "cancellation_reason": payment.cancellation_reason or "",
        }
    else:
        status = payment.transaction_status
        payload = {
            "id": payment.transaction_id,
            "date": admin_date_label(payment.payment_time),
            "amount": admin_money(payment.amount),
            "currency": payment.currency,
            "tariff": (invoice and get_tariff_display_name(invoice.tariff_id))
            or payment.order_description,
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
        return JsonResponse(
            {
                "status": "ok",
                "result": admin_referral_payload(db_session, user),
                "top": top,
            }
        )
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
    yk_count = (
        db_session.query(func.count(YkPayment.id))
        .filter(YkPayment.user_id == user_id, YkPayment.status == "succeeded")
        .scalar()
        or 0
    )
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
    referrals = (
        db_session.query(User)
        .filter(User.referred_by_id == user.id)
        .order_by(User.id.desc())
        .all()
    )
    bonuses = (
        db_session.query(ReferralBonus)
        .filter(ReferralBonus.referrer_id == user.id)
        .all()
    )
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
                "children_count": db_session.query(func.count(User.id))
                .filter(User.referred_by_id == referral.id)
                .scalar()
                or 0,
            }
        )
        nodes.append(admin_referral_graph_node(referral))
        edges.append({"from": user.id, "to": referral.id})
        children = (
            db_session.query(User)
            .filter(User.referred_by_id == referral.id)
            .order_by(User.id.desc())
            .limit(30)
            .all()
        )
        for child in children:
            nodes.append(admin_referral_graph_node(child))
            edges.append({"from": referral.id, "to": child.id})

    return {
        "user": admin_user_payload(user),
        "referral_block": admin_referral_block_payload(db_session, user),
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
        "label": (
            f"@{user.username}"
            if user.username
            else f"ID {user.telegram_id or user.id}"
        ),
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
            return JsonResponse(
                {"status": "ok", "result": {"user": admin_user_payload(user)}}
            )

        BAN_SQUAD_UUID = os.getenv(
            "BAN_SQUAD_UUID", "be4e12f0-098a-4d44-88d4-37cff58bf2d7"
        )

        if action == "disable_subscription":
            # Как «Отключение подписки» в трафик-алертах бота: статус RWMS
            # DISABLED + выключаем автоплатёж. Ручная оплата реактивирует.
            user.autopay_allow = False
            removed_recurrents = (
                db_session.query(YkRecurrentPayment)
                .filter(YkRecurrentPayment.user_id == user.id)
                .delete(synchronize_session=False)
            )
            admin_audit_write(
                db_session, request, "subscription_disable", target=user.username
            )
            db_session.commit()
            rwms_updated = False
            rwms_user = rwms_client.get_user_by_username(user.username)
            if rwms_user:
                response = rwms_client.update_user(
                    proto.UpdateUserRequest(
                        uuid=rwms_user.uuid,
                        status=proto.UserStatus.DISABLED,
                    )
                )
                rwms_updated = response is not None
            return JsonResponse({
                "status": "ok",
                "result": {
                    "user": admin_user_payload(user),
                    "action_label": "Подписка отключена (DISABLED)",
                    "removed_recurrents": removed_recurrents,
                    "rwms_updated": rwms_updated,
                },
            })

        if action == "temp_ban":
            # Как «Временный бан» в трафик-алертах бота: переводим подписку в
            # ban-сквад и пишем TemporarySquadBan(unban_at). Снимает бан фоновый
            # unban-watcher бота (process_due_unbans) по достижении unban_at.
            try:
                hours = float(request.POST.get("hours") or "0")
            except ValueError:
                return JsonResponse(
                    {"status": "error", "message": "Часы должны быть числом"}, status=400
                )
            if hours <= 0:
                return JsonResponse(
                    {"status": "error", "message": "Длительность должна быть больше 0"},
                    status=400,
                )
            rwms_user = rwms_client.get_user_by_username(user.username)
            if rwms_user is None:
                return JsonResponse(
                    {"status": "error", "message": "Подписка в RWMS не найдена"},
                    status=404,
                )
            response = rwms_client.update_user(
                proto.UpdateUserRequest(
                    uuid=rwms_user.uuid,
                    active_internal_squads=[BAN_SQUAD_UUID],
                )
            )
            if response is None:
                return JsonResponse(
                    {"status": "error", "message": "RWMS не применил ban-сквад"},
                    status=502,
                )
            now_naive = datetime.utcnow()
            unban_at = now_naive + timedelta(hours=hours)
            ban = (
                db_session.query(TemporarySquadBan)
                .filter(TemporarySquadBan.user_id == user.id)
                .one_or_none()
            )
            if ban is None:
                db_session.add(TemporarySquadBan(
                    user_id=user.id, banned_at=now_naive, unban_at=unban_at,
                ))
            else:
                ban.banned_at = now_naive
                ban.unban_at = unban_at
                ban.restored_at = None
            admin_audit_write(
                db_session, request, "temp_ban", target=user.username, hours=hours
            )
            db_session.commit()
            return JsonResponse({
                "status": "ok",
                "result": {
                    "user": admin_user_payload(user),
                    "action_label": f"Временный бан до {unban_at:%Y-%m-%d %H:%M} UTC (снимет бот)",
                    "rwms_updated": True,
                },
            })

        if action == "stop_autopay":
            old_value = bool(user.autopay_allow)
            user.autopay_allow = False
            removed_recurrents = (
                db_session.query(YkRecurrentPayment)
                .filter(YkRecurrentPayment.user_id == user.id)
                .delete(synchronize_session=False)
            )
            admin_audit_write(
                db_session, request, "stop_autopay", target=user.username
            )
            db_session.commit()
            return JsonResponse(
                {
                    "status": "ok",
                    "result": {
                        "user": admin_user_payload(user),
                        "old_autopay_allow": old_value,
                        "removed_recurrents": removed_recurrents,
                    },
                }
            )

        if action == "set_trial_hour":
            target_expire = datetime.now(timezone.utc) + timedelta(hours=1)
        elif action == "extend":
            try:
                days = int(request.POST.get("days") or "0")
            except ValueError:
                return JsonResponse(
                    {"status": "error", "message": "Дни должны быть числом"}, status=400
                )
            if days < 1:
                return JsonResponse(
                    {"status": "error", "message": "Интервал должен быть больше нуля"},
                    status=400,
                )
            current_expire = admin_dt(user.expire_at)
            base = max(current_expire or datetime.utcnow(), datetime.utcnow())
            target_expire = base.replace(tzinfo=timezone.utc) + timedelta(days=days)
        else:
            return JsonResponse(
                {"status": "error", "message": "Неизвестное действие"}, status=400
            )

        old_expire = user.expire_at
        user.expire_at = target_expire.replace(tzinfo=None)
        admin_audit_write(
            db_session,
            request,
            "subscription_" + action,
            target=user.username,
            old=str(old_expire),
            new=str(user.expire_at),
        )
        db_session.commit()

        rwms_user = rwms_client.get_user_by_username(user.username)
        rwms_updated = False
        if rwms_user:
            user_email = (
                rwms_user.email if rwms_user.email and "@" in rwms_user.email else None
            )
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
        else:
            logging.warning(
                "RWMS subscription for admin-updated user %s is missing, recreating",
                user.username,
            )
            response = create_user_until(
                rwms_client=rwms_client,
                username=user.username,
                expire_at=target_expire,
                email=user.email,
                telegram_id=user.telegram_id,
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


def support_admin_api_runtime_settings(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "GET":
            settings_by_key = {
                setting.key: setting
                for setting in db_session.query(SystemSetting).all()
            }
            items = [
                admin_runtime_setting_payload(key, settings_by_key.get(key))
                for key in RUNTIME_SETTING_KEYS
            ]
            known_keys = set(RUNTIME_SETTING_KEYS)
            extra_items = [
                {
                    "key": setting.key,
                    "value": setting.value,
                    "display_value": admin_mask_setting_value(
                        setting.key, setting.value
                    ),
                    "is_set": True,
                    "type": "custom",
                    "sensitive": False,
                    "description": "Пользовательская настройка",
                    "allowed_values": [],
                    "updated_at": admin_date_label(setting.updated_at),
                }
                for setting in settings_by_key.values()
                if setting.key not in known_keys
            ]
            return JsonResponse({"status": "ok", "settings": items + extra_items})

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        action = request.POST.get("action")
        key = (request.POST.get("key") or "").strip()
        if action == "delete":
            if key not in RUNTIME_SETTING_KEYS:
                return JsonResponse(
                    {"status": "error", "message": "Неизвестная настройка"}, status=400
                )
            db_session.execute(sa_delete(SystemSetting).where(SystemSetting.key == key))
            admin_audit_write(db_session, request, "setting_delete", target=key)
            db_session.commit()
            return JsonResponse({"status": "ok"})

        if action == "save":
            raw_value = request.POST.get("value") or ""
            if key in SENSITIVE_RUNTIME_SETTINGS and not raw_value.strip():
                return JsonResponse(
                    {
                        "status": "error",
                        "message": "Введите новое значение секрета или удалите настройку из БД",
                    },
                    status=400,
                )
            normalized_value, error = admin_validate_runtime_setting(key, raw_value)
            if error:
                return JsonResponse({"status": "error", "message": error}, status=400)
            pair_error = admin_validate_traffic_usage_threshold_pair(
                db_session, key, normalized_value
            )
            if pair_error:
                return JsonResponse(
                    {"status": "error", "message": pair_error}, status=400
                )
            setting = admin_upsert_system_setting(db_session, key, normalized_value)
            admin_audit_write(
                db_session,
                request,
                "setting_save",
                target=key,
                value=admin_mask_setting_value(key, normalized_value),
            )
            db_session.commit()
            db_session.refresh(setting)
            return JsonResponse(
                {"status": "ok", "setting": admin_runtime_setting_payload(key, setting)}
            )

        return JsonResponse(
            {"status": "error", "message": "Неизвестное действие"}, status=400
        )
    finally:
        db_session.close()


def support_admin_api_referral_antifraud(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    keys = (
        BOT_REFERRAL_REGISTRATION_AUTOBLOCK_ENABLED_SETTING,
        BOT_REFERRAL_REGISTRATION_BURST_LIMIT_SETTING,
        BOT_REFERRAL_REGISTRATION_BURST_WINDOW_MINUTES_SETTING,
    )
    db_session = session_factory()
    try:
        if request.method == "POST":
            action = request.POST.get("action")
            if action in {"enable", "disable"}:
                admin_upsert_system_setting(
                    db_session,
                    BOT_REFERRAL_REGISTRATION_AUTOBLOCK_ENABLED_SETTING,
                    "1" if action == "enable" else "0",
                )
            elif action == "set":
                for key, form_key in (
                    (BOT_REFERRAL_REGISTRATION_BURST_LIMIT_SETTING, "limit"),
                    (
                        BOT_REFERRAL_REGISTRATION_BURST_WINDOW_MINUTES_SETTING,
                        "window_minutes",
                    ),
                ):
                    normalized_value, error = admin_validate_runtime_setting(
                        key, request.POST.get(form_key) or ""
                    )
                    if error:
                        return JsonResponse(
                            {"status": "error", "message": error}, status=400
                        )
                    admin_upsert_system_setting(db_session, key, normalized_value)
            else:
                return JsonResponse(
                    {"status": "error", "message": "Неизвестное действие"}, status=400
                )
            db_session.commit()
        elif request.method != "GET":
            return JsonResponse({"status": "error"}, status=405)

        settings_by_key = {
            setting.key: setting
            for setting in db_session.query(SystemSetting)
            .filter(SystemSetting.key.in_(keys))
            .all()
        }
        return JsonResponse(
            {
                "status": "ok",
                "settings": [
                    admin_runtime_setting_payload(key, settings_by_key.get(key))
                    for key in keys
                ],
            }
        )
    finally:
        db_session.close()


def support_admin_api_referral_block(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response
    if request.method != "POST":
        return JsonResponse({"status": "error"}, status=405)

    db_session = session_factory()
    try:
        user = admin_find_user(db_session, request.POST.get("q"))
        if not user:
            return JsonResponse({"status": "not_found"}, status=404)

        action = request.POST.get("action")
        if action == "block":
            reason = (request.POST.get("reason") or "manual admin block").strip()
            block = db_session.get(ReferralProgramBlock, user.id)
            if block:
                block.reason = reason
            else:
                db_session.add(ReferralProgramBlock(user_id=user.id, reason=reason))
            admin_audit_write(
                db_session, request, "referral_block", target=user.username,
                reason=reason,
            )
            db_session.commit()
        elif action == "unblock":
            db_session.execute(
                sa_delete(ReferralProgramBlock).where(
                    ReferralProgramBlock.user_id == user.id
                )
            )
            admin_audit_write(
                db_session, request, "referral_unblock", target=user.username
            )
            db_session.commit()
        elif action != "status":
            return JsonResponse(
                {"status": "error", "message": "Неизвестное действие"}, status=400
            )

        return JsonResponse(
            {"status": "ok", "result": admin_referral_block_payload(db_session, user)}
        )
    finally:
        db_session.close()


def support_admin_api_recurrents(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response
    if request.method != "GET":
        return JsonResponse({"status": "error"}, status=405)

    db_session = session_factory()
    try:
        limit = min(max(int(request.GET.get("limit") or 50), 1), 200)
    except ValueError:
        limit = 50
    try:
        rows = (
            db_session.query(YkRecurrentPayment, User)
            .join(User, User.id == YkRecurrentPayment.user_id)
            .order_by(YkRecurrentPayment.captured_at.desc())
            .limit(limit)
            .all()
        )
        return JsonResponse(
            {
                "status": "ok",
                "recurrents": [
                    {
                        "id": recurrent.id,
                        "payment_id": recurrent.recurrent_payment_id,
                        "user": admin_user_payload(user),
                        "amount": recurrent.amount,
                        "currency": recurrent.currency,
                        "tariff": get_tariff_display_name(
                            recurrent.subscription_period
                        ),
                        "captured_at": admin_date_label(recurrent.captured_at),
                        "scheduled_payment": bool(recurrent.scheduled_payment),
                        "trial": bool(recurrent.is_trial_promotion),
                    }
                    for recurrent, user in rows
                ],
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


def should_send_payment_login_email(request, user):
    return not (request.user.is_authenticated and str(request.user.id) == str(user.id))


def wants_payment_launch_json(request):
    return (
        request.headers.get("X-Payment-Launch") == "new-tab"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )


def pay(request):
    if request.method == "POST":
        capture_tracking_params(request)
        email_raw = request.POST.get("email")
        tariff_id = request.POST.get("tariff_id")
        raw_purchase_token = None
        use_permanent_purchase_link = False
        payment_status_url = None
        is_authenticated_payment = False
        payment_launch_json = wants_payment_launch_json(request)
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
            logging.warning(
                "payment request rejected: missing email, tariff_id=%s", tariff_id
            )
            return HttpResponse("Email обязателен", status=400)

        email = email_raw.lower().strip()

        if not email or not tariff_id:
            logging.warning(
                "payment request rejected: email_or_tariff_missing email_present=%s tariff_id=%s",
                bool(email),
                tariff_id,
            )
            return HttpResponse("Не указан email или тариф", status=400)

        db_session = session_factory()
        try:
            tariff = next(
                (
                    t
                    for t in get_runtime_actual_tariffs(db_session)
                    if t.db_tariff_id == tariff_id
                ),
                None,
            )
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

            authenticated_user = None
            if request.user.is_authenticated:
                authenticated_user = (
                    db_session.query(User).filter(User.id == request.user.id).first()
                )
                if not authenticated_user:
                    logging.warning(
                        "payment request rejected: authenticated user not found user_id=%s",
                        request.user.id,
                    )
                    return HttpResponse("Аккаунт не найден", status=401)

            # Ищем или создаем пользователя. Для авторизованного аккаунта используем
            # именно текущую запись, чтобы платеж не создал дубль по email.
            if authenticated_user:
                user = authenticated_user
                is_authenticated_payment = True
                if user.email and user.email != email:
                    logging.warning(
                        "payment request rejected: email mismatch for authenticated user "
                        "user_id=%s user_email=%s submitted_email=%s",
                        user.id,
                        user.email,
                        email,
                    )
                    return HttpResponse(
                        "Email не совпадает с текущим аккаунтом",
                        status=400,
                    )

                if not user.email:
                    email_owner = (
                        db_session.query(User)
                        .filter(User.email == email, User.id != user.id)
                        .first()
                    )
                    if email_owner:
                        logging.warning(
                            "payment request rejected: email already belongs to another user "
                            "email=%s current_user_id=%s owner_user_id=%s",
                            email,
                            user.id,
                            email_owner.id,
                        )
                        return HttpResponse(
                            "Этот email уже привязан к другому аккаунту",
                            status=400,
                        )
                    user.email = email
                    db_session.flush()
                    logging.info(
                        "payment email attached to authenticated user: "
                        "email=%s user_id=%s username=%s tariff_id=%s",
                        email,
                        user.id,
                        user.username,
                        tariff.db_tariff_id,
                    )

                logging.info(
                    "payment existing authenticated user tracking preserved: "
                    "email=%s user_id=%s username=%s tariff_id=%s",
                    email,
                    user.id,
                    user.username,
                    tariff.db_tariff_id,
                )
            else:
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

            # Полностью заблокированный аккаунт не может оплачивать — проверяем
            # уже разрешённого пользователя, это закрывает и анонимную оплату
            # по email заблокированного (иначе платёж реактивировал бы подписку).
            if is_user_blocked(db_session, user.id):
                logging.warning(
                    "payment request rejected: user %s is blocked (email=%s)",
                    user.id,
                    email,
                )
                return HttpResponse(ACCOUNT_BLOCKED_MESSAGE, status=403)

            use_permanent_purchase_link = (
                request.POST.get("login_link_kind") == "purchase_permanent"
            )
            base_url = get_current_base_url(request)
            payment_success_redirect_url = f"{base_url}/dashboard/"
            payment_fail_redirect_url = f"{base_url}/"
            login_link = None

            if use_permanent_purchase_link:
                raw_purchase_token = create_purchase_login_token(db_session, user)
                logging.info(
                    "created permanent purchase login link: email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )

            if not raw_purchase_token:
                raw_purchase_token = create_purchase_login_token(db_session, user)
                logging.info(
                    "created payment status token: email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )

            login_link = build_purchase_login_link(request, raw_purchase_token)
            payment_status_url = build_payment_status_url(request, raw_purchase_token)
            payment_success_redirect_url = payment_status_url
            payment_fail_redirect_url = append_query_params(
                payment_status_url,
                {"result": "failed"},
            )

            tariff, promo_discount_applied = site_apply_first_purchase_discount(
                db_session, user, tariff
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
                    email=email or user.email,
                    promo=promo_discount_applied,
                )
                confirmation_url = created_payment.confirmation_url
                login_token = get_purchase_login_token(
                    db_session,
                    raw_purchase_token,
                )
                login_token.payment_gateway = "yookassa"
                login_token.payment_reference = created_payment.reference

            invoice_event = create_invoice_event_for_tariff(tariff.db_tariff_id)
            if invoice_event:
                add_event_log(db_session, user, invoice_event)

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
                login_link = (
                    f"{get_current_base_url(request)}/login/magic/{magic.token}/"
                )
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
            request.session[payment_session_url_key(raw_purchase_token)] = (
                confirmation_url
            )
            request.session.modified = True
            logging.info(
                "payment db transaction committed: email=%s user_id=%s tariff_id=%s",
                email,
                user.id,
                tariff.db_tariff_id,
            )

            if not should_send_payment_login_email(request, user):
                logging.info(
                    "payment login email skipped for authenticated user: "
                    "email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )
            else:
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
                    logging.exception(
                        f"failed to send payment magic link to {email}: {e}"
                    )

            logging.info(
                "payment redirecting to confirmation_url: email=%s user_id=%s tariff_id=%s json=%s",
                email,
                user.id,
                tariff.db_tariff_id,
                payment_launch_json,
            )
            if payment_launch_json:
                return JsonResponse(
                    {
                        "status": "ok",
                        "payment_url": confirmation_url,
                        "payment_status_url": payment_status_url,
                    }
                )
            return redirect(confirmation_url)

        except Exception as e:
            db_session.rollback()
            logging.exception("Pay error")
            messages.error(request, "Ошибка платежной системы")
            error_message = (
                "Не удалось открыть форму оплаты. Попробуйте еще раз "
                "или напишите в поддержку."
            )
            if "creating subscription for site user" in str(e):
                error_message = (
                    "Не удалось подготовить личный кабинет для оплаты. "
                    "Попробуйте еще раз или напишите в поддержку."
                )
            if payment_launch_json:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": error_message,
                    },
                    status=502,
                )
            if settings.PAYMENT_GATEWAY.lower() == "wata":
                return render(
                    request,
                    "payment_status.html",
                    {
                        "initial_status": "failed",
                        "initial_message": error_message,
                        "login_url": "",
                        "payment_url": "",
                        "status_api_url": "",
                        "support_telegram_url": settings.SUPPORT_TELEGRAM_URL,
                    },
                    status=502,
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


def support_admin_api_traffic_nodes(request):
    """Список нод Remnawave для вкладки «Трафик нод» (только admin)."""
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    nodes = node_traffic.list_nodes(rwms_client)
    if nodes is None:
        return JsonResponse(
            {"status": "error", "message": "rwms недоступен, список нод не получен"},
            status=502,
        )

    return JsonResponse(
        {
            "status": "ok",
            "result": [
                {
                    "uuid": node.uuid,
                    "name": node.name,
                    "country_code": (
                        node.country_code if node.HasField("country_code") else None
                    ),
                    "is_connected": node.is_connected,
                    "is_disabled": node.is_disabled,
                }
                for node in nodes
            ],
        }
    )


def support_admin_api_node_traffic(request):
    """Отчет по потреблению трафика подписками на ноде/всех нодах (только admin).

    GET-параметры: node ("all" или uuid ноды), hours (период от текущего
    момента назад, по умолчанию 24, максимум 2160 = 90 дней), top (сколько
    строк вернуть, по умолчанию 50), min_gib (порог трафика в GiB).
    """
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    node_param = request.GET.get("node", "all")
    try:
        hours = float(request.GET.get("hours", "24"))
        top = int(request.GET.get("top", "50"))
        min_gib = float(request.GET.get("min_gib", "0"))
    except ValueError:
        return JsonResponse(
            {"status": "error", "message": "Неверный формат параметров"}, status=400
        )

    if not 0 < hours <= 90 * 24:
        return JsonResponse(
            {"status": "error", "message": "Период должен быть от 0 до 90 дней"},
            status=400,
        )

    all_nodes = node_traffic.list_nodes(rwms_client)
    if all_nodes is None:
        return JsonResponse(
            {"status": "error", "message": "rwms недоступен, список нод не получен"},
            status=502,
        )

    if node_param == "all":
        nodes = all_nodes
    else:
        nodes = [node for node in all_nodes if node.uuid == node_param]
        if not nodes:
            return JsonResponse(
                {"status": "error", "message": "Нода не найдена"}, status=404
            )

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)

    report = node_traffic.build_report(
        rwms_client,
        nodes,
        start,
        end,
        top=top,
        min_gib=min_gib,
    )
    return JsonResponse({"status": "ok", "result": report})


def admin_censor_run_payload(run, with_details=False):
    payload = {
        "id": run.id,
        "msm_id": run.msm_id,
        "status": run.status,
        "error_message": run.error_message,
        "total": run.total_probes,
        "scheduled": run.scheduled_probes,
        "ok": run.ok_probes,
        "blocked": run.blocked_probes,
        "blocked_by_provider": run.blocked_asns or {},
        "created_at": admin_date_label(run.created_at),
        "completed_at": (
            admin_date_label(run.completed_at) if run.completed_at else None
        ),
    }
    if with_details:
        payload["probes"] = run.results or []
    return payload


def admin_mask_api_key(value):
    if not value:
        return ""
    return f"…{value[-6:]}" if len(value) > 6 else "…"


def admin_ripe_key_payload(key):
    return {
        "id": key.id,
        "name": key.name,
        "api_key_masked": admin_mask_api_key(key.api_key),
        "is_default": key.is_default,
        "public_measurements": key.public_measurements,
    }


def admin_censor_check_payload(check, last_run=None, keys_by_id=None):
    key_name = None
    if check.api_key_id and keys_by_id is not None:
        key = keys_by_id.get(check.api_key_id)
        key_name = key.name if key else "удалённый ключ"
    return {
        "id": check.id,
        "name": check.name,
        "target_ip": check.target_ip,
        "sni": check.sni,
        "port": check.port,
        "interval_minutes": check.interval_minutes,
        "is_enabled": check.is_enabled,
        "geo_mode": check.geo_mode,
        "light_mode": check.light_mode,
        "alerts_enabled": check.alerts_enabled,
        "api_key_id": check.api_key_id,
        "api_key_name": key_name,
        "last_run": admin_censor_run_payload(last_run) if last_run else None,
    }


def support_admin_api_ripe_keys(request):
    """Управление ключами RIPE Atlas (только admin).

    GET — список ключей (сам ключ маскируется). POST action=save|delete|set_default.
    Один ключ может быть «по умолчанию» — он используется проверками без
    явно выбранного ключа.
    """
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "POST":
            action = request.POST.get("action", "save")

            if action == "save":
                key_id = request.POST.get("key_id")
                name = request.POST.get("name", "").strip()
                api_key = request.POST.get("api_key", "").strip()
                make_default = request.POST.get("is_default") == "1"
                public_measurements = request.POST.get("public_measurements") == "1"

                if key_id:
                    key = db_session.get(RipeApiKey, int(key_id))
                    if not key:
                        return JsonResponse({"status": "not_found"}, status=404)
                    if not name:
                        return JsonResponse(
                            {"status": "error", "message": "Укажите название"},
                            status=400,
                        )
                    key.name = name[:160]
                    # Пустое поле ключа при редактировании — не менять значение
                    if api_key:
                        key.api_key = api_key[:128]
                    key.public_measurements = public_measurements
                else:
                    if not name or not api_key:
                        return JsonResponse(
                            {
                                "status": "error",
                                "message": "Заполните название и ключ",
                            },
                            status=400,
                        )
                    key = RipeApiKey(
                        name=name[:160],
                        api_key=api_key[:128],
                        public_measurements=public_measurements,
                    )
                    db_session.add(key)
                    db_session.flush()
                    # Первый добавленный ключ автоматически становится дефолтным
                    if db_session.query(RipeApiKey).count() == 1:
                        make_default = True

                if make_default:
                    db_session.query(RipeApiKey).filter(
                        RipeApiKey.id != key.id
                    ).update({RipeApiKey.is_default: False})
                    key.is_default = True
                key.updated_at = datetime.utcnow()
                db_session.commit()
                return JsonResponse({"status": "ok"})

            key = db_session.get(RipeApiKey, int(request.POST.get("key_id") or 0))
            if not key:
                return JsonResponse({"status": "not_found"}, status=404)

            if action == "delete":
                db_session.delete(key)
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action == "set_default":
                db_session.query(RipeApiKey).filter(
                    RipeApiKey.id != key.id
                ).update({RipeApiKey.is_default: False})
                key.is_default = True
                key.updated_at = datetime.utcnow()
                db_session.commit()
                return JsonResponse({"status": "ok"})

            return JsonResponse(
                {"status": "error", "message": "Неизвестное действие"}, status=400
            )

        if request.method != "GET":
            return JsonResponse({"status": "error"}, status=405)

        keys = db_session.query(RipeApiKey).order_by(RipeApiKey.id.asc()).all()
        return JsonResponse(
            {
                "status": "ok",
                "keys": [admin_ripe_key_payload(key) for key in keys],
            }
        )
    finally:
        db_session.close()


def support_admin_api_censor_checks(request):
    """Вкладка «Замеры ТСПУ»: список проверок и действия над ними (только admin).

    GET — список проверок с последним прогоном. Заодно лениво закрывает
    pending-прогоны и запускает просроченные по расписанию проверки, чтобы
    расписание работало даже без крона (пока админку кто-то открывает);
    основной путь для расписания — management-команда censor_checks по крону.

    POST action=save|bulk_update|delete|toggle|run.
    """
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    fallback_key = settings.RIPE_ATLAS_API_KEY

    db_session = session_factory()
    try:
        if request.method == "POST":
            action = request.POST.get("action", "save")

            if action == "bulk_update":
                updates = {}
                updated_fields = []

                if request.POST.get("apply_mode") == "1":
                    mode = request.POST.get("mode", "")
                    if mode not in {"op-full", "op-light", "geo-full", "geo-light"}:
                        return JsonResponse(
                            {"status": "error", "message": "Неверный режим замера"},
                            status=400,
                        )
                    updates[CensorCheck.geo_mode] = mode.startswith("geo")
                    updates[CensorCheck.light_mode] = mode.endswith("light")
                    updated_fields.append("mode")

                if request.POST.get("apply_api_key") == "1":
                    api_key_raw = request.POST.get("api_key_id", "").strip()
                    if api_key_raw:
                        try:
                            api_key_id = int(api_key_raw)
                        except ValueError:
                            return JsonResponse(
                                {"status": "error", "message": "Неверный ключ"},
                                status=400,
                            )
                        if not db_session.get(RipeApiKey, api_key_id):
                            return JsonResponse(
                                {"status": "error", "message": "Ключ не найден"},
                                status=400,
                            )
                    else:
                        default_key = (
                            db_session.query(RipeApiKey)
                            .filter(RipeApiKey.is_default.is_(True))
                            .first()
                        )
                        if not default_key and not fallback_key:
                            return JsonResponse(
                                {
                                    "status": "error",
                                    "message": "Ключ по умолчанию не настроен",
                                },
                                status=400,
                            )
                        api_key_id = None
                    updates[CensorCheck.api_key_id] = api_key_id
                    updated_fields.append("api_key_id")

                if request.POST.get("apply_interval") == "1":
                    interval_raw = request.POST.get("interval_minutes", "").strip()
                    if interval_raw:
                        try:
                            interval_minutes = int(interval_raw)
                        except ValueError:
                            return JsonResponse(
                                {"status": "error", "message": "Неверный интервал"},
                                status=400,
                            )
                        if interval_minutes < 60:
                            return JsonResponse(
                                {
                                    "status": "error",
                                    "message": "Минимальный интервал — 60 минут",
                                },
                                status=400,
                            )
                    else:
                        interval_minutes = None
                    updates[CensorCheck.interval_minutes] = interval_minutes
                    updated_fields.append("interval_minutes")

                for flag_name, value_name, model_field, field_label in (
                    (
                        "apply_alerts",
                        "alerts_enabled",
                        CensorCheck.alerts_enabled,
                        "alerts_enabled",
                    ),
                    (
                        "apply_enabled",
                        "is_enabled",
                        CensorCheck.is_enabled,
                        "is_enabled",
                    ),
                ):
                    if request.POST.get(flag_name) != "1":
                        continue
                    raw_value = request.POST.get(value_name, "")
                    if raw_value not in {"0", "1"}:
                        return JsonResponse(
                            {
                                "status": "error",
                                "message": "Неверное логическое значение",
                            },
                            status=400,
                        )
                    updates[model_field] = raw_value == "1"
                    updated_fields.append(field_label)

                if not updated_fields:
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": "Выберите хотя бы один параметр",
                        },
                        status=400,
                    )

                updates[CensorCheck.updated_at] = datetime.utcnow()
                updated_count = db_session.query(CensorCheck).update(
                    updates, synchronize_session=False
                )
                db_session.commit()
                logging.info(
                    "Admin bulk-updated %s censor checks fields=%s",
                    updated_count,
                    ",".join(updated_fields),
                )
                return JsonResponse(
                    {
                        "status": "ok",
                        "updated": updated_count,
                        "fields": updated_fields,
                    }
                )

            if action == "save":
                check_id = request.POST.get("check_id")
                target_ip = request.POST.get("target_ip", "").strip()
                sni = request.POST.get("sni", "").strip()
                # Отдельное имя в форме не спрашиваем — проверку идентифицирует SNI
                name = request.POST.get("name", "").strip() or sni
                api_key_raw = request.POST.get("api_key_id", "").strip()
                if api_key_raw:
                    try:
                        api_key_id = int(api_key_raw)
                    except ValueError:
                        return JsonResponse(
                            {"status": "error", "message": "Неверный ключ"},
                            status=400,
                        )
                    if not db_session.get(RipeApiKey, api_key_id):
                        return JsonResponse(
                            {"status": "error", "message": "Ключ не найден"},
                            status=400,
                        )
                else:
                    api_key_id = None
                try:
                    port = int(request.POST.get("port") or 443)
                except ValueError:
                    port = 443
                interval_raw = request.POST.get("interval_minutes", "").strip()
                if interval_raw:
                    try:
                        interval_minutes = int(interval_raw)
                    except ValueError:
                        return JsonResponse(
                            {"status": "error", "message": "Неверный интервал"},
                            status=400,
                        )
                    # Прогон стоит ~660 кредитов Atlas: не даём случайно выставить
                    # интервал, который сожжёт дневной доход зонда за пару часов.
                    if interval_minutes < 60:
                        return JsonResponse(
                            {
                                "status": "error",
                                "message": "Минимальный интервал — 60 минут",
                            },
                            status=400,
                        )
                else:
                    interval_minutes = None

                if not target_ip or not sni:
                    return JsonResponse(
                        {"status": "error", "message": "Заполните IP и SNI"},
                        status=400,
                    )
                if not 0 < port < 65536:
                    return JsonResponse(
                        {"status": "error", "message": "Неверный порт"}, status=400
                    )

                if check_id:
                    check = db_session.get(CensorCheck, int(check_id))
                    if not check:
                        return JsonResponse({"status": "not_found"}, status=404)
                else:
                    check = CensorCheck()
                    db_session.add(check)

                check.name = name[:160]
                check.target_ip = target_ip[:64]
                check.sni = sni[:256]
                check.port = port
                check.interval_minutes = interval_minutes
                # Режим приходит одной опцией: op-full | op-light | geo-full | geo-light
                mode = request.POST.get("mode", "op-full")
                check.geo_mode = mode.startswith("geo")
                check.light_mode = mode.endswith("light")
                check.alerts_enabled = request.POST.get("alerts_enabled") == "1"
                check.api_key_id = api_key_id
                check.updated_at = datetime.utcnow()
                db_session.commit()
                return JsonResponse({"status": "ok"})

            check = db_session.get(
                CensorCheck, int(request.POST.get("check_id") or 0)
            )
            if not check:
                return JsonResponse({"status": "not_found"}, status=404)

            if action == "delete":
                db_session.query(CensorCheckRun).filter(
                    CensorCheckRun.check_id == check.id
                ).delete()
                db_session.delete(check)
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action == "toggle":
                check.is_enabled = not check.is_enabled
                check.updated_at = datetime.utcnow()
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action == "run":
                api_key = ripe_atlas.resolve_api_key(
                    db_session, check, fallback_key
                )
                if not api_key:
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": "Нет ключа RIPE Atlas — добавьте ключ и назначьте его по умолчанию",
                        },
                        status=400,
                    )
                is_public = ripe_atlas.resolve_public_flag(db_session, check)
                # Ручной запуск — явное действие админа «покажи, как сейчас».
                # Сбрасываем запомненное состояние, чтобы результат оценился с
                # нуля и алерт пришёл, даже если нода уже числилась
                # заблокированной (иначе залипшее состояние глушит уведомление).
                check.last_alert_state = None
                db_session.commit()
                run = ripe_atlas.start_run(
                    db_session,
                    check,
                    api_key,
                    is_public,
                    bool(check.geo_mode),
                    bool(check.light_mode),
                )
                status_code = 200 if run.status != ripe_atlas.RUN_STATUS_ERROR else 502
                return JsonResponse(
                    {"status": "ok", "run": admin_censor_run_payload(run)},
                    status=status_code,
                )

            return JsonResponse(
                {"status": "error", "message": "Неизвестное действие"}, status=400
            )

        if request.method != "GET":
            return JsonResponse({"status": "error"}, status=405)

        # Ленивое обслуживание: закрываем pending-прогоны и запускаем
        # просроченные проверки. Ошибки сети Atlas не должны ронять список.
        try:
            ripe_atlas.finalize_pending_runs(db_session, fallback_key)
            ripe_atlas.schedule_due_checks(db_session, fallback_key)
        except Exception:
            logging.exception("censor checks: lazy maintenance failed")
            db_session.rollback()

        keys = db_session.query(RipeApiKey).order_by(RipeApiKey.id.asc()).all()
        keys_by_id = {key.id: key for key in keys}
        checks = (
            db_session.query(CensorCheck).order_by(CensorCheck.id.asc()).all()
        )
        last_runs = {}
        for check in checks:
            last_runs[check.id] = (
                db_session.query(CensorCheckRun)
                .filter(CensorCheckRun.check_id == check.id)
                .order_by(CensorCheckRun.id.desc())
                .first()
            )
        has_default = any(key.is_default for key in keys)
        return JsonResponse(
            {
                "status": "ok",
                "keys": [admin_ripe_key_payload(key) for key in keys],
                "has_default_key": has_default or bool(fallback_key),
                "env_key_present": bool(fallback_key),
                "checks": [
                    admin_censor_check_payload(
                        check, last_runs.get(check.id), keys_by_id
                    )
                    for check in checks
                ],
            }
        )
    finally:
        db_session.close()


def support_admin_api_censor_check_runs(request):
    """История и детали прогонов замеров (только admin).

    GET ?check_id= — последние прогоны проверки;
    GET ?run_id= — детальный результат прогона (по-зондовая разбивка);
    pending-прогон при запросе деталей пытается дозабрать результаты.
    """
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response
    if request.method != "GET":
        return JsonResponse({"status": "error"}, status=405)

    db_session = session_factory()
    try:
        run_id = request.GET.get("run_id")
        if run_id:
            run = db_session.get(CensorCheckRun, int(run_id))
            if not run:
                return JsonResponse({"status": "not_found"}, status=404)
            check = db_session.get(CensorCheck, run.check_id)
            if run.status == ripe_atlas.RUN_STATUS_PENDING:
                try:
                    api_key = ripe_atlas.resolve_api_key(
                        db_session, check, settings.RIPE_ATLAS_API_KEY
                    )
                    if api_key:
                        ripe_atlas.finalize_run(db_session, run, api_key)
                except Exception:
                    logging.exception("censor checks: finalize on demand failed")
                    db_session.rollback()
            return JsonResponse(
                {
                    "status": "ok",
                    "run": admin_censor_run_payload(run, with_details=True),
                    "check": admin_censor_check_payload(check) if check else None,
                }
            )

        check_id = request.GET.get("check_id")
        if not check_id:
            return JsonResponse(
                {"status": "error", "message": "Нужен check_id или run_id"},
                status=400,
            )
        runs = (
            db_session.query(CensorCheckRun)
            .filter(CensorCheckRun.check_id == int(check_id))
            .order_by(CensorCheckRun.id.desc())
            .limit(30)
            .all()
        )
        return JsonResponse(
            {
                "status": "ok",
                "runs": [admin_censor_run_payload(run) for run in runs],
            }
        )
    except ValueError:
        return JsonResponse(
            {"status": "error", "message": "Неверный формат параметров"}, status=400
        )
    finally:
        db_session.close()


# ---------------------------------------------------------------------------
# Аналитика привлечения (вкладка «Привлечение» в админ-дашборде)
# ---------------------------------------------------------------------------
# Все платежи (Wata + исторические YooKassa) объединяются в единый поток
# (user_id, paid_at UTC-naive, amount RUB); дни бакетируются по МСК, чтобы
# графики совпадали с «московскими» сутками, которыми оперирует бизнес.

from sqlalchemy import text as sa_text  # noqa: E402

ACQ_PAYS_CTE = """
    pays AS (
        SELECT wi.user_id AS user_id,
               (t.payment_time AT TIME ZONE 'UTC') AS paid_at,
               t.amount::numeric AS amount
        FROM wata_transactions t
        JOIN wata_invoices wi ON wi.order_id = t.order_id
        WHERE t.transaction_status = 'Paid'
        UNION ALL
        SELECT p.user_id, p.created_at, p.amount::numeric
        FROM yk_payments p
        WHERE p.status = 'succeeded'
    ),
    first_pay AS (
        SELECT user_id, min(paid_at) AS first_at
        FROM pays GROUP BY user_id
    )
"""

ACQ_MSK_DAY = "((paid_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date"

ACQ_SELLING_TYPES = (
    "winback-1", "winback-2", "winback-3", "winback-4",
    "subscription-expired", "1-day-left", "3-days-left",
)

# Last-touch атрибуция оплат к продающим пушам: окна разных пушей серии
# (3-days-left → 1-day-left → expired → winback-N) перекрываются, поэтому
# каждая оплата привязывается только к ПОСЛЕДНЕМУ продающему пушу перед ней
# (DISTINCT ON по оплате, ORDER BY ts DESC), а каждый пуш засчитывается
# сконвертившим не более одного раза (первая атрибутированная оплата).
ACQ_PUSH_ATTRIBUTION_CTE = """
    ev AS (
        SELECT id, user_id, timestamp AS ts,
               event_payload->>'notification_type' AS ntype
        FROM event_logs
        WHERE event_type = 'notification_sent'
          AND event_payload->>'notification_type' = ANY(:selling)
          AND timestamp >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
    ),
    attributed AS (
        SELECT DISTINCT ON (p.user_id, p.paid_at)
               ev.id AS event_id, ev.ntype, ev.ts,
               p.user_id, p.paid_at, p.amount
        FROM ev
        JOIN pays p ON p.user_id = ev.user_id
         AND p.paid_at >= ev.ts
         AND p.paid_at <= ev.ts + interval '72 hours'
        ORDER BY p.user_id, p.paid_at, ev.ts DESC
    ),
    conv_pay AS (
        SELECT DISTINCT ON (a.event_id) a.*
        FROM attributed a
        ORDER BY a.event_id, a.paid_at
    )
"""


def _acq_rows(db_session, sql, **params):
    return db_session.execute(sa_text(sql), params).mappings().all()


def _acq_new_repeat(db_session, days, start=None, end=None):
    if start and end:
        where = f"{ACQ_MSK_DAY} BETWEEN :start AND :end"
        params = {"start": start, "end": end}
    else:
        where = "paid_at >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)"
        params = {"days": days}
    rows = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE}
        SELECT {ACQ_MSK_DAY} AS day,
               count(*) FILTER (WHERE paid_at = first_at) AS new_payers,
               COALESCE(sum(amount) FILTER (WHERE paid_at = first_at), 0) AS new_rub,
               count(*) FILTER (WHERE paid_at <> first_at) AS repeat_payers,
               COALESCE(sum(amount) FILTER (WHERE paid_at <> first_at), 0) AS repeat_rub
        FROM pays JOIN first_pay USING (user_id)
        WHERE {where}
        GROUP BY 1 ORDER BY 1
        """,
        **params,
    )
    return {
        "days": [
            {
                "day": r["day"].isoformat(),
                "new_payers": r["new_payers"],
                "new_rub": float(r["new_rub"]),
                "repeat_payers": r["repeat_payers"],
                "repeat_rub": float(r["repeat_rub"]),
            }
            for r in rows
        ]
    }


def _acq_renew45(db_session, months):
    """Отвал/удержание базы коротких тарифов по месяцам.

    Когорта месяца — пользователи, оплатившие в нём короткий тариф
    (день/3 дня/неделя/месяц). Продлившим считается тот, у кого в течение
    45 дней после его последней оплаты в месяце есть любая следующая оплата
    (включая апгрейд на длинный тариф). Длинные тарифы (3/6/12 мес) в когорту
    не входят: их окно продления заведомо длиннее 45 дней. Когорты, у которых
    45-дневное окно ещё не закрыто, помечаются mature=False — их процент
    занижен и на графике не показывается.
    """
    rows = _acq_rows(
        db_session,
        """
        WITH pays AS (
            SELECT wi.user_id AS user_id,
                   (t.payment_time AT TIME ZONE 'UTC') AS paid_at,
                   COALESCE(wi.tariff_id, '') AS tariff
            FROM wata_transactions t
            JOIN wata_invoices wi ON wi.order_id = t.order_id
            WHERE t.transaction_status = 'Paid'
            UNION ALL
            SELECT p.user_id, p.created_at, p.subscription_period
            FROM yk_payments p
            WHERE p.status = 'succeeded'
        ),
        cohort AS (
            SELECT date_trunc('month', (paid_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS m,
                   user_id,
                   max(paid_at) AS last_in_month
            FROM pays
            WHERE tariff IN ('oneday', 'threedays', 'oneweek', 'month')
            GROUP BY 1, 2
        ),
        renew AS (
            SELECT c.m, c.user_id,
                   bool_or(p.paid_at <= c.last_in_month + interval '45 days') AS renewed
            FROM cohort c
            LEFT JOIN pays p ON p.user_id = c.user_id AND p.paid_at > c.last_in_month
            GROUP BY 1, 2
        )
        SELECT m AS month, count(*) AS payers,
               count(*) FILTER (WHERE renewed) AS renewed
        FROM renew
        WHERE m >= date_trunc('month', (now() AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date
                   - make_interval(months => :months)
        GROUP BY 1 ORDER BY 1
        """,
        months=months,
    )
    result = []
    for r in rows:
        m = r["month"]
        next_month = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        payers = r["payers"]
        renewed = r["renewed"]
        result.append(
            {
                "month": m.isoformat(),
                "payers": payers,
                "renewed": renewed,
                "renew_pct": round(100.0 * renewed / payers, 1) if payers else 0.0,
                "mature": date.today() >= next_month + timedelta(days=45),
            }
        )
    return {"months": result}


def _acq_funnel(db_session, weeks):
    events = _acq_rows(
        db_session,
        """
        SELECT date_trunc('week', (timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS week,
               count(*) FILTER (WHERE event_type = 'subscription_created') AS trials,
               count(*) FILTER (WHERE event_type LIKE 'create_invoice%') AS invoice_clicks,
               count(DISTINCT user_id) FILTER (
                   WHERE event_type = 'traffic_threshold_reached'
                     AND event_payload->>'threshold' = '0') AS connected,
               count(DISTINCT user_id) FILTER (
                   WHERE event_type = 'traffic_threshold_reached'
                     AND event_payload->>'threshold' = '5') AS mb5,
               count(DISTINCT user_id) FILTER (
                   WHERE event_type = 'traffic_threshold_reached'
                     AND event_payload->>'threshold' = '100') AS mb100
        FROM event_logs
        WHERE timestamp >= now() AT TIME ZONE 'UTC' - make_interval(weeks => :weeks)
          AND (event_type = 'subscription_created'
               OR event_type LIKE 'create_invoice%'
               OR event_type = 'traffic_threshold_reached')
        GROUP BY 1 ORDER BY 1
        """,
        weeks=weeks,
    )
    pays = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE}
        SELECT date_trunc('week', (paid_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS week,
               count(*) AS payments,
               count(DISTINCT user_id) AS payers,
               count(*) FILTER (WHERE paid_at = first_at) AS new_payers,
               COALESCE(sum(amount) FILTER (WHERE paid_at = first_at), 0) AS new_rub
        FROM pays JOIN first_pay USING (user_id)
        WHERE paid_at >= now() AT TIME ZONE 'UTC' - make_interval(weeks => :weeks)
        GROUP BY 1 ORDER BY 1
        """,
        weeks=weeks,
    )
    merged = {}
    for r in events:
        merged.setdefault(r["week"], {}).update(
            trials=r["trials"],
            invoice_clicks=r["invoice_clicks"],
            connected=r["connected"],
            mb5=r["mb5"],
            mb100=r["mb100"],
        )
    for r in pays:
        merged.setdefault(r["week"], {}).update(
            payments=r["payments"], payers=r["payers"],
            new_payers=r["new_payers"], new_rub=float(r["new_rub"]),
        )
    return {
        "weeks": [
            {"week": wk.isoformat(),
             "trials": v.get("trials", 0),
             "connected": v.get("connected", 0),
             "mb5": v.get("mb5", 0),
             "mb100": v.get("mb100", 0),
             "invoice_clicks": v.get("invoice_clicks", 0),
             "payments": v.get("payments", 0),
             "payers": v.get("payers", 0),
             "new_payers": v.get("new_payers", 0),
             "new_rub": v.get("new_rub", 0.0)}
            for wk, v in sorted(merged.items())
        ]
    }


def _acq_ads(db_session, weeks):
    # Таблица ad_spends (и колонки impressions/clicks под CSV Директа)
    # появляются alembic-миграцией в common; до её наката отдаём флаг
    # needs_migration, чтобы вкладка объяснила, что делать.
    try:
        spends = _acq_rows(
            db_session,
            """
            SELECT id, day, channel, account, amount_rub, impressions, clicks, comment
            FROM ad_spends
            WHERE day >= (now() AT TIME ZONE 'Europe/Moscow')::date - make_interval(weeks => :weeks)
            ORDER BY day DESC, channel, account
            """,
            weeks=weeks,
        )
        accounts = [
            r["account"]
            for r in _acq_rows(
                db_session,
                "SELECT DISTINCT account FROM ad_spends ORDER BY account",
            )
        ]
    except Exception:
        db_session.rollback()
        return {"needs_migration": True, "weeks": [], "spends": [], "accounts": []}

    weekly_spend = {}
    for r in spends:
        wk = (r["day"] - timedelta(days=r["day"].weekday())).isoformat()
        weekly_spend[wk] = weekly_spend.get(wk, 0.0) + float(r["amount_rub"])

    pays = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE}
        SELECT date_trunc('week', (paid_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS week,
               count(*) FILTER (WHERE paid_at = first_at) AS new_payers,
               COALESCE(sum(amount) FILTER (WHERE paid_at = first_at), 0) AS new_rub
        FROM pays JOIN first_pay USING (user_id)
        WHERE paid_at >= now() AT TIME ZONE 'UTC' - make_interval(weeks => :weeks)
        GROUP BY 1 ORDER BY 1
        """,
        weeks=weeks,
    )
    subs_rows = _acq_rows(
        db_session,
        """
        SELECT date_trunc('week', (timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS week,
               count(*) AS subs
        FROM event_logs
        WHERE event_type = 'subscription_created'
          AND timestamp >= now() AT TIME ZONE 'UTC' - make_interval(weeks => :weeks)
        GROUP BY 1
        """,
        weeks=weeks,
    )
    weekly_subs = {r["week"].isoformat(): r["subs"] for r in subs_rows}
    # Подключение = первый трафик по подписке (traffic_threshold_reached
    # с threshold=0) — та же метрика, что в разделе «Аналитика».
    conn_rows = _acq_rows(
        db_session,
        """
        SELECT date_trunc('week', (timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS week,
               count(DISTINCT user_id) AS conns
        FROM event_logs
        WHERE event_type = 'traffic_threshold_reached'
          AND (event_payload->>'threshold')::int = 0
          AND timestamp >= now() AT TIME ZONE 'UTC' - make_interval(weeks => :weeks)
        GROUP BY 1
        """,
        weeks=weeks,
    )
    weekly_conns = {r["week"].isoformat(): r["conns"] for r in conn_rows}
    out = []
    for r in pays:
        wk = r["week"].isoformat()
        spend = weekly_spend.get(wk, 0.0)
        new_payers = r["new_payers"]
        new_rub = float(r["new_rub"])
        subs = weekly_subs.get(wk, 0)
        conns = weekly_conns.get(wk, 0)
        out.append({
            "week": wk,
            "spend": round(spend, 2),
            "subs": subs,
            "cost_per_sub": round(spend / subs, 2) if spend and subs else None,
            "conns": conns,
            "cost_per_conn": round(spend / conns, 2) if spend and conns else None,
            "new_payers": new_payers,
            "new_rub": round(new_rub, 2),
            "cost_per_sale": round(spend / new_payers, 2)
            if spend and new_payers else None,
            "romi": round(new_rub / spend, 2) if spend else None,
            "drr": round(100.0 * spend / new_rub, 1) if spend and new_rub else None,
        })
    return {
        "needs_migration": False,
        "weeks": out,
        "accounts": accounts,
        "spends": [
            {"id": r["id"], "day": r["day"].isoformat(), "channel": r["channel"],
             "account": r["account"],
             "amount_rub": float(r["amount_rub"]),
             "impressions": r["impressions"], "clicks": r["clicks"],
             "comment": r["comment"] or ""}
            for r in spends
        ],
    }


def _acq_ads_daily(db_session, days=92, group="day"):
    """Ежедневная (или помесячная) экономика рекламы.

    Для каждого дня (МСК): траты/показы/клики из ad_spends, созданные
    подписки, подключения (первый трафик, threshold=0) и продажи (новые
    покупатели — первая оплата пользователя). Производные метрики (CPC,
    цена подписки/подключения/продажи) считаются после агрегации, чтобы
    помесячная группировка делила суммы, а не усредняла дневные ratio.
    """
    try:
        spend_rows = _acq_rows(
            db_session,
            """
            SELECT day, COALESCE(sum(amount_rub), 0) AS spend,
                   sum(impressions) AS impressions, sum(clicks) AS clicks
            FROM ad_spends
            WHERE day >= (now() AT TIME ZONE 'Europe/Moscow')::date - make_interval(days => :days)
            GROUP BY 1
            """,
            days=days,
        )
    except Exception:
        db_session.rollback()
        return {"needs_migration": True, "rows": []}

    subs_rows = _acq_rows(
        db_session,
        """
        SELECT ((timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS day,
               count(*) AS subs
        FROM event_logs
        WHERE event_type = 'subscription_created'
          AND timestamp >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        GROUP BY 1
        """,
        days=days,
    )
    conn_rows = _acq_rows(
        db_session,
        """
        SELECT ((timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS day,
               count(DISTINCT user_id) AS conns
        FROM event_logs
        WHERE event_type = 'traffic_threshold_reached'
          AND (event_payload->>'threshold')::int = 0
          AND timestamp >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        GROUP BY 1
        """,
        days=days,
    )
    sale_rows = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE}
        SELECT ((first_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS day,
               count(*) AS sales
        FROM first_pay
        WHERE first_at >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        GROUP BY 1
        """,
        days=days,
    )

    merged = {}

    def bucket(day):
        iso = day.isoformat()
        key = iso[:7] if group == "month" else iso
        return merged.setdefault(key, {
            "spend": 0.0, "impressions": 0, "clicks": 0,
            "has_traffic": False, "subs": 0, "conns": 0, "sales": 0,
        })

    for r in spend_rows:
        b = bucket(r["day"])
        b["spend"] += float(r["spend"])
        if r["impressions"] is not None or r["clicks"] is not None:
            b["has_traffic"] = True
            b["impressions"] += int(r["impressions"] or 0)
            b["clicks"] += int(r["clicks"] or 0)
    for r in subs_rows:
        bucket(r["day"])["subs"] += int(r["subs"])
    for r in conn_rows:
        bucket(r["day"])["conns"] += int(r["conns"])
    for r in sale_rows:
        bucket(r["day"])["sales"] += int(r["sales"])

    def ratio(spend, count):
        return round(spend / count, 2) if spend and count else None

    rows = []
    for key in sorted(merged):
        b = merged[key]
        spend = round(b["spend"], 2)
        rows.append({
            "period": key,
            "spend": spend,
            "impressions": b["impressions"] if b["has_traffic"] else None,
            "clicks": b["clicks"] if b["has_traffic"] else None,
            "cpc": ratio(spend, b["clicks"]) if b["has_traffic"] else None,
            "subs": b["subs"],
            "cost_per_sub": ratio(spend, b["subs"]),
            "conns": b["conns"],
            "cost_per_conn": ratio(spend, b["conns"]),
            "sales": b["sales"],
            "cost_per_sale": ratio(spend, b["sales"]),
        })
    return {"needs_migration": False, "group": group, "rows": rows}


# --- Импорт CSV из рекламного кабинета Яндекс Директа ----------------------
# Формат выгрузки «по дням»: заголовок (День,Показы,Клики,"Расход, ₽",...),
# строка «Итого» и дневные строки с датой dd.mm.yyyy. Числа могут быть
# с запятой-десятичным разделителем, пробелами-разрядами и «-» вместо
# пустых значений.

def _parse_direct_number(raw):
    value = (raw or "").strip().strip('"').replace("\xa0", "").replace(" ", "")
    value = value.replace("₽", "").replace("%", "")
    if not value or value in ("-", "—"):
        return None
    if "," in value and "." in value:
        value = value.replace(",", "")
    else:
        value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def _parse_yandex_direct_csv(text):
    """Возвращает список {day, amount_rub, impressions, clicks}.

    Заголовок ищется по всему файлу (перед ним Директ может добавлять
    строки с названием отчёта); строки без даты в первой колонке
    («Итого», подвал) молча пропускаются.
    """
    import csv
    import io

    reader = csv.reader(io.StringIO(text))
    columns = None
    rows = []
    for row in reader:
        if not row or not any(cell.strip() for cell in row):
            continue
        if columns is None:
            lowered = [cell.strip().lstrip("﻿").lower() for cell in row]
            if (
                any(cell in ("день", "дата") for cell in lowered)
                and any(cell.startswith("расход") for cell in lowered)
            ):
                columns = {}
                for i, cell in enumerate(lowered):
                    if cell in ("день", "дата"):
                        columns["day"] = i
                    elif cell == "показы":
                        columns["impressions"] = i
                    elif cell == "клики":
                        columns["clicks"] = i
                    elif cell.startswith("расход"):
                        columns["spend"] = i
            continue

        def cell(name):
            i = columns.get(name)
            return row[i] if i is not None and i < len(row) else ""

        try:
            day = datetime.strptime(cell("day").strip(), "%d.%m.%Y").date()
        except ValueError:
            continue
        spend = _parse_direct_number(cell("spend"))
        if spend is None:
            continue
        impressions = _parse_direct_number(cell("impressions"))
        clicks = _parse_direct_number(cell("clicks"))
        rows.append({
            "day": day,
            "amount_rub": round(spend, 2),
            "impressions": int(impressions) if impressions is not None else None,
            "clicks": int(clicks) if clicks is not None else None,
        })
    return rows


def _acq_cohorts(db_session, months):
    ltv = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE}
        SELECT to_char(date_trunc('month', (f.first_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow'), 'YYYY-MM') AS cohort,
               (EXTRACT(YEAR FROM p.paid_at)::int * 12 + EXTRACT(MONTH FROM p.paid_at)::int)
             - (EXTRACT(YEAR FROM f.first_at)::int * 12 + EXTRACT(MONTH FROM f.first_at)::int) AS offset_m,
               sum(p.amount) AS rub
        FROM pays p JOIN first_pay f USING (user_id)
        WHERE f.first_at >= now() AT TIME ZONE 'UTC' - make_interval(months => :months)
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        months=months,
    )
    retention = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE},
        ranked AS (
            SELECT user_id, paid_at,
                   row_number() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn
            FROM pays
        ),
        seconds AS (SELECT user_id, paid_at AS second_at FROM ranked WHERE rn = 2)
        SELECT to_char(date_trunc('month', (f.first_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow'), 'YYYY-MM') AS cohort,
               count(*) AS size,
               count(*) FILTER (WHERE s.second_at <= f.first_at + interval '30 days') AS r30,
               count(*) FILTER (WHERE s.second_at <= f.first_at + interval '60 days') AS r60,
               count(*) FILTER (WHERE s.second_at <= f.first_at + interval '90 days') AS r90,
               count(*) FILTER (WHERE s.second_at <= f.first_at + interval '180 days') AS r180,
               count(*) FILTER (WHERE s.second_at <= f.first_at + interval '365 days') AS r365
        FROM first_pay f
        LEFT JOIN seconds s USING (user_id)
        WHERE f.first_at >= now() AT TIME ZONE 'UTC' - make_interval(months => :months)
        GROUP BY 1 ORDER BY 1
        """,
        months=months,
    )
    sizes = {r["cohort"]: r["size"] for r in retention}
    matrix = {}
    for r in ltv:
        matrix.setdefault(r["cohort"], {})[int(r["offset_m"])] = float(r["rub"])
    cohorts = []
    for cohort in sorted(matrix):
        size = sizes.get(cohort, 0)
        cumulative, acc = [], 0.0
        for off in range(0, 7):
            acc += matrix[cohort].get(off, 0.0)
            cumulative.append(round(acc / size, 1) if size else 0.0)
        cohorts.append({"cohort": cohort, "size": size, "ltv_per_user": cumulative})
    # Зрелость окна: горизонт "дожит", если у ПОСЛЕДНЕГО первого платежа
    # когорты (конец месяца) уже прошло h дней. Иначе процент занижен просто
    # потому, что время ещё не вышло, — фронт помечает такие значения.
    today_msk = (datetime.utcnow() + timedelta(hours=3)).date()

    def _matured(cohort_ym, horizon_days):
        year, month = int(cohort_ym[:4]), int(cohort_ym[5:7])
        if month == 12:
            month_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)
        return month_end + timedelta(days=horizon_days) <= today_msk

    retention_out = []
    for r in retention:
        row = {"cohort": r["cohort"], "size": r["size"]}
        for horizon in (30, 60, 90, 180, 365):
            key = f"r{horizon}"
            row[key] = round(100.0 * r[key] / r["size"], 1) if r["size"] else 0
            row[f"{key}_matured"] = _matured(r["cohort"], horizon)
        retention_out.append(row)

    return {"ltv": cohorts, "retention": retention_out}


# Допустимые окна конверсии для графика «подписки → первая оплата».
# Окно шире 10 дней делает правый край графика «недозрелым» на всю свою
# длину, поэтому набор фиксированный, а не произвольное число из запроса.
ACQ_TRIALS_WINDOWS = (10, 30, 60)


def _acq_trials(db_session, days, window_days=10):
    if window_days not in ACQ_TRIALS_WINDOWS:
        window_days = 10
    rows = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE},
        ev AS (
            SELECT user_id, timestamp AS ts,
                   ((timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS day
            FROM event_logs
            WHERE event_type = 'subscription_created'
              AND timestamp >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        )
        SELECT ev.day, count(*) AS trials,
               count(*) FILTER (
                   WHERE f.first_at >= ev.ts
                     AND f.first_at <= ev.ts + make_interval(days => :window_days)
               ) AS converted
        FROM ev LEFT JOIN first_pay f USING (user_id)
        GROUP BY 1 ORDER BY 1
        """,
        days=days,
        window_days=window_days,
    )
    return {
        "window_days": window_days,
        "days": [
            {"day": r["day"].isoformat(), "trials": r["trials"],
             "converted": r["converted"],
             "conv_pct": round(100.0 * r["converted"] / r["trials"], 1) if r["trials"] else 0}
            for r in rows
        ]
    }


def _acq_pushes(db_session, days):
    selling = list(ACQ_SELLING_TYPES)
    daily = _acq_rows(
        db_session,
        """
        SELECT ((timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date AS day,
               count(DISTINCT (user_id, event_payload->>'notification_type'))
                   FILTER (WHERE event_payload->>'notification_type' = ANY(:selling)) AS selling,
               count(DISTINCT (user_id, event_payload->>'notification_type'))
                   FILTER (WHERE NOT (event_payload->>'notification_type' = ANY(:selling))) AS other
        FROM event_logs
        WHERE event_type = 'notification_sent'
          AND timestamp >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        GROUP BY 1 ORDER BY 1
        """,
        days=days, selling=selling,
    )
    revenue = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE}
        SELECT {ACQ_MSK_DAY} AS day, COALESCE(sum(amount), 0) AS rub
        FROM pays
        WHERE paid_at >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        GROUP BY 1 ORDER BY 1
        """,
        days=days,
    )
    winback = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE},
        {ACQ_PUSH_ATTRIBUTION_CTE},
        -- Пуш одному юзеру логируется каждым ботом (vpn/vps) отдельно, поэтому
        -- считаем уникальные (юзер, день), а не сырые события — иначе
        -- «Отправлено» задваивается и конверсия занижается.
        sent AS (
            SELECT ntype, count(DISTINCT (user_id, ts::date)) AS sent
            FROM ev GROUP BY 1
        ),
        conv AS (
            SELECT cp.ntype,
                   count(*) AS converted,
                   count(*) FILTER (WHERE cp.paid_at = fp.first_at) AS new_payers,
                   COALESCE(sum(cp.amount), 0) AS rub,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(epoch FROM cp.paid_at - cp.ts) / 3600.0
                   ) AS median_hours
            FROM conv_pay cp
            JOIN first_pay fp USING (user_id)
            GROUP BY 1
        )
        SELECT s.ntype, s.sent,
               COALESCE(c.converted, 0) AS paid_72h,
               COALESCE(c.new_payers, 0) AS new_payers,
               COALESCE(c.converted, 0) - COALESCE(c.new_payers, 0) AS repeat_payers,
               COALESCE(c.rub, 0) AS rub,
               c.median_hours
        FROM sent s LEFT JOIN conv c USING (ntype) ORDER BY 1
        """,
        days=days, selling=selling,
    )
    # Распределение чеков по атрибутированным оплатам — показывает,
    # какие тарифы реально покупают с каждого типа пуша.
    amounts = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE},
        {ACQ_PUSH_ATTRIBUTION_CTE}
        SELECT ntype, amount, count(*) AS cnt
        FROM conv_pay GROUP BY 1, 2 ORDER BY 1, cnt DESC, 2
        """,
        days=days, selling=selling,
    )
    amounts_by_type = {}
    for r in amounts:
        amounts_by_type.setdefault(r["ntype"], []).append(
            {"amount": float(r["amount"]), "count": r["cnt"]}
        )
    rev_by_day = {r["day"].isoformat(): float(r["rub"]) for r in revenue}
    return {
        "days": [
            {"day": r["day"].isoformat(), "selling": r["selling"], "other": r["other"],
             "rub": rev_by_day.get(r["day"].isoformat(), 0.0)}
            for r in daily
        ],
        "revenue_days": [
            {"day": r["day"].isoformat(), "rub": float(r["rub"])} for r in revenue
        ],
        "winback": [
            {"type": r["ntype"], "sent": r["sent"], "paid_72h": r["paid_72h"],
             "conv_pct": round(100.0 * r["paid_72h"] / r["sent"], 1) if r["sent"] else 0,
             "new_payers": r["new_payers"], "repeat_payers": r["repeat_payers"],
             "rub": float(r["rub"]),
             "median_hours": round(float(r["median_hours"]), 1)
             if r["median_hours"] is not None else None,
             "amounts": amounts_by_type.get(r["ntype"], [])}
            for r in winback
        ],
    }


def _acq_patterns(db_session, days=30):
    heatmap = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE}
        SELECT EXTRACT(ISODOW FROM (paid_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::int AS dow,
               EXTRACT(HOUR FROM (paid_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::int AS hr,
               count(*) AS payments, COALESCE(sum(amount), 0) AS rub
        FROM pays
        WHERE paid_at >= now() AT TIME ZONE 'UTC' - interval '60 days'
        GROUP BY 1, 2
        """,
    )
    hourly = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE}
        SELECT {ACQ_MSK_DAY} AS day,
               EXTRACT(HOUR FROM (paid_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::int AS hr,
               COALESCE(sum(amount), 0) AS rub
        FROM pays
        WHERE paid_at >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        days=days + 2,
    )
    today_msk = (datetime.utcnow() + timedelta(hours=3)).date()
    by_day = {}
    for r in hourly:
        by_day.setdefault(r["day"], [0.0] * 24)[r["hr"]] = float(r["rub"])
    def cumulative(vals):
        out, acc = [], 0.0
        for v in vals:
            acc += v
            out.append(round(acc))
        return out
    history = [cumulative(v) for d, v in by_day.items() if d != today_msk]
    p25, p50, p75 = [], [], []
    for hr in range(24):
        col = sorted(day[hr] for day in history) or [0]
        n = len(col)
        p25.append(col[max(0, int(n * 0.25) - 1)])
        p50.append(col[max(0, int(n * 0.50) - 1)])
        p75.append(col[max(0, int(n * 0.75) - 1)])
    return {
        "heatmap": [
            {"dow": r["dow"], "hr": r["hr"], "payments": r["payments"], "rub": float(r["rub"])}
            for r in heatmap
        ],
        "today": cumulative(by_day.get(today_msk, [0.0] * 24)),
        "current_hour_msk": (datetime.utcnow() + timedelta(hours=3)).hour,
        "typical": {"p25": p25, "p50": p50, "p75": p75},
    }


# Тарифный поток платежей: как ACQ_PAYS_CTE, но с тарифом каждого платежа
# (yk: subscription_period, wata: tariff_id инвойса) — для аналитики переходов
# между тарифами.
ACQ_PAYS_TARIFF_CTE = """
    pays_t AS (
        SELECT wi.user_id AS user_id,
               (t.payment_time AT TIME ZONE 'UTC') AS paid_at,
               t.amount::numeric AS amount,
               COALESCE(wi.tariff_id, '?') AS tariff
        FROM wata_transactions t
        JOIN wata_invoices wi ON wi.order_id = t.order_id
        WHERE t.transaction_status = 'Paid'
        UNION ALL
        SELECT p.user_id, p.created_at, p.amount::numeric,
               COALESCE(p.subscription_period, '?')
        FROM yk_payments p
        WHERE p.status = 'succeeded'
    )
"""

# Границы бакетов «на какой день после старта триала пришла первая оплата».
ACQ_TIMING_BUCKETS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 14, 21, 30, 60]
ACQ_TIMING_LABELS = [
    "0–1", "1–2", "2–3", "3–4", "4–5", "5–6", "6–7", "7–8",
    "8–10", "10–14", "14–21", "21–30", "30–60", "60+",
]


def _acq_trial_timing(db_session, days=365):
    """Распределение дня первой оплаты относительно старта триала.

    Когорта — пользователи, чья ПЕРВАЯ подписка (subscription_created) создана
    за последние :days. Показывает, на каком этапе триала конвертятся новые
    клиенты: сразу, в зоне пушей об окончании, в момент истечения или сильно
    позже (winback).
    """
    edges = ",".join(str(e) for e in ACQ_TIMING_BUCKETS)
    totals = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE},
        subs AS (
            SELECT user_id, min(timestamp) AS created_at
            FROM event_logs
            WHERE event_type = 'subscription_created'
            GROUP BY user_id
        )
        SELECT count(*) AS trials,
               count(*) FILTER (
                   WHERE f.first_at IS NOT NULL AND f.first_at >= s.created_at
               ) AS converted
        FROM subs s
        LEFT JOIN first_pay f USING (user_id)
        WHERE s.created_at >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        """,
        days=days,
    )[0]
    rows = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE},
        subs AS (
            SELECT user_id, min(timestamp) AS created_at
            FROM event_logs
            WHERE event_type = 'subscription_created'
            GROUP BY user_id
        )
        SELECT width_bucket(
                   EXTRACT(EPOCH FROM (f.first_at - s.created_at)) / 86400.0,
                   ARRAY[{edges}]
               ) AS bucket,
               count(*) AS users
        FROM subs s
        JOIN first_pay f USING (user_id)
        WHERE s.created_at >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
          AND f.first_at >= s.created_at
        GROUP BY 1
        """,
        days=days,
    )
    by_bucket = {int(r["bucket"]): int(r["users"]) for r in rows}
    converted = int(totals["converted"] or 0)
    trials = int(totals["trials"] or 0)
    buckets = []
    for idx, label in enumerate(ACQ_TIMING_LABELS, start=1):
        users = by_bucket.get(idx, 0)
        buckets.append({
            "label": label,
            "users": users,
            "pct": round(100.0 * users / converted, 1) if converted else 0,
        })
    return {
        "trials": trials,
        "converted": converted,
        "conversion_pct": round(100.0 * converted / trials, 1) if trials else 0,
        "buckets": buckets,
    }


def _acq_renewal_ladder(db_session, months=12):
    """Лестница продлений по когортам месяца привлечения.

    Когорта — месяц (МСК) первой подписки пользователя. Для каждой когорты:
    привлечено → оплатили ≥1 раз → ≥2 → ≥3 → ≥4 раз. Проценты шагов
    считаются на фронте/потребителе от предыдущей ступени.
    """
    rows = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE},
        subs AS (
            SELECT user_id, min(timestamp) AS created_at
            FROM event_logs
            WHERE event_type = 'subscription_created'
            GROUP BY user_id
        ),
        pay_counts AS (
            SELECT user_id, count(*) AS n FROM pays GROUP BY user_id
        )
        SELECT to_char(
                   date_trunc('month',
                       (s.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow'),
                   'YYYY-MM'
               ) AS cohort,
               count(*) AS attracted,
               count(*) FILTER (WHERE pc.n >= 1) AS paid1,
               count(*) FILTER (WHERE pc.n >= 2) AS paid2,
               count(*) FILTER (WHERE pc.n >= 3) AS paid3,
               count(*) FILTER (WHERE pc.n >= 4) AS paid4
        FROM subs s
        LEFT JOIN pay_counts pc USING (user_id)
        WHERE s.created_at >= now() AT TIME ZONE 'UTC' - make_interval(months => :months)
        GROUP BY 1 ORDER BY 1
        """,
        months=months,
    )
    cohorts = []
    totals = {"attracted": 0, "paid1": 0, "paid2": 0, "paid3": 0, "paid4": 0}
    for r in rows:
        item = {
            "cohort": r["cohort"],
            "attracted": int(r["attracted"] or 0),
            "paid1": int(r["paid1"] or 0),
            "paid2": int(r["paid2"] or 0),
            "paid3": int(r["paid3"] or 0),
            "paid4": int(r["paid4"] or 0),
        }
        for key in totals:
            totals[key] += item[key]
        cohorts.append(item)
    return {"cohorts": cohorts, "totals": totals}


def _acq_tariff_paths(db_session, months=12):
    """Переходы между тарифами: откуда пришли покупатели каждого тарифа.

    Для каждого пользователя берётся ПЕРВАЯ покупка каждого тарифа за окно
    :months; prev_tariff — тариф платежа, непосредственно предшествовавшего
    этой покупке (lag по всем платежам пользователя за всю историю).
    prev_tariff IS NULL — тариф куплен самой первой покупкой в жизни.
    """
    transitions = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_TARIFF_CTE},
        ordered AS (
            SELECT user_id, tariff, paid_at,
                   lag(tariff) OVER (PARTITION BY user_id ORDER BY paid_at) AS prev_tariff,
                   row_number() OVER (
                       PARTITION BY user_id, tariff ORDER BY paid_at
                   ) AS rn_tariff
            FROM pays_t
        )
        SELECT tariff, prev_tariff, count(*) AS users
        FROM ordered
        WHERE rn_tariff = 1
          AND paid_at >= now() AT TIME ZONE 'UTC' - make_interval(months => :months)
        GROUP BY 1, 2
        """,
        months=months,
    )
    volumes = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_TARIFF_CTE}
        SELECT tariff,
               count(*) AS payments,
               count(DISTINCT user_id) AS buyers,
               COALESCE(sum(amount), 0) AS rub
        FROM pays_t
        WHERE paid_at >= now() AT TIME ZONE 'UTC' - make_interval(months => :months)
        GROUP BY 1
        """,
        months=months,
    )
    stats = {}
    for r in volumes:
        stats[r["tariff"]] = {
            "tariff": r["tariff"],
            "payments": int(r["payments"] or 0),
            "buyers": int(r["buyers"] or 0),
            "rub": float(r["rub"]),
            "adopters": 0,
            "first_purchase": 0,
            "from": [],
        }
    for r in transitions:
        entry = stats.setdefault(
            r["tariff"],
            {"tariff": r["tariff"], "payments": 0, "buyers": 0, "rub": 0.0,
             "adopters": 0, "first_purchase": 0, "from": []},
        )
        users = int(r["users"] or 0)
        entry["adopters"] += users
        if r["prev_tariff"] is None:
            entry["first_purchase"] += users
        else:
            entry["from"].append({"tariff": r["prev_tariff"], "users": users})
    result = []
    for entry in stats.values():
        entry["from"].sort(key=lambda item: item["users"], reverse=True)
        adopters = entry["adopters"]
        entry["first_purchase_pct"] = (
            round(100.0 * entry["first_purchase"] / adopters, 1) if adopters else 0
        )
        result.append(entry)
    result.sort(key=lambda item: item["rub"], reverse=True)
    return {"tariffs": result}


# Месячный эквивалент тарифа для MRR: сколько месяцев покрывает оплата.
# Короткие тарифы (день/3 дня/неделя) в recognized-MRR не участвуют — это
# разовые покупки, а не подписочный доход.
ACQ_MRR_TARIFF_MONTHS_SQL = """
    CASE tariff
        WHEN 'month' THEN 1
        WHEN 'threemonths' THEN 3
        WHEN 'sixmonths' THEN 6
        WHEN 'year' THEN 12
        ELSE NULL
    END
"""

# Множитель приведения суммы автосписания к месяцу для прогноза MRR из живых
# рекуррентов (та же формула, что в боте /recurrents).
ACQ_MRR_RECURRENT_FACTORS = {
    "oneday": 30.0,
    "threedays": 10.0,
    "oneweek": 30.0 / 7.0,
    "month": 1.0,
    "threemonths": 1.0 / 3.0,
    "sixmonths": 1.0 / 6.0,
    "year": 1.0 / 12.0,
}

ACQ_AUTOPAY_EVENT_TYPES = (
    "payment_regular_autopay_success",
    "payment_trial_to_regular_autopay_success",
    "payment_regular_autopay_failure",
    "payment_trial_to_regular_autopay_failure",
    "confirm_cancel_autopay_clicked",
)

# Причины фейла автосписания, означающие отзыв разрешения на стороне банка:
# в churn считаются отменой (как кнопка «отменить автоплатёж»), а не фейлом.
# permission_revoked — код из документации ЮКассы; recurring_permission_revoked
# приходит в проде для СБП-рекуррентов.
ACQ_AUTOPAY_REVOKED_REASONS = {
    "permission_revoked",
    "recurring_permission_revoked",
}


def _acq_recurrent_dynamics(event_rows, months, today=None):
    """Восстанавливает помесячную динамику рекуррентной базы из событий.

    Чистая функция (rows -> список месяцев) — покрыта тестами отдельно от БД.
    event_rows: [{user_id, m (date месяца), event_type, reason, cnt}].
    """
    success_months = {}  # user_id -> set of months
    cancel_months = {}
    failure_months = {}  # user_id -> set of months (фейлы автосписаний)
    per_month = {}  # month -> {"cancel_users": set(), "failures": int, ...}
    for row in event_rows:
        month = row["m"]
        bucket = per_month.setdefault(
            month, {"cancel_users": set(), "failures": 0, "success_users": set()}
        )
        if row["event_type"].endswith("autopay_success"):
            success_months.setdefault(row["user_id"], set()).add(month)
            bucket["success_users"].add(row["user_id"])
        elif row["event_type"].endswith("autopay_failure"):
            if row["reason"] in ACQ_AUTOPAY_REVOKED_REASONS:
                # Отзыв разрешения на автосписания в банке — это отмена
                # автоплатежа, просто другой кнопкой; в churn считаем отменой.
                cancel_months.setdefault(row["user_id"], set()).add(month)
                bucket["cancel_users"].add(row["user_id"])
            else:
                failure_months.setdefault(row["user_id"], set()).add(month)
                bucket["failures"] += int(row["cnt"])
        else:
            cancel_months.setdefault(row["user_id"], set()).add(month)
            bucket["cancel_users"].add(row["user_id"])

    def month_back(d, n):
        year = d.year
        month = d.month - n
        while month <= 0:
            month += 12
            year -= 1
        return d.replace(year=year, month=month, day=1)

    def active_base(month):
        # Активен на конец месяца: был autopay-успех за последние 12 месяцев,
        # после которого не было ни отмены, ни фейла автосписания.
        # - отмена в том же месяце, что успех, консервативно считается отменой;
        # - фейл в том же месяце, что успех, считается ЖИВЫМ (типичный сценарий
        #   «фейл → пополнил карту → успешный ретрай» внутри одного месяца);
        # - фейл строго после последнего успеха — карта умерла (при фейле
        #   payment либо удаляет рекуррент, либо ретраит максимум ~4 суток,
        #   и без нового успеха списаний больше не будет).
        window_start = month_back(month, 11)
        active = set()
        for user_id, smonths in success_months.items():
            past = [s for s in smonths if s <= month]
            if not past:
                continue
            last_success = max(past)
            if last_success < window_start:
                continue
            cpast = [c for c in cancel_months.get(user_id, ()) if c <= month]
            if cpast and max(cpast) >= last_success:
                continue
            fpast = [f for f in failure_months.get(user_id, ()) if f <= month]
            if fpast and max(fpast) > last_success:
                continue
            active.add(user_id)
        return active

    today = today or date.today()
    today_month = date(today.year, today.month, 1)
    dynamics = []
    month_iter = month_back(today_month, months - 1)
    prev_active = active_base(month_back(month_iter, 1))
    while month_iter <= today_month:
        bucket = per_month.get(
            month_iter, {"cancel_users": set(), "failures": 0, "success_users": set()}
        )
        active = active_base(month_iter)
        churned = bucket["cancel_users"] & prev_active
        churn_pct = (
            round(100.0 * len(churned) / len(prev_active), 1) if prev_active else None
        )
        dynamics.append(
            {
                "month": month_iter.isoformat(),
                "active_recurrents": len(active),
                "autopay_success_users": len(bucket["success_users"]),
                "autopay_failures": bucket["failures"],
                "cancels": len(bucket["cancel_users"]),
                "churn_pct": churn_pct,
            }
        )
        prev_active = active
        month_iter = (
            month_iter.replace(year=month_iter.year + 1, month=1)
            if month_iter.month == 12
            else month_iter.replace(month=month_iter.month + 1)
        )

    return dynamics


def _acq_mrr(db_session, months):
    months = min(max(months, 3), 36)

    # Recognized MRR: каждая подписочная оплата равномерно «размазывается»
    # на покрытые месяцы (год за 1799 в январе даёт ~150 ₽/мес на 12 месяцев).
    mrr_rows = _acq_rows(
        db_session,
        f"""
        WITH mrr_pays AS (
            SELECT wi.user_id,
                   date_trunc('month', (t.payment_time AT TIME ZONE 'Europe/Moscow'))::date AS pay_month,
                   t.amount::numeric AS amount,
                   wi.tariff_id AS tariff
            FROM wata_transactions t
            JOIN wata_invoices wi ON wi.order_id = t.order_id
            WHERE t.transaction_status = 'Paid'
            UNION ALL
            SELECT p.user_id,
                   date_trunc('month', (coalesce(p.captured_at, p.created_at)
                       AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Moscow'))::date,
                   p.amount::numeric,
                   p.subscription_period
            FROM yk_payments p
            WHERE p.status = 'succeeded'
        ),
        mrr_norm AS (
            SELECT user_id, pay_month, amount,
                   {ACQ_MRR_TARIFF_MONTHS_SQL} AS cover_months
            FROM mrr_pays
        ),
        covered AS (
            SELECT (pay_month + make_interval(months => g.i))::date AS m,
                   amount / cover_months AS mrr_part,
                   user_id
            FROM mrr_norm, LATERAL generate_series(0, cover_months - 1) AS g(i)
            WHERE cover_months IS NOT NULL
        )
        SELECT m,
               round(sum(mrr_part))::bigint AS mrr,
               count(DISTINCT user_id) AS covered_users
        FROM covered
        WHERE m >  date_trunc('month', now() AT TIME ZONE 'Europe/Moscow')::date
                   - make_interval(months => :months)
          AND m <= date_trunc('month', now() AT TIME ZONE 'Europe/Moscow')::date
        GROUP BY m ORDER BY m
        """,
        months=months,
    )

    # Прогноз MRR из живых рекуррентов (yk_recurrent_payments — текущее
    # состояние; история отмен восстанавливается ниже из event_logs).
    live_rows = _acq_rows(
        db_session,
        """
        SELECT subscription_period AS tariff,
               count(*) AS cnt,
               coalesce(sum(amount), 0) AS total
        FROM yk_recurrent_payments
        GROUP BY 1
        """,
    )
    live_mrr = 0.0
    live_by_tariff = []
    live_total = 0
    for row in live_rows:
        factor = ACQ_MRR_RECURRENT_FACTORS.get(row["tariff"])
        part = float(row["total"]) * factor if factor else 0.0
        live_mrr += part
        live_total += int(row["cnt"])
        live_by_tariff.append(
            {
                "tariff": get_tariff_display_name(row["tariff"]),
                "count": int(row["cnt"]),
                "mrr": round(part),
            }
        )

    # Динамика рекуррентной базы из event_logs: строки yk_recurrent_payments
    # при отмене удаляются, поэтому история считается по событиям
    # autopay_success / autopay_failure / confirm_cancel_autopay_clicked.
    # reason в payload фейла появился в августе 2026 (payment пишет причину
    # отмены из вебхука); у старых событий его нет.
    event_rows = _acq_rows(
        db_session,
        """
        SELECT user_id,
               date_trunc('month', (timestamp AT TIME ZONE 'UTC'
                   AT TIME ZONE 'Europe/Moscow'))::date AS m,
               event_type,
               event_payload->>'reason' AS reason,
               count(*) AS cnt
        FROM event_logs
        WHERE event_type = ANY(:types)
        GROUP BY 1, 2, 3, 4
        """,
        types=list(ACQ_AUTOPAY_EVENT_TYPES),
    )

    dynamics = _acq_recurrent_dynamics(event_rows, months)

    return {
        "mrr": [
            {
                "month": r["m"].isoformat(),
                "mrr": int(r["mrr"]),
                "covered_users": int(r["covered_users"]),
            }
            for r in mrr_rows
        ],
        "live": {
            "recurrents_total": live_total,
            "mrr_forecast": round(live_mrr),
            "by_tariff": sorted(live_by_tariff, key=lambda x: -x["mrr"]),
        },
        "dynamics": dynamics,
    }


def _acq_payment_health(db_session, days, group):
    days = min(max(days, 7), 365)
    grp = "week" if group == "week" else "day"

    yk_rows = _acq_rows(
        db_session,
        f"""
        SELECT date_trunc('{grp}', (created_at AT TIME ZONE 'UTC'
                   AT TIME ZONE 'Europe/Moscow'))::date AS period,
               count(*) AS created,
               count(*) FILTER (WHERE status = 'succeeded') AS paid
        FROM yk_payments
        WHERE created_at >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        GROUP BY 1 ORDER BY 1
        """,
        days=days,
    )
    wata_rows = _acq_rows(
        db_session,
        f"""
        SELECT date_trunc('{grp}', (wi.creation_time AT TIME ZONE 'Europe/Moscow'))::date AS period,
               count(*) AS created,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM wata_transactions t
                   WHERE t.order_id = wi.order_id
                     AND t.transaction_status = 'Paid'
               )) AS paid
        FROM wata_invoices wi
        WHERE wi.creation_time >= now() - make_interval(days => :days)
        GROUP BY 1 ORDER BY 1
        """,
        days=days,
    )
    autopay_rows = _acq_rows(
        db_session,
        f"""
        SELECT date_trunc('{grp}', (timestamp AT TIME ZONE 'UTC'
                   AT TIME ZONE 'Europe/Moscow'))::date AS period,
               count(*) FILTER (WHERE event_type LIKE '%autopay_success') AS success,
               count(*) FILTER (WHERE event_type LIKE '%autopay_failure') AS failure
        FROM event_logs
        WHERE event_type = ANY(:types)
          AND timestamp >= now() AT TIME ZONE 'UTC' - make_interval(days => :days)
        GROUP BY 1 ORDER BY 1
        """,
        days=days,
        types=[t for t in ACQ_AUTOPAY_EVENT_TYPES if "cancel" not in t],
    )

    def series(rows, created_key="created", paid_key="paid"):
        return [
            {
                "period": r["period"].isoformat(),
                "created": int(r[created_key]),
                "paid": int(r[paid_key]),
                "rate": (
                    round(100.0 * r[paid_key] / r[created_key], 1)
                    if r[created_key]
                    else None
                ),
            }
            for r in rows
        ]

    def totals(rows, created_key="created", paid_key="paid"):
        created = sum(int(r[created_key]) for r in rows)
        paid = sum(int(r[paid_key]) for r in rows)
        return {
            "created": created,
            "paid": paid,
            "rate": round(100.0 * paid / created, 1) if created else None,
        }

    autopay_series = [
        {
            "period": r["period"].isoformat(),
            "success": int(r["success"]),
            "failure": int(r["failure"]),
            "rate": (
                round(100.0 * r["success"] / (r["success"] + r["failure"]), 1)
                if (r["success"] + r["failure"])
                else None
            ),
        }
        for r in autopay_rows
    ]
    autopay_success = sum(r["success"] for r in autopay_series)
    autopay_failure = sum(r["failure"] for r in autopay_series)

    return {
        "group": grp,
        "days": days,
        "yookassa": {"series": series(yk_rows), "totals": totals(yk_rows)},
        "wata": {"series": series(wata_rows), "totals": totals(wata_rows)},
        "autopay": {
            "series": autopay_series,
            "totals": {
                "success": autopay_success,
                "failure": autopay_failure,
                "rate": (
                    round(
                        100.0 * autopay_success / (autopay_success + autopay_failure),
                        1,
                    )
                    if (autopay_success + autopay_failure)
                    else None
                ),
            },
        },
    }


ACQ_SECTIONS = {
    "new_repeat": lambda s, req: _acq_new_repeat(
        s, int(req.GET.get("days", 60)),
        req.GET.get("start") or None, req.GET.get("end") or None,
    ),
    "renew45": lambda s, req: _acq_renew45(s, int(req.GET.get("months", 12))),
    "funnel": lambda s, req: _acq_funnel(s, int(req.GET.get("weeks", 12))),
    "ads": lambda s, req: _acq_ads(s, int(req.GET.get("weeks", 12))),
    "ads_daily": lambda s, req: _acq_ads_daily(
        s, int(req.GET.get("days", 92)),
        "month" if req.GET.get("group") == "month" else "day",
    ),
    "cohorts": lambda s, req: _acq_cohorts(s, int(req.GET.get("months", 14))),
    "trials": lambda s, req: _acq_trials(
        s, int(req.GET.get("days", 60)), int(req.GET.get("window", 10))
    ),
    "pushes": lambda s, req: _acq_pushes(s, int(req.GET.get("days", 30))),
    "patterns": lambda s, req: _acq_patterns(s, int(req.GET.get("days", 30))),
    "trial_timing": lambda s, req: _acq_trial_timing(s, int(req.GET.get("days", 365))),
    "renewal_ladder": lambda s, req: _acq_renewal_ladder(
        s, int(req.GET.get("months", 12))
    ),
    "tariff_paths": lambda s, req: _acq_tariff_paths(
        s, int(req.GET.get("months", 12))
    ),
    "mrr": lambda s, req: _acq_mrr(s, int(req.GET.get("months", 24))),
    "payment_health": lambda s, req: _acq_payment_health(
        s, int(req.GET.get("days", 90)),
        "week" if req.GET.get("group") == "week" else "day",
    ),
}


def support_admin_api_acquisition(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response
    section = request.GET.get("section", "")
    handler = ACQ_SECTIONS.get(section)
    if handler is None:
        return JsonResponse({"status": "error", "message": "unknown section"}, status=400)
    db_session = session_factory()
    try:
        return JsonResponse({"status": "ok", "result": handler(db_session, request)})
    except ValueError:
        return JsonResponse({"status": "error", "message": "bad params"}, status=400)
    finally:
        db_session.close()


def support_admin_api_ad_spends(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response
    if request.method != "POST":
        return JsonResponse({"status": "error"}, status=405)

    db_session = session_factory()
    try:
        action = request.POST.get("action")
        if action == "upsert":
            day = date.fromisoformat(request.POST.get("day", ""))
            channel = (request.POST.get("channel") or "default").strip()[:128]
            account = (
                request.POST.get("account") or "default"
            ).strip()[:64] or "default"
            amount = float(request.POST.get("amount_rub", ""))
            comment = (request.POST.get("comment") or "").strip()[:512] or None
            db_session.execute(
                sa_text(
                    """
                    INSERT INTO ad_spends (day, channel, account, amount_rub, comment)
                    VALUES (:day, :channel, :account, :amount, :comment)
                    ON CONFLICT (day, channel, account)
                    DO UPDATE SET amount_rub = EXCLUDED.amount_rub,
                                  comment = EXCLUDED.comment,
                                  updated_at = now()
                    """
                ),
                {"day": day, "channel": channel, "account": account,
                 "amount": amount, "comment": comment},
            )
            db_session.commit()
            return JsonResponse({"status": "ok"})
        if action == "delete":
            db_session.execute(
                sa_text("DELETE FROM ad_spends WHERE id = :id"),
                {"id": int(request.POST.get("id", ""))},
            )
            db_session.commit()
            return JsonResponse({"status": "ok"})
        if action == "rename_account":
            # Переименование аккаунта задним числом: типовой случай — данные,
            # залитые до появления мульти-аккаунтов, лежат под "default" и их
            # нужно объявить конкретным кабинетом перед дозаливкой второго.
            src = (request.POST.get("from") or "").strip()[:64]
            dst = (request.POST.get("to") or "").strip()[:64]
            if not src or not dst or src == dst:
                return JsonResponse(
                    {"status": "error", "message": "нужны разные имена from/to"},
                    status=400,
                )
            overlap = db_session.execute(
                sa_text(
                    """
                    SELECT count(*) FROM ad_spends a
                    WHERE a.account = :src
                      AND EXISTS (
                        SELECT 1 FROM ad_spends b
                        WHERE b.account = :dst
                          AND b.day = a.day AND b.channel = a.channel
                      )
                    """
                ),
                {"src": src, "dst": dst},
            ).scalar()
            if overlap:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": (
                            f"у аккаунта «{dst}» уже есть данные за {overlap} "
                            "пересекающихся дней — переименование прервано, "
                            "чтобы ничего не затереть"
                        ),
                    },
                    status=409,
                )
            renamed = db_session.execute(
                sa_text(
                    """
                    UPDATE ad_spends SET account = :dst, updated_at = now()
                    WHERE account = :src
                    """
                ),
                {"src": src, "dst": dst},
            ).rowcount
            db_session.commit()
            logging.info(
                "ad_spends account renamed: %s -> %s (%s rows)", src, dst, renamed
            )
            return JsonResponse({"status": "ok", "renamed": renamed})
        if action == "import_csv":
            channel = (
                request.POST.get("channel") or "yandex-direct"
            ).strip()[:128] or "yandex-direct"
            account = (
                request.POST.get("account") or "default"
            ).strip()[:64] or "default"
            upload = request.FILES.get("file")
            if upload is None:
                return JsonResponse(
                    {"status": "error", "message": "нет файла"}, status=400
                )
            raw = upload.read()
            try:
                text_content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text_content = raw.decode("cp1251")
            parsed = _parse_yandex_direct_csv(text_content)
            if not parsed:
                return JsonResponse(
                    {"status": "error",
                     "message": "в файле не найдено дневных строк Директа"},
                    status=400,
                )
            # Идемпотентный импорт в разрезе аккаунта: повторная загрузка того
            # же файла перезаписывает те же дни того же аккаунта, а данные
            # других аккаунтов за эти дни не трогаются — в аналитике суммы
            # по дню аккумулируются по всем аккаунтам.
            for item in parsed:
                db_session.execute(
                    sa_text(
                        """
                        INSERT INTO ad_spends
                            (day, channel, account, amount_rub, impressions, clicks, comment)
                        VALUES (:day, :channel, :account, :amount, :impressions, :clicks, :comment)
                        ON CONFLICT (day, channel, account)
                        DO UPDATE SET amount_rub = EXCLUDED.amount_rub,
                                      impressions = EXCLUDED.impressions,
                                      clicks = EXCLUDED.clicks,
                                      comment = EXCLUDED.comment,
                                      updated_at = now()
                        """
                    ),
                    {
                        "day": item["day"], "channel": channel, "account": account,
                        "amount": item["amount_rub"],
                        "impressions": item["impressions"],
                        "clicks": item["clicks"],
                        "comment": "csv-import",
                    },
                )
            db_session.commit()
            days_sorted = sorted(item["day"] for item in parsed)
            logging.info(
                "ad_spends csv import: %s rows, %s..%s, channel=%s, account=%s",
                len(parsed), days_sorted[0], days_sorted[-1], channel, account,
            )
            return JsonResponse({
                "status": "ok",
                "imported": len(parsed),
                "from": days_sorted[0].isoformat(),
                "to": days_sorted[-1].isoformat(),
            })
        return JsonResponse({"status": "error", "message": "unknown action"}, status=400)
    except (ValueError, TypeError):
        db_session.rollback()
        return JsonResponse({"status": "error", "message": "bad params"}, status=400)
    except Exception:
        db_session.rollback()
        logging.exception("ad_spends update failed")
        return JsonResponse(
            {"status": "error", "message": "ошибка (миграция ad_spends накатана?)"},
            status=500,
        )
    finally:
        db_session.close()


# === Этап 3 плана админки: аудит-лог, таймлайн клиента, diff RWMS, сообщения ==


def support_admin_actor(request):
    # Этап 6 положит в сессию логин персонального admin-аккаунта; до тех пор
    # актором считается роль сессии (admin/support).
    return (
        request.session.get("support_admin_account")
        or support_admin_role(request)
        or "unknown"
    )


def admin_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.META.get("REMOTE_ADDR") or "")[:64] or None


def admin_audit_write(db_session, request, action, target=None, **details):
    """Запись в журнал действий админов; коммитит вызывающая сторона."""
    try:
        db_session.add(
            AdminAuditLog(
                actor=str(support_admin_actor(request))[:128],
                source="site",
                action=action[:64],
                target=str(target)[:256] if target is not None else None,
                details=details,
                ip=admin_client_ip(request),
            )
        )
    except Exception:
        logging.exception("failed to write admin audit log for action %s", action)


def support_admin_api_audit_log(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        try:
            days = min(max(int(request.GET.get("days") or 30), 1), 365)
            limit = min(max(int(request.GET.get("limit") or 200), 1), 1000)
        except ValueError:
            return JsonResponse({"status": "error", "message": "bad params"}, status=400)

        query = (
            db_session.query(AdminAuditLog)
            .filter(
                AdminAuditLog.created_at
                >= datetime.utcnow() - timedelta(days=days)
            )
            .order_by(AdminAuditLog.created_at.desc())
        )
        action = (request.GET.get("action") or "").strip()
        if action:
            query = query.filter(AdminAuditLog.action == action)
        actor = (request.GET.get("actor") or "").strip()
        if actor:
            query = query.filter(AdminAuditLog.actor == actor)
        rows = query.limit(limit).all()

        if request.GET.get("format") == "csv":
            import csv
            import io

            def csv_cell(value):
                text_value = "" if value is None else str(value)
                # Защита от CSV-инъекций в Excel/Sheets.
                if text_value[:1] in ("=", "+", "-", "@"):
                    return "'" + text_value
                return text_value

            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["created_at", "actor", "source", "action", "target", "ip", "details"])
            for row in rows:
                writer.writerow(
                    [
                        row.created_at.isoformat() if row.created_at else "",
                        csv_cell(row.actor),
                        csv_cell(row.source),
                        csv_cell(row.action),
                        csv_cell(row.target),
                        csv_cell(row.ip),
                        csv_cell(json.dumps(row.details, ensure_ascii=False)),
                    ]
                )
            response = HttpResponse(
                "﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8"
            )
            response["Content-Disposition"] = "attachment; filename=admin_audit_log.csv"
            return response

        return JsonResponse(
            {
                "status": "ok",
                "result": [
                    {
                        "created_at": admin_date_label(row.created_at),
                        "actor": row.actor,
                        "source": row.source,
                        "action": row.action,
                        "target": row.target,
                        "details": row.details,
                        "ip": row.ip,
                    }
                    for row in rows
                ],
            }
        )
    finally:
        db_session.close()


ADMIN_TIMELINE_LIMIT = 300

# Человекочитаемые подписи событий event_logs для таймлайна клиента.
ADMIN_TIMELINE_EVENT_LABELS = {
    "subscription_created": ("Подписка", "Подписка создана"),
    "subscription_merged": ("Подписка", "Аккаунты объединены"),
    "subscription_expired": ("Подписка", "Подписка истекла"),
    "subscription_activated": ("Подписка", "Подписка активирована"),
    "first_successful_login": ("Кабинет", "Первый вход в кабинет"),
    "traffic_source_changed": ("Прочее", "Смена источника трафика"),
    "traffic_threshold_reached": ("Трафик", "Порог трафика"),
    "notification_sent": ("Пуши", "Уведомление"),
    "confirm_cancel_autopay_clicked": ("Платежи", "Отключил автоплатёж"),
}


def admin_user_timeline(db_session, user, limit=ADMIN_TIMELINE_LIMIT):
    items = []

    for event in (
        db_session.query(EventLog)
        .filter(EventLog.user_id == user.id)
        .order_by(EventLog.timestamp.desc())
        .limit(limit)
        .all()
    ):
        category, title = ADMIN_TIMELINE_EVENT_LABELS.get(
            event.event_type, ("Событие", event.event_type)
        )
        payload = event.event_payload or {}
        if event.event_type == "traffic_threshold_reached":
            title = f"Трафик: порог {payload.get('threshold', '?')} МБ"
        elif event.event_type == "notification_sent":
            title = (
                f"Пуш {payload.get('notification_type', '?')}"
                f" ({payload.get('channel', 'telegram')})"
            )
        elif event.event_type.startswith("payment_"):
            category = "Платежи"
            title = event.event_type.replace("payment_", "Платёж: ")
        elif event.event_type.startswith("create_invoice"):
            category = "Платежи"
            title = "Создан инвойс " + event.event_type.replace("create_invoice_", "")
        elif event.event_type.startswith("install_"):
            category = "Установка"
            title = event.event_type
        items.append(
            {
                "ts": event.timestamp,
                "category": category,
                "title": title,
                "details": payload,
            }
        )

    for payment in (
        db_session.query(YkPayment)
        .filter(YkPayment.user_id == user.id)
        .order_by(YkPayment.created_at.desc())
        .limit(50)
        .all()
    ):
        items.append(
            {
                "ts": payment.captured_at or payment.created_at,
                "category": "Платежи",
                "title": f"ЮКасса {payment.amount} ₽ · {payment.status}",
                "details": {
                    "tariff": payment.subscription_period,
                    "payment_id": payment.payment_id,
                },
            }
        )

    for invoice, transaction in (
        db_session.query(WataInvoice, WataTransaction)
        .outerjoin(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
        .filter(WataInvoice.user_id == user.id)
        .order_by(WataInvoice.creation_time.desc())
        .limit(50)
        .all()
    ):
        status = transaction.transaction_status if transaction else invoice.status
        items.append(
            {
                "ts": invoice.creation_time.replace(tzinfo=None)
                if invoice.creation_time
                else None,
                "category": "Платежи",
                "title": f"WATA {invoice.amount} ₽ · {status}",
                "details": {"tariff": invoice.tariff_id, "order_id": invoice.order_id},
            }
        )

    for bonus in (
        db_session.query(ReferralBonus)
        .filter(
            (ReferralBonus.referrer_id == user.id)
            | (ReferralBonus.referral_id == user.id)
        )
        .order_by(ReferralBonus.created_at.desc())
        .limit(50)
        .all()
    ):
        direction = "получил бонус" if bonus.referrer_id == user.id else "принёс бонус"
        items.append(
            {
                "ts": bonus.created_at,
                "category": "Рефералка",
                "title": f"{direction} {bonus.bonus_type.value} +{bonus.days_added} дн.",
                "details": {},
            }
        )

    block = db_session.get(UserBlock, user.id)
    if block:
        items.append(
            {
                "ts": block.created_at,
                "category": "Блокировки",
                "title": "Полная блокировка",
                "details": {"reason": block.reason},
            }
        )
    referral_block = db_session.get(ReferralProgramBlock, user.id)
    if referral_block:
        items.append(
            {
                "ts": referral_block.created_at,
                "category": "Блокировки",
                "title": "Блокировка рефералки",
                "details": {"reason": referral_block.reason},
            }
        )

    for message in (
        db_session.query(AdminDirectMessage)
        .filter(AdminDirectMessage.user_id == user.id)
        .order_by(AdminDirectMessage.created_at.desc())
        .limit(20)
        .all()
    ):
        items.append(
            {
                "ts": message.created_at,
                "category": "Поддержка",
                "title": f"Сообщение от админа ({message.created_by})",
                "details": {"text": message.text[:200]},
            }
        )

    items = [item for item in items if item["ts"] is not None]
    items.sort(key=lambda item: item["ts"], reverse=True)
    return items[:limit]


def support_admin_api_user_timeline(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        user = admin_find_user(db_session, request.GET.get("q"))
        if not user:
            return JsonResponse({"status": "not_found"}, status=404)

        items = admin_user_timeline(db_session, user)
        return JsonResponse(
            {
                "status": "ok",
                "result": {
                    "user": admin_user_payload(user),
                    "items": [
                        {
                            "ts": admin_date_label(item["ts"]),
                            "category": item["category"],
                            "title": item["title"],
                            "details": item["details"],
                        }
                        for item in items
                    ],
                },
            }
        )
    finally:
        db_session.close()


RWMS_EXPIRE_DIFF_TOLERANCE = timedelta(minutes=5)


def admin_rwms_diff(user, rwms_user):
    """Список расхождений БД ↔ панель для карточки клиента."""
    diffs = []
    if rwms_user is None:
        diffs.append(
            {
                "kind": "missing_in_panel",
                "label": "Подписки нет в Remnawave",
                "db": admin_date_label(user.expire_at),
                "panel": "—",
            }
        )
        return diffs

    # rwms_expire_at уже возвращает naive UTC (protobuf ToDatetime).
    # astimezone() здесь недопустим: на naive-значении Python трактует его как
    # локальное время сервера (МСК) и «конвертирует» в UTC, сдвигая на -3 часа —
    # из-за этого у всех клиентов показывался ложный рассинхрон ровно на 3 часа.
    panel_expire_naive = rwms_expire_at(rwms_user)
    db_expire = admin_dt(user.expire_at)
    if panel_expire_naive and db_expire:
        if abs(panel_expire_naive - db_expire) > RWMS_EXPIRE_DIFF_TOLERANCE:
            diffs.append(
                {
                    "kind": "expire_diff",
                    "label": "Разные даты окончания",
                    "db": admin_date_label(db_expire),
                    "panel": admin_date_label(panel_expire_naive),
                }
            )
    status_name = proto.UserStatus.Name(rwms_user.status)
    now = datetime.utcnow()
    db_active = bool(db_expire and db_expire > now)
    panel_active = status_name == "ACTIVE"
    if db_active and not panel_active:
        diffs.append(
            {
                "kind": "status_diff",
                "label": "В БД активна, в панели " + status_name,
                "db": "ACTIVE",
                "panel": status_name,
            }
        )
    return diffs


def support_admin_api_rwms_sync(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "GET" and request.GET.get("action") == "mismatches":
            rows = (
                db_session.query(RwmsSyncMismatch)
                .filter(RwmsSyncMismatch.resolved_at.is_(None))
                .order_by(RwmsSyncMismatch.detected_at.desc())
                .limit(500)
                .all()
            )
            return JsonResponse(
                {
                    "status": "ok",
                    "result": [
                        {
                            "username": row.username,
                            "kind": row.kind,
                            "details": row.details,
                            "detected_at": admin_date_label(row.detected_at),
                        }
                        for row in rows
                    ],
                }
            )

        query = (
            request.GET.get("q") if request.method == "GET" else request.POST.get("q")
        )
        user = admin_find_user(db_session, query)
        if not user:
            return JsonResponse({"status": "not_found"}, status=404)

        rwms_user = rwms_client.get_user_by_username(user.username)

        if request.method == "GET":
            panel_payload = None
            if rwms_user is not None:
                # rwms_expire_at возвращает naive UTC — показываем как есть,
                # без astimezone (см. комментарий в admin_rwms_diff).
                panel_expire = rwms_expire_at(rwms_user)
                panel_payload = {
                    "expire_at": admin_date_label(panel_expire),
                    "status": proto.UserStatus.Name(rwms_user.status),
                    "squads": [
                        squad.uuid for squad in rwms_user.active_internal_squads
                    ],
                }
            return JsonResponse(
                {
                    "status": "ok",
                    "result": {
                        "user": admin_user_payload(user),
                        "panel": panel_payload,
                        "diffs": admin_rwms_diff(user, rwms_user),
                    },
                }
            )

        # POST: направленная синхронизация. Только UPDATE полей подписки —
        # никаких удалений/пересозданий (Remnawave Safety Rules).
        action = request.POST.get("action")
        if action == "push_to_panel":
            if rwms_user is None:
                return JsonResponse(
                    {"status": "error", "message": "Подписки нет в панели — пуш недоступен"},
                    status=404,
                )
            db_expire = admin_dt(user.expire_at)
            if db_expire is None:
                return JsonResponse(
                    {"status": "error", "message": "В БД нет expire_at"}, status=400
                )
            response = rwms_client.update_user(
                proto.UpdateUserRequest(
                    uuid=rwms_user.uuid,
                    expire_at=db_expire.replace(tzinfo=timezone.utc),
                    status=proto.UserStatus.ACTIVE
                    if db_expire > datetime.utcnow()
                    else rwms_user.status,
                    traffic_limit_strategy=proto.TrafficLimitStrategy.NO_RESET,
                    active_internal_squads=[
                        squad.uuid for squad in rwms_user.active_internal_squads
                    ],
                )
            )
            if response is None:
                return JsonResponse(
                    {"status": "error", "message": "RWMS не принял обновление"},
                    status=502,
                )
            admin_audit_write(
                db_session,
                request,
                "rwms_sync_push",
                target=user.username,
                expire_at=str(user.expire_at),
            )
            db_session.commit()
            return JsonResponse({"status": "ok", "result": {"synced": "to_panel"}})

        if action == "pull_from_panel":
            if rwms_user is None:
                return JsonResponse(
                    {"status": "error", "message": "Подписки нет в панели"}, status=404
                )
            panel_expire = rwms_expire_at(rwms_user)
            if panel_expire is None:
                return JsonResponse(
                    {"status": "error", "message": "В панели нет expire_at"}, status=400
                )
            old_expire = user.expire_at
            # panel_expire — уже naive UTC; astimezone() тут сдвигал бы время
            # на -3 часа (трактуя naive как МСК) и портил users.expire_at.
            user.expire_at = panel_expire
            admin_audit_write(
                db_session,
                request,
                "rwms_sync_pull",
                target=user.username,
                old=str(old_expire),
                new=str(user.expire_at),
            )
            db_session.commit()
            return JsonResponse(
                {
                    "status": "ok",
                    "result": {
                        "synced": "from_panel",
                        "user": admin_user_payload(user),
                    },
                }
            )

        return JsonResponse(
            {"status": "error", "message": "Неизвестное действие"}, status=400
        )
    finally:
        db_session.close()


ADMIN_DIRECT_MESSAGE_MAX_LEN = 3500


def support_admin_api_direct_message(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "POST":
            user = admin_find_user(db_session, request.POST.get("q"))
            if not user:
                return JsonResponse({"status": "not_found"}, status=404)
            text_value = (request.POST.get("text") or "").strip()
            if not text_value:
                return JsonResponse(
                    {"status": "error", "message": "Пустое сообщение"}, status=400
                )
            if len(text_value) > ADMIN_DIRECT_MESSAGE_MAX_LEN:
                return JsonResponse(
                    {"status": "error", "message": "Сообщение слишком длинное"},
                    status=400,
                )
            if not user.telegram_id:
                return JsonResponse(
                    {"status": "error", "message": "У пользователя нет Telegram ID"},
                    status=400,
                )
            message = AdminDirectMessage(
                user_id=user.id,
                text=text_value,
                created_by=str(support_admin_actor(request))[:128],
            )
            db_session.add(message)
            admin_audit_write(
                db_session,
                request,
                "direct_message",
                target=user.username,
                length=len(text_value),
            )
            db_session.commit()
            return JsonResponse({"status": "ok", "result": {"message_id": message.id}})

        user = admin_find_user(db_session, request.GET.get("q"))
        if not user:
            return JsonResponse({"status": "not_found"}, status=404)
        messages = (
            db_session.query(AdminDirectMessage)
            .filter(AdminDirectMessage.user_id == user.id)
            .order_by(AdminDirectMessage.created_at.desc())
            .limit(20)
            .all()
        )
        deliveries = {}
        if messages:
            for delivery in (
                db_session.query(AdminDirectMessageDelivery)
                .filter(
                    AdminDirectMessageDelivery.message_id.in_(
                        [message.id for message in messages]
                    )
                )
                .all()
            ):
                deliveries.setdefault(delivery.message_id, []).append(
                    {"bot": delivery.bot_id, "status": delivery.status}
                )
        return JsonResponse(
            {
                "status": "ok",
                "result": [
                    {
                        "id": message.id,
                        "text": message.text,
                        "created_by": message.created_by,
                        "created_at": admin_date_label(message.created_at),
                        "deliveries": deliveries.get(message.id, []),
                    }
                    for message in messages
                ],
            }
        )
    finally:
        db_session.close()


# === Этап 4 плана админки: сегменты, рассылки, массовые операции =============


def support_admin_api_segments(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        result = []
        for key, (label, _condition) in ADMIN_SEGMENTS.items():
            count = db_session.execute(sa_text(segment_count_sql(key))).scalar() or 0
            result.append({"key": key, "label": label, "count": int(count)})
        return JsonResponse({"status": "ok", "result": result})
    finally:
        db_session.close()


BROADCAST_TEXT_MAX_LEN = 3500
BROADCAST_MAX_BUTTONS = 3


def admin_broadcast_progress(db_session, broadcast):
    covered, sent = db_session.execute(
        sa_text(
            """
            SELECT count(DISTINCT user_id),
                   count(DISTINCT user_id) FILTER (WHERE status = 'sent')
            FROM broadcast_deliveries WHERE broadcast_id = :bid
            """
        ),
        {"bid": broadcast.id},
    ).one()
    return int(covered), int(sent)


def admin_broadcast_payload(db_session, broadcast):
    covered, sent = admin_broadcast_progress(db_session, broadcast)
    total = broadcast.total or 0
    # Рассылка исчерпана: каждый получатель сегмента имеет хотя бы одну
    # терминальную запись доставки. Помечаем done лениво при опросе.
    if broadcast.status == "running" and total and covered >= total:
        broadcast.status = "done"
        broadcast.finished_at = datetime.utcnow()
        db_session.commit()
    return {
        "id": broadcast.id,
        "title": broadcast.title,
        "segment": broadcast.segment,
        "segment_label": ADMIN_SEGMENTS.get(broadcast.segment, (broadcast.segment,))[0],
        "status": broadcast.status,
        "text": broadcast.text,
        "buttons": broadcast.buttons or [],
        "total": total,
        "covered": covered,
        "sent": sent,
        "progress_pct": round(100.0 * covered / total, 1) if total else 0,
        "created_by": broadcast.created_by,
        "created_at": admin_date_label(broadcast.created_at),
        "finished_at": admin_date_label(broadcast.finished_at),
    }


def support_admin_api_broadcasts(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "GET":
            broadcasts = (
                db_session.query(Broadcast)
                .order_by(Broadcast.created_at.desc())
                .limit(50)
                .all()
            )
            return JsonResponse(
                {
                    "status": "ok",
                    "result": [
                        admin_broadcast_payload(db_session, broadcast)
                        for broadcast in broadcasts
                    ],
                }
            )

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        action = request.POST.get("action")
        if action == "stop":
            broadcast = db_session.get(Broadcast, int(request.POST.get("id") or 0))
            if not broadcast:
                return JsonResponse({"status": "not_found"}, status=404)
            if broadcast.status == "running":
                broadcast.status = "stopped"
                broadcast.finished_at = datetime.utcnow()
                admin_audit_write(
                    db_session, request, "broadcast_stop", target=str(broadcast.id)
                )
                db_session.commit()
            return JsonResponse(
                {"status": "ok", "result": admin_broadcast_payload(db_session, broadcast)}
            )

        if action == "create":
            title = (request.POST.get("title") or "").strip()[:256]
            text_value = (request.POST.get("text") or "").strip()
            segment = (request.POST.get("segment") or "").strip()
            if not title or not text_value:
                return JsonResponse(
                    {"status": "error", "message": "Заполните название и текст"},
                    status=400,
                )
            if len(text_value) > BROADCAST_TEXT_MAX_LEN:
                return JsonResponse(
                    {"status": "error", "message": "Текст слишком длинный"}, status=400
                )
            if segment not in ADMIN_SEGMENTS:
                return JsonResponse(
                    {"status": "error", "message": "Неизвестный сегмент"}, status=400
                )
            try:
                buttons = json.loads(request.POST.get("buttons") or "[]")
            except ValueError:
                return JsonResponse(
                    {"status": "error", "message": "Кнопки: некорректный JSON"},
                    status=400,
                )
            if not isinstance(buttons, list) or len(buttons) > BROADCAST_MAX_BUTTONS:
                return JsonResponse(
                    {"status": "error", "message": "Не больше 3 кнопок"}, status=400
                )
            clean_buttons = []
            for button in buttons:
                text_label = str(button.get("text") or "").strip()[:64]
                url = str(button.get("url") or "").strip()[:512]
                if not text_label or not url.startswith("https://"):
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": "У кнопки нужны текст и https-ссылка",
                        },
                        status=400,
                    )
                clean_buttons.append({"text": text_label, "url": url})

            total = db_session.execute(sa_text(segment_count_sql(segment))).scalar() or 0
            if not total:
                return JsonResponse(
                    {"status": "error", "message": "В сегменте нет получателей"},
                    status=400,
                )

            broadcast = Broadcast(
                title=title,
                text=text_value,
                segment=segment,
                status="running",
                buttons=clean_buttons,
                total=int(total),
                created_by=str(support_admin_actor(request))[:128],
            )
            db_session.add(broadcast)
            admin_audit_write(
                db_session,
                request,
                "broadcast_create",
                target=title,
                segment=segment,
                total=int(total),
            )
            db_session.commit()
            return JsonResponse(
                {"status": "ok", "result": admin_broadcast_payload(db_session, broadcast)}
            )

        return JsonResponse(
            {"status": "error", "message": "Неизвестное действие"}, status=400
        )
    finally:
        db_session.close()


BULK_MAX_IDS = 500
BULK_ACTIONS = {"extend_days", "block", "unblock", "referral_block", "referral_unblock"}


def admin_bulk_find_users(db_session, raw_ids):
    """Резолвит строки (telegram_id / username / email) в пользователей."""
    tokens = []
    for line in raw_ids.replace(",", "\n").splitlines():
        token = line.strip()
        if token:
            tokens.append(token)
    tokens = tokens[: BULK_MAX_IDS + 1]

    resolved = []
    missing = []
    seen_ids = set()
    for token in tokens:
        user = admin_find_user(db_session, token)
        if user is None:
            missing.append(token)
        elif user.id not in seen_ids:
            seen_ids.add(user.id)
            resolved.append((token, user))
    return resolved, missing


def admin_bulk_extend(db_session, user, days):
    current_expire = admin_dt(user.expire_at)
    base = max(current_expire or datetime.utcnow(), datetime.utcnow())
    target_expire = base.replace(tzinfo=timezone.utc) + timedelta(days=days)
    user.expire_at = target_expire.replace(tzinfo=None)

    rwms_user = rwms_client.get_user_by_username(user.username)
    if rwms_user is None:
        return "продлено в БД; подписки нет в RWMS (панель не тронута)"
    user_email = (
        rwms_user.email if rwms_user.email and "@" in rwms_user.email else None
    )
    response = rwms_client.update_user(
        proto.UpdateUserRequest(
            uuid=rwms_user.uuid,
            email=user_email,
            telegram_id=rwms_user.telegram_id,
            expire_at=target_expire,
            status=proto.UserStatus.ACTIVE,
            traffic_limit_strategy=proto.TrafficLimitStrategy.NO_RESET,
            active_internal_squads=[
                squad.uuid for squad in rwms_user.active_internal_squads
            ],
        )
    )
    if response is None:
        raise RuntimeError("RWMS не принял продление")
    return f"продлено до {target_expire:%Y-%m-%d %H:%M} UTC"


def admin_bulk_block(db_session, user, reason):
    if db_session.get(UserBlock, user.id) is None:
        db_session.add(UserBlock(user_id=user.id, reason=reason[:512]))
    user.autopay_allow = False
    db_session.query(YkRecurrentPayment).filter(
        YkRecurrentPayment.user_id == user.id
    ).delete(synchronize_session=False)
    rwms_user = rwms_client.get_user_by_username(user.username)
    if rwms_user is not None:
        rwms_client.update_user(
            proto.UpdateUserRequest(
                uuid=rwms_user.uuid, status=proto.UserStatus.DISABLED
            )
        )
    return "заблокирован (DISABLED в панели, автоплатёж снят)"


def admin_bulk_unblock(db_session, user):
    db_session.query(UserBlock).filter(UserBlock.user_id == user.id).delete(
        synchronize_session=False
    )
    db_expire = admin_dt(user.expire_at)
    if db_expire and db_expire > datetime.utcnow():
        rwms_user = rwms_client.get_user_by_username(user.username)
        if rwms_user is not None:
            rwms_client.update_user(
                proto.UpdateUserRequest(
                    uuid=rwms_user.uuid, status=proto.UserStatus.ACTIVE
                )
            )
        return "разблокирован (ACTIVE в панели)"
    return "разблокирован (подписка истекла, панель не активировалась)"


def support_admin_api_bulk(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response
    if request.method != "POST":
        return JsonResponse({"status": "error"}, status=405)

    action = request.POST.get("action")
    if action not in BULK_ACTIONS:
        return JsonResponse(
            {"status": "error", "message": "Неизвестное действие"}, status=400
        )
    dry_run = (request.POST.get("dry_run") or "") == "1"
    reason = (request.POST.get("reason") or "bulk admin action").strip()

    days = 0
    if action == "extend_days":
        try:
            days = int(request.POST.get("days") or "0")
        except ValueError:
            return JsonResponse(
                {"status": "error", "message": "Дни должны быть числом"}, status=400
            )
        if not 1 <= days <= 365:
            return JsonResponse(
                {"status": "error", "message": "Дни: от 1 до 365"}, status=400
            )

    db_session = session_factory()
    try:
        resolved, missing = admin_bulk_find_users(
            db_session, request.POST.get("ids") or ""
        )
        if len(resolved) > BULK_MAX_IDS:
            return JsonResponse(
                {
                    "status": "error",
                    "message": f"Не больше {BULK_MAX_IDS} пользователей за раз",
                },
                status=400,
            )
        if not resolved:
            return JsonResponse(
                {"status": "error", "message": "Ни один пользователь не найден"},
                status=400,
            )

        results = []
        applied = 0
        for token, user in resolved:
            if dry_run:
                current = admin_dt(user.expire_at)
                preview = {
                    "extend_days": (
                        f"будет продлён на {days} дн. "
                        f"(сейчас до {current:%Y-%m-%d %H:%M} UTC)"
                        if current
                        else f"будет продлён на {days} дн. (сейчас без подписки)"
                    ),
                    "block": "будет заблокирован (DISABLED, автоплатёж снят)",
                    "unblock": "будет разблокирован",
                    "referral_block": "рефералка будет заблокирована",
                    "referral_unblock": "рефералка будет разблокирована",
                }[action]
                results.append(
                    {"token": token, "username": user.username, "ok": True,
                     "message": preview}
                )
                continue
            try:
                if action == "extend_days":
                    message = admin_bulk_extend(db_session, user, days)
                elif action == "block":
                    message = admin_bulk_block(db_session, user, reason)
                elif action == "unblock":
                    message = admin_bulk_unblock(db_session, user)
                elif action == "referral_block":
                    block = db_session.get(ReferralProgramBlock, user.id)
                    if block:
                        block.reason = reason[:512]
                    else:
                        db_session.add(
                            ReferralProgramBlock(user_id=user.id, reason=reason[:512])
                        )
                    message = "рефералка заблокирована"
                else:
                    db_session.execute(
                        sa_delete(ReferralProgramBlock).where(
                            ReferralProgramBlock.user_id == user.id
                        )
                    )
                    message = "рефералка разблокирована"
                db_session.commit()
                applied += 1
                results.append(
                    {"token": token, "username": user.username, "ok": True,
                     "message": message}
                )
            except Exception as error:
                db_session.rollback()
                logging.exception("bulk action %s failed for %s", action, user.username)
                results.append(
                    {"token": token, "username": user.username, "ok": False,
                     "message": str(error)[:200]}
                )

        if not dry_run:
            admin_audit_write(
                db_session,
                request,
                "bulk_" + action,
                target=f"{applied}/{len(resolved)} users",
                days=days if action == "extend_days" else None,
                missing=len(missing),
            )
            db_session.commit()

        return JsonResponse(
            {
                "status": "ok",
                "result": {
                    "dry_run": dry_run,
                    "applied": applied,
                    "total": len(resolved),
                    "missing": missing,
                    "rows": results,
                },
            }
        )
    finally:
        db_session.close()


# === Этап 5 плана админки: промокоды и купоны ================================

PROMO_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без похожих символов
PROMO_BATCH_MAX_CODES = 500


def generate_promo_code(prefix=""):
    import secrets

    body = "".join(secrets.choice(PROMO_CODE_ALPHABET) for _ in range(8))
    return (prefix + body)[:64].upper()


def admin_promo_payload(promo, bot_username):
    return {
        "id": promo.id,
        "code": promo.code,
        "deep_link": f"https://t.me/{bot_username}?start=promo_{promo.code}",
        "batch_id": promo.batch_id,
        "promo_type": promo.promo_type,
        "value": promo.value,
        "max_uses": promo.max_uses,
        "used_count": promo.used_count,
        "first_purchase_only": bool(promo.first_purchase_only),
        "valid_until": admin_date_label(promo.valid_until),
        "is_active": bool(promo.is_active),
        "comment": promo.comment,
        "created_by": promo.created_by,
        "created_at": admin_date_label(promo.created_at),
    }


def support_admin_api_promocodes(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response

    bot_username = settings.TG_BOT_USERNAME
    db_session = session_factory()
    try:
        if request.method == "GET":
            if request.GET.get("action") == "batch_links":
                try:
                    batch_id = int(request.GET.get("batch_id") or 0)
                except ValueError:
                    return JsonResponse({"status": "error"}, status=400)
                batch = db_session.get(PromoBatch, batch_id)
                if not batch:
                    return JsonResponse({"status": "not_found"}, status=404)
                codes = (
                    db_session.query(PromoCode)
                    .filter(PromoCode.batch_id == batch_id)
                    .order_by(PromoCode.id)
                    .all()
                )
                lines = [
                    f"{promo.code}\thttps://t.me/{bot_username}?start=promo_{promo.code}"
                    for promo in codes
                ]
                response = HttpResponse(
                    "\n".join(lines), content_type="text/plain; charset=utf-8"
                )
                response["Content-Disposition"] = (
                    f"attachment; filename=promo_batch_{batch_id}.txt"
                )
                return response

            promos = (
                db_session.query(PromoCode)
                .order_by(PromoCode.created_at.desc())
                .limit(300)
                .all()
            )
            batches = (
                db_session.query(PromoBatch)
                .order_by(PromoBatch.created_at.desc())
                .limit(50)
                .all()
            )
            batch_counts = dict(
                db_session.execute(
                    sa_text(
                        """
                        SELECT batch_id, count(*) FROM promo_codes
                        WHERE batch_id IS NOT NULL GROUP BY batch_id
                        """
                    )
                ).all()
            )
            batch_used = dict(
                db_session.execute(
                    sa_text(
                        """
                        SELECT batch_id, coalesce(sum(used_count), 0)
                        FROM promo_codes
                        WHERE batch_id IS NOT NULL GROUP BY batch_id
                        """
                    )
                ).all()
            )
            return JsonResponse(
                {
                    "status": "ok",
                    "result": {
                        "codes": [
                            admin_promo_payload(promo, bot_username)
                            for promo in promos
                            if promo.batch_id is None
                        ],
                        "batches": [
                            {
                                "id": batch.id,
                                "name": batch.name,
                                "comment": batch.comment,
                                "codes": int(batch_counts.get(batch.id, 0)),
                                "used": int(batch_used.get(batch.id, 0)),
                                "created_at": admin_date_label(batch.created_at),
                            }
                            for batch in batches
                        ],
                    },
                }
            )

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        action = request.POST.get("action")

        def parse_common():
            promo_type = request.POST.get("promo_type")
            if promo_type not in ("days", "discount"):
                raise ValueError("Тип: days или discount")
            value = int(request.POST.get("value") or 0)
            if promo_type == "days" and not 1 <= value <= 365:
                raise ValueError("Дни: от 1 до 365")
            if promo_type == "discount" and not 1 <= value <= 99:
                raise ValueError("Скидка: от 1 до 99%")
            max_uses = int(request.POST.get("max_uses") or 0)
            if max_uses < 0:
                raise ValueError("Лимит не может быть отрицательным")
            valid_until = None
            raw_until = (request.POST.get("valid_until") or "").strip()
            if raw_until:
                valid_until = datetime.fromisoformat(raw_until)
            return promo_type, value, max_uses, valid_until

        if action == "create":
            try:
                promo_type, value, max_uses, valid_until = parse_common()
            except ValueError as error:
                return JsonResponse(
                    {"status": "error", "message": str(error)}, status=400
                )
            code = (request.POST.get("code") or "").strip().upper()[:64]
            if not code:
                code = generate_promo_code()
            if db_session.query(PromoCode).filter(PromoCode.code == code).first():
                return JsonResponse(
                    {"status": "error", "message": "Такой код уже существует"},
                    status=400,
                )
            promo = PromoCode(
                code=code,
                promo_type=promo_type,
                value=value,
                max_uses=max_uses,
                first_purchase_only=(request.POST.get("first_purchase_only") == "1"),
                valid_until=valid_until,
                comment=(request.POST.get("comment") or "").strip()[:512] or None,
                created_by=str(support_admin_actor(request))[:128],
            )
            db_session.add(promo)
            admin_audit_write(
                db_session, request, "promo_create", target=code,
                promo_type=promo_type, value=value,
            )
            db_session.commit()
            return JsonResponse(
                {"status": "ok", "result": admin_promo_payload(promo, bot_username)}
            )

        if action == "create_batch":
            try:
                promo_type, value, _max_uses, valid_until = parse_common()
                count = int(request.POST.get("count") or 0)
            except ValueError as error:
                return JsonResponse(
                    {"status": "error", "message": str(error)}, status=400
                )
            if not 1 <= count <= PROMO_BATCH_MAX_CODES:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": f"Число кодов: от 1 до {PROMO_BATCH_MAX_CODES}",
                    },
                    status=400,
                )
            name = (request.POST.get("name") or "").strip()[:128]
            if not name:
                return JsonResponse(
                    {"status": "error", "message": "Назовите партию"}, status=400
                )
            batch = PromoBatch(
                name=name,
                comment=(request.POST.get("comment") or "").strip()[:512] or None,
                created_by=str(support_admin_actor(request))[:128],
            )
            db_session.add(batch)
            db_session.flush()
            existing_codes = {
                row[0] for row in db_session.query(PromoCode.code).all()
            }
            created = 0
            while created < count:
                code = generate_promo_code()
                if code in existing_codes:
                    continue
                existing_codes.add(code)
                db_session.add(
                    PromoCode(
                        code=code,
                        batch_id=batch.id,
                        promo_type=promo_type,
                        value=value,
                        max_uses=1,  # купоны партии всегда одноразовые
                        first_purchase_only=(
                            request.POST.get("first_purchase_only") == "1"
                        ),
                        valid_until=valid_until,
                        created_by=str(support_admin_actor(request))[:128],
                    )
                )
                created += 1
            admin_audit_write(
                db_session, request, "promo_batch_create", target=name,
                count=count, promo_type=promo_type, value=value,
            )
            db_session.commit()
            return JsonResponse({"status": "ok", "result": {"batch_id": batch.id}})

        if action == "toggle":
            promo = db_session.get(PromoCode, int(request.POST.get("id") or 0))
            if not promo:
                return JsonResponse({"status": "not_found"}, status=404)
            promo.is_active = not promo.is_active
            admin_audit_write(
                db_session, request,
                "promo_enable" if promo.is_active else "promo_disable",
                target=promo.code,
            )
            db_session.commit()
            return JsonResponse(
                {"status": "ok", "result": admin_promo_payload(promo, bot_username)}
            )

        return JsonResponse(
            {"status": "error", "message": "Неизвестное действие"}, status=400
        )
    finally:
        db_session.close()


# === Этап 6 плана админки: персональные аккаунты сотрудников =================

ADMIN_ACCOUNT_ROLES = ("full", "marketer", "support")


def support_admin_api_accounts(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "GET":
            accounts = (
                db_session.query(AdminAccount).order_by(AdminAccount.login).all()
            )
            return JsonResponse(
                {
                    "status": "ok",
                    "result": [
                        {
                            "id": account.id,
                            "login": account.login,
                            "display_name": account.display_name,
                            "role": account.role,
                            "is_active": bool(account.is_active),
                            "created_at": admin_date_label(account.created_at),
                            "last_login_at": admin_date_label(account.last_login_at),
                        }
                        for account in accounts
                    ],
                }
            )

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        action = request.POST.get("action")

        if action == "create":
            login_name = (request.POST.get("login") or "").strip().lower()[:64]
            password = request.POST.get("password") or ""
            role = request.POST.get("role") or ""
            if not login_name or not login_name.isidentifier():
                return JsonResponse(
                    {"status": "error", "message": "Логин: латиница/цифры/подчёркивание"},
                    status=400,
                )
            if len(password) < 8:
                return JsonResponse(
                    {"status": "error", "message": "Пароль: минимум 8 символов"},
                    status=400,
                )
            if role not in ADMIN_ACCOUNT_ROLES:
                return JsonResponse(
                    {"status": "error", "message": "Роль: full, marketer или support"},
                    status=400,
                )
            if (
                db_session.query(AdminAccount)
                .filter(AdminAccount.login == login_name)
                .first()
            ):
                return JsonResponse(
                    {"status": "error", "message": "Логин уже занят"}, status=400
                )
            account = AdminAccount(
                login=login_name,
                password_hash=make_password(password),
                display_name=(request.POST.get("display_name") or "").strip()[:128]
                or None,
                role=role,
            )
            db_session.add(account)
            admin_audit_write(
                db_session, request, "admin_account_create", target=login_name,
                role=role,
            )
            db_session.commit()
            return JsonResponse({"status": "ok"})

        account = db_session.get(AdminAccount, int(request.POST.get("id") or 0))
        if not account:
            return JsonResponse({"status": "not_found"}, status=404)

        if action == "toggle":
            account.is_active = not account.is_active
            admin_audit_write(
                db_session, request,
                "admin_account_enable" if account.is_active else "admin_account_disable",
                target=account.login,
            )
            db_session.commit()
            return JsonResponse({"status": "ok"})

        if action == "set_role":
            role = request.POST.get("role") or ""
            if role not in ADMIN_ACCOUNT_ROLES:
                return JsonResponse(
                    {"status": "error", "message": "Роль: full, marketer или support"},
                    status=400,
                )
            account.role = role
            admin_audit_write(
                db_session, request, "admin_account_set_role",
                target=account.login, role=role,
            )
            db_session.commit()
            return JsonResponse({"status": "ok"})

        if action == "set_password":
            password = request.POST.get("password") or ""
            if len(password) < 8:
                return JsonResponse(
                    {"status": "error", "message": "Пароль: минимум 8 символов"},
                    status=400,
                )
            account.password_hash = make_password(password)
            admin_audit_write(
                db_session, request, "admin_account_set_password",
                target=account.login,
            )
            db_session.commit()
            return JsonResponse({"status": "ok"})

        return JsonResponse(
            {"status": "error", "message": "Неизвестное действие"}, status=400
        )
    finally:
        db_session.close()
