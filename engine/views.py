import os
import uuid
import ipaddress
from types import SimpleNamespace
import hmac
import hashlib
import logging
import threading
from time import monotonic as _monotonic
import math
import base64
import json
import resend
import secrets
import warnings
from PIL import Image, UnidentifiedImageError
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
from urllib.parse import quote
from django.http import HttpResponse
from django.http import JsonResponse
from django.http import FileResponse
from django.http import Http404
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.shortcuts import render
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.contrib.auth import SESSION_KEY
from django.contrib.auth import BACKEND_SESSION_KEY
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.auth.decorators import login_required
from django.middleware.csrf import rotate_token
from django.core.mail import send_mail
from django.core import signing
from django.core.signing import BadSignature
from django.core.signing import SignatureExpired
from django.urls import reverse
from django.templatetags.static import static
from django.template.loader import render_to_string
from django.utils.text import get_valid_filename
from django.utils.html import strip_tags
from django.utils.cache import parse_etags
from sqlalchemy import func
from sqlalchemy import and_
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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import ProgrammingError
from common.managed_traffic_limits import APPLIED_BY_BACKFILL
from common.managed_traffic_limits import APPLIED_BY_SITE_ADMIN_PREFIX
from common.managed_traffic_limits import APPLIED_BY_SITE_BULK
from common.managed_traffic_limits import EVENT_TRAFFIC_LIMIT_APPLIED
from common.managed_traffic_limits import EVENT_TRAFFIC_LIMIT_RELEASED
from common.managed_traffic_limits import REASON_TRIAL
from common.managed_traffic_limits import RELEASE_ON_PAYMENT
from common.managed_traffic_limits import delete_managed_limit
from common.managed_traffic_limits import get_managed_limit
from common.managed_traffic_limits import is_managed
from common.managed_traffic_limits import releasable_on_payment
from common.managed_traffic_limits import resolve_managed_limit
from common.managed_traffic_limits import upsert_managed_limit
from common.models.db import User
from engine.email_change import issue_email_change, consume_email_change, sync_email_change, email_change_already_applied, has_fresh_unused_email_change
from common.models.db import TemporarySquadBan
from common.models.db import TrafficSource
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
from common.models.db import PurchaseLoginToken, WebsitePaymentAttempt
from engine.report_cache import report_response
from engine.checkout_attempts import fingerprint_for, find_reusable_attempt, persist_before_send, finish_attempt, mark_attempt_failed
from common.models.db import ClientUaRule
from common.models.db import CustomConfigTemplate
from common.models.db import SupportTicket
from common.models.db import SupportTicketMessage
from common.models.db import SupportTicketMessageSender
from common.models.db import SupportTicketStatus
from common.models.db import SupportTicketAttachment
from common.models.db import SupportReplyTemplate
from common.models.db import SystemSetting
from common.models.db import IpAlert
from common.models.db import UserIpObservation
from django.contrib.auth.hashers import check_password
from django.contrib.auth.hashers import make_password
from common.models.db import AdminAccount
from common.models.db import AdminAuditLog
from common.models.db import Broadcast
from common.models.db import BroadcastDelivery
from common.models.segments import ADMIN_SEGMENTS
from common.models.segments import PAYS_EXISTS_SQL
from common.models.segments import segment_count_sql
from common.models.segments import segments_counts_sql
from common.models.segments import segment_where_sql
from common.models.segments import user_never_paid_from_row
from common.models.db import AdminDirectMessage
from common.models.db import AdminDirectMessageDelivery
from common.models.db import RwmsSyncMismatch
from common.models.db import UserBlock
from common.models.db import PromoCodeUse
from common.models.db import UserDiscount
from common.models.db import PromoBatch
from common.models.db import PromoCode
from common.models.db import ReferralProgramBlock
from engine.bot_push import push_admin_temporary_ban
from common.models.db import CensorCheck
from common.models.db import CensorCheckRun
from common.models.db import RipeApiKey
from common.models.db import InfraServerIp
from common.models.db import NodeInstallScript
from common.models.db import NodeProvisionRequest
from common.models.db import NodeProvisionStage
from common.models.db import NodeProvisionStatus
from engine.user_block import ACCOUNT_BLOCKED_MESSAGE
from engine.user_block import is_user_blocked
from common.models.settings import BOOL_RUNTIME_SETTINGS
from common.models.settings import BOT_APPLE_RECOMMENDED_APP_SETTING
from common.models.settings import BOT_TRIAL_PERIOD_DAYS_SETTING
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
from common.models.settings import DEFAULT_IPGUARD_ALERTS_ENABLED
from common.models.settings import DEFAULT_IPGUARD_ALERT_COOLDOWN_HOURS
from common.models.settings import DEFAULT_IPGUARD_ALERT_SEGMENT
from common.models.settings import DEFAULT_IPGUARD_SUBNETS_PER_HWID
from common.models.settings import DEFAULT_IPGUARD_WINDOW_HOURS
from common.models.settings import DEFAULT_IPGUARD_WARNINGS_ENABLED
from common.models.settings import DEFAULT_IPGUARD_WARNING_SUBNETS_PER_HWID
from common.models.settings import DEFAULT_IPGUARD_BURST_ENABLED
from common.models.settings import DEFAULT_IPGUARD_BURST_WINDOW_MINUTES
from common.models.settings import DEFAULT_IPGUARD_BURST_IPS_PER_HWID
from common.models.settings import DEFAULT_IPGUARD_BURST_CONFIRMATIONS
from common.models.settings import DEFAULT_IPGUARD_BURST_MIN_SUBNETS
from common.models.settings import DEFAULT_IPGUARD_GEO_ENABLED
from common.models.settings import DEFAULT_IPGUARD_GEO_WINDOW_MINUTES
from common.models.settings import DEFAULT_IPGUARD_GEO_MIN_REGIONS
from common.models.settings import DEFAULT_IPGUARD_AUTOBAN_ENABLED
from common.models.settings import DEFAULT_IPGUARD_AUTOBAN_SEGMENT
from common.models.settings import DEFAULT_IPGUARD_AUTOBAN_ESCALATION_WINDOW_HOURS
from common.models.settings import DEFAULT_IPGUARD_AUTOBAN_MAX_PER_HOUR
# Порог автобана — одно абсолютное число по суточному слою, гистерезис по
# прогонам и пробный режим (бан не выполняется, только помечается в алерте).
from common.models.settings import DEFAULT_IPGUARD_AUTOBAN_MIN_SUBNETS
from common.models.settings import DEFAULT_IPGUARD_AUTOBAN_CONFIRMATIONS
from common.models.settings import DEFAULT_IPGUARD_AUTOBAN_DRY_RUN
from common.models.settings import DEFAULT_IPGUARD_MAX_ALERTS_PER_HOUR
# Отдельный тумблер суточного слоя и период прогона детектора: без них по
# админке нельзя понять, какой слой молчит и как часто слои вообще смотрят
# на данные.
from common.models.settings import DEFAULT_IPGUARD_SUBNETS_ENABLED
from common.models.settings import DEFAULT_IPGUARD_CHECK_INTERVAL_SECONDS
from common.models.settings import IPGUARD_ALERTS_ENABLED_SETTING
from common.models.settings import IPGUARD_ALERT_COOLDOWN_HOURS_SETTING
from common.models.settings import IPGUARD_ALERT_SEGMENT_ALL
from common.models.settings import IPGUARD_ALERT_SEGMENT_NEVER_PAID
from common.models.settings import IPGUARD_ALERT_SEGMENT_SETTING
from common.models.settings import IPGUARD_SUBNETS_ENABLED_SETTING
from common.models.settings import IPGUARD_CHECK_INTERVAL_SECONDS_SETTING
from common.models.settings import IPGUARD_SUBNETS_PER_HWID_SETTING
from common.models.settings import IPGUARD_WINDOW_HOURS_SETTING
from common.models.settings import IPGUARD_WARNINGS_ENABLED_SETTING
from common.models.settings import IPGUARD_WARNING_SUBNETS_PER_HWID_SETTING
from common.models.settings import IPGUARD_BURST_ENABLED_SETTING
from common.models.settings import IPGUARD_BURST_WINDOW_MINUTES_SETTING
from common.models.settings import IPGUARD_BURST_IPS_PER_HWID_SETTING
from common.models.settings import IPGUARD_BURST_CONFIRMATIONS_SETTING
from common.models.settings import IPGUARD_BURST_MIN_SUBNETS_SETTING
from common.models.settings import IPGUARD_GEO_ENABLED_SETTING
from common.models.settings import IPGUARD_GEO_WINDOW_MINUTES_SETTING
from common.models.settings import IPGUARD_GEO_MIN_REGIONS_SETTING
from common.models.settings import IPGUARD_AUTOBAN_ENABLED_SETTING
from common.models.settings import IPGUARD_AUTOBAN_SEGMENT_SETTING
from common.models.settings import IPGUARD_AUTOBAN_STEPS_MINUTES_SETTING
from common.models.settings import IPGUARD_AUTOBAN_ESCALATION_WINDOW_HOURS_SETTING
from common.models.settings import IPGUARD_AUTOBAN_MAX_PER_HOUR_SETTING
from common.models.settings import IPGUARD_AUTOBAN_MIN_SUBNETS_SETTING
from common.models.settings import IPGUARD_AUTOBAN_CONFIRMATIONS_SETTING
from common.models.settings import IPGUARD_AUTOBAN_DRY_RUN_SETTING
from common.models.settings import IPGUARD_AUTOBAN_MAX_STEPS
from common.models.settings import IPGUARD_AUTOBAN_STEP_MINUTES_MAX
from common.models.settings import IPGUARD_MAX_ALERTS_PER_HOUR_SETTING
from common.models.settings import IPGUARD_EXCLUDED_IPS_SETTING
from common.models.settings import IPGUARD_EXCLUDED_USERNAMES_SETTING
from common.models.settings import IPGUARD_MAX_EXCLUDED_ENTRIES
from common.models.settings import IPGUARD_MAX_EXCLUDED_USERNAMES
from common.models.settings import ipguard_warning_threshold_is_valid
from common.models.settings import parse_ipguard_autoban_steps
from common.models.settings import parse_ipguard_excluded_ips
from common.models.settings import parse_ipguard_excluded_usernames
from common.runtime_tariffs import resolve_runtime_tariffs
from engine.request_ip import client_ip
from engine.rate_limit import rate_limit_exceeded
from engine.rate_limit import rate_limit_hit
from engine.rate_limit import rate_limit_log_digest
from engine.rate_limit import rate_limit_peek
from common.models.settings import TRIAL_TRAFFIC_LIMIT_ENABLED_SETTING
from common.models.settings import TRIAL_TRAFFIC_LIMIT_GB_SETTING
from common.models.settings import TRIAL_TRAFFIC_LIMIT_STRATEGY_DAY
from common.models.settings import TRIAL_TRAFFIC_LIMIT_STRATEGY_MONTH
from common.models.settings import TRIAL_TRAFFIC_LIMIT_STRATEGY_MONTH_ROLLING
from common.models.settings import TRIAL_TRAFFIC_LIMIT_STRATEGY_NO_RESET
from common.models.settings import TRIAL_TRAFFIC_LIMIT_STRATEGY_PROTO_NAMES
from common.models.settings import TRIAL_TRAFFIC_LIMIT_STRATEGY_SETTING
from common.models.settings import TRIAL_TRAFFIC_LIMIT_STRATEGY_WEEK
from common.models.settings import normalize_ipguard_alert_segment
from common.models.settings import parse_bool_setting
from common.models.settings import parse_positive_int_setting
from common.models import analytics_event
from common.models.tariff import Tariff
from common.models.tariff import TrialPromotionTariff
from common.models.tariff import OneDayTariff
from common.models.tariff import OneMonthTariff
from common.models.tariff import OneYearTariff
from common.models.tariff import ThreeMonthsTariff
from common.rwms_client import RwmsUnavailableError
from common.rwms_client_sync import RwmsClientSync

from . import node_traffic
from . import node_provisioning
from . import ripe_atlas
from . import infra
from .rwms_helpers import RwmsSubscriptionOwnershipError
from .rwms_helpers import trial_traffic_limit_configured
from .rwms_helpers import trial_traffic_limit_enabled
from .rwms_helpers import trial_traffic_limit_for_new_trial
from .rwms_helpers import trial_traffic_limit_for_user
from .rwms_helpers import add_traffic_limit_event
from .rwms_helpers import adopted_trial_limit
from .rwms_helpers import managed_limits_table_available
from .rwms_helpers import panel_limit_matches
from .rwms_helpers import record_trial_limit_marker
from .rwms_helpers import assert_subscription_owned_by_email
from .rwms_helpers import assert_subscription_owned_by_telegram_id
from .rwms_helpers import create_user
from .rwms_helpers import create_user_until
from .rwms_helpers import deterministic_username
from .rwms_helpers import get_proto_optional
from .rwms_helpers import is_valid_email
from .rwms_helpers import normalize_email
from .rwms_helpers import usable_panel_email
from .encrypt_happ_url import encrypt_happ_url1
from .incy import IncyEncoderError
from .incy import encrypt_incy_url
from .sql_helpers import lock_registration_email
from .sql_helpers import lock_registration_telegram_id
from .sql_helpers import save_wata_invoice
from .sql_helpers import user_never_paid

from database import session_factory
from engine.payments import create_yk_payment_sync
from engine.payments import create_wata_payment_sync
from engine.payments import fetch_wata_transaction_status
from engine.payments import ProviderRejected
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

# Дедлайн по умолчанию для каждого RPC сайта; массовые вызовы (GetAllUsers,
# CreateNode) передают RWMS_BULK_RPC_TIMEOUT_SECONDS явно.
rwms_client = RwmsClientSync(
    settings.RWMS_HOST,
    settings.RWMS_PORT,
    timeout=settings.RWMS_RPC_TIMEOUT_SECONDS,
)
SUPPORT_ADMIN_SESSION_KEY = "support_admin_authenticated"
SUPPORT_ADMIN_ROLE_SESSION_KEY = "support_admin_role"
SUPPORT_ADMIN_ROLE_ADMIN = "admin"
SUPPORT_ADMIN_ROLE_SUPPORT = "support"
SUPPORT_ADMIN_ROLE_MARKETER = "marketer"
SUPPORT_ADMIN_ACCOUNT_SESSION_KEY = "support_admin_account"
SUPPORT_ADMIN_AUTH_HASH_SESSION_KEY = "support_admin_auth_hash"
SUPPORT_ATTACHMENT_IMAGE_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
SUPPORT_ATTACHMENT_VIDEO_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
}
SUPPORT_ATTACHMENT_ALLOWED_TYPES = (
    SUPPORT_ATTACHMENT_IMAGE_TYPES | SUPPORT_ATTACHMENT_VIDEO_TYPES
)
EMAIL_CONFIRMATION_SALT = "dashboard-email-confirmation"
EMAIL_CONFIRMATION_MAX_AGE_SECONDS = 15 * 60
SITE_REGISTRATION_CONFIRMATION_SALT = "site-registration-confirmation"
SITE_REGISTRATION_CONFIRMATION_MAX_AGE_SECONDS = 15 * 60
TELEGRAM_BIND_TOKEN_MAX_AGE_SECONDS = 15 * 60
# Ссылку из сессии показываем повторно только первые 5 минут: TTL у бота —
# 15 минут (TELEGRAM_BIND_TOKEN_TTL_MINUTES), поэтому любая показанная в
# кабинете ссылка проживёт в боте ещё не меньше 10 минут.
TELEGRAM_BIND_TOKEN_REUSE_SECONDS = 5 * 60
TELEGRAM_BIND_SESSION_TOKEN_KEY = "telegram_bind_token"
TELEGRAM_BIND_SESSION_CREATED_KEY = "telegram_bind_token_created_at"
PURCHASE_LOGIN_TOKEN_PREFIX = "plogin_"
PURCHASE_STATUS_TOKEN_PREFIX = "pstatus_"


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


def get_all_telegram_auth_bots(host=None):
    """Все доверенные Telegram-боты для проверки подписи Mini App initData.

    Кнопка Mini App у vpn- и vps-бота ведёт на один и тот же кабинетный
    домен (runtime-настройка webapp_url общая), а initData подписан токеном
    того бота, из которого открыли приложение. Проверять подпись только
    ботом, привязанным к домену, нельзя — вход из «не доменного» бота падал
    бы с ложным «Не удалось подтвердить вход». Возвращает ботов без дублей,
    первым — привязанного к домену (частый случай, меньше лишних HMAC).
    """
    bots = []
    seen_tokens = set()

    candidates = []
    domain_bot = get_telegram_auth_bot(host) if host else None
    if domain_bot:
        candidates.append(domain_bot)
    candidates.extend(settings.TELEGRAM_AUTH_BOTS.values())
    if settings.TG_BOT_USERNAME and settings.TELEGRAM_AUTH_BOT_TOKEN:
        candidates.append(
            {
                "username": settings.TG_BOT_USERNAME.lstrip("@"),
                "token": settings.TELEGRAM_AUTH_BOT_TOKEN,
            }
        )

    for bot in candidates:
        if bot["token"] in seen_tokens:
            continue
        seen_tokens.add(bot["token"])
        bots.append(bot)

    return bots


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
        if content_type not in SUPPORT_ATTACHMENT_ALLOWED_TYPES:
            logging.warning(
                "unsupported support attachment content type %s", content_type
            )
            continue

        if uploaded_file.size > settings.SUPPORT_ATTACHMENT_MAX_BYTES:
            logging.warning("support attachment %s is too large", uploaded_file.name)
            continue

        if not support_attachment_signature_matches(uploaded_file, content_type):
            logging.warning(
                "support attachment signature does not match content type: name=%s type=%s",
                uploaded_file.name,
                content_type,
            )
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


def validate_support_attachments(uploaded_files):
    """Return a user-facing validation error before opening a DB transaction."""
    files = list(uploaded_files or [])
    max_files = max(0, settings.SUPPORT_ATTACHMENT_MAX_FILES)
    if len(files) > max_files:
        return f"Можно прикрепить не больше {max_files} файлов."

    total_size = sum(max(0, int(getattr(item, "size", 0) or 0)) for item in files)
    if total_size > settings.SUPPORT_ATTACHMENT_TOTAL_MAX_BYTES:
        total_limit_mb = settings.SUPPORT_ATTACHMENT_TOTAL_MAX_BYTES // (1024 * 1024)
        return f"Общий размер вложений превышает {total_limit_mb} МБ."

    for uploaded_file in files:
        content_type = uploaded_file.content_type or "application/octet-stream"
        if content_type not in SUPPORT_ATTACHMENT_ALLOWED_TYPES:
            return f"Формат файла «{uploaded_file.name}» не поддерживается."
        if uploaded_file.size > settings.SUPPORT_ATTACHMENT_MAX_BYTES:
            file_limit_mb = settings.SUPPORT_ATTACHMENT_MAX_BYTES // (1024 * 1024)
            return f"Файл «{uploaded_file.name}» превышает {file_limit_mb} МБ."
        if not support_attachment_signature_matches(uploaded_file, content_type):
            return f"Содержимое файла «{uploaded_file.name}» не соответствует формату."
    return None


def support_attachment_validation_response(request, message):
    if is_ajax(request):
        return JsonResponse({"status": "error", "message": message}, status=400)
    return HttpResponse(message, status=400, content_type="text/plain; charset=utf-8")


def support_attachment_signature_matches(uploaded_file, content_type):
    """Verify the small set of media signatures accepted by support uploads."""
    try:
        position = uploaded_file.tell()
    except (AttributeError, OSError):
        position = 0
    try:
        header = uploaded_file.read(16)
        uploaded_file.seek(position)
    except (AttributeError, OSError):
        return False

    if content_type.startswith("image/"):
        expected = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}.get(content_type)
        if not expected:
            return False
        try:
            uploaded_file.seek(0)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(uploaded_file) as picture:
                    width, height = picture.size
                    if picture.format != expected or width <= 0 or height <= 0 or width * height > 20_000_000:
                        return False
                    picture.verify()
                # verify() alone is a no-op for some decoders (including JPEG).
                uploaded_file.seek(0)
                with Image.open(uploaded_file) as picture:
                    frames = getattr(picture, "n_frames", 1)
                    if frames > 200 or width * height * frames > 80_000_000:
                        return False
                    for index in range(frames):
                        picture.seek(index)
                        if picture.width * picture.height > 20_000_000:
                            return False
                        picture.load()
            return True
        except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            return False
        except Exception as error:
            # Повреждённый файл роняет декодеры Pillow и другими исключениями
            # (у GIF — IndexError/struct.error из n_frames/seek). Это битый
            # файл, а не сбой сервера: отклоняем с понятной ошибкой 400.
            logging.warning(
                "support attachment rejected: image decoder failed "
                "content_type=%s error=%s",
                content_type,
                type(error).__name__,
            )
            return False
        finally:
            uploaded_file.seek(position)
    if content_type in {"video/mp4", "video/quicktime"}:
        return len(header) >= 12 and header[4:8] == b"ftyp"
    if content_type == "video/webm":
        return header.startswith(b"\x1aE\xdf\xa3")
    return False


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
    return attachment.content_type in SUPPORT_ATTACHMENT_IMAGE_TYPES


def is_video_attachment(attachment):
    return attachment.content_type in SUPPORT_ATTACHMENT_VIDEO_TYPES


def support_attachment_file_response(attachment, path):
    """Serve known media inline and force legacy/unknown files to download."""
    is_safe_inline = attachment.content_type in SUPPORT_ATTACHMENT_ALLOWED_TYPES
    if is_safe_inline and settings.SUPPORT_ATTACHMENT_X_ACCEL_REDIRECT:
        response = HttpResponse(content_type=attachment.content_type)
        # Путь кодируется: не-ASCII заголовок Django MIME-кодирует
        # (=?utf-8?b?…?=), а latin-1 уходит сырыми байтами — nginx в обоих
        # случаях отдаёт 404. %XX nginx раскодирует сам; get_valid_filename
        # вырезает '%', '?' и '/', так что двойного раскодирования нет.
        response["X-Accel-Redirect"] = "/_protected_support_media/" + quote(
            attachment.storage_path.lstrip("/"), safe="/"
        )
        response["Content-Disposition"] = "inline"
        response["X-Content-Type-Options"] = "nosniff"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        return response
    response = FileResponse(
        path.open("rb"),
        as_attachment=not is_safe_inline,
        content_type=(
            attachment.content_type if is_safe_inline else "application/octet-stream"
        ),
        filename=attachment.file_name,
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    if not is_safe_inline:
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


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
        SUPPORT_ADMIN_ROLE_SUPPORT,
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
SUPPORT_ADMIN_ACCOUNT_ROLE_MAP = {
    "full": SUPPORT_ADMIN_ROLE_ADMIN,
    "marketer": SUPPORT_ADMIN_ROLE_MARKETER,
    "support": SUPPORT_ADMIN_ROLE_SUPPORT,
}


def support_admin_auth_fingerprint(identity, credential):
    return hmac.new(
        settings.SECRET_KEY.encode(),
        f"{identity}:{credential}".encode(),
        hashlib.sha256,
    ).hexdigest()


def clear_support_admin_session(request):
    for key in (
        SUPPORT_ADMIN_SESSION_KEY,
        SUPPORT_ADMIN_ROLE_SESSION_KEY,
        SUPPORT_ADMIN_ACCOUNT_SESSION_KEY,
        SUPPORT_ADMIN_AUTH_HASH_SESSION_KEY,
    ):
        request.session.pop(key, None)
    request.session.modified = True


class SupportAdminSessionCheckUnavailable(Exception):
    """Сессию админки не удалось проверить (БД недоступна) — это не отзыв прав."""


def support_admin_unavailable_response(request):
    message = "База данных временно недоступна. Обновите страницу через минуту."
    path = getattr(request, "path", "") or ""
    headers = getattr(request, "headers", None) or {}
    wants_json = (
        "/api/" in path
        or path.endswith("-json/")
        or headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (headers.get("Accept") or "")
    )
    if wants_json:
        response = JsonResponse({"status": "error", "message": message}, status=503)
    else:
        response = HttpResponse(
            message, status=503, content_type="text/plain; charset=utf-8"
        )
    response["Retry-After"] = "30"
    return response


def validate_support_admin_session(request):
    """Refresh role/active state and revoke sessions after credential changes.

    Raises SupportAdminSessionCheckUnavailable when the account cannot be read.
    """
    account_login = request.session.get(SUPPORT_ADMIN_ACCOUNT_SESSION_KEY)
    stored_fingerprint = request.session.get(SUPPORT_ADMIN_AUTH_HASH_SESSION_KEY, "")
    if account_login:
        db_session = session_factory()
        try:
            account = (
                db_session.query(AdminAccount)
                .filter(AdminAccount.login == account_login)
                .first()
            )
        except Exception:
            logging.exception("failed to validate admin session for %s", account_login)
            raise SupportAdminSessionCheckUnavailable()
        finally:
            db_session.close()

        if not account or not account.is_active:
            return False
        expected = support_admin_auth_fingerprint(
            f"account:{account.login}", account.password_hash
        )
        if not stored_fingerprint or not hmac.compare_digest(
            stored_fingerprint, expected
        ):
            return False
        request.session[SUPPORT_ADMIN_ROLE_SESSION_KEY] = (
            SUPPORT_ADMIN_ACCOUNT_ROLE_MAP.get(
                account.role, SUPPORT_ADMIN_ROLE_SUPPORT
            )
        )
        return True

    role = support_admin_role(request)
    credential = {
        SUPPORT_ADMIN_ROLE_ADMIN: settings.SUPPORT_ADMIN_PASSWORD,
        SUPPORT_ADMIN_ROLE_SUPPORT: settings.SUPPORT_STAFF_PASSWORD,
    }.get(role, "")
    if not credential:
        return False
    expected = support_admin_auth_fingerprint(f"shared:{role}", credential)
    return bool(stored_fingerprint) and hmac.compare_digest(
        stored_fingerprint, expected
    )


def require_support_admin(request):
    if not support_admin_is_authenticated(request):
        return redirect("support_admin_login")

    try:
        session_is_valid = validate_support_admin_session(request)
    except SupportAdminSessionCheckUnavailable:
        # Сбой БД — не отзыв прав: сессию не очищаем, иначе короткий блип
        # Postgres или исчерпание пула разлогинивает всех операторов разом.
        # Очищаем только при подтверждённом отсутствии/деактивации аккаунта
        # или несовпадении fingerprint (validate вернул False).
        return support_admin_unavailable_response(request)

    if not session_is_valid:
        logging.warning(
            "support admin session revoked: account=%s role=%s",
            request.session.get(SUPPORT_ADMIN_ACCOUNT_SESSION_KEY),
            request.session.get(SUPPORT_ADMIN_ROLE_SESSION_KEY),
        )
        clear_support_admin_session(request)
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


def build_apple_subscription_link(
    db_session, subscription_url, recommended_app=None
):
    """(приложение, ссылка добавления подписки) для iOS/macOS в кабинете.

    Как в боте: INCY получает шифрованную incy://crypt1/-ссылку на базовый
    subscription_url (панель отдаёт по нему тот же конфиг, что раньше был на
    /custom-json). Если энкодер недоступен (нет node в образе и т.п.) — молча
    откатываемся на Happ, кабинет ломать нельзя.
    """
    recommended = (
        recommended_app
        if recommended_app is not None
        else apple_recommended_app_from_db(db_session)
    )
    happ_link = encrypt_happ_url1(subscription_url)
    if recommended != APPLE_RECOMMENDED_APP_INCY:
        return APPLE_RECOMMENDED_APP_HAPP, happ_link

    try:
        return (
            APPLE_RECOMMENDED_APP_INCY,
            encrypt_incy_url(subscription_url),
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


class SiteRegistrationUnavailable(Exception):
    """Регистрацию сайтового пользователя сейчас завершить нельзя.

    Базовый случай — ВРЕМЕННЫЙ: состояние подписки в Remnawave установить не
    удалось, потому что панель недоступна. Тогда НЕЛЬЗЯ ни создавать подписку
    (получим сироту/дубль), ни заводить локальный аккаунт «как будто подписки
    нет» — это разошло бы БД и панель. Вьюхи ловят это и просят пользователя
    повторить позже; регистрация не «ломается навсегда» — следующая попытка
    выведет ТО ЖЕ детерминированное имя и продолжит с того же места.

    Неоднозначное ВЛАДЕНИЕ (найденная подписка или локальная строка явно
    принадлежат другому) — это отдельный, НЕ временный случай: см. наследника
    ``SiteRegistrationOwnershipConflict``.
    """


class SiteRegistrationOwnershipConflict(SiteRegistrationUnavailable):
    """Регистрация упёрлась в чужого владельца — сама собой не рассосётся.

    Подписка под вычисленным именем или строка users под ним принадлежат
    другому email/telegram-аккаунту. Повтор выведет ТО ЖЕ детерминированное имя
    и упрётся в тот же guard, поэтому «повторите через пару минут» здесь —
    неправда: нужен оператор (ALERT в логе уже есть).

    Наследуется от ``SiteRegistrationUnavailable`` намеренно: все существующие
    обработчики (в том числе в соседних ветках кода) продолжают ловить отказ и
    НЕ создают ни подписку, ни локальный аккаунт; отличается только текст и код
    ответа в тех вьюхах, что обрабатывают конфликт явно.
    """


SITE_REGISTRATION_RETRY_MESSAGE = (
    "Сервис временно недоступен, попробуйте позже. "
    "Ваш аккаунт не потерян — повторите вход через пару минут."
)

SITE_REGISTRATION_SUPPORT_MESSAGE = (
    "Не удалось завершить регистрацию автоматически. "
    "Напишите в поддержку — мы вручную свяжем аккаунт с вашей почтой."
)


def rwms_expire_at(rw_user):
    expire_at = get_proto_optional(rw_user, "expire_at")
    if expire_at is None:
        return None

    return expire_at.ToDatetime().replace(tzinfo=None)


def find_rwms_user_by_identity(email=None, telegram_id=None, username=None):
    normalized_email = (email or "").lower().strip()
    if username:
        # Current site identities have a deterministic panel username. One
        # indexed lookup replaces the former full subscription-list download.
        rw_user = rwms_client.get_user_by_username_strict(username)
        if rw_user is not None:
            return rw_user

    if not settings.SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED:
        return None

    logging.warning(
        "legacy RWMS identity scan enabled for email_present=%s telegram_present=%s",
        bool(normalized_email),
        telegram_id is not None,
    )
    users_reply = rwms_client.get_all_users(
        timeout=settings.RWMS_BULK_RPC_TIMEOUT_SECONDS
    )
    if users_reply is None:
        # None — панель не ответила (RpcError, дедлайн), а не «подписки нет».
        # Считать это отсутствием нельзя: пользователь получил бы второй
        # аккаунт, а после оплаты — вторую подписку в Remnawave на тот же
        # email. Регистрацию откладываем, повтор пройдёт тот же путь.
        logging.error(
            "legacy RWMS identity scan got no reply from GetAllUsers; "
            "site registration postponed as ambiguous"
        )
        raise SiteRegistrationUnavailable("legacy RWMS identity scan unavailable")

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
        # Второй рубеж владения (первый — assert_subscription_owned_by_email по
        # email ПАНЕЛЬНОЙ записи). Имя детерминировано от email, поэтому строку
        # users по этому имени можно найти и тогда, когда её владелец давно
        # сменил почту: панель при смене email обновляется best-effort и на
        # блипе RWMS остаётся со старым адресом. Без этой проверки человек,
        # получивший освобождённый адрес A, при регистрации попадал бы в чужой
        # аккаунт (magic-link/purchase-token выдались бы на строку жертвы).
        # Явное расхождение email = неоднозначное состояние: панель не трогаем,
        # регистрацию останавливаем.
        existing_email = normalize_email(user.email)
        requested_email = normalize_email(email)
        if existing_email and requested_email and existing_email != requested_email:
            logging.critical(
                "ALERT: refusing to adopt local user %s (id=%s) for %s: "
                "row email %s does not match requested %s; "
                "panel untouched, registration stopped",
                user.username,
                user.id,
                creation_channel,
                existing_email,
                requested_email,
            )
            raise SiteRegistrationOwnershipConflict(
                f"local user {user.username} belongs to another email"
            )

        existing_telegram_id = user.telegram_id
        requested_telegram_id = telegram_id
        if (
            existing_telegram_id is not None
            and requested_telegram_id is not None
            and int(existing_telegram_id) != int(requested_telegram_id)
        ):
            logging.critical(
                "ALERT: refusing to adopt local user %s (id=%s) for %s: "
                "row telegram_id %s does not match requested %s; "
                "panel untouched, registration stopped",
                user.username,
                user.id,
                creation_channel,
                existing_telegram_id,
                requested_telegram_id,
            )
            raise SiteRegistrationOwnershipConflict(
                f"local user {user.username} belongs to another telegram account"
            )

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
    # Антиабьюз v2: adoption после crash-окна — наш прошлый AddUser поставил
    # лимит, а маркер пропал с транзакцией. Правило backfill: тумблер включён
    # И панель несёт ровно текущий лимит пробных → маркер; иначе панель и
    # маркеры не трогаем (ручной лимит владельца остаётся ручным).
    marker = record_trial_limit_marker(
        db_session, user, adopted_trial_limit(db_session, rw_user)
    )
    add_traffic_limit_event(
        db_session, user.id, EVENT_TRAFFIC_LIMIT_APPLIED, marker, adopted=True
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


def site_registration_username(email, telegram_id=None):
    """Имя подписки для НОВОГО сайтового пользователя.

    Регистрация по email (magic link, Google/Yandex OAuth, форма оплаты) —
    детерминированное имя от email: то же самое, что использует mobile_api, и
    единственное, что закрывает окно «панель создала — БД не успела». Повтор
    регистрации выводит ТО ЖЕ имя, находит подписку и принимает её вместо
    создания второй.

    Регистрация только по telegram_id (виджет/Mini App, email ещё нет) использует
    ``str(telegram_id)`` — тот же стабильный username, что и бот. Это закрывает
    гонку/крэш после AddUser: повтор находит ту же подписку вместо создания
    новой со случайным uuid. Уже существующие legacy-строки с uuid не меняются.

    Существующих пользователей это не касается: функция вызывается ТОЛЬКО при
    создании новой строки users, ни один путь не пересчитывает username
    существующего аккаунта.
    """
    normalized_email = (email or "").strip().lower()
    if normalized_email:
        return deterministic_username(normalized_email)
    if telegram_id is not None:
        return str(int(telegram_id))
    return str(uuid.uuid4().hex)


def resolve_existing_site_subscription(username, email):
    """Строгое чтение подписки ``username`` перед созданием новой.

    Возвращает подписку для adoption либо ``None`` при ДОСТОВЕРНОМ NOT_FOUND.

    - ``RwmsUnavailableError`` → ``SiteRegistrationUnavailable``: недоступность
      панели нельзя трактовать ни как «имя свободно» (создадим дубль), ни как
      «подписка есть» (примем непроверенную).
    - подписка есть, но её email явно чужой → ALERT и
      ``SiteRegistrationOwnershipConflict``: панель не трогаем, регистрацию
      останавливаем, и это НЕ временный отказ (повтор выведет то же имя).
    """
    try:
        existing = rwms_client.get_user_by_username_strict(username)
    except RwmsUnavailableError as error:
        logging.warning(
            "RWMS unavailable while checking username %s for site user %s, "
            "aborting registration: %s",
            username,
            email,
            error,
        )
        raise SiteRegistrationUnavailable(
            f"rwms unavailable for {username}"
        ) from error

    if existing is not None:
        try:
            assert_subscription_owned_by_email(existing, email, flow="site")
        except RwmsSubscriptionOwnershipError as error:
            raise SiteRegistrationOwnershipConflict(str(error)) from error

    return existing


def resolve_existing_telegram_subscription(username, telegram_id):
    """Строго найти и проверить подписку Telegram-only регистрации."""
    try:
        existing = rwms_client.get_user_by_username_strict(username)
    except RwmsUnavailableError as error:
        logging.warning(
            "RWMS unavailable while checking username %s for telegram user %s, "
            "aborting registration: %s",
            username,
            telegram_id,
            error,
        )
        raise SiteRegistrationUnavailable(
            f"rwms unavailable for {username}"
        ) from error

    if existing is not None:
        try:
            assert_subscription_owned_by_telegram_id(
                existing, telegram_id, flow="site_telegram"
            )
        except RwmsSubscriptionOwnershipError as error:
            raise SiteRegistrationOwnershipConflict(str(error)) from error

    return existing


def create_site_user(
    db_session,
    email,
    request,
    telegram_id=None,
    creation_channel="site",
    allow_trial=True,
):
    # allow_trial=False: вызывающий код ещё не проверил владение email
    # (анонимная покупка с лендинга, B12) — пробная подписка в панели не
    # создаётся даже при включённом site trial; дальше тот же путь, что при
    # выключенном trial (strict-поиск существующей подписки или локальный
    # аккаунт без подписки).
    #
    # Лок берём ПЕРВЫМ делом — до чтения контекста и до любых обращений к
    # панели: две параллельные регистрации на один email иначе обе дойдут до
    # AddUser, и подписка проигравшего останется сиротой.
    lock_registration_email(db_session, email)
    lock_registration_telegram_id(db_session, telegram_id)

    context = get_registration_context(request, db_session)

    # Вызывающая view читает users до входа в create_site_user. Пока запрос
    # ждал advisory-lock, конкурент мог уже закоммитить строку. Повторное
    # чтение под локом не даёт дойти до второго AddUser/дублирующего INSERT.
    if email:
        existing_local_user = (
            db_session.query(User).filter(User.email == email).first()
        )
    elif telegram_id is not None:
        existing_local_user = (
            db_session.query(User)
            .filter(User.telegram_id == telegram_id)
            .first()
        )
    else:
        existing_local_user = None

    if existing_local_user is not None:
        same_email_owner = bool(
            email
            and normalize_email(existing_local_user.email) == normalize_email(email)
        )
        same_telegram_owner = bool(
            telegram_id is not None
            and existing_local_user.telegram_id is not None
            and int(existing_local_user.telegram_id) == int(telegram_id)
        )
        if same_email_owner or same_telegram_owner:
            # Конкурент уже полностью создал именно этого владельца, пока мы
            # ждали advisory-lock. Повторного AddUser/INSERT не требуется.
            return existing_local_user
        # Несовпавшую строку нельзя возвращать как владельцу запроса. Для
        # deterministic username продолжаем strict panel lookup: второй guard
        # в sync_local_user_from_rwms оставит ALERT и остановит adoption.

    referrer = context["referrer"]
    username = site_registration_username(email, telegram_id)
    user_label = email or f"telegram_id={telegram_id}"
    create_trial_subscription = allow_trial and should_create_trial_for_channel(
        db_session,
        creation_channel,
    )

    if not create_trial_subscription:
        logging.info(
            "site trial subscription disabled for channel=%s (allow_trial=%s), "
            "creating local account without RWMS subscription",
            creation_channel,
            allow_trial,
        )
        try:
            rw_user = find_rwms_user_by_identity(
                email=email,
                telegram_id=telegram_id,
                username=username,
            )
        except RwmsUnavailableError:
            # FINAL-PAY-01. Регистрация с входом (magic link, OAuth, Telegram)
            # и включённый legacy-скан — fail-closed, как раньше. Анонимная
            # покупка (allow_trial=False) в панель не пишет вовсе: задача
            # payment сходится по детерминированному username (найдёт подписку
            # и продлит или создаст), поэтому, как в HEAD, аккаунт без подписки
            # создаётся, а недоступность панели не блокирует оплату.
            if allow_trial or settings.SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED:
                logging.exception(
                    "failed to recover existing RWMS subscription for site user %s; "
                    "registration stopped to avoid a duplicate local identity",
                    user_label,
                )
                raise SiteRegistrationUnavailable from None
            logging.error(
                "ALERT: RWMS unavailable during anonymous purchase for %s: "
                "local account %s created without panel lookup, payment will "
                "converge the subscription by this username",
                user_label,
                username,
                exc_info=True,
            )
            rw_user = None
        except Exception:
            logging.exception(
                "failed to recover existing RWMS subscription for site user %s; "
                "registration stopped to avoid a duplicate local identity",
                user_label,
            )
            raise SiteRegistrationUnavailable from None

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

    if email:
        # Регистрация по email: имя детерминировано, поэтому перед AddUser
        # можно СТРОГО проверить, не создали ли мы эту подписку в прошлый раз и
        # не упали ли до commit'а. Есть подписка → принимаем её и панель не
        # трогаем (никакого второго AddUser, никакого recreate — существующие
        # клиентские конфиги остаются рабочими). Панель недоступна или подписка
        # явно чужая → SiteRegistrationUnavailable (см. resolve_...).
        adoptable = resolve_existing_site_subscription(username, email)
        if adoptable is not None:
            logging.warning(
                "adopting existing RWMS subscription %s for site user %s "
                "(crash-window recovery, panel untouched, channel=%s)",
                username,
                user_label,
                creation_channel,
            )
            return sync_local_user_from_rwms(
                db_session,
                adoptable,
                email,
                telegram_id,
                context,
                creation_channel,
            )
    elif telegram_id is not None:
        # Telegram-only путь теперь столь же восстанавливаем, как email:
        # стабильный username + strict lookup + ownership guard. Это также
        # принимает подписку, ранее созданную ботом, если DB-строка потеряна.
        adoptable = resolve_existing_telegram_subscription(
            username, telegram_id
        )
        if adoptable is not None:
            logging.warning(
                "adopting existing RWMS subscription %s for telegram user %s "
                "(crash-window recovery, panel untouched, channel=%s)",
                username,
                telegram_id,
                creation_channel,
            )
            return sync_local_user_from_rwms(
                db_session,
                adoptable,
                email,
                telegram_id,
                context,
                creation_channel,
            )

    # Антиабьюз: лимит трафика новых пробных подписок (только при включённой
    # настройке; новый пользователь платежей не имеет по определению). Маркер
    # managed_traffic_limits пишется ниже, в той же сессии, что и строка users.
    trial_limit = trial_traffic_limit_for_new_trial(db_session)
    rw_user = create_user(
        rwms_client=rwms_client,
        username=username,
        trial_period_days=trial_period_days,
        from_referrer=referrer is not None,
        email=email,
        telegram_id=telegram_id,
        traffic_limit=trial_limit,
    )

    if rw_user is None:
        logging.warning(
            "RWMS AddUser returned an unknown result for site user %s; "
            "checking the deterministic username before deciding",
            user_label,
        )
        if email:
            rw_user = resolve_existing_site_subscription(username, email)
        else:
            rw_user = resolve_existing_telegram_subscription(username, telegram_id)

        if rw_user is None:
            logging.warning(
                "RWMS subscription was not confirmed after AddUser for %s; "
                "aborting the trial registration so it can be retried safely",
                user_label,
            )
            raise SiteRegistrationUnavailable(
                f"rwms trial creation was not confirmed for {username}"
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
    # Антиабьюз v2: лимит поставлен в AddUser — маркер «управляемый» (trial,
    # снимается оплатой) в той же транзакции; без таблицы — warning, без маркера.
    marker = record_trial_limit_marker(db_session, user, trial_limit)
    add_traffic_limit_event(
        db_session, user.id, EVENT_TRAFFIC_LIMIT_APPLIED, marker,
        creation_channel=creation_channel,
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
    # Rotate the session before elevating it to an authenticated one. Without
    # this, an attacker who fixed an anonymous session key could keep using the
    # same key after the victim opened a magic/login link.
    current_principal = request.session.get(SESSION_KEY)
    if current_principal and str(current_principal) != str(user.id):
        preserved_tracking = {
            f"tracking_{key}": request.session.get(f"tracking_{key}")
            for key in TRACKING_PARAM_KEYS
            if request.session.get(f"tracking_{key}") is not None
        }
        flush = getattr(request.session, "flush", None)
        if callable(flush):
            flush()
            request.session.update(preserved_tracking)
    else:
        cycle_key = getattr(request.session, "cycle_key", None)
        if callable(cycle_key):
            cycle_key()
    request.session[SESSION_KEY] = str(user.id)
    request.session[BACKEND_SESSION_KEY] = "engine.auth_backend.SQLAlchemyBackend"

    # Хэш пароля нам не нужен, так как вход по ссылке,
    # но если Django будет его требовать, можно поставить заглушку:
    request.session[HASH_SESSION_KEY] = ""
    rotate_token(request)

    # Важно: после ручного обновления сессии ее нужно сохранить
    request.session.modified = True


def hash_telegram_login_token(token):
    payload = f"telegram-login:{token}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def hash_telegram_bind_token(token):
    # Отдельное пространство хэшей для одноразовых bind-токенов. Они лежат в
    # той же таблице telegram_login_tokens, а вход /login/telegram/<token>/
    # проверяет только hash и revoked_at: с префиксом telegram-login
    # непогашенный bind-токен после появления у аккаунта telegram_id стал бы
    # бессрочной ссылкой входа. Контракт с ботом: тот же префикс и SECRET_KEY
    # (monkey-island-vpn-bot handlers/menu.py::hash_telegram_bind_token).
    payload = f"telegram-bind:{token}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def get_or_create_telegram_bind_link(request, db_session, user, bot_username):
    """Return a strong, expiring, single-use account-bind link.

    The session link is reused only within TELEGRAM_BIND_TOKEN_REUSE_SECONDS,
    so a shown link keeps at least 10 minutes of the bot's 15-minute TTL.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    raw_token = request.session.get(TELEGRAM_BIND_SESSION_TOKEN_KEY)
    created_timestamp = request.session.get(TELEGRAM_BIND_SESSION_CREATED_KEY)
    try:
        created_at = datetime.fromtimestamp(
            float(created_timestamp), timezone.utc
        ).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        created_at = None

    # Rotate links produced by the previous overlong format, even before TTL.
    payload_fits = len(f"bind_{user.id}_{raw_token or ''}") <= 64
    if raw_token and created_at and payload_fits:
        fresh_after = now - timedelta(seconds=TELEGRAM_BIND_TOKEN_REUSE_SECONDS)
        token_row = (
            db_session.query(TelegramLoginToken.id)
            .filter(
                TelegramLoginToken.user_id == user.id,
                TelegramLoginToken.token_hash == hash_telegram_bind_token(raw_token),
                TelegramLoginToken.created_at >= fresh_after,
                TelegramLoginToken.last_used_at.is_(None),
                TelegramLoginToken.revoked_at.is_(None),
            )
            .first()
        )
        if token_row is not None:
            return f"https://t.me/{bot_username}?start=bind_{user.id}_{raw_token}"

    # 152 random bits; with a signed BIGINT user id the payload is at most
    # 64 characters. Hex preserves compatibility with the bot's delimiters.
    raw_token = secrets.token_hex(19)
    db_session.add(
        TelegramLoginToken(
            user_id=user.id,
            token_hash=hash_telegram_bind_token(raw_token),
        )
    )
    request.session[TELEGRAM_BIND_SESSION_TOKEN_KEY] = raw_token
    request.session[TELEGRAM_BIND_SESSION_CREATED_KEY] = now.timestamp()
    request.session.modified = True
    return f"https://t.me/{bot_username}?start=bind_{user.id}_{raw_token}"


def issue_dashboard_telegram_bind_link(request, user, bot_username):
    """Bind-ссылка для кабинета в отдельной короткой транзакции.

    Кабинет обязан рендериться и без неё: сбой записи (read-only при
    failover, lock/statement timeout) означает отсутствие баннера привязки,
    а не 500 на странице с подпиской и оплатой.
    """
    db_session = session_factory()
    try:
        link = get_or_create_telegram_bind_link(
            request, db_session, user, bot_username
        )
        db_session.commit()
        return link
    except Exception:
        try:
            db_session.rollback()
        except Exception:
            pass
        # Токен без закоммиченной строки в сессии не держим.
        request.session.pop(TELEGRAM_BIND_SESSION_TOKEN_KEY, None)
        request.session.pop(TELEGRAM_BIND_SESSION_CREATED_KEY, None)
        request.session.modified = True
        logging.exception(
            "dashboard: telegram bind link was not issued user_id=%s",
            getattr(user, "id", None),
        )
        return None
    finally:
        db_session.close()


def hash_purchase_login_token(token):
    payload = f"purchase-login:{token}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def hash_purchase_status_token(token):
    payload = f"purchase-status:{token}:{settings.SECRET_KEY}".encode()
    return hashlib.sha256(payload).hexdigest()


def create_purchase_login_token(db_session, user):
    raw_token = f"{PURCHASE_LOGIN_TOKEN_PREFIX}{secrets.token_urlsafe(48)}"
    db_session.add(
        PurchaseLoginToken(
            user_id=user.id,
            token_hash=hash_purchase_login_token(raw_token),
        )
    )
    return raw_token


def create_purchase_status_token(db_session, user):
    """Create a browser-visible token that can only inspect this payment."""
    raw_token = f"{PURCHASE_STATUS_TOKEN_PREFIX}{secrets.token_urlsafe(48)}"
    db_session.add(
        PurchaseLoginToken(
            user_id=user.id,
            token_hash=hash_purchase_status_token(raw_token),
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


def build_magic_link(base_url, token):
    """Собирает ссылку входа и ОТКАЗЫВАЕТСЯ собрать её с пустым токеном.

    Зачем проверка: `MagicToken.token` — питоновский column default
    (`default=uuid.uuid4`), а он применяется на INSERT, а не при создании
    объекта. Забыл `flush()` перед чтением — и в письмо уезжает
    `/login/magic/None/`, которое URL-конвертер `<uuid:token>` не матчит:
    пользователь получает 404 и не может войти вообще никак. Инцидент
    2026-09-12: так сломался основной вход на сайт, письма уходили молча,
    люди приходили в поддержку.

    Исключение тут лучше, чем битая ссылка: обработчик входа ловит его,
    отвечает «попробуйте ещё раз» и пишет traceback в лог — сбой становится
    видимым сразу, а не через обращения в поддержку.
    """
    if token is None or str(token).strip() in ("", "None"):
        raise ValueError(
            "magic link token is empty — most likely the object was not "
            "flushed before reading MagicToken.token"
        )
    return f"{base_url}/login/magic/{token}/"


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
        resend.default_http_client = resend.RequestsClient(
            timeout=settings.EMAIL_TIMEOUT
        )
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
        resend.default_http_client = resend.RequestsClient(
            timeout=settings.EMAIL_TIMEOUT
        )
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


def build_email_confirmation_token(user_id, email, previous_email, nonce=None):
    return signing.dumps(
        {
            "user_id": user_id,
            "email": email,
            "previous_email": previous_email or "",
            "nonce": nonce,
        },
        salt=EMAIL_CONFIRMATION_SALT,
    )


def load_email_confirmation_token(token):
    return signing.loads(
        token,
        salt=EMAIL_CONFIRMATION_SALT,
        max_age=EMAIL_CONFIRMATION_MAX_AGE_SECONDS,
    )


def build_site_registration_token(email, tracking_params):
    safe_tracking = {
        key: str(value)[:512]
        for key, value in (tracking_params or {}).items()
        if key in TRACKING_PARAM_KEYS and value not in (None, "")
    }
    return signing.dumps(
        {"email": email, "tracking": safe_tracking},
        salt=SITE_REGISTRATION_CONFIRMATION_SALT,
    )


def load_site_registration_token(token):
    return signing.loads(
        token,
        salt=SITE_REGISTRATION_CONFIRMATION_SALT,
        max_age=SITE_REGISTRATION_CONFIRMATION_MAX_AGE_SECONDS,
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


def magic_link_rate_limited_response(retry_after):
    response = JsonResponse(
        {
            "status": "error",
            "message": "Слишком много попыток. Подождите и попробуйте снова.",
        },
        status=429,
    )
    response["Retry-After"] = str(retry_after)
    return response


def send_magic_link(request):
    if request.method != "POST":
        return JsonResponse({"status": "error"}, status=405)

    capture_tracking_params(request)
    email = request.POST.get("email", "").lower().strip()
    auth_base_url = get_current_base_url(request)
    entry_host = normalize_host(request.get_host())

    if not email:
        return JsonResponse({"status": "ok"})

    # Лимиты по порядку: IP → email → общий. Отбитый запрос следующие
    # счётчики не расходует, иначе один IP выбивал бы вход всем.
    limited, retry_after = rate_limit_exceeded(
        "magic-link",
        (
            (
                "ip",
                client_ip(request) or "unknown",
                settings.MAGIC_LINK_IP_RATE_LIMIT,
                settings.MAGIC_LINK_RATE_WINDOW_SECONDS,
            ),
        ),
    )
    if limited:
        return magic_link_rate_limited_response(retry_after)

    # Бакеты email и global считают только адреса валидного формата: мусорные
    # адреса не расходуют общий лимит и лимит чужого email. Битый адрес идёт
    # прежним путём и ограничен только IP-бакетом: владелец уже записанного
    # адреса получает ссылку, новая регистрация отклоняется 400 ниже.
    if is_valid_email(email):
        email_limited, _ = rate_limit_exceeded(
            "magic-link",
            (
                (
                    "email",
                    email,
                    settings.MAGIC_LINK_EMAIL_RATE_LIMIT,
                    settings.MAGIC_LINK_RATE_WINDOW_SECONDS,
                ),
            ),
        )
        if email_limited:
            # Не 429: адресная атака не должна показывать жертве блокировку,
            # а ответ не отличается от обычной отправки. Новое письмо не
            # уходит, уже выданные ссылки продолжают действовать.
            logging.warning(
                "magic link email rate limit reached, letter not sent "
                "(email_digest=%s)",
                rate_limit_log_digest(email),
            )
            return JsonResponse({"status": "ok"})

        limited, retry_after = rate_limit_exceeded(
            "magic-link",
            (
                (
                    "global",
                    "all",
                    settings.MAGIC_LINK_GLOBAL_RATE_LIMIT,
                    settings.MAGIC_LINK_GLOBAL_RATE_WINDOW_SECONDS,
                ),
            ),
        )
        if limited:
            logging.warning(
                "magic link global rate limit reached (limit=%s window=%ss)",
                settings.MAGIC_LINK_GLOBAL_RATE_LIMIT,
                settings.MAGIC_LINK_GLOBAL_RATE_WINDOW_SECONDS,
            )
            return magic_link_rate_limited_response(retry_after)

    db_session = session_factory()
    try:
        with db_session.begin():
            user = db_session.query(User).filter(User.email == email).first()
            if user:
                registration_context = get_registration_context(request, db_session)
                sync_existing_user_tracking(
                    db_session,
                    user,
                    registration_context["traffic_source"],
                    registration_context["ymid"],
                )
                magic = MagicToken(user_id=user.id)
                db_session.add(magic)
                # flush ОБЯЗАТЕЛЕН: до него magic.token ещё None (питоновский
                # column default применяется на INSERT), и в письмо уходит
                # ссылка /login/magic/None/ — 404 вместо входа.
                db_session.flush()
                link = build_magic_link(auth_base_url, magic.token)
            else:
                # Existing malformed addresses remain reachable for their owner,
                # but a new account may only be requested for a valid address.
                if not is_valid_email(email):
                    logging.warning(
                        "magic link registration rejected: invalid email "
                        "format email=%s",
                        email,
                    )
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": (
                                "Проверьте адрес электронной почты: "
                                "похоже, в нём опечатка."
                            ),
                        },
                        status=400,
                    )

                # The recipient must prove ownership before a DB account or an
                # RWMS trial subscription is created.
                registration_token = build_site_registration_token(
                    email,
                    get_tracking_params(request),
                )
                link = (
                    f"{auth_base_url}"
                    f"{reverse('registration_auth', args=[registration_token])}"
                )

        logging.info(
            "Magic link requested from host %s, target auth host is %s for %s",
            entry_host,
            auth_base_url,
            email,
        )
        send_magic_link_email(email, link)
    except Exception as error:
        logging.exception("failed to prepare or send login email for %s: %s", email, error)
        return JsonResponse(
            {
                "status": "error",
                "message": "Не удалось отправить письмо. Попробуйте ещё раз.",
            },
            status=503,
        )
    finally:
        db_session.close()

    # Do not disclose whether the address already exists.
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
    except SiteRegistrationOwnershipConflict as e:
        logging.warning(
            "google oauth registration ownership conflict for %s: %s", email, e
        )
        return render_login(
            request,
            {"error": SITE_REGISTRATION_SUPPORT_MESSAGE},
            status=409,
        )
    except SiteRegistrationUnavailable as e:
        logging.warning("google oauth registration postponed for %s: %s", email, e)
        return render_login(
            request,
            {"error": SITE_REGISTRATION_RETRY_MESSAGE},
            status=503,
        )
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
    except SiteRegistrationOwnershipConflict as e:
        logging.warning(
            "yandex oauth registration ownership conflict for %s: %s", email, e
        )
        return render_login(
            request,
            {"error": SITE_REGISTRATION_SUPPORT_MESSAGE},
            status=409,
        )
    except SiteRegistrationUnavailable as e:
        logging.warning("yandex oauth registration postponed for %s: %s", email, e)
        return render_login(
            request,
            {"error": SITE_REGISTRATION_RETRY_MESSAGE},
            status=503,
        )
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
                logging.warning("magic login points to missing user %s", user_id)
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


def auth_by_registration_link(request, token):
    try:
        payload = load_site_registration_token(token)
    except (BadSignature, SignatureExpired):
        return render_login(request, {"error": "Ссылка истекла или неверна"})

    email = (payload.get("email") or "").lower().strip()
    tracking = payload.get("tracking") or {}
    if not is_valid_email(email) or not isinstance(tracking, dict):
        return render_login(request, {"error": "Ссылка истекла или неверна"})

    tracking_restored = False
    for key in TRACKING_PARAM_KEYS:
        value = tracking.get(key)
        if value not in (None, ""):
            request.session[f"tracking_{key}"] = str(value)[:512]
            tracking_restored = True
    if tracking_restored:
        request.session.modified = True

    db_session = session_factory()
    try:
        with db_session.begin():
            user = db_session.query(User).filter(User.email == email).first()
            if user is None:
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

            add_event_log_once(
                db_session,
                user,
                analytics_event.FirstSuccessfulLogin(login_method="magic_link"),
            )

        authorize_user_session(request, user)
        return redirect("dashboard")
    except SiteRegistrationOwnershipConflict as error:
        logging.warning(
            "confirmed site registration ownership conflict for %s: %s",
            email,
            error,
        )
        return render_login(
            request,
            {"error": SITE_REGISTRATION_SUPPORT_MESSAGE},
            status=409,
        )
    except SiteRegistrationUnavailable as error:
        logging.warning("confirmed site registration postponed for %s: %s", email, error)
        return render_login(
            request,
            {"error": SITE_REGISTRATION_RETRY_MESSAGE},
            status=503,
        )
    except Exception as error:
        logging.exception("confirmed site registration failed for %s: %s", email, error)
        return render_login(
            request,
            {"error": "Не удалось подготовить личный кабинет. Попробуйте ещё раз."},
            status=503,
        )
    finally:
        db_session.close()


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


LEGACY_PURCHASE_LOGIN_MESSAGE = (
    "Ссылки для входа из писем об оплате больше не действуют. Введите email — "
    "мы пришлём новую ссылку. Подписка продолжает работать."
)


def auth_by_purchase_link(request, token):
    # Legacy unprefixed tokens cannot be distinguished from status links (they
    # were the same browser-visible token) and therefore NEVER authenticate.
    # Answer without touching the database: no validity oracle for old links.
    if is_legacy_purchase_token(token):
        logging.info("legacy purchase login link was used; asking for email login")
        return render_login(request, {"error": LEGACY_PURCHASE_LOGIN_MESSAGE})

    session = session_factory()
    try:
        # Status tokens are intentionally returned to the browser that starts a
        # payment. Only the separately emailed plogin_ token may authenticate.
        login_token = get_purchase_login_token(session, token)
        if not login_token:
            logging.warning("invalid purchase login token was used")
            return render_login(request, {"error": "Ссылка истекла или неверна"})

        user = session.query(User).filter(User.id == login_token.user_id).first()
        if not user:
            logging.warning("purchase login token points to missing user")
            return render_login(request, {"error": "Ссылка истекла или неверна"})

        # Токен выпускается ДО оплаты, и его сырое значение возвращается
        # инициатору платежа в payment_status_url (см. pay()). Без проверки
        # оплаты любой, кто ввёл ЧУЖОЙ email в форму оплаты, получал бы
        # рабочую ссылку входа в чужой аккаунт, ничего не заплатив.
        # Вход разрешён только по подтверждённой оплате этого токена.
        payment_status, _ = get_purchase_payment_status(session, login_token)
        if payment_status != "succeeded":
            logging.warning(
                "ALERT: purchase login token used without a confirmed payment: "
                "user_id=%s status=%s gateway=%s reference=%s",
                login_token.user_id,
                payment_status,
                login_token.payment_gateway,
                login_token.payment_reference,
            )
            return render_login(
                request,
                {
                    "error": (
                        "Оплата по этой ссылке пока не подтверждена. "
                        "Если вы только что оплатили — подождите минуту и "
                        "откройте ссылку снова. Чтобы войти без оплаты, "
                        "запросите ссылку для входа по вашей почте ниже."
                    )
                },
            )

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
    if not token.startswith(PURCHASE_LOGIN_TOKEN_PREFIX):
        return None
    token_hash = hash_purchase_login_token(token)
    token_row = (
        db_session.query(PurchaseLoginToken)
        .filter(
            PurchaseLoginToken.token_hash == token_hash,
            PurchaseLoginToken.revoked_at.is_(None),
        )
        .first()
    )
    if not token_row or not hmac.compare_digest(token_row.token_hash, token_hash):
        return None
    return token_row


def get_purchase_status_token(db_session, token):
    if not token.startswith(PURCHASE_STATUS_TOKEN_PREFIX):
        return None
    token_hash = hash_purchase_status_token(token)
    token_row = (
        db_session.query(PurchaseLoginToken)
        .filter(
            PurchaseLoginToken.token_hash == token_hash,
            PurchaseLoginToken.revoked_at.is_(None),
        )
        .first()
    )
    if not token_row or not hmac.compare_digest(token_row.token_hash, token_hash):
        return None
    return token_row


# Токены до введения префиксов (одна и та же строка была ссылкой статуса и
# «постоянной» ссылкой входа). Для платежей, начатых до деплоя, страница
# статуса ещё неделю показывает ТОЛЬКО статус оплаты: без входа и без retry.
LEGACY_PURCHASE_STATUS_MAX_AGE = timedelta(days=7)
LEGACY_PURCHASE_STATUS_NEUTRAL_MESSAGE = (
    "Статус оплаты и подписку можно посмотреть в личном кабинете после входа"
)


def is_legacy_purchase_token(token):
    return not token.startswith(
        (PURCHASE_LOGIN_TOKEN_PREFIX, PURCHASE_STATUS_TOKEN_PREFIX)
    )


def get_legacy_purchase_status_token(db_session, token):
    """Read-only lookup of a pre-prefix token by the HEAD hash formula.

    The row is used ONLY to show payment status; it never authenticates (B01).
    """
    if not is_legacy_purchase_token(token):
        return None
    token_hash = hash_purchase_login_token(token)
    token_row = (
        db_session.query(PurchaseLoginToken)
        .filter(
            PurchaseLoginToken.token_hash == token_hash,
            PurchaseLoginToken.revoked_at.is_(None),
            PurchaseLoginToken.created_at
            >= datetime.utcnow() - LEGACY_PURCHASE_STATUS_MAX_AGE,
        )
        .first()
    )
    if not token_row or not hmac.compare_digest(token_row.token_hash, token_hash):
        return None
    return token_row


# Терминальный вебхук Wata по уже начатому платежу (СБП/3DS) приходит вторым
# и может опоздать относительно expiration_datetime на десятки секунд. Поэтому
# начатый платёж ждём ещё это окно после протухания инвойса — и только потом
# закрываем экран «время истекло», чтобы опрос не крутился вечно из-за
# застрявшей нетерминальной строки.
WATA_PENDING_AFTER_EXPIRY_GRACE = timedelta(minutes=10)


def is_wata_invoice_expired(invoice, grace=None):
    if not invoice or not invoice.expiration_datetime:
        return False

    expires_at = invoice.expiration_datetime
    if grace is not None:
        expires_at = expires_at + grace
    if expires_at.tzinfo is None:
        return datetime.utcnow() > expires_at
    return datetime.now(timezone.utc) > expires_at


def get_purchase_wata_invoice(db_session, purchase_token):
    if (
        purchase_token.payment_gateway != "wata"
        or not purchase_token.payment_reference
    ):
        return None

    return (
        db_session.query(WataInvoice)
        .filter(
            WataInvoice.user_id == purchase_token.user_id,
            WataInvoice.order_id == purchase_token.payment_reference,
        )
        .order_by(WataInvoice.creation_time.desc())
        .first()
    )


def has_paid_wata_transaction(db_session, order_ids):
    """Есть ли хотя бы одна ОПЛАЧЕННАЯ транзакция среди этих заказов.

    Исход заказа нельзя выводить из «последней по payment_time» транзакции.
    У одного order_id бывает несколько попыток, и ``payment_time`` каждой
    строки — это время ЕЁ события из вебхука Wata (см.
    monkey-island-payment/wata_webhook_handler.py: ``_add_wata_transaction`` и
    ``_mark_wata_transaction_paid``). Отклонённая попытка, случившаяся ПОСЛЕ
    оплаченной, поэтому легко становится «последней», и человек, который
    заплатил, видел бы «платёж не прошёл».

    Оплата — терминальный и необратимый для пользователя исход: сначала ищем
    Paid, и только если его нет, падаем в fallback «последняя по времени».
    """
    unique_order_ids = [order_id for order_id in dict.fromkeys(order_ids) if order_id]
    if not unique_order_ids:
        return False

    return (
        db_session.query(WataTransaction)
        .filter(
            WataTransaction.order_id.in_(unique_order_ids),
            WataTransaction.transaction_status == "Paid",
        )
        .first()
        is not None
    )


PURCHASE_PENDING_STATUS = ("pending", "Ждем подтверждения платежа")


def wata_transaction_purchase_status(wata_transaction):
    """Статус покупки по строке транзакции Wata.

    Терминальных статусов у Wata ровно два: ``Paid`` (успех) и ``Declined``
    (отказ). Всё остальное (``Created``/``Pending`` у СБП и 3DS) — ПРОМЕЖУТОЧНОЕ
    состояние: платёжный сервис сохраняет такую строку по вебхуку
    (monkey-island-payment/wata_webhook_handler.py, ветка «прочие статусы» ->
    ``_add_wata_transaction``) и переводит её в ``Paid`` отдельным вебхуком
    через несколько секунд.

    Поэтому отказом считаем ТОЛЬКО явный ``Declined`` — симметрично тому, что
    успехом считается только явный ``Paid`` (см. ``active_wata_status_for_token``).
    Иначе страница /payment/status/<token>/ показала бы «Платеж не прошел» ещё
    до того, как платёж завершился: шаблон payment_status.html на статусе
    ``failed`` НАВСЕГДА останавливает опрос, шлёт аналитику ``payment_failed`` и
    прячет кнопку входа, так что пришедший позже ``Paid`` пользователь уже не
    увидит.
    """
    if wata_transaction.transaction_status == "Paid":
        return "succeeded", "Платеж прошел успешно"
    if wata_transaction.transaction_status == "Declined":
        return "failed", "Платеж не прошел"
    return PURCHASE_PENDING_STATUS


def get_purchase_payment_status(db_session, purchase_token):
    gateway = purchase_token.payment_gateway
    reference = purchase_token.payment_reference
    if not gateway or not reference:
        return PURCHASE_PENDING_STATUS

    if gateway == "yookassa":
        yk_payment = (
            db_session.query(YkPayment)
            .filter(
                YkPayment.user_id == purchase_token.user_id,
                YkPayment.payment_id == reference,
            )
            .first()
        )
        if not yk_payment:
            return PURCHASE_PENDING_STATUS
        if yk_payment.status == "succeeded":
            return "succeeded", "Платеж прошел успешно"
        if yk_payment.status == "canceled":
            return "failed", "Платеж не прошел"
        return PURCHASE_PENDING_STATUS

    if gateway != "wata":
        logging.warning(
            "purchase token has unsupported payment gateway: user_id=%s gateway=%s",
            purchase_token.user_id,
            gateway,
        )
        return PURCHASE_PENDING_STATUS

    wata_invoice = get_purchase_wata_invoice(db_session, purchase_token)

    # ПЕРВЫМ делом — факт оплаты по заказам этой покупки. Только если Paid нет,
    # смотрим на «последнюю по времени» транзакцию (см. has_paid_wata_transaction).
    purchase_order_ids = []
    if wata_invoice:
        purchase_order_ids.append(wata_invoice.order_id)
    purchase_order_ids.append(reference)
    if has_paid_wata_transaction(db_session, purchase_order_ids):
        return "succeeded", "Платеж прошел успешно"

    if wata_invoice:
        wata_transaction = (
            db_session.query(WataTransaction)
            .filter(WataTransaction.order_id == wata_invoice.order_id)
            .order_by(WataTransaction.payment_time.desc())
            .first()
        )
        if wata_transaction:
            status = wata_transaction_purchase_status(wata_transaction)
            # Нетерминальная строка (Created/Pending) означает «ждём», но
            # ждать вечно нельзя: если инвойс уже протух, оплаты не будет, и
            # экран должен закрыться понятным «время истекло», а не крутить
            # опрос бесконечно. Терминальный Declined остаётся отказом.
            if status == PURCHASE_PENDING_STATUS and is_wata_invoice_expired(
                wata_invoice, grace=WATA_PENDING_AFTER_EXPIRY_GRACE
            ):
                return "failed", "Время оплаты истекло"
            return status
        if is_wata_invoice_expired(wata_invoice):
            return "failed", "Время оплаты истекло"

    if not wata_invoice:
        wata_transaction = (
            db_session.query(WataTransaction)
            .filter(WataTransaction.order_id == reference)
            .order_by(WataTransaction.payment_time.desc())
            .first()
        )
        if wata_transaction:
            return wata_transaction_purchase_status(wata_transaction)

    return PURCHASE_PENDING_STATUS


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


CHECKOUT_PREPARED_STATUS_MESSAGE = "Подготавливаем платёж"
CHECKOUT_REVIEW_STATUS_MESSAGE = (
    "Форма оплаты не была открыта, оплата по этому заказу не проводилась. "
    "Можно попробовать снова."
)
CHECKOUT_FAILED_STATUS_MESSAGE = (
    "Платёж не создан, деньги не списаны, попробуйте ещё раз"
)
# Успешная оплата без сессии владельца (анонимная покупка с лендинга, возврат
# из YooKassa во внешний браузер): login_url не выдаётся, страница ведёт на
# вход. Email плательщика в ответ не попадает.
PAYMENT_SUCCEEDED_LOGIN_LINK_SENT_MESSAGE = (
    "Оплата прошла. Ссылка для входа в личный кабинет отправлена на email, "
    "указанный при оплате. Если письма нет, войдите по email на странице входа."
)
PAYMENT_SUCCEEDED_SIGN_IN_MESSAGE = (
    "Оплата прошла. Войдите в личный кабинет по email или вернитесь в Telegram"
)


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
        status_token = get_purchase_status_token(db_session, token)
        if not status_token and is_legacy_purchase_token(token):
            # Ссылка статуса, выданная до деплоя: только статус оплаты, без
            # login_url и без retry; старше недели — нейтральный текст
            # (не «failed», чтобы не подталкивать к повторной оплате).
            legacy_token = get_legacy_purchase_status_token(db_session, token)
            if legacy_token is None:
                # FE-FINAL-02: исход по такой ссылке уже не узнать. terminal
                # останавливает опрос страницы статуса и убирает «ожидаем
                # подтверждение»; статус и текст остаются нейтральными.
                return {
                    "status": "pending",
                    "message": LEGACY_PURCHASE_STATUS_NEUTRAL_MESSAGE,
                    "login_url": "",
                    "payment_url": "",
                    "terminal": True,
                }
            status, message = get_purchase_payment_status(db_session, legacy_token)
            if status == "succeeded":
                # Legacy-ссылка никогда не авторизует: страница ведёт на вход.
                message = PAYMENT_SUCCEEDED_SIGN_IN_MESSAGE
            return {
                "status": status,
                "message": message,
                "login_url": "",
                "payment_url": "",
            }
        if not status_token:
            return {
                "status": "failed",
                "message": "Ссылка проверки платежа истекла или неверна",
                "login_url": "",
                "payment_url": "",
            }

        status, message = get_purchase_payment_status(db_session, status_token)
        # Если в БД ещё нет подтверждения (вебхук не дошёл), но это разрешено
        # вызывающим — спрашиваем статус напрямую у Wata, чтобы не ждать её
        # 10-секундный экран успеха.
        if status == "pending" and allow_active_check:
            active = active_wata_status_for_token(status_token)
            if active:
                status, message = active
        wata_invoice = get_purchase_wata_invoice(db_session, status_token)
        session_payment_url = request.session.get(payment_session_url_key(token), "")
        attempt = None
        try:
            attempt = db_session.query(WebsitePaymentAttempt).filter_by(status_token_hash=status_token.token_hash).first()
        except ProgrammingError:
            # Код сайта выкачен раньше alembic-миграции common: страница
            # статуса работает как до появления попыток, а не отдаёт 500.
            db_session.rollback()
            logging.error(
                "payment status: website_payment_attempts is unavailable "
                "(common migration not applied?)"
            )
        if attempt is not None:
            session_payment_url = attempt.confirmation_url or session_payment_url
            if status == "pending" and attempt.state == "review":
                # Ссылка на оплату по такой попытке клиенту не выдавалась.
                status, message = "failed", CHECKOUT_REVIEW_STATUS_MESSAGE
            elif status == "pending" and attempt.state == "failed":
                status, message = "failed", CHECKOUT_FAILED_STATUS_MESSAGE
            elif status == "pending" and attempt.state == "prepared":
                message = CHECKOUT_PREPARED_STATUS_MESSAGE
        authenticated_for_purchase = (
            getattr(request.user, "is_authenticated", False)
            and str(getattr(request.user, "id", "")) == str(status_token.user_id)
        )
        if status == "succeeded" and not authenticated_for_purchase:
            # Без сессии владельца login_url нет. Если ссылка входа ушла письмом
            # (покупка без входа, R03), говорим об этом, не раскрывая адрес;
            # иначе предлагаем войти по email или вернуться в Telegram.
            message = (
                PAYMENT_SUCCEEDED_LOGIN_LINK_SENT_MESSAGE
                if getattr(attempt, "login_token_hash", None)
                else PAYMENT_SUCCEEDED_SIGN_IN_MESSAGE
            )
        return {
            "status": status,
            "message": message,
            "login_url": (
                reverse("dashboard")
                if status == "succeeded" and authenticated_for_purchase
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
        status_token = get_purchase_status_token(db_session, token)
        if not status_token:
            return redirect("payment_status", token=token)

        wata_invoice = get_purchase_wata_invoice(db_session, status_token)
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
            "initial_terminal": bool(payload.get("terminal")),
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

    telegram_bots = get_all_telegram_auth_bots(request.get_host())
    if not telegram_bots:
        logging.warning("telegram webapp auth requested but bot token is missing")
        return render_login(
            request,
            {"error": "Вход через Telegram временно недоступен"},
            status=503,
        )

    # Mini App могут открыть из любого из наших ботов (vpn/vps), а домен
    # кабинета у них общий — подпись initData проверяем всеми доверенными
    # токенами, а не только ботом, привязанным к домену.
    init_data = request.POST.get("init_data", "")
    init_data_fields = None
    for telegram_bot in telegram_bots:
        init_data_fields = verify_telegram_webapp_init_data(
            init_data, telegram_bot["token"]
        )
        if init_data_fields is not None:
            logging.info(
                "telegram webapp auth verified by bot %s",
                telegram_bot["username"],
            )
            break
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


def landing_client_ip_context(request):
    """IP посетителя и страна (локальная MMDB) для топ-бара VPS-лендингов.

    Внешние geo-API не используются: страна берётся из той же локальной базы,
    что и в админке (engine.geoip_lookup); без базы показывается только IP.
    """
    client_ip = admin_client_ip(request)
    country = None
    if client_ip:
        try:
            from engine.geoip_lookup import lookup_ip

            location = lookup_ip(client_ip)
            if location is not None:
                country = location.country_name
        except Exception:
            logging.exception("landing geoip lookup failed")
    return {"client_ip": client_ip, "client_ip_country": country}


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
                "payment_gateway": settings.PAYMENT_GATEWAY.lower(),
                **landing_client_ip_context(request),
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
            "payment_gateway": settings.PAYMENT_GATEWAY.lower(),
            **landing_client_ip_context(request),
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
            **landing_client_ip_context(request),
        },
    )


def offer(request):
    offer_tariffs = get_runtime_offer_tariffs()
    return render(
        request,
        "offer.html",
        {
            "site_role": get_site_role(request),
            # В таблице 2.1 — все платные тарифы, включая «Пробный период на
            # 3 дня» и «Подписку на 1 день» (продаются в Telegram-боте): оферта
            # обязана перечислять всё, что можно оплатить, особенно тарифы с
            # автопродлением через YooKassa (раздел 4).
            "tariffs": [
                offer_tariffs[tariff.db_tariff_id] for tariff in OFFER_TARIFFS
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


def _days_in_month(year, month):
    first_next = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return (first_next - timedelta(days=1)).day


def traffic_limit_next_reset(strategy_key, created_at_date=None, now=None):
    """Ближайший автоматический сброс трафика панелью Remnawave (полночь UTC).

    day — каждый день, week — по понедельникам, month — 1-го числа,
    month_rolling — по числу даты создания подписки. no_reset/None → None:
    автосброса нет, лимит снимается вручную или оплатой.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    key = (str(strategy_key or "")).lower()
    if key == "day":
        base = today + timedelta(days=1)
    elif key == "week":
        days_ahead = (7 - today.weekday()) % 7 or 7
        base = today + timedelta(days=days_ahead)
    elif key == "month":
        base = date(today.year + (today.month == 12), 1 if today.month == 12 else today.month + 1, 1)
    elif key == "month_rolling" and created_at_date is not None:
        target_day = created_at_date.day
        base = date(today.year, today.month, min(target_day, _days_in_month(today.year, today.month)))
        if base <= today:
            year = today.year + (today.month == 12)
            month = 1 if today.month == 12 else today.month + 1
            base = date(year, month, min(target_day, _days_in_month(year, month)))
    else:
        return None
    return datetime.combine(base, time.min, tzinfo=timezone.utc)


@login_required(login_url="/login/")
def dashboard(request):
    user = request.user

    tg_bot = settings.TG_BOT_USERNAME

    tg_bind_link = None

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

        bonus_days = (
            session.query(func.coalesce(func.sum(ReferralBonus.days_added), 0))
            .filter(ReferralBonus.referrer_id == user.id)
            .scalar()
        )
        apple_recommended_app = apple_recommended_app_from_db(session)
        runtime_tariffs = get_runtime_actual_tariffs(session)
    finally:
        session.close()

    # Выпуск bind-ссылки пишет в БД — отдельной транзакцией после закрытия
    # основной сессии. commit в основной истекал бы загруженные выше ORM-
    # объекты (traffic_progress, support_ticket), и обращение к ним после
    # close падало бы DetachedInstanceError; а сбой записи ронял бы весь
    # кабинет. Без ссылки кабинет рендерится без баннера привязки.
    if not user.telegram_id:
        tg_bind_link = issue_dashboard_telegram_bind_link(request, user, tg_bot)

    # Политика «БД — истина по времени, панель — истина по существованию ключа»:
    # - strict вернул None (достоверный NOT_FOUND) — подписки в панели нет,
    #   показываем «истекла» (легитимный кейс: удалена после долгой просрочки);
    # - RwmsUnavailableError (блип RWMS/панели) — деградация к БД: остаток
    #   считаем из user.time_until_expiration и НИКОГДА не показываем
    #   «истекла» платящему клиенту из-за недоступности панели.
    rwms_unavailable = False
    try:
        subscription = rwms_client.get_user_by_username_strict(user.username)
    except RwmsUnavailableError as error:
        subscription = None
        rwms_unavailable = True
        logging.warning(
            "dashboard: RWMS unavailable for %s, degrading to DB expiration: %s",
            user.username,
            error,
        )
    seconds_left = (
        user.time_until_expiration.total_seconds() if user.time_until_expiration else -1
    )
    if subscription is None and not rwms_unavailable:
        seconds_left = -1

    has_subscription_access = (
        subscription is not None or rwms_unavailable
    ) and seconds_left > 0
    days_left = int((seconds_left + 86399) // 86400) if seconds_left > 0 else 0

    # Статус подписки и трафик из RWMS для десктопной Главной — те же данные,
    # что показывает «Мой профиль» в боте: статус панели (ACTIVE/DISABLED/
    # LIMITED/EXPIRED), использованный трафик за всё время, лимит и стратегия
    # его автосброса. При деградации RWMS всё остаётся None — шаблон молчит.
    rw_status = None
    rw_traffic_total_gb = None
    rw_traffic_limit_gb = None
    rw_traffic_limit_used_gb = None
    rw_traffic_strategy_label = None
    rw_traffic_reset_at = None
    if subscription is not None:
        rw_status = admin_user_status_name(get_proto_optional(subscription, "status"))
        rw_traffic_total_gb = (
            getattr(subscription, "lifetime_used_traffic_bytes", 0) or 0
        ) / (1024 ** 3)
        rw_limit_bytes = get_proto_optional(subscription, "traffic_limit_bytes") or 0
        if rw_limit_bytes:
            rw_traffic_limit_gb = rw_limit_bytes / (1024 ** 3)
            rw_traffic_limit_used_gb = (
                getattr(subscription, "used_traffic_bytes", 0) or 0
            ) / (1024 ** 3)
            rw_strategy_key = admin_traffic_limit_strategy_key(
                get_proto_optional(subscription, "traffic_limit_strategy")
            )
            rw_traffic_strategy_label = admin_traffic_limit_strategy_label(rw_strategy_key)
            rw_created_at = get_proto_optional(subscription, "created_at")
            rw_traffic_reset_at = traffic_limit_next_reset(
                rw_strategy_key,
                rw_created_at.ToDatetime().date() if rw_created_at is not None else None,
            )
    expiring_banner_threshold_seconds = 3 * 24 * 60 * 60
    show_expiring_banner = 0 < seconds_left <= expiring_banner_threshold_seconds
    # Без выпущенной ссылки (сбой записи в БД) баннер привязки не показываем.
    show_telegram_bind_banner = not user.telegram_id and bool(tg_bind_link)
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
            # Симулируем достоверный NOT_FOUND (подписки нет в панели),
            # а не недоступность панели — иначе сработала бы деградация к БД.
            subscription = None
            rwms_unavailable = False
            seconds_left = -1

        show_expiring_banner = 0 < seconds_left <= expiring_banner_threshold_seconds
        days_left = int((seconds_left + 86399) // 86400) if seconds_left > 0 else 0

    has_subscription_access = (
        subscription is not None or rwms_unavailable
    ) and seconds_left > 0

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

    # В режиме деградации (rwms_unavailable) панельных данных нет: ссылки
    # установки пустые (для них нужна панель), но кабинет рендерится штатно
    # с остатком из БД.
    has_panel_data = has_subscription_access and subscription is not None
    plain_subscription_url = (
        subscription.subscription_url if has_panel_data else ""
    )
    happ_subscription_url = (
        encrypt_happ_url1(subscription.subscription_url)
        if has_panel_data
        else ""
    )
    # Рекомендуемое приложение для iOS/macOS из админки: happ (по умолчанию)
    # или incy — для INCY ссылка добавления подписки шифруется отдельно.
    if has_panel_data:
        apple_recommended_app, apple_subscription_url = build_apple_subscription_link(
            None,
            subscription.subscription_url,
            recommended_app=apple_recommended_app,
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
            "max_referral_bonus_days": (
                join_referrer_bonus_days
                + traffic_referrer_bonus_days
                + purchase_referrer_bonus_days
            ),
            "tariffs": runtime_tariffs,
            "pending_checkout_tariff_id": request.session.pop("pending_checkout_tariff_id", ""),
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
            "rw_status": rw_status,
            "rw_traffic_total_gb": rw_traffic_total_gb,
            "rw_traffic_limit_gb": rw_traffic_limit_gb,
            "rw_traffic_limit_used_gb": rw_traffic_limit_used_gb,
            "rw_traffic_strategy_label": rw_traffic_strategy_label,
            "rw_traffic_reset_at": rw_traffic_reset_at,
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

    # Привязка НОВОГО адреса — дедушкина оговорка здесь не нужна: человек
    # вводит адрес прямо сейчас и может его исправить. Раньше проверялась
    # только непустота, и в users.email попадал мусор, который потом ронял
    # запросы к панели (инцидент 2026-09-10).
    if not is_valid_email(new_email):
        logging.warning(
            "email binding rejected: invalid email format email=%s user_id=%s",
            new_email,
            request.user.id,
        )
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Проверьте адрес: похоже, в нём опечатка.",
            "email": new_email,
        }
        request.session.modified = True
        return redirect("dashboard")

    session = session_factory()
    try:
        db_user = session.query(User).filter(User.id == request.user.id).with_for_update().first()
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

        nonce = issue_email_change(session, db_user, new_email)
        token = build_email_confirmation_token(db_user.id, new_email, db_user.email, nonce)
        session.commit()
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


# Повторный счёт в этом окне не перевыпускает только что отправленную ссылку
# подтверждения на тот же адрес: иначе ссылка из первого письма стала бы
# «устаревшей». Меньше срока жизни ссылки (EMAIL_CONFIRMATION_MAX_AGE_SECONDS).
PAYMENT_EMAIL_CONFIRMATION_RESEND_SECONDS = 10 * 60
# ZONE-EMAIL-01: не больше писем подтверждения из /pay/ на аккаунт за окно,
# независимо от адреса (каждый новый email чека иначе давал бы новое письмо).
PAYMENT_EMAIL_CONFIRMATION_RATE_LIMIT = 3
PAYMENT_EMAIL_CONFIRMATION_RATE_WINDOW_SECONDS = 60 * 60


def send_payment_email_confirmation(request, user_id, email):
    """Предложить привязать email, введённый при оплате, штатным подтверждением.

    Для авторизованного аккаунта без email (например, пришёл из Telegram).
    users.email здесь НЕ меняется: email чека не доказывает владение адресом
    (B23). Адрес привяжется только по ссылке из письма (confirm_email), тогда
    же уйдёт в панель, а следующая оплата ЮKassa подключит автоплатёж.
    Вызывается после commit счёта; любая ошибка только логируется, оплату не
    ломает. Возвращает True, если письмо отправлено.
    """
    session = session_factory()
    try:
        db_user = (
            session.query(User).filter(User.id == user_id).with_for_update().first()
        )
        if db_user is None or db_user.email:
            return False
        email_owner = (
            session.query(User.id)
            .filter(User.email == email, User.id != db_user.id)
            .first()
        )
        if email_owner is not None:
            logging.info(
                "payment email confirmation skipped: email belongs to another "
                "account user_id=%s",
                user_id,
            )
            return False
        if has_fresh_unused_email_change(
            session, db_user.id, email, PAYMENT_EMAIL_CONFIRMATION_RESEND_SECONDS
        ):
            logging.info(
                "payment email confirmation skipped: a recent link is still valid "
                "user_id=%s",
                user_id,
            )
            return False
        # Лимит считается только перед реальным выпуском ссылки: пропуски выше
        # его не расходуют. При превышении ссылку не перевыпускаем (иначе
        # прежняя ссылка из письма стала бы устаревшей) и письмо не шлём.
        limited, _ = rate_limit_exceeded(
            "payment-email-confirmation",
            (
                (
                    "user",
                    str(user_id),
                    PAYMENT_EMAIL_CONFIRMATION_RATE_LIMIT,
                    PAYMENT_EMAIL_CONFIRMATION_RATE_WINDOW_SECONDS,
                ),
            ),
        )
        if limited:
            logging.warning(
                "payment email confirmation skipped: rate limit reached "
                "user_id=%s email_digest=%s limit=%s window=%ss",
                user_id,
                rate_limit_log_digest(email),
                PAYMENT_EMAIL_CONFIRMATION_RATE_LIMIT,
                PAYMENT_EMAIL_CONFIRMATION_RATE_WINDOW_SECONDS,
            )
            return False
        nonce = issue_email_change(session, db_user, email)
        token = build_email_confirmation_token(db_user.id, email, db_user.email, nonce)
        link = f"{get_current_base_url(request)}{reverse('confirm_email', args=[token])}"
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        logging.exception(
            "payment email confirmation was not issued user_id=%s", user_id
        )
        return False
    finally:
        session.close()

    try:
        send_email_confirmation_email(email, link)
    except Exception:
        logging.exception("payment email confirmation was not sent user_id=%s", user_id)
        return False
    logging.info("payment email confirmation sent user_id=%s", user_id)
    return True


def cabinet_payments_history(request):
    """История платежей текущего пользователя для личного кабинета.

    Показываем только успешные оплаты (YooKassa succeeded + WATA Paid) —
    отменённые/протухшие инвойсы для пользователя шум. Свежие первыми.
    """
    if request.method != "GET" or not request.user.is_authenticated:
        return JsonResponse({"status": "error"}, status=403)

    db_session = session_factory()
    try:
        yk_payments = (
            db_session.query(YkPayment)
            .filter(
                YkPayment.user_id == request.user.id,
                YkPayment.status == "succeeded",
            )
            .order_by(YkPayment.created_at.desc())
            .limit(100)
            .all()
        )
        wata_payments = (
            db_session.query(WataTransaction, WataInvoice)
            .join(WataInvoice, WataInvoice.order_id == WataTransaction.order_id)
            .filter(
                WataInvoice.user_id == request.user.id,
                WataTransaction.transaction_status == "Paid",
            )
            .order_by(WataTransaction.payment_time.desc())
            .limit(100)
            .all()
        )

        history = []
        for payment in yk_payments:
            history.append(
                {
                    "date": admin_date_label(payment.created_at, with_time=False),
                    "date_sort": admin_dt(payment.created_at) or datetime.min,
                    "amount": admin_money(payment.amount),
                    "currency": payment.currency,
                    "tariff": get_tariff_display_name(payment.subscription_period),
                    "trial": bool(payment.is_trial_promotion),
                }
            )
        for payment, invoice in wata_payments:
            history.append(
                {
                    "date": admin_date_label(payment.payment_time, with_time=False),
                    "date_sort": admin_dt(payment.payment_time) or datetime.min,
                    "amount": admin_money(payment.amount),
                    "currency": payment.currency,
                    "tariff": invoice.tariff_id
                    and get_tariff_display_name(invoice.tariff_id)
                    or payment.order_description,
                    "trial": False,
                }
            )

        history.sort(key=lambda item: item["date_sort"], reverse=True)
        history = history[:100]
        for item in history:
            item.pop("date_sort", None)

        return JsonResponse({"status": "ok", "payments": history})
    finally:
        db_session.close()


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
        # То же событие, что пишет бот при подтверждении отмены: без него
        # отмены из кабинета были невидимы для аналитики (график churn,
        # ежедневный отчёт, таймлайн клиента) и занижали отток рекуррентов.
        add_event_log(session, db_user, analytics_event.ConfirmCancelAutopayClicked())
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
    previous_email = (payload.get("previous_email") or "").lower().strip()
    if not user_id or not new_email or "previous_email" not in payload:
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Ссылка подтверждения неверна. Введите email еще раз.",
        }
        request.session.modified = True
        return redirect("dashboard" if request.user.is_authenticated else "login")

    # Токен подтверждения живёт 15 минут, поэтому ссылки, выпущенные ДО выката
    # валидации в update_email, ещё какое-то время донесли бы битый адрес и до
    # users.email, и до запроса к панели. Проверяем и здесь.
    if not is_valid_email(new_email):
        logging.warning(
            "email confirmation rejected: invalid email format email=%s user_id=%s",
            new_email,
            user_id,
        )
        request.session["email_bind_modal"] = {
            "open": True,
            "error": "Проверьте адрес: похоже, в нём опечатка.",
            "email": new_email,
        }
        request.session.modified = True
        return redirect("dashboard" if request.user.is_authenticated else "login")

    session = session_factory()
    username = None
    try:
        db_user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not db_user:
            logging.warning(f"missing user {user_id} while confirming email")
            return redirect("login")

        # Повторный переход по уже применённой ссылке: почтовые сканеры и
        # антифишинг открывают GET заранее, и пользователь видел бы «ссылка
        # устарела» при уже сменённом адресе. Если это та же погашенная заявка
        # (nonce и адреса совпадают, used_at в пределах TTL) и users.email уже
        # равен запрошенному — тот же успех, что и при первом переходе.
        if normalize_email(db_user.email) == new_email and email_change_already_applied(
            session, db_user, payload, EMAIL_CONFIRMATION_MAX_AGE_SECONDS
        ):
            logging.info(
                "repeated confirmation of an already applied email change user_id=%s",
                user_id,
            )
            if request.user.is_authenticated and request.user.id == db_user.id:
                request.user.email = new_email
            else:
                authorize_user_session(request, db_user)
            return redirect("dashboard")

        if normalize_email(db_user.email) != normalize_email(previous_email):
            logging.warning(
                "stale or replayed email confirmation rejected for user %s",
                user_id,
            )
            request.session["email_bind_modal"] = {
                "open": True,
                "error": "Эта ссылка уже использована или устарела. Введите email ещё раз.",
                "email": new_email,
            }
            request.session.modified = True
            return redirect("dashboard" if request.user.is_authenticated else "login")

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

        if not consume_email_change(session, db_user, payload, EMAIL_CONFIRMATION_MAX_AGE_SECONDS):
            request.session["email_bind_modal"] = {"open": True, "email": new_email,
                "error": "Ссылка уже использована или устарела. Запросите новое письмо."}
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

    # The transaction above saved the outbox flag. A worker retries on failure.
    try:
        sync_email_change(session_factory, rwms_client, user_id, proto.UpdateUserRequest)
    except Exception:
        logging.exception("email reconciliation deferred user_id=%s", user_id)

    return redirect("dashboard")


def _hwid_device_to_dict(device) -> dict:
    def ts_iso(ts):
        try:
            if ts.seconds or ts.nanos:
                return ts.ToDatetime().isoformat() + "Z"
        except Exception:
            pass
        return None

    return {
        "hwid": device.hwid,
        "platform": device.platform or "",
        "os_version": device.os_version or "",
        "device_model": device.device_model or "",
        "user_agent": device.user_agent or "",
        "created_at": ts_iso(device.created_at),
        "updated_at": ts_iso(device.updated_at),
    }


def _cabinet_rw_subscription(request):
    """UserResponse подписки текущего пользователя из панели.

    None — только достоверный NOT_FOUND (подписки нет в панели). При
    недоступности RWMS/панели бросает RwmsUnavailableError: вызывающие
    эндпоинты обязаны отвечать «данные временно недоступны», а не
    «подписки нет» (Политика: БД — истина по времени, панель — истина
    по существованию ключа)."""
    return rwms_client.get_user_by_username_strict(request.user.username)


def _cabinet_rwms_unavailable_response():
    return JsonResponse(
        {
            "status": "error",
            "message": "Данные временно недоступны, попробуйте позже",
        },
        status=503,
    )


# Панельный fallback-лимит HWID общий для всех подписок и меняется только
# руками в панели — кешируем, чтобы не дёргать RWMS на каждый запрос кабинета.
_HWID_SETTINGS_CACHE_TTL = 600  # секунд
_hwid_settings_cache = {"value": None, "expires_at": 0.0}


def _panel_hwid_fallback_limit():
    now = monotonic()
    if now < _hwid_settings_cache["expires_at"]:
        return _hwid_settings_cache["value"]
    reply = rwms_client.get_hwid_settings()
    if reply is None:
        # RWMS недоступен — не кешируем отказ, попробуем при следующем запросе
        return None
    value = (
        reply.fallback_device_limit
        if reply.enabled
        and reply.HasField("fallback_device_limit")
        and reply.fallback_device_limit > 0
        else None
    )
    _hwid_settings_cache["value"] = value
    _hwid_settings_cache["expires_at"] = now + _HWID_SETTINGS_CACHE_TTL
    return value


def _cabinet_device_limit(subscription):
    """Реальный лимит устройств подписки: личный hwid_device_limit,
    иначе глобальный fallback панели, и лишь затем продуктовая константа."""
    if (
        subscription.HasField("hwid_device_limit")
        and subscription.hwid_device_limit > 0
    ):
        return subscription.hwid_device_limit
    panel_limit = _panel_hwid_fallback_limit()
    if panel_limit:
        return panel_limit
    return settings.CABINET_DEVICE_LIMIT_FALLBACK


@login_required(login_url="/login/")
def cabinet_devices(request):
    """Список HWID-устройств подписки текущего пользователя."""
    try:
        subscription = _cabinet_rw_subscription(request)
    except RwmsUnavailableError:
        return _cabinet_rwms_unavailable_response()
    if subscription is None:
        return JsonResponse(
            {"status": "error", "message": "subscription not found"}, status=404
        )

    limit = _cabinet_device_limit(subscription)

    resp = rwms_client.get_user_hwid_devices(subscription.uuid)
    if resp is None:
        return JsonResponse(
            {"status": "error", "message": "devices unavailable"}, status=502
        )

    return JsonResponse(
        {
            "status": "ok",
            "total": resp.total,
            "limit": limit,
            "devices": [_hwid_device_to_dict(d) for d in resp.devices],
        }
    )


@login_required(login_url="/login/")
def cabinet_device_delete(request):
    """Удаление одного HWID-устройства из подписки текущего пользователя.

    Удаление строго в рамках uuid собственной подписки пользователя —
    чужие устройства недостижимы by design.
    """
    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "method not allowed"}, status=405
        )

    hwid = (request.POST.get("hwid") or "").strip()
    if not hwid:
        return JsonResponse(
            {"status": "error", "message": "hwid required"}, status=400
        )

    try:
        subscription = _cabinet_rw_subscription(request)
    except RwmsUnavailableError:
        return _cabinet_rwms_unavailable_response()
    if subscription is None:
        return JsonResponse(
            {"status": "error", "message": "subscription not found"}, status=404
        )

    logging.info(
        "cabinet device delete requested: user_id=%s rw_uuid=%s hwid=%s",
        request.user.id,
        subscription.uuid,
        hwid,
    )

    resp = rwms_client.delete_user_hwid_device(subscription.uuid, hwid)
    if resp is None:
        return JsonResponse(
            {"status": "error", "message": "delete failed"}, status=502
        )

    limit = _cabinet_device_limit(subscription)

    return JsonResponse(
        {
            "status": "ok",
            "total": resp.total,
            "limit": limit,
            "devices": [_hwid_device_to_dict(d) for d in resp.devices],
        }
    )


@login_required(login_url="/login/")
def create_support_ticket(request):
    if request.method != "POST":
        return dashboard_support_redirect()

    subject = request.POST.get("subject", "").strip()
    message = request.POST.get("message", "").strip()
    uploaded_files = request.FILES.getlist("attachments")

    if not message:
        return dashboard_support_redirect()

    attachment_error = validate_support_attachments(uploaded_files)
    if attachment_error:
        return support_attachment_validation_response(request, attachment_error)

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
                uploaded_files,
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
    uploaded_files = request.FILES.getlist("attachments")
    if not message:
        return dashboard_support_redirect()
    attachment_error = validate_support_attachments(uploaded_files)
    if attachment_error:
        return support_attachment_validation_response(request, attachment_error)

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
                uploaded_files,
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

        return support_attachment_file_response(attachment, path)
    finally:
        db_session.close()


def support_admin_login_rate_buckets(request, login_name):
    """Блокирующие лимиты входа в админку: IP и пара «аккаунт+IP».

    Бакет по аккаунту без IP не блокирует: иначе чужие неудачные попытки
    (включая общий пароль с пустым логином) запирали бы вход сотрудникам.
    """
    address = client_ip(request) or "unknown"
    window = settings.ADMIN_LOGIN_RATE_WINDOW_SECONDS
    return (
        ("ip", address, settings.ADMIN_LOGIN_IP_RATE_LIMIT, window),
        (
            "account-ip",
            f"{login_name or 'shared'}|{address}",
            settings.ADMIN_LOGIN_ACCOUNT_RATE_LIMIT,
            window,
        ),
    )


def record_support_admin_login_failure(login_buckets, login_name):
    """Неудачная попытка расходует лимиты IP и «аккаунт+IP».

    Общий счётчик аккаунта со всех IP только поднимает ALERT при первом
    превышении ADMIN_LOGIN_ACCOUNT_ALERT_LIMIT в окне и вход не блокирует.
    Логин и пароль в лог не пишутся, только ключевой отпечаток аккаунта.
    """
    account = login_name or "shared"
    window = settings.ADMIN_LOGIN_RATE_WINDOW_SECONDS
    alert_limit = settings.ADMIN_LOGIN_ACCOUNT_ALERT_LIMIT
    counts = rate_limit_hit(
        "admin-login",
        tuple(login_buckets) + (("account", account, alert_limit, window),),
    )
    if alert_limit and alert_limit > 0 and counts.get("account") == alert_limit + 1:
        logging.error(
            "ALERT: support admin login failures for one account exceeded %s "
            "within %ss from all IPs (account_digest=%s); login is not blocked",
            alert_limit,
            window,
            rate_limit_log_digest(account),
        )


def support_admin_login(request):
    if support_admin_is_authenticated(request):
        auth_response = require_support_admin(request)
        if auth_response:
            return auth_response
        return redirect("support_admin_tickets")

    if request.method == "POST":
        password = request.POST.get("password", "")
        login_name = (request.POST.get("login") or "").strip().lower()
        login_buckets = support_admin_login_rate_buckets(request, login_name)
        # До проверки пароля лимиты только читаются: успешный вход ничего не
        # расходует, счётчики растут лишь на неудачных попытках ниже.
        limited, retry_after = rate_limit_peek("admin-login", login_buckets)
        if limited:
            logging.warning(
                "support admin login rate limited (account_digest=%s ip=%s)",
                rate_limit_log_digest(login_name or "shared"),
                admin_client_ip(request),
            )
            response = render(
                request,
                "support_admin_login.html",
                {"error": "Слишком много попыток. Подождите и попробуйте снова."},
                status=429,
            )
            response["Retry-After"] = str(retry_after)
            return response

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
                    cycle_key = getattr(request.session, "cycle_key", None)
                    if callable(cycle_key):
                        cycle_key()
                    request.session[SUPPORT_ADMIN_SESSION_KEY] = True
                    request.session[SUPPORT_ADMIN_ROLE_SESSION_KEY] = (
                        SUPPORT_ADMIN_ACCOUNT_ROLE_MAP.get(
                            account.role, SUPPORT_ADMIN_ROLE_SUPPORT
                        )
                    )
                    request.session[SUPPORT_ADMIN_ACCOUNT_SESSION_KEY] = account.login
                    request.session[SUPPORT_ADMIN_AUTH_HASH_SESSION_KEY] = (
                        support_admin_auth_fingerprint(
                            f"account:{account.login}", account.password_hash
                        )
                    )
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
            record_support_admin_login_failure(login_buckets, login_name)
            logging.warning(
                "admin account login failed (account_digest=%s)",
                rate_limit_log_digest(login_name),
            )
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
            cycle_key = getattr(request.session, "cycle_key", None)
            if callable(cycle_key):
                cycle_key()
            request.session[SUPPORT_ADMIN_SESSION_KEY] = True
            request.session[SUPPORT_ADMIN_ROLE_SESSION_KEY] = SUPPORT_ADMIN_ROLE_ADMIN
            request.session.pop(SUPPORT_ADMIN_ACCOUNT_SESSION_KEY, None)
            request.session[SUPPORT_ADMIN_AUTH_HASH_SESSION_KEY] = (
                support_admin_auth_fingerprint(
                    f"shared:{SUPPORT_ADMIN_ROLE_ADMIN}",
                    settings.SUPPORT_ADMIN_PASSWORD,
                )
            )
            request.session.modified = True
            return redirect("support_admin_tickets")

        if settings.SUPPORT_STAFF_PASSWORD and hmac.compare_digest(
            password,
            settings.SUPPORT_STAFF_PASSWORD,
        ):
            cycle_key = getattr(request.session, "cycle_key", None)
            if callable(cycle_key):
                cycle_key()
            request.session[SUPPORT_ADMIN_SESSION_KEY] = True
            request.session[SUPPORT_ADMIN_ROLE_SESSION_KEY] = SUPPORT_ADMIN_ROLE_SUPPORT
            request.session.pop(SUPPORT_ADMIN_ACCOUNT_SESSION_KEY, None)
            request.session[SUPPORT_ADMIN_AUTH_HASH_SESSION_KEY] = (
                support_admin_auth_fingerprint(
                    f"shared:{SUPPORT_ADMIN_ROLE_SUPPORT}",
                    settings.SUPPORT_STAFF_PASSWORD,
                )
            )
            request.session.modified = True
            return redirect("support_admin_tickets")

        record_support_admin_login_failure(login_buckets, login_name)
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
    clear_support_admin_session(request)
    return redirect("support_admin_login")


SUPPORT_ADMIN_TICKETS_PAGE_SIZE = 50
SUPPORT_ADMIN_TICKETS_MAX_PAGE_SIZE = 200


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
            "support_tickets_has_more": tickets_data["has_more"],
            "support_tickets_next_updated_at": tickets_data["next_updated_at"],
            "support_tickets_next_id": tickets_data["next_id"],
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


def _support_ticket_cursor(updated_at_value, ticket_id_value):
    """Parse a stable `(updated_at, id)` keyset cursor from query params."""
    if not updated_at_value and not ticket_id_value:
        return None
    if not updated_at_value or not ticket_id_value:
        raise ValueError("Неполный курсор списка тикетов")
    try:
        updated_at = datetime.fromisoformat(str(updated_at_value))
        ticket_id = int(ticket_id_value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Некорректный курсор списка тикетов") from error
    if updated_at.tzinfo is not None:
        updated_at = updated_at.astimezone(timezone.utc).replace(tzinfo=None)
    if ticket_id < 1:
        raise ValueError("Некорректный курсор списка тикетов")
    return updated_at, ticket_id


def load_support_admin_tickets(
    status_filter,
    *,
    limit=SUPPORT_ADMIN_TICKETS_PAGE_SIZE,
    before_updated_at=None,
    before_id=None,
):
    limit = min(max(int(limit), 1), SUPPORT_ADMIN_TICKETS_MAX_PAGE_SIZE)
    cursor = _support_ticket_cursor(before_updated_at, before_id)
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

        if cursor is not None:
            cursor_updated_at, cursor_id = cursor
            query = query.filter(
                or_(
                    SupportTicket.updated_at < cursor_updated_at,
                    and_(
                        SupportTicket.updated_at == cursor_updated_at,
                        SupportTicket.id < cursor_id,
                    ),
                )
            )
        rows = (
            query.order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc())
            .limit(limit + 1)
            .all()
        )
        has_more = len(rows) > limit
        tickets = rows[:limit]
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

        last_ticket = tickets[-1][0] if tickets else None
        return {
            "tickets": tickets,
            "ticket_payloads": [
                support_admin_ticket_payload(ticket, user) for ticket, user in tickets
            ],
            "status_filter": status_filter,
            "open_count": open_count,
            "closed_count": closed_count,
            "has_more": has_more,
            "next_updated_at": (
                last_ticket.updated_at.isoformat()
                if has_more and last_ticket is not None
                else ""
            ),
            "next_id": last_ticket.id if has_more and last_ticket is not None else None,
        }
    finally:
        db_session.close()


def support_admin_tickets_json(request):
    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    try:
        limit = int(request.GET.get("limit") or SUPPORT_ADMIN_TICKETS_PAGE_SIZE)
        tickets_data = load_support_admin_tickets(
            request.GET.get("status", "open"),
            limit=limit,
            before_updated_at=request.GET.get("before_updated_at"),
            before_id=request.GET.get("before_id"),
        )
    except (TypeError, ValueError, OverflowError) as error:
        return JsonResponse(
            {"status": "error", "message": str(error) or "Некорректная страница"},
            status=400,
        )

    response = JsonResponse(
        {
            "status": "ok",
            "status_filter": tickets_data["status_filter"],
            "open_count": tickets_data["open_count"],
            "closed_count": tickets_data["closed_count"],
            "tickets": tickets_data["ticket_payloads"],
            "has_more": tickets_data["has_more"],
            "next_cursor": {
                "updated_at": tickets_data["next_updated_at"],
                "id": tickets_data["next_id"],
            },
        }
    )
    # Hash the representation actually sent: all rows, user fields and cursors.
    etag = f'"{hashlib.sha256(response.content).hexdigest()}"'
    # GZipMiddleware делает ETag слабым (W/"…"), и браузер присылает его в
    # If-None-Match именно так; значений может быть и несколько. Для
    # If-None-Match сравнение по RFC 9110 слабое.
    client_etags = parse_etags(request.headers.get("If-None-Match", ""))
    if client_etags == ["*"] or etag in {
        value.removeprefix("W/") for value in client_etags
    }:
        response = HttpResponse(status=304)
    response["Cache-Control"] = "private, no-cache"
    response["Vary"] = "Cookie"
    response["ETag"] = etag
    return response


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
        return resolve_runtime_tariffs(db_session, tariffs)
    finally:
        if should_close_session:
            db_session.close()


def admin_dt(value):
    if not value:
        return None
    if value.tzinfo:
        value = value.replace(tzinfo=None)
    return value


def admin_naive_utc(value):
    """Время к naive UTC — как хранится большинство колонок проекта.

    В отличие от admin_dt, который просто срезает tzinfo, здесь значение
    сначала переводится в UTC. Часть колонок (wata_transactions.payment_time)
    объявлена timestamptz, и psycopg2 отдаёт их в TimeZone сессии БД: срезать
    у такого значения tzinfo значит сдвинуть метку на смещение сессии.
    """
    if not value:
        return None
    if value.tzinfo:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# Админка работает по московскому времени (UTC+3, сезонных переходов нет).
# В БД все таймстампы хранятся naive UTC; смещение применяется на границах:
# в метках времени (admin_date_label), в разбиении на дни/недели/месяцы в
# аналитике (admin_stats_bucket_sql + границы периодов) и в текстовых подписях.
ADMIN_TZ_OFFSET = timedelta(hours=3)
ADMIN_TZ_LABEL = "МСК"


def admin_msk_today():
    return (datetime.utcnow() + ADMIN_TZ_OFFSET).date()


def admin_date_label(value, with_time=True):
    value = admin_dt(value)
    if not value:
        return "Нет данных"
    value = value + ADMIN_TZ_OFFSET
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


def admin_stats_msk_sql(value):
    """naive-UTC таймстамп БД → naive-МСК: timezone('Europe/Moscow',
    timezone('UTC', value)). Бакеты аналитики режутся по московским суткам."""
    return func.timezone("Europe/Moscow", func.timezone("UTC", value))


def admin_stats_bucket_sql(value, granularity):
    value = admin_stats_msk_sql(value)
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


# Подписи меток трафика: в событиях лежит только номер (TS_217), а что это за
# канал — знал только владелец. Хранятся в traffic_sources (id = номер метки,
# name — описание, budget — бюджет, ₽), редактируются прямо в «Источниках
# трафика»; в API stats к каждому источнику подмешиваются name и budget.
TRAFFIC_SOURCE_NAME_MAX = 256


def admin_traffic_source_notes(db_session):
    """{"217": {"name": ..., "budget": ...}} по всем строкам traffic_sources."""
    try:
        rows = db_session.query(TrafficSource).all()
    except Exception:
        logging.exception("failed to load traffic source notes")
        return {}
    return {
        str(row.id): {"name": (row.name or "").strip(), "budget": row.budget}
        for row in rows
    }


def admin_attach_source_notes(db_session, sources):
    notes = admin_traffic_source_notes(db_session)
    for source in sources:
        key = source.get("traffic_source")
        note = notes.get(str(key)) if key is not None else None
        source["name"] = note["name"] if note else ""
        source["budget"] = note["budget"] if note else None
    return sources


def admin_stats_sales_series_totals(buckets, unique_paying_users=0):
    """Собирает кассовые итоги из тех же бакетов, которые показаны на графике."""
    totals = {
        "payments": 0,
        "revenue": 0,
        "unique_paying_users": int(unique_paying_users or 0),
        "tariffs": {},
    }
    for bucket in buckets:
        totals["payments"] += int(bucket.get("payments") or 0)
        totals["revenue"] += admin_money(bucket.get("revenue"))
        for tariff in bucket.get("tariffs") or []:
            name = tariff.get("name") or "Без тарифа"
            totals["tariffs"][name] = totals["tariffs"].get(name, 0) + int(
                tariff.get("count") or 0
            )
    return totals


def admin_stats_apply_sales_mode(totals, total_tariffs, sales_series):
    """Подменяет только кассовые KPI, сохраняя когортные метрики воронки."""
    selected_totals = dict(totals)
    selected_tariffs = dict(total_tariffs)
    cohort_unique_paying_users = int(totals.get("unique_paying_users") or 0)

    if sales_series.get("mode") == "absolute":
        sales_totals = sales_series.get("totals") or {}
        selected_totals["payments"] = int(sales_totals.get("payments") or 0)
        selected_totals["revenue"] = admin_money(sales_totals.get("revenue"))
        selected_totals["unique_paying_users"] = int(
            sales_totals.get("unique_paying_users") or 0
        )
        selected_tariffs = dict(sales_totals.get("tariffs") or {})

    return selected_totals, selected_tariffs, cohort_unique_paying_users


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
    # Границы периода — московские сутки, в UTC-времени БД это −3 часа.
    start_datetime = datetime.combine(start_date, time.min) - ADMIN_TZ_OFFSET
    end_datetime = datetime.combine(end_date, time.max) - ADMIN_TZ_OFFSET
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
    unique_paying_users = (
        db_session.query(func.count(func.distinct(payer_events.c.user_id))).scalar()
        or 0
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
        "totals": admin_stats_sales_series_totals(
            output_buckets, unique_paying_users
        ),
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

# Верхняя граница лимита трафика пробных (ГиБ). Значение в панель уезжает как
# traffic_limit_bytes = round(ГиБ × 1024³) в int64: 100 000 ГиБ ≈ 1.07e14 байт —
# запас к 2^63−1 ≈ 9.2e18 в ~85 000 раз, при этом любой реальный лимит внутри.
TRIAL_TRAFFIC_LIMIT_MAX_GB = 100_000


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
        # float() принимает "nan"/"inf": такое значение сохранилось бы в БД
        # мусором, а parse_positive_float_setting в сервисах молча ушёл бы на
        # дефолт — отклоняем на входе.
        if not math.isfinite(float_value):
            return None, "Значение должно быть конечным числом"
        if float_value <= 0:
            return None, "Значение должно быть больше нуля"
        if (
            key == TRIAL_TRAFFIC_LIMIT_GB_SETTING
            and float_value > TRIAL_TRAFFIC_LIMIT_MAX_GB
        ):
            # Верхняя граница: traffic_limit_bytes = round(ГиБ × 1024³) обязан
            # влезать в int64 proto (иначе ValueError до RPC ломает создание
            # ВСЕХ новых триалов); 100 000 ГиБ ≈ 1.07e14 байт при 2^63 ≈ 9.2e18.
            return None, (
                f"Лимит не может превышать {TRIAL_TRAFFIC_LIMIT_MAX_GB} ГиБ"
            )
        # 10 знаков: лимит пробных в МиБ (100 МиБ = 0.09765625 ГиБ) не должен
        # терять точность при хранении в ГиБ.
        normalized = ("%.10f" % float_value).rstrip("0").rstrip(".")
        if float(normalized) <= 0:
            # Слишком маленькое значение округлилось до «0»: в БД оно легло бы
            # нулём и сервисы молча ушли бы на дефолт.
            return None, "Значение слишком мало: округляется до нуля"
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


# --- Антиабьюз: парная валидация и подписи -----------------------------------
#
# Обе защиты (лимит трафика новых пробных, алерты ip-guard по подсетям)
# управляются ТОЛЬКО отсюда (system_settings), по умолчанию выключены;
# выключение возвращает поведение «как раньше». Ключи, типы и дефолты —
# в common/models/settings.py (единый реестр для бота/сайта/user-notify/ip-guard).

ANTIABUSE_TRIAL_KEYS = (
    TRIAL_TRAFFIC_LIMIT_ENABLED_SETTING,
    TRIAL_TRAFFIC_LIMIT_GB_SETTING,
    TRIAL_TRAFFIC_LIMIT_STRATEGY_SETTING,
)
ANTIABUSE_IPGUARD_KEYS = (
    # Главный рубильник над ВСЕМИ слоями и период прогона — карточка-рубильник
    # идёт первой на вкладке, поэтому и ключи первые.
    IPGUARD_ALERTS_ENABLED_SETTING,
    IPGUARD_CHECK_INTERVAL_SECONDS_SETTING,
    # Суточный слой: свой тумблер публикации, чтобы выключение подсетей не
    # гасило всплеск и гео (и наоборот).
    IPGUARD_SUBNETS_ENABLED_SETTING,
    IPGUARD_ALERT_SEGMENT_SETTING,
    IPGUARD_SUBNETS_PER_HWID_SETTING,
    IPGUARD_WINDOW_HOURS_SETTING,
    IPGUARD_ALERT_COOLDOWN_HOURS_SETTING,
    IPGUARD_WARNINGS_ENABLED_SETTING,
    IPGUARD_WARNING_SUBNETS_PER_HWID_SETTING,
    # v3: слои «всплеск» и «гео», автобан с предохранителями и ручные
    # исключения адресов. Порядок = порядок карточек на вкладке.
    IPGUARD_BURST_ENABLED_SETTING,
    IPGUARD_BURST_WINDOW_MINUTES_SETTING,
    IPGUARD_BURST_IPS_PER_HWID_SETTING,
    IPGUARD_BURST_CONFIRMATIONS_SETTING,
    # Нижняя граница по подсетям для всплеска: первый боевой алерт слоя был
    # ложным (4 IP за минуту из одной /24 — ротация CGNAT-оператора).
    IPGUARD_BURST_MIN_SUBNETS_SETTING,
    IPGUARD_GEO_ENABLED_SETTING,
    IPGUARD_GEO_WINDOW_MINUTES_SETTING,
    IPGUARD_GEO_MIN_REGIONS_SETTING,
    IPGUARD_AUTOBAN_ENABLED_SETTING,
    IPGUARD_AUTOBAN_SEGMENT_SETTING,
    IPGUARD_AUTOBAN_STEPS_MINUTES_SETTING,
    IPGUARD_AUTOBAN_ESCALATION_WINDOW_HOURS_SETTING,
    IPGUARD_AUTOBAN_MAX_PER_HOUR_SETTING,
    # Порог автобана — одно абсолютное число по суточному слою (подсети за
    # окно), гистерезис по прогонам и пробный режим. Ключи идут в карточке
    # автобана, поэтому и в реестре — сразу за её предохранителем.
    IPGUARD_AUTOBAN_MIN_SUBNETS_SETTING,
    IPGUARD_AUTOBAN_CONFIRMATIONS_SETTING,
    IPGUARD_AUTOBAN_DRY_RUN_SETTING,
    IPGUARD_MAX_ALERTS_PER_HOUR_SETTING,
    IPGUARD_EXCLUDED_IPS_SETTING,
    # Исключённые подписки — второе поле карточки исключений.
    IPGUARD_EXCLUDED_USERNAMES_SETTING,
)
# Пара порогов ip-guard: предупреждение (suspicious) обязано быть СТРОГО
# меньше алерта, иначе предупреждение никогда не отделить от алерта.
IPGUARD_THRESHOLD_PAIR_KEYS = (
    IPGUARD_SUBNETS_PER_HWID_SETTING,
    IPGUARD_WARNING_SUBNETS_PER_HWID_SETTING,
)
IPGUARD_THRESHOLD_DEFAULTS = {
    IPGUARD_SUBNETS_PER_HWID_SETTING: DEFAULT_IPGUARD_SUBNETS_PER_HWID,
    IPGUARD_WARNING_SUBNETS_PER_HWID_SETTING: DEFAULT_IPGUARD_WARNING_SUBNETS_PER_HWID,
}
ANTIABUSE_SETTING_KEYS = ANTIABUSE_TRIAL_KEYS + ANTIABUSE_IPGUARD_KEYS

# Подписи стратегий сброса — как в панели Remnawave. Ключи — значения
# настройки trial_traffic_limit_strategy (нижний регистр); имена proto — через
# TRIAL_TRAFFIC_LIMIT_STRATEGY_PROTO_NAMES (знает MONTH_ROLLING = 4).
TRAFFIC_LIMIT_STRATEGY_LABELS = {
    TRIAL_TRAFFIC_LIMIT_STRATEGY_NO_RESET: "Никогда",
    TRIAL_TRAFFIC_LIMIT_STRATEGY_DAY: "Ежедневно",
    TRIAL_TRAFFIC_LIMIT_STRATEGY_WEEK: "Еженедельно",
    TRIAL_TRAFFIC_LIMIT_STRATEGY_MONTH: "Ежемесячно",
    TRIAL_TRAFFIC_LIMIT_STRATEGY_MONTH_ROLLING: "Ежемесячно по дате создания",
}
IPGUARD_SEGMENT_LABELS = {
    IPGUARD_ALERT_SEGMENT_NEVER_PAID: "Только пробные без платежа",
    IPGUARD_ALERT_SEGMENT_ALL: "Все подписки",
}
_PROTO_STRATEGY_NAME_TO_KEY = {
    name: key for key, name in TRIAL_TRAFFIC_LIMIT_STRATEGY_PROTO_NAMES.items()
}


def admin_traffic_limit_strategy_key(value):
    """proto TrafficLimitStrategy (число или имя) → ключ настройки (day, ...).
    Неизвестное значение (новая стратегия панели) — строкой, без ValueError."""
    if value is None:
        return None
    if isinstance(value, str):
        name = value
    else:
        try:
            name = proto.TrafficLimitStrategy.Name(int(value))
        except (ValueError, TypeError):
            return str(value)
    return _PROTO_STRATEGY_NAME_TO_KEY.get(name.upper(), name.lower())


def admin_traffic_limit_strategy_label(strategy_key):
    if strategy_key is None:
        return "—"
    return TRAFFIC_LIMIT_STRATEGY_LABELS.get(str(strategy_key).lower(), str(strategy_key))


def admin_user_status_name(value):
    """proto UserStatus → имя (ACTIVE/DISABLED/LIMITED/EXPIRED); None — нет данных."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.upper()
    try:
        return proto.UserStatus.Name(int(value))
    except (ValueError, TypeError):
        return str(value)


def admin_safe_int(value, default=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number


def admin_format_gib(value):
    return ("%.10f" % float(value)).rstrip("0").rstrip(".")


def admin_traffic_limit_label(limit):
    return (
        f"{admin_format_gib(limit.limit_gb)} ГиБ · "
        f"{admin_traffic_limit_strategy_label(limit.strategy_key).lower()}"
    )


def admin_ipguard_threshold_value(db_session, key, pending=None):
    """Значение порога ip-guard для парной проверки: из этого же сохранения
    (``pending``: {key: нормализованное}), иначе из БД, иначе дефолт common."""
    if pending and key in pending:
        return pending[key]
    setting = db_session.get(SystemSetting, key)
    if setting is not None:
        return setting.value
    return IPGUARD_THRESHOLD_DEFAULTS[key]


def admin_validate_ipguard_threshold_pair(db_session, key, normalized_value, pending=None):
    """Порог предупреждения ip-guard строго меньше порога алерта
    (``ipguard_warning_threshold_is_valid``). Второе значение пары берётся из
    этого же сохранения, иначе из БД, иначе дефолт."""
    values = {key: normalized_value}
    for other_key in IPGUARD_THRESHOLD_PAIR_KEYS:
        if other_key not in values:
            values[other_key] = admin_ipguard_threshold_value(
                db_session, other_key, pending
            )
    warning = values[IPGUARD_WARNING_SUBNETS_PER_HWID_SETTING]
    alert = values[IPGUARD_SUBNETS_PER_HWID_SETTING]
    if ipguard_warning_threshold_is_valid(warning, alert):
        return None
    return (
        "Порог предупреждения ip-guard (ipguard_warning_subnets_per_hwid="
        f"{warning}) должен быть положительным и строго меньше порога алерта "
        f"(ipguard_subnets_per_hwid={alert})"
    )


def admin_ipguard_csv_items(value):
    """Элементы CSV-настройки ip-guard так, как их видит парсер common
    (разделители «,» и «;», пустые отброшены)."""
    return [
        item.strip()
        for item in str(value if value is not None else "").replace(";", ",").split(",")
        if item.strip()
    ]


def admin_validate_ipguard_autoban_steps_value(value):
    """Лестница наказаний автобана: текст ошибки либо None.

    ``parse_ipguard_autoban_steps`` намеренно МОЛЧА выбрасывает мусорные шаги,
    а на пустом результате отдаёт дефолтную лестницу — для рантайма это
    правильно (включённый автобан не остаётся без длительности), но в админке
    это «сохранил 0, получил 15,60,1440 и не заметил». Поэтому на входе
    требуем, чтобы разбор совпал с введённым списком.
    """
    items = admin_ipguard_csv_items(value)
    if not items:
        return (
            "Лестница наказаний ip-guard пуста: укажите хотя бы одну "
            "длительность бана в минутах (например, 15,60,1440)"
        )
    if len(items) > IPGUARD_AUTOBAN_MAX_STEPS:
        return f"Шагов в лестнице не больше {IPGUARD_AUTOBAN_MAX_STEPS}"
    numbers = []
    for item in items:
        try:
            number = int(item)
        except ValueError:
            return f"Шаг «{item}» не число: нужны минуты через запятую"
        if number <= 0:
            return f"Шаг «{item}»: длительность бана должна быть больше нуля"
        if number > IPGUARD_AUTOBAN_STEP_MINUTES_MAX:
            return (
                f"Шаг «{item}» больше предела "
                f"{IPGUARD_AUTOBAN_STEP_MINUTES_MAX} минут (неделя)"
            )
        numbers.append(number)
    if tuple(numbers) != parse_ipguard_autoban_steps(value):
        # Страховка от расхождения с парсером common: сохранять значение,
        # которое ip-guard прочитает иначе, нельзя.
        return "Лестница наказаний ip-guard не читается: исправьте список"
    return None


# Границы списка исключений ip-guard. Минимальный префикс: маска шире /8 (и
# /32 у IPv6) исключает такой кусок интернета, что детектор фактически
# выключается — это почти наверняка опечатка, а не намерение. Максимальная
# длина — под колонку system_settings.value (VARCHAR(512)).
IPGUARD_EXCLUDED_MIN_PREFIX_V4 = 8
IPGUARD_EXCLUDED_MIN_PREFIX_V6 = 32
IPGUARD_EXCLUDED_MAX_LENGTH = 500


def admin_validate_ipguard_excluded_ips_value(value):
    """Список исключений ip-guard: текст ошибки либо None.

    ``parse_ipguard_excluded_ips`` пропускает нераспознанные записи молча (одна
    опечатка не должна выключать список целиком) — в админке это значит
    «сохранил, а адрес не применился». Проверяем каждую запись тем же парсером
    и называем конкретную. Пустое значение допустимо: это «исключений нет».
    """
    items = admin_ipguard_csv_items(value)
    if len(items) > IPGUARD_MAX_EXCLUDED_ENTRIES:
        return f"Записей в списке исключений не больше {IPGUARD_MAX_EXCLUDED_ENTRIES}"
    for item in items:
        parsed = parse_ipguard_excluded_ips(item)
        if not parsed:
            return f"«{item}» не похоже на IP-адрес или подсеть (CIDR)"
        # Слишком широкая маска — это не исключение, а выключение ip-guard
        # целиком: «37.143.13.212/0» разбирается в 0.0.0.0/0, под неё попадает
        # весь интернет, наблюдения перестают записываться вовсе, и детектор
        # молча слепнет. Опечатка в одном символе не должна так стоить.
        network = ipaddress.ip_network(parsed[0], strict=False)
        min_prefix = (
            IPGUARD_EXCLUDED_MIN_PREFIX_V4
            if network.version == 4
            else IPGUARD_EXCLUDED_MIN_PREFIX_V6
        )
        if network.prefixlen < min_prefix:
            return (
                f"«{item}» — слишком широкая маска ({network}): так исключается "
                f"{network.num_addresses:,} адресов и ip-guard перестанет "
                f"видеть подключения. Минимум /{min_prefix}"
            ).replace(",", " ")
    # Значение целиком уходит в system_settings.value (VARCHAR(512)): длинный
    # список иначе падает на вставке, ничего не сохранив.
    normalized = ", ".join(items)
    if len(normalized) > IPGUARD_EXCLUDED_MAX_LENGTH:
        return (
            f"Список длиннее {IPGUARD_EXCLUDED_MAX_LENGTH} символов "
            f"({len(normalized)}) — сократите записи или объедините их в подсети"
        )
    return None


def admin_validate_ipguard_excluded_usernames_value(value):
    """Список исключённых подписок ip-guard: текст ошибки либо None.

    ``parse_ipguard_excluded_usernames`` пропускает мусор молча (одна опечатка
    не должна снимать исключение с остальных подписок) — в админке это значит
    «сохранил, а подписка не исключилась». Каждая запись проверяется тем же
    парсером и называется конкретно. Пустое значение допустимо: это «исключений
    нет» (очистка списка).
    """
    # Имя ключа — в каждом сообщении: парная ошибка уходит админу как есть,
    # без префикса «ключ: …» типовой валидации.
    key = IPGUARD_EXCLUDED_USERNAMES_SETTING
    items = admin_ipguard_csv_items(value)
    if len(items) > IPGUARD_MAX_EXCLUDED_USERNAMES:
        return (
            f"Подписок в списке исключений ({key}) не больше "
            f"{IPGUARD_MAX_EXCLUDED_USERNAMES}"
        )
    for item in items:
        if not parse_ipguard_excluded_usernames(item):
            return (
                f"«{item}» не похоже на юзернейм подписки ({key}): нужен "
                "числовой ID панели (как в строке «Юзернейм» алерта, напр. "
                "594514115) или имя без пробелов до 64 символов"
            )
    # Значение целиком уходит в system_settings.value (VARCHAR(512)).
    normalized = ", ".join(items)
    if len(normalized) > IPGUARD_EXCLUDED_MAX_LENGTH:
        return (
            f"Список подписок ({key}) длиннее {IPGUARD_EXCLUDED_MAX_LENGTH} "
            f"символов ({len(normalized)}) — сократите его"
        )
    return None


def admin_validate_antiabuse_setting_pair(
    db_session, key, normalized_value, pending=None
):
    """Парная проверка антиабьюз-настроек (по образцу порогов трафика).

    Включать тумблер можно только при корректных зависимых значениях в БД:
    лимит/стратегия для пробных, сегмент/подсети/окно/кулдаун для ip-guard,
    пара порогов для предупреждений ip-guard. Отсутствующий ключ — дефолт
    common, это нормально; битое значение в БД (руками/старой версией) при
    включении молча ушло бы в дефолт — вместо этого просим исправить его до
    включения. Пороги ip-guard (алерт / предупреждение) проверяются парой при
    сохранении любого из них: второе значение — из этого же сохранения
    (``pending``), иначе из БД, иначе дефолт.
    """
    if key in IPGUARD_THRESHOLD_PAIR_KEYS:
        return admin_validate_ipguard_threshold_pair(
            db_session, key, normalized_value, pending
        )
    if key == IPGUARD_WARNINGS_ENABLED_SETTING and normalized_value == "1":
        return admin_validate_ipguard_threshold_pair(
            db_session,
            IPGUARD_WARNING_SUBNETS_PER_HWID_SETTING,
            admin_ipguard_threshold_value(
                db_session, IPGUARD_WARNING_SUBNETS_PER_HWID_SETTING, pending
            ),
            pending,
        )
    # CSV-ключи ip-guard: общий валидатор проверяет только форму записи
    # («числа через запятую» / «строки через запятую»), а парсеры common молча
    # выбрасывают то, что не поняли, — без строгой проверки админ сохранит
    # значение, которое ip-guard прочитает иначе.
    if key == IPGUARD_AUTOBAN_STEPS_MINUTES_SETTING:
        return admin_validate_ipguard_autoban_steps_value(normalized_value)
    if key == IPGUARD_EXCLUDED_IPS_SETTING:
        return admin_validate_ipguard_excluded_ips_value(normalized_value)
    if key == IPGUARD_EXCLUDED_USERNAMES_SETTING:
        return admin_validate_ipguard_excluded_usernames_value(normalized_value)
    if key == TRIAL_TRAFFIC_LIMIT_ENABLED_SETTING and normalized_value == "1":
        checks = (
            (
                TRIAL_TRAFFIC_LIMIT_GB_SETTING,
                "Лимит трафика пробных (trial_traffic_limit_gb) в БД некорректен: "
                "исправьте его перед включением",
            ),
            (
                TRIAL_TRAFFIC_LIMIT_STRATEGY_SETTING,
                "Стратегия сброса (trial_traffic_limit_strategy) в БД некорректна: "
                "исправьте её перед включением",
            ),
        )
    elif (
        key in (IPGUARD_ALERTS_ENABLED_SETTING, IPGUARD_SUBNETS_ENABLED_SETTING)
        and normalized_value == "1"
    ):
        # Оба тумблера включают публикацию суточного слоя (главный рубильник —
        # ещё и остальных), поэтому зависимости у них одни и те же: битое
        # значение в БД молча ушло бы в дефолт, и админ увидел бы не свои числа.
        checks = (
            (
                IPGUARD_ALERT_SEGMENT_SETTING,
                "Сегмент ip-guard (ipguard_alert_segment) в БД некорректен: "
                "исправьте его перед включением",
            ),
            (
                IPGUARD_CHECK_INTERVAL_SECONDS_SETTING,
                "Период прогона (ipguard_check_interval_seconds) в БД "
                "некорректен: исправьте значение перед включением",
            ),
            (
                IPGUARD_SUBNETS_PER_HWID_SETTING,
                "Подсетей на HWID (ipguard_subnets_per_hwid) в БД некорректно: "
                "исправьте значение перед включением",
            ),
            (
                IPGUARD_WINDOW_HOURS_SETTING,
                "Окно ip-guard (ipguard_window_hours) в БД некорректно: "
                "исправьте значение перед включением",
            ),
            (
                IPGUARD_ALERT_COOLDOWN_HOURS_SETTING,
                "Кулдаун ip-guard (ipguard_alert_cooldown_hours) в БД некорректен: "
                "исправьте значение перед включением",
            ),
        )
    elif key == IPGUARD_BURST_ENABLED_SETTING and normalized_value == "1":
        checks = (
            (
                IPGUARD_BURST_WINDOW_MINUTES_SETTING,
                "Окно всплеска (ipguard_burst_window_minutes) в БД некорректно: "
                "исправьте значение перед включением",
            ),
            (
                IPGUARD_BURST_IPS_PER_HWID_SETTING,
                "IP на HWID во всплеске (ipguard_burst_ips_per_hwid) в БД "
                "некорректно: исправьте значение перед включением",
            ),
            (
                IPGUARD_BURST_CONFIRMATIONS_SETTING,
                "Подтверждений всплеска (ipguard_burst_confirmations) в БД "
                "некорректно: исправьте значение перед включением",
            ),
            (
                IPGUARD_BURST_MIN_SUBNETS_SETTING,
                "Минимум подсетей всплеска (ipguard_burst_min_subnets) в БД "
                "некорректно: исправьте значение перед включением",
            ),
        )
    elif key == IPGUARD_GEO_ENABLED_SETTING and normalized_value == "1":
        checks = (
            (
                IPGUARD_GEO_WINDOW_MINUTES_SETTING,
                "Окно гео-детекта (ipguard_geo_window_minutes) в БД некорректно: "
                "исправьте значение перед включением",
            ),
            (
                IPGUARD_GEO_MIN_REGIONS_SETTING,
                "Регионов для гео-детекта (ipguard_geo_min_regions) в БД "
                "некорректно: исправьте значение перед включением",
            ),
        )
    elif (key == IPGUARD_AUTOBAN_ENABLED_SETTING and normalized_value == "1") or (
        key == IPGUARD_AUTOBAN_DRY_RUN_SETTING and normalized_value == "0"
    ):
        # Лестницу проверяем строгим валидатором отдельно: общий CSV-int
        # пропускает «0» и отрицательные, а автобан по fallback-длительности —
        # это бан, которого админ не задавал. Автобан трогает живые подписки,
        # поэтому включение с сомнительными зависимостями запрещено. Выключение
        # пробного режима — тот же порог риска (с него баны становятся
        # боевыми), поэтому проверяется теми же зависимостями.
        if pending and IPGUARD_AUTOBAN_STEPS_MINUTES_SETTING in pending:
            steps_raw = pending[IPGUARD_AUTOBAN_STEPS_MINUTES_SETTING]
        else:
            steps_setting = db_session.get(
                SystemSetting, IPGUARD_AUTOBAN_STEPS_MINUTES_SETTING
            )
            steps_raw = steps_setting.value if steps_setting is not None else None
        if steps_raw is not None and admin_validate_ipguard_autoban_steps_value(
            steps_raw
        ):
            return (
                "Лестница наказаний (ipguard_autoban_steps_minutes) в БД "
                "некорректна: исправьте её перед включением автобана"
            )
        checks = (
            (
                IPGUARD_AUTOBAN_SEGMENT_SETTING,
                "Сегмент автобана (ipguard_autoban_segment) в БД некорректен: "
                "исправьте его перед включением",
            ),
            (
                IPGUARD_AUTOBAN_ESCALATION_WINDOW_HOURS_SETTING,
                "Окно эскалации (ipguard_autoban_escalation_window_hours) в БД "
                "некорректно: исправьте значение перед включением",
            ),
            (
                IPGUARD_AUTOBAN_MAX_PER_HOUR_SETTING,
                "Предохранитель автобанов (ipguard_autoban_max_per_hour) в БД "
                "некорректен: исправьте значение перед включением",
            ),
            (
                IPGUARD_AUTOBAN_MIN_SUBNETS_SETTING,
                "Порог автобана (ipguard_autoban_min_subnets) в БД некорректен: "
                "исправьте значение перед включением",
            ),
            (
                IPGUARD_AUTOBAN_CONFIRMATIONS_SETTING,
                "Подтверждений автобана (ipguard_autoban_confirmations) в БД "
                "некорректно: исправьте значение перед включением",
            ),
        )
    else:
        return None

    for other_key, message in checks:
        setting = db_session.get(SystemSetting, other_key)
        if setting is None:
            continue
        _, error = admin_validate_runtime_setting(other_key, setting.value)
        if error:
            return message
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


def admin_rwms_traffic_payload(username, client=None):
    """Фактическое потребление подписки из RWMS без локальных эвристик.

    Карточка клиента загружает этот payload отдельным запросом, поэтому
    недоступность панели не должна скрывать профиль, платежи и действия.
    RPC только читает UserResponse и никогда не изменяет подписку.
    """

    empty = {
        "available": False,
        "used_traffic_bytes": None,
        "lifetime_used_traffic_bytes": None,
        "first_connected": None,
        "traffic_limit_bytes": None,
        "traffic_limit_strategy": None,
        "traffic_limit_strategy_label": None,
        "status": None,
        "is_limited": False,
        "hwid_devices": None,
    }
    if not username:
        return empty

    rwms = client or rwms_client
    try:
        rwms_user = rwms.get_user_by_username(username)
    except Exception:
        logging.exception("support admin: failed to load RWMS traffic for %s", username)
        return empty
    if rwms_user is None:
        return empty

    def safe_bytes(value):
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0
        if not math.isfinite(number):
            return 0
        return max(0, int(number))

    # Дата первого подключения из панели (optional-поле proto): показывается
    # в карточке клиента и быстрой карточке «Трафика нод».
    first_connected = None
    try:
        if rwms_user.HasField("first_connected"):
            first_connected = admin_date_label(rwms_user.first_connected.ToDatetime())
    except (AttributeError, ValueError, TypeError):
        first_connected = None

    # Антиабьюз: лимит трафика, стратегия сброса и статус подписки (LIMITED —
    # лимит исчерпан) плюс число HWID-устройств — второй read-only RPC
    # (GetUserHwidDevices); его недоступность не прячет остальной payload.
    status = admin_user_status_name(getattr(rwms_user, "status", None))
    strategy_key = admin_traffic_limit_strategy_key(
        getattr(rwms_user, "traffic_limit_strategy", None)
    )
    hwid_devices = None
    user_uuid = getattr(rwms_user, "uuid", None)
    if user_uuid:
        try:
            devices = rwms.get_user_hwid_devices(user_uuid)
        except Exception:
            logging.exception(
                "support admin: failed to load hwid devices for %s", username
            )
            devices = None
        if devices is not None:
            hwid_devices = admin_safe_int(getattr(devices, "total", None))

    return {
        "available": True,
        "used_traffic_bytes": safe_bytes(rwms_user.used_traffic_bytes),
        "lifetime_used_traffic_bytes": safe_bytes(
            rwms_user.lifetime_used_traffic_bytes
        ),
        "first_connected": first_connected,
        "traffic_limit_bytes": safe_bytes(
            getattr(rwms_user, "traffic_limit_bytes", 0)
        ),
        "traffic_limit_strategy": strategy_key,
        "traffic_limit_strategy_label": admin_traffic_limit_strategy_label(
            strategy_key
        ),
        "status": status,
        "is_limited": status == "LIMITED",
        "hwid_devices": hwid_devices,
    }


# --- Антиабьюз: применение/снятие лимита трафика в панели ---------------------


def admin_rwms_apply_trial_limit(rw_user, limit, client=None):
    """UpdateUser(uuid, traffic_limit_bytes, traffic_limit_strategy) — БЕЗ
    active_internal_squads и статуса. Возвращает (изменено, сообщение);
    RuntimeError, если RWMS не принял запрос. Уже такой же лимит — no-op."""
    rwms = client or rwms_client
    current_bytes = admin_safe_int(getattr(rw_user, "traffic_limit_bytes", 0), 0)
    current_strategy = admin_traffic_limit_strategy_key(
        getattr(rw_user, "traffic_limit_strategy", None)
    )
    label = admin_traffic_limit_label(limit)
    if current_bytes == limit.limit_bytes and current_strategy == limit.strategy_key:
        return False, f"лимит уже {label}"
    response = rwms.update_user(
        proto.UpdateUserRequest(
            uuid=rw_user.uuid,
            traffic_limit_bytes=int(limit.limit_bytes),
            traffic_limit_strategy=limit.strategy,
        )
    )
    if response is None:
        raise RuntimeError("RWMS не применил лимит трафика")
    logging.info(
        "trial traffic limit applied from admin panel: uuid=%s limit=%s",
        rw_user.uuid,
        label,
    )
    return True, f"лимит {label}"


def admin_rwms_remove_traffic_limit(rw_user, client=None):
    """Снятие лимита «как было»: traffic_limit_bytes=0, NO_RESET, status=ACTIVE,
    БЕЗ active_internal_squads (пустой список RWMS трактует как «не менять» —
    ban-сквад временной блокировки не снимется).

    status=ACTIVE ставится только подписке ACTIVE/LIMITED (LIMITED — лимит
    исчерпан, доступ нужно вернуть). DISABLED (полная блокировка аккаунта) и
    EXPIRED не трогаем: снятие лимита не должно разблокировать подписку.
    """
    rwms = client or rwms_client
    current_bytes = admin_safe_int(getattr(rw_user, "traffic_limit_bytes", 0), 0)
    status = admin_user_status_name(getattr(rw_user, "status", None))
    if current_bytes == 0 and status != "LIMITED":
        return False, "лимита нет"
    request = proto.UpdateUserRequest(
        uuid=rw_user.uuid,
        traffic_limit_bytes=0,
        traffic_limit_strategy=proto.TrafficLimitStrategy.NO_RESET,
    )
    if status in (None, "ACTIVE", "LIMITED"):
        request.status = proto.UserStatus.ACTIVE
    response = rwms.update_user(request)
    if response is None:
        raise RuntimeError("RWMS не снял лимит трафика")
    logging.info(
        "traffic limit removed from admin panel: uuid=%s previous_bytes=%s status=%s",
        rw_user.uuid,
        current_bytes,
        status,
    )
    return True, "лимит снят" + (
        "" if status in (None, "ACTIVE", "LIMITED") else f" (статус {status} не менялся)"
    )


# --- Антиабьюз v2: управляемые лимиты (маркеры managed_traffic_limits) --------
#
# Ручные лимиты владельца неприкосновенны: автоматика (оплата, страховка
# user-notify, кнопки и массовые операции админки) снимает только лимит с
# маркером, у которого панель показывает ровно limit_bytes/strategy
# (is_managed). Маркер пишется в той же сессии, что и постановка лимита;
# история — event_logs traffic_limit_applied / traffic_limit_released.

MANAGED_LIMITS_MIGRATION_MESSAGE = (
    "Таблица managed_traffic_limits отсутствует: примените миграцию "
    "managed_traffic_limits и повторите действие (панель не тронута)"
)


class AdminManagedLimitsUnavailable(Exception):
    """Таблица маркеров исчезла между проверкой и записью — действие прервано."""


def admin_applied_by(request):
    """applied_by маркера для действий из карточки клиента: site:admin:<login>."""
    return f"{APPLIED_BY_SITE_ADMIN_PREFIX}{support_admin_actor(request)}"


def admin_bytes_label(value):
    """Объём по основанию 1024: 536870912 → «512 МиБ», 5368709120 → «5 ГиБ»."""
    number = admin_safe_int(value, 0) or 0
    if number <= 0:
        return "0"
    if number < 1024**3:
        return f"{admin_format_gib(number / 1024**2)} МиБ"
    return f"{admin_format_gib(number / 1024**3)} ГиБ"


def admin_panel_limit_label(rw_user):
    """Подпись лимита панели: «1 ГиБ · ежедневно»."""
    strategy_key = admin_traffic_limit_strategy_key(
        getattr(rw_user, "traffic_limit_strategy", None)
    )
    return (
        f"{admin_bytes_label(getattr(rw_user, 'traffic_limit_bytes', 0))} · "
        f"{admin_traffic_limit_strategy_label(strategy_key).lower()}"
    )


def admin_marker_snapshot(marker):
    """Копия полей маркера (SimpleNamespace) — переживает закрытие сессии."""
    if marker is None:
        return None
    return SimpleNamespace(
        user_id=marker.user_id,
        limit_bytes=int(marker.limit_bytes),
        strategy=marker.strategy,
        reason=marker.reason,
        release_on=marker.release_on,
        applied_by=marker.applied_by,
        applied_at=marker.applied_at,
        expires_at=marker.expires_at,
        note=marker.note,
    )


def admin_managed_limit_payload(marker, panel_bytes, panel_strategy, available=True):
    """Блок «управляемый / ручной лимит» для карточки клиента.

    kind: managed (маркер и панель совпадают) | manual (лимит в панели без
    маркера или маркер не совпадает — ставил владелец) | none (лимита нет) |
    unknown (панель недоступна или таблицы маркеров нет)."""
    marker_payload = None
    if marker is not None:
        marker_payload = {
            "reason": marker.reason,
            "applied_by": marker.applied_by,
            "applied_at": admin_date_label(marker.applied_at),
            "release_on": marker.release_on,
            "limit_bytes": int(marker.limit_bytes),
            "limit_label": admin_bytes_label(marker.limit_bytes),
            "strategy": admin_traffic_limit_strategy_key(marker.strategy),
            "expires_at": admin_date_label(marker.expires_at) if marker.expires_at else None,
            "note": marker.note,
        }
    if not available:
        return {
            "available": False,
            "kind": "unknown",
            "managed": False,
            "label": (
                "Маркеры недоступны: примените миграцию managed_traffic_limits"
            ),
            "marker": marker_payload,
        }
    if panel_bytes is None:
        return {
            "available": True,
            "kind": "unknown",
            "managed": False,
            "label": (
                "Нет данных RWMS"
                + (" · есть маркер управляемого лимита" if marker_payload else "")
            ),
            "marker": marker_payload,
        }
    panel_limit = admin_safe_int(panel_bytes, 0) or 0
    managed = is_managed(marker, panel_limit, panel_strategy)
    if managed:
        kind = "managed"
        label = (
            f"Управляемый лимит ({marker.reason}, с {marker_payload['applied_at']}, "
            f"{marker.applied_by}) — снимается "
            + ("оплатой" if marker.release_on == RELEASE_ON_PAYMENT else marker.release_on)
        )
    elif panel_limit > 0:
        kind = "manual"
        label = "Ручной лимит панели — снимает только владелец в Remnawave"
        if marker_payload:
            label += " (маркер не совпадает с панелью и будет снят)"
    else:
        kind = "none"
        label = "Без лимита"
    return {
        "available": True,
        "kind": kind,
        "managed": managed,
        "label": label,
        "marker": marker_payload,
    }


def admin_panel_limit_is_manual(db_session, user_id, rw_user, limit):
    """В панели стоит лимит, который автоматика не ставила: маркера нет (или он
    не совпадает с панелью — resolve его снимает с логом), лимит > 0 и это не
    ровно текущий лимит пробных (такой считается нашим по правилу backfill)."""
    panel_bytes = admin_safe_int(getattr(rw_user, "traffic_limit_bytes", 0), 0)
    if panel_bytes <= 0:
        return False
    marker = resolve_managed_limit(
        db_session, user_id, panel_bytes, getattr(rw_user, "traffic_limit_strategy", None)
    )
    if marker is not None:
        return False
    return not panel_limit_matches(rw_user, limit)


def admin_panel_admin_limit_marker(db_session, user_id, rw_user, limit):
    """Маркер управляемого лимита, который оплата НЕ снимает (`release_on`
    не `payment`): лимит из кнопки «Лимит трафика» в алертах бота (ip_abuse /
    traffic_abuse, `bot:admin:<id>`) — и который «Применить лимит» ПЕРЕПИСАЛ
    БЫ. Автоматика сайта его не перезаписывает — только с отдельным
    подтверждением админа. None — маркера нет, он не совпадает с панелью
    (устаревший, снят resolve), снимается оплатой, либо панель уже несёт ровно
    лимит пробных (применение — no-op `unchanged`, release_on не меняется)."""
    panel_bytes = admin_safe_int(getattr(rw_user, "traffic_limit_bytes", 0), 0)
    if panel_bytes <= 0 or panel_limit_matches(rw_user, limit):
        return None
    marker = resolve_managed_limit(
        db_session, user_id, panel_bytes, getattr(rw_user, "traffic_limit_strategy", None)
    )
    if marker is None or releasable_on_payment(marker):
        return None
    return marker


def admin_admin_limit_label(marker, rw_user):
    """Подпись лимита админа из бота: «ip_abuse, bot:admin:1, 1 ГиБ · ежедневно»."""
    return f"{marker.reason}, {marker.applied_by}, {admin_panel_limit_label(rw_user)}"


def admin_apply_trial_limit_refusal(
    rw_user,
    never_paid,
    manual,
    admin_marker,
    forced=False,
    override_manual=False,
    override_admin_limit=False,
):
    """Что ещё не подтверждено перед «Применить лимит» из карточки: словарь
    для 409 (`paid` / `manual` / `admin_limit` — True только у НЕподтверждённых
    вопросов, `<flag>_message` по каждому, `message` — все вместе) либо None.

    Подтверждения независимы: `force=1` отвечает только на «клиент платил»,
    `override_manual=1` — только на «ручной лимит владельца»,
    `override_admin_limit=1` — только на «лимит админа из бота». Одно
    подтверждение никогда не гасит другой вопрос (инвариант: ручной лимит
    заменяется только по явному решению именно о ручном лимите)."""
    pending = {}
    if never_paid is False and not forced:
        pending["paid"] = (
            "Клиент платил: лимит пробного к нему не применяется. "
            "Подтвердите, если это нужно намеренно."
        )
    if manual and not override_manual:
        pending["manual"] = (
            "В панели стоит ручной лимит "
            f"({admin_panel_limit_label(rw_user)}), автоматика его не трогает. "
            "Подтвердите, если нужно заменить его лимитом пробного (станет "
            "управляемым и снимется оплатой)."
        )
    if admin_marker is not None and not override_admin_limit:
        pending["admin_limit"] = (
            "В панели стоит лимит, поставленный админом из бота "
            f"({admin_admin_limit_label(admin_marker, rw_user)}; оплата его не "
            "снимает). Подтвердите, если нужно заменить его лимитом пробного "
            "(станет снимаемым оплатой)."
        )
    if not pending:
        return None
    refusal = {flag: flag in pending for flag in ("paid", "manual", "admin_limit")}
    for flag, text in pending.items():
        refusal[f"{flag}_message"] = text
    refusal["message"] = " ".join(pending.values())
    return refusal


def admin_apply_managed_trial_limit(
    db_session,
    user_id,
    rw_user,
    limit,
    applied_by,
    allow_manual_override=False,
    allow_admin_limit_override=False,
):
    """Поставить лимит пробного и маркер (одна транзакция: маркер → UpdateUser;
    отказ RWMS откатывает savepoint с маркером). Возвращает (outcome, текст):
    applied — панель изменена; marked — панель уже несла ровно этот лимит,
    добавлен только маркер; unchanged — уже управляемый и такой же;
    skipped_admin_limit — управляемый лимит, который оплата не снимает
    (release_on='manual' — лимит админа из бота), не тронут (без override);
    skipped_manual — ручной лимит панели, не тронут (без override).
    RuntimeError — RWMS не принял; AdminManagedLimitsUnavailable — таблицы нет."""
    panel_bytes = admin_safe_int(getattr(rw_user, "traffic_limit_bytes", 0), 0)
    panel_strategy = getattr(rw_user, "traffic_limit_strategy", None)
    marker = resolve_managed_limit(db_session, user_id, panel_bytes, panel_strategy)
    matches = panel_limit_matches(rw_user, limit)
    if marker is not None and matches:
        return "unchanged", f"лимит уже {admin_traffic_limit_label(limit)}, управляемый"
    if (
        marker is not None
        and not releasable_on_payment(marker)
        and not allow_admin_limit_override
    ):
        # Маркер бота с release_on='manual': перезапись в trial/payment сделала
        # бы fair-usage-кап админа снимаемым оплатой — без подтверждения не трогаем.
        return (
            "skipped_admin_limit",
            f"лимит админа из бота ({admin_admin_limit_label(marker, rw_user)}; "
            "оплата не снимает) — не трогаем",
        )
    if marker is None and panel_bytes > 0 and not matches and not allow_manual_override:
        return (
            "skipped_manual",
            f"ручной лимит панели ({admin_panel_limit_label(rw_user)}) — не трогаем",
        )
    with db_session.begin_nested():
        marker = upsert_managed_limit(
            db_session,
            user_id,
            limit.limit_bytes,
            limit.strategy_name,
            REASON_TRIAL,
            RELEASE_ON_PAYMENT,
            applied_by,
        )
        if marker is None:
            raise AdminManagedLimitsUnavailable(MANAGED_LIMITS_MIGRATION_MESSAGE)
        changed, message = admin_rwms_apply_trial_limit(rw_user, limit)
    add_traffic_limit_event(
        db_session,
        user_id,
        EVENT_TRAFFIC_LIMIT_APPLIED,
        marker,
        changed=changed,
        previous_limit=panel_bytes,
        source="site",
    )
    if changed:
        return "applied", f"применён {message}, помечен управляемым"
    return "marked", f"{message}; помечен управляемым (снимется оплатой)"


def admin_release_managed_limit(db_session, user_id, rw_user, released_by):
    """Снять лимит ТОЛЬКО если он управляемый (маркер + панель совпадают):
    UpdateUser(0, NO_RESET, status=ACTIVE для ACTIVE/LIMITED, без сквадов),
    событие traffic_limit_released, маркер удалён. Возвращает (outcome, текст):
    released | skipped_manual (лимит без маркера — владелец) | unchanged.
    RuntimeError — RWMS не принял (маркер остаётся)."""
    panel_bytes = admin_safe_int(getattr(rw_user, "traffic_limit_bytes", 0), 0)
    panel_strategy = getattr(rw_user, "traffic_limit_strategy", None)
    marker = resolve_managed_limit(db_session, user_id, panel_bytes, panel_strategy)
    if marker is None:
        if panel_bytes > 0:
            return (
                "skipped_manual",
                f"ручной лимит панели ({admin_panel_limit_label(rw_user)}) — "
                "снимает только владелец в Remnawave",
            )
        return "unchanged", "лимита нет"
    changed, message = admin_rwms_remove_traffic_limit(rw_user)
    add_traffic_limit_event(
        db_session,
        user_id,
        EVENT_TRAFFIC_LIMIT_RELEASED,
        marker,
        changed=changed,
        released_by=released_by,
        source="site",
    )
    delete_managed_limit(db_session, user_id)
    return "released", message


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


def support_admin_api_user_traffic(request):
    """Read-only фактический трафик одного клиента из RWMS."""

    auth_response = require_support_admin(request)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        user = admin_find_user(db_session, request.GET.get("q"))
        if not user:
            return JsonResponse({"status": "not_found"}, status=404)
        username = user.username
        # Антиабьюз v2: маркер управляемого лимита читаем в сессии (только
        # чтение — устаревший маркер здесь не удаляем), RPC — после закрытия.
        markers_available = managed_limits_table_available(db_session)
        marker = (
            admin_marker_snapshot(get_managed_limit(db_session, user.id))
            if markers_available
            else None
        )
    finally:
        db_session.close()

    result = admin_rwms_traffic_payload(username)
    result["managed_limit"] = admin_managed_limit_payload(
        marker,
        result["traffic_limit_bytes"] if result["available"] else None,
        result["traffic_limit_strategy"],
        available=markers_available,
    )
    return JsonResponse({"status": "ok", "result": result})


def build_admin_interval_stats(
    db_session, start_date, end_date, requested_granularity="week", sales_mode="cohort"
):
    # Границы периода — московские сутки, в UTC-времени БД это −3 часа.
    start_datetime = datetime.combine(start_date, time.min) - ADMIN_TZ_OFFSET
    end_datetime = datetime.combine(end_date, time.max) - ADMIN_TZ_OFFSET
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
    totals, total_tariffs, cohort_unique_paying_users = admin_stats_apply_sales_mode(
        totals, total_tariffs, sales_series
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
        cohort_unique_paying_users / totals["subscriptions"] * 100
        if totals["subscriptions"]
        else 0
    )

    # Не держим временную таблицу на пуловом соединении после ответа.
    drop_admin_cohort_table(db_session)
    admin_attach_source_notes(db_session, sources)

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
    # Границы периодов — московские сутки, в UTC-времени БД это −3 часа.
    period_start_dt = datetime.combine(period_start, time.min) - ADMIN_TZ_OFFSET
    period_end_dt = datetime.combine(period_end, time.max) - ADMIN_TZ_OFFSET
    cohort_start_dt = datetime.combine(cohort_start, time.min) - ADMIN_TZ_OFFSET
    cohort_end_dt = datetime.combine(cohort_end, time.max) - ADMIN_TZ_OFFSET

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

    admin_attach_source_notes(db_session, sources)
    return {
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "cohort": {"start": cohort_start.isoformat(), "end": cohort_end.isoformat()},
        "totals": totals,
        "sources": sources,
        "sales_series": sales_series,
    }


def admin_referral_activity(db_session, start_date, end_date):
    """Реферальная активность периода: кто пригласил и что это принесло.

    «Приглашённый в периоде» — реферал, чья ПЕРВАЯ подписка (первое событие
    subscription_created, ровно как когорта сквозного анализа) попала в
    выбранные даты; поэтому сумма «пригласил» бьётся с пилюлей «+N рефералов»
    в итогах. Выручка — успешные платежи этих рефералов за тот же период
    (не LTV). Бонусные дни — начисления рефереру за период.
    """
    start_datetime = datetime.combine(start_date, time.min) - ADMIN_TZ_OFFSET
    end_datetime = datetime.combine(end_date, time.max) - ADMIN_TZ_OFFSET
    empty = {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "totals": {
            "referrers": 0, "referrals": 0, "connected": 0,
            "paid_users": 0, "revenue": 0, "bonus_days": 0,
        },
        "rows": [],
        "truncated": False,
    }

    first_sub = (
        db_session.query(
            EventLog.user_id.label("user_id"),
            EventLog.timestamp.label("timestamp"),
            func.row_number()
            .over(
                partition_by=EventLog.user_id,
                order_by=(EventLog.timestamp, EventLog.id),
            )
            .label("rn"),
        )
        .filter(EventLog.event_type == "subscription_created")
        .filter(EventLog.timestamp <= end_datetime)
        .subquery()
    )
    pairs = (
        db_session.query(User.id, User.referred_by_id)
        .join(first_sub, first_sub.c.user_id == User.id)
        .filter(first_sub.c.rn == 1)
        .filter(first_sub.c.timestamp >= start_datetime)
        .filter(User.referred_by_id.isnot(None))
        .all()
    )
    if not pairs:
        return empty

    referral_ids = [referral_id for referral_id, _ in pairs]
    invited_by: dict = {}
    for referral_id, referrer_id in pairs:
        invited_by.setdefault(referrer_id, []).append(referral_id)
    referrer_ids = list(invited_by)

    connected_ids = {
        row[0]
        for row in db_session.query(UserTrafficProgress.user_id)
        .filter(
            UserTrafficProgress.user_id.in_(referral_ids),
            UserTrafficProgress.passed_5mb.is_(True),
        )
        .all()
    }

    # Платежи рефералов за период — по обоим шлюзам, в рублях
    pay_by_user: dict = {}
    yk_time = func.coalesce(YkPayment.captured_at, YkPayment.created_at)
    for user_id, amount in (
        db_session.query(
            YkPayment.user_id, func.coalesce(func.sum(YkPayment.amount), 0)
        )
        .filter(
            YkPayment.user_id.in_(referral_ids),
            YkPayment.status == "succeeded",
            yk_time >= start_datetime,
            yk_time <= end_datetime,
        )
        .group_by(YkPayment.user_id)
        .all()
    ):
        pay_by_user[user_id] = pay_by_user.get(user_id, 0) + admin_money(amount)
    for user_id, amount in (
        db_session.query(
            WataInvoice.user_id,
            func.coalesce(func.sum(WataTransaction.amount), 0),
        )
        .select_from(WataInvoice)
        .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
        .filter(
            WataInvoice.user_id.in_(referral_ids),
            WataTransaction.transaction_status == "Paid",
            WataTransaction.payment_time >= start_datetime,
            WataTransaction.payment_time <= end_datetime,
        )
        .group_by(WataInvoice.user_id)
        .all()
    ):
        pay_by_user[user_id] = pay_by_user.get(user_id, 0) + admin_money(amount)

    bonus_by_referrer = dict(
        db_session.query(
            ReferralBonus.referrer_id,
            func.coalesce(func.sum(ReferralBonus.days_added), 0),
        )
        .filter(
            ReferralBonus.referrer_id.in_(referrer_ids),
            ReferralBonus.created_at >= start_datetime,
            ReferralBonus.created_at <= end_datetime,
        )
        .group_by(ReferralBonus.referrer_id)
        .all()
    )
    lifetime_by_referrer = dict(
        db_session.query(User.referred_by_id, func.count(User.id))
        .filter(User.referred_by_id.in_(referrer_ids))
        .group_by(User.referred_by_id)
        .all()
    )
    users_by_id = {
        user.id: user
        for user in db_session.query(User).filter(User.id.in_(referrer_ids)).all()
    }

    rows = []
    for referrer_id, invited_ids in invited_by.items():
        user = users_by_id.get(referrer_id)
        if user is None:
            continue
        paid_users = sum(1 for rid in invited_ids if pay_by_user.get(rid))
        rows.append(
            {
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email or "",
                    "telegram_id": str(user.telegram_id or ""),
                },
                "invited": len(invited_ids),
                "connected": sum(1 for rid in invited_ids if rid in connected_ids),
                "paid_users": paid_users,
                "revenue": sum(pay_by_user.get(rid, 0) for rid in invited_ids),
                "bonus_days": admin_money(bonus_by_referrer.get(referrer_id)),
                "lifetime_invited": int(lifetime_by_referrer.get(referrer_id, 0)),
            }
        )
    rows.sort(key=lambda row: (-row["invited"], -row["revenue"], row["user"]["id"]))
    truncated = len(rows) > 500
    rows = rows[:500]
    totals = {
        "referrers": len(invited_by),
        "referrals": len(referral_ids),
        "connected": sum(row["connected"] for row in rows),
        "paid_users": sum(row["paid_users"] for row in rows),
        "revenue": sum(row["revenue"] for row in rows),
        "bonus_days": sum(row["bonus_days"] for row in rows),
    }
    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "totals": totals,
        "rows": rows,
        "truncated": truncated,
    }


def support_admin_api_referral_activity(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response
    try:
        today = admin_msk_today()
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
    except ValueError:
        return JsonResponse(
            {"status": "error", "message": "Неверный формат даты"}, status=400
        )
    if start_date > end_date:
        return JsonResponse(
            {"status": "error", "message": "Начальная дата больше конечной"},
            status=400,
        )
    db_session = session_factory()
    try:
        result = admin_referral_activity(db_session, start_date, end_date)
    finally:
        db_session.close()
    return JsonResponse({"status": "ok", "result": result})


def support_admin_api_stats(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response

    try:
        today = admin_msk_today()
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

    def build_response():
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

    return report_response("support_admin_api_stats", [start_date, end_date, request.GET.get("granularity", "week"), request.GET.get("sales_mode", "cohort")], build_response)


def support_admin_api_stats_source_users(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response

    try:
        today = admin_msk_today()
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

    # Границы периода — московские сутки, в UTC-времени БД это −3 часа.
    start_datetime = datetime.combine(start_date, time.min) - ADMIN_TZ_OFFSET
    end_datetime = datetime.combine(end_date, time.max) - ADMIN_TZ_OFFSET
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
        today = admin_msk_today()
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

    def build_response():
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

    return report_response("support_admin_api_cohort_stats", [period_start, period_end, cohort_start, cohort_end, request.GET.get("granularity", "month")], build_response)


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
                    "system": "YooKassa",
                    "id": payment.payment_id,
                    "date": admin_date_label(payment.created_at),
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
                    "system": "Wata",
                    "id": payment.transaction_id,
                    "date": admin_date_label(payment.payment_time),
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
        "account_block": admin_account_block_payload(db_session, user),
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

        if action in ("block_account", "disable_subscription"):
            # «Отключить доступ» = полная блокировка, 1:1 с /block-user в боте:
            # user_blocks (бот отвечает только ACCOUNT_BLOCKED), блок рефералки,
            # автоплатёж выключен и рекуррент удалён, подписка в RWMS → DISABLED.
            # Ключ disable_subscription оставлен для обратной совместимости.
            reason = (
                request.POST.get("reason") or "admin panel: access blocked"
            ).strip()[:512]
            removed_recurrents, rwms_updated = admin_block_account(
                db_session, request, user, reason
            )
            return JsonResponse({
                "status": "ok",
                "result": {
                    "user": admin_user_payload(user),
                    "account_block": admin_account_block_payload(db_session, user),
                    "action_label": (
                        "Аккаунт заблокирован: бот отвечает только «аккаунт "
                        "заблокирован», рефералка и автоплатёж отключены, "
                        "RWMS → DISABLED"
                    ),
                    "removed_recurrents": removed_recurrents,
                    "rwms_updated": rwms_updated,
                },
            })

        if action == "unblock_account":
            # Как /unblock-user: доступ к боту и рефералка возвращаются, RWMS →
            # ACTIVE только если срок не истёк; автоплатёж остаётся выключенным.
            if db_session.get(UserBlock, user.id) is None:
                return JsonResponse(
                    {"status": "error", "message": "Аккаунт не заблокирован"},
                    status=400,
                )
            rwms_updated = admin_unblock_account(db_session, request, user)
            return JsonResponse({
                "status": "ok",
                "result": {
                    "user": admin_user_payload(user),
                    "account_block": admin_account_block_payload(db_session, user),
                    "action_label": (
                        "Блокировка снята: доступ к боту и рефералка возвращены"
                        + (
                            ", RWMS → ACTIVE"
                            if rwms_updated
                            else ", RWMS не активировался (срок истёк или ошибка)"
                        )
                        + "; автоплатёж остаётся выключенным"
                    ),
                    "rwms_updated": bool(rwms_updated),
                },
            })

        if action in ("apply_trial_limit", "remove_traffic_limit"):
            # Антиабьюз: лимит трафика пробной подписки в панели. Панель читаем
            # СТРОГО (как продление): блип RWMS — 503, а не «подписки нет».
            # v2: ручные лимиты владельца неприкосновенны — снимается только
            # управляемый (маркер managed_traffic_limits + панель совпадает);
            # без таблицы маркеров действия недоступны (503, панель не тронута).
            if not managed_limits_table_available(db_session):
                return JsonResponse(
                    {"status": "error", "message": MANAGED_LIMITS_MIGRATION_MESSAGE},
                    status=503,
                )
            try:
                rwms_user = rwms_client.get_user_by_username_strict(user.username)
            except RwmsUnavailableError as error:
                logging.warning(
                    "RWMS unavailable while applying admin action %s for user %s: %s",
                    action,
                    user.username,
                    error,
                )
                return JsonResponse(
                    {
                        "status": "error",
                        "message": (
                            "RWMS/панель временно недоступны: лимит не изменён, "
                            "повторите позже"
                        ),
                    },
                    status=503,
                )
            if rwms_user is None:
                return JsonResponse(
                    {"status": "error", "message": "Подписка в RWMS не найдена"},
                    status=404,
                )
            never_paid = user_never_paid(db_session, user.id)
            # Три независимых подтверждения: force=1 — «клиент платил»;
            # override_manual=1 — заменить ручной лимит владельца;
            # override_admin_limit=1 — заменить лимит админа из бота
            # (release_on='manual'). Одно подтверждение другой вопрос не гасит.
            forced = (request.POST.get("force") or "") == "1"
            override_manual = (request.POST.get("override_manual") or "") == "1"
            override_admin_limit = (request.POST.get("override_admin_limit") or "") == "1"
            outcome = "unchanged"
            if action == "apply_trial_limit":
                limit = trial_traffic_limit_configured(db_session)
                manual = admin_panel_limit_is_manual(db_session, user.id, rwms_user, limit)
                admin_marker = admin_panel_admin_limit_marker(
                    db_session, user.id, rwms_user, limit
                )
                refusal = admin_apply_trial_limit_refusal(
                    rwms_user,
                    never_paid,
                    manual,
                    admin_marker,
                    forced=forced,
                    override_manual=override_manual,
                    override_admin_limit=override_admin_limit,
                )
                if refusal is not None:
                    # Все неподтверждённые вопросы — одним 409 (UI переспросит
                    # по каждому отдельно). Устаревший маркер (панель менялась
                    # руками) уже снят resolve — фиксируем это, панель не тронута.
                    db_session.commit()
                    return JsonResponse({"status": "error", **refusal}, status=409)
                try:
                    outcome, message = admin_apply_managed_trial_limit(
                        db_session,
                        user.id,
                        rwms_user,
                        limit,
                        admin_applied_by(request),
                        allow_manual_override=override_manual,
                        allow_admin_limit_override=override_admin_limit,
                    )
                except AdminManagedLimitsUnavailable as error:
                    return JsonResponse(
                        {"status": "error", "message": str(error)}, status=503
                    )
                except RuntimeError as error:
                    return JsonResponse(
                        {"status": "error", "message": str(error)}, status=502
                    )
                changed = outcome == "applied"
                admin_audit_write(
                    db_session,
                    request,
                    "apply_trial_limit",
                    target=user.username,
                    limit_gb=limit.limit_gb,
                    limit_bytes=limit.limit_bytes,
                    strategy=limit.strategy_key,
                    changed=changed,
                    outcome=outcome,
                    never_paid=never_paid,
                    forced=forced,
                    override_manual=override_manual,
                    override_admin_limit=override_admin_limit,
                    manual_overridden=manual,
                    admin_limit_overridden=admin_marker is not None,
                )
                action_label = (
                    f"Лимит трафика применён: {admin_traffic_limit_label(limit)} "
                    "(помечен управляемым, снимется оплатой)"
                    if changed
                    else f"Без изменений в панели: {message}"
                )
            else:
                try:
                    outcome, message = admin_release_managed_limit(
                        db_session, user.id, rwms_user, admin_applied_by(request)
                    )
                except RuntimeError as error:
                    return JsonResponse(
                        {"status": "error", "message": str(error)}, status=502
                    )
                changed = outcome == "released"
                admin_audit_write(
                    db_session,
                    request,
                    "remove_traffic_limit",
                    target=user.username,
                    changed=changed,
                    outcome=outcome,
                    never_paid=never_paid,
                )
                if changed:
                    action_label = (
                        f"Лимит трафика снят ({message}; сквады не менялись; "
                        "маркер удалён)"
                    )
                elif outcome == "skipped_manual":
                    action_label = f"Не снят: {message}"
                else:
                    action_label = f"Без изменений: {message}"
            db_session.commit()
            return JsonResponse({
                "status": "ok",
                "result": {
                    "user": admin_user_payload(user),
                    "action_label": action_label,
                    "rwms_updated": changed,
                    "outcome": outcome,
                    "manual": outcome == "skipped_manual",
                    "never_paid": never_paid,
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
            # Уведомление пользователю — тем же текстом, что у кнопки
            # «Временный бан» в трафик-алертах бота (NOTIFY_TEMPORARY_BAN):
            # служебный пуш в Redis-очереди ботов, шлёт сам бот.
            user_notified = push_admin_temporary_ban(
                user.telegram_id, int(round(hours * 60))
            )
            return JsonResponse({
                "status": "ok",
                "result": {
                    "user": admin_user_payload(user),
                    "action_label": f"Временный бан до {unban_at + ADMIN_TZ_OFFSET:%Y-%m-%d %H:%M} МСК (снимет бот)",
                    "rwms_updated": True,
                    "user_notified": user_notified,
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

        removed_recurrents = None
        if action == "set_trial_hour":
            # Подготовка возврата: срок = 1 час. Обязательно снимаем автоплатёж
            # (как stop_autopay / бот-команда /set-trial-hour): иначе подписка
            # через минуты попадёт в окно автосписания yk-recurrent
            # [expire-4ч; expire+overdue] и клиенту, которому возвращают
            # деньги, спишут их снова. Ручная оплата снова включит автоплатёж.
            target_expire = datetime.now(timezone.utc) + timedelta(hours=1)
            user.autopay_allow = False
            removed_recurrents = (
                db_session.query(YkRecurrentPayment)
                .filter(YkRecurrentPayment.user_id == user.id)
                .delete(synchronize_session=False)
            )
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
            removed_recurrents=removed_recurrents,
        )
        db_session.commit()

        # Панель читаем СТРОГО: None — только достоверный NOT_FOUND. Блип
        # RWMS/панели (RwmsUnavailableError) нельзя трактовать как «подписки
        # нет» и пересоздавать её через AddUser (Remnawave Safety Rules):
        # срок в БД уже сохранён (БД — истина по времени), панель не трогаем,
        # админ повторяет синхронизацию «БД → панель» позже.
        try:
            rwms_user = rwms_client.get_user_by_username_strict(user.username)
        except RwmsUnavailableError as error:
            logging.warning(
                "RWMS unavailable while applying admin action %s for user %s: %s; "
                "panel left untouched (no recreate)",
                action,
                user.username,
                error,
            )
            return JsonResponse(
                {
                    "status": "error",
                    "message": (
                        "Срок в БД обновлён, но RWMS/панель временно недоступны: "
                        "подписка в панели не изменена и не пересоздавалась. "
                        "Повторите позже синхронизацию «БД → панель»."
                    ),
                    "result": {
                        "user": admin_user_payload(user),
                        "old_expire_at": admin_date_label(old_expire),
                        "new_expire_at": admin_date_label(user.expire_at),
                        "rwms_updated": False,
                        "removed_recurrents": removed_recurrents,
                    },
                },
                status=503,
            )
        rwms_updated = False
        if rwms_user:
            # Проверка «есть @» пропускала домен без точки, а панель валидирует
            # email через EmailStr — такой запрос падал целиком (инцидент
            # 2026-09-10). Невалидный адрес не задаём: панель оставит свой.
            user_email = usable_panel_email(
                rwms_user.email, getattr(rwms_user, "username", "?"), "UpdateUser"
            )
            active_squads = [squad.uuid for squad in rwms_user.active_internal_squads]
            # Антиабьюз: traffic_limit_bytes/traffic_limit_strategy НЕ передаём —
            # RWMS оставляет лимит и стратегию сброса панели как есть (явный
            # NO_RESET ломал ежедневный/еженедельный сброс ограниченного триала).
            # Снятие лимита — только admin_rwms_remove_traffic_limit и оплата.
            response = rwms_client.update_user(
                proto.UpdateUserRequest(
                    uuid=rwms_user.uuid,
                    email=user_email,
                    telegram_id=rwms_user.telegram_id,
                    expire_at=target_expire,
                    status=proto.UserStatus.ACTIVE,
                    active_internal_squads=active_squads,
                )
            )
            rwms_updated = response is not None
        else:
            logging.warning(
                "RWMS subscription for admin-updated user %s is missing, recreating",
                user.username,
            )
            # Антиабьюз: при пересоздании лимит пробного — только если
            # включён И пользователь never_paid (платившему лимит не ставится);
            # маркер управляемого лимита — в той же сессии (site:admin:<login>).
            recreate_limit = trial_traffic_limit_for_user(db_session, user)
            response = create_user_until(
                rwms_client=rwms_client,
                username=user.username,
                expire_at=target_expire,
                email=user.email,
                telegram_id=user.telegram_id,
                traffic_limit=recreate_limit,
            )
            rwms_updated = response is not None
            if rwms_updated and recreate_limit is not None:
                marker = record_trial_limit_marker(
                    db_session, user, recreate_limit, admin_applied_by(request)
                )
                add_traffic_limit_event(
                    db_session, user.id, EVENT_TRAFFIC_LIMIT_APPLIED, marker,
                    recreated=True, source="site",
                )
                db_session.commit()

        return JsonResponse(
            {
                "status": "ok",
                "result": {
                    "user": admin_user_payload(user),
                    "old_expire_at": admin_date_label(old_expire),
                    "new_expire_at": admin_date_label(user.expire_at),
                    "rwms_updated": rwms_updated,
                    "removed_recurrents": removed_recurrents,
                    "action_label": (
                        "Возврат подготовлен: срок 1 час, автоплатёж отключён"
                        f" (удалено рекуррентов: {removed_recurrents})"
                        if action == "set_trial_hour"
                        else None
                    ),
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
            pair_error = admin_validate_antiabuse_setting_pair(
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


# --- Антиабьюз: вкладка «Система → Антиабьюз» --------------------------------


def admin_antiabuse_effective(db_session):
    """Действующие значения (БД либо дефолт common) — то, чем реально
    руководствуются сайт/бот/user-notify/payment/ip-guard."""

    def raw(key):
        setting = db_session.get(SystemSetting, key)
        return setting.value if setting is not None else None

    limit = trial_traffic_limit_configured(db_session)
    segment_raw = raw(IPGUARD_ALERT_SEGMENT_SETTING)
    segment = (
        normalize_ipguard_alert_segment(segment_raw)
        if segment_raw is not None
        else DEFAULT_IPGUARD_ALERT_SEGMENT
    )
    autoban_segment_raw = raw(IPGUARD_AUTOBAN_SEGMENT_SETTING)
    autoban_segment = (
        normalize_ipguard_alert_segment(autoban_segment_raw)
        if autoban_segment_raw is not None
        else DEFAULT_IPGUARD_AUTOBAN_SEGMENT
    )
    # Для CSV-ключей действующее значение — результат парсера common, а не
    # строка из БД: именно его видит ip-guard (адрес канонизируется в /32,
    # мусор отбрасывается), и именно его должен видеть админ.
    autoban_steps = parse_ipguard_autoban_steps(
        raw(IPGUARD_AUTOBAN_STEPS_MINUTES_SETTING)
    )
    excluded_ips = parse_ipguard_excluded_ips(raw(IPGUARD_EXCLUDED_IPS_SETTING))
    excluded_usernames = parse_ipguard_excluded_usernames(
        raw(IPGUARD_EXCLUDED_USERNAMES_SETTING)
    )
    return {
        "trial_traffic_limit_enabled": trial_traffic_limit_enabled(db_session),
        "trial_traffic_limit_gb": limit.limit_gb,
        "trial_traffic_limit_bytes": limit.limit_bytes,
        "trial_traffic_limit_strategy": limit.strategy_key,
        "trial_traffic_limit_strategy_label": admin_traffic_limit_strategy_label(
            limit.strategy_key
        ),
        "trial_traffic_limit_label": admin_traffic_limit_label(limit),
        "ipguard_alerts_enabled": parse_bool_setting(
            raw(IPGUARD_ALERTS_ENABLED_SETTING), DEFAULT_IPGUARD_ALERTS_ENABLED
        ),
        # Период прогона общий для всех слоёв: если он больше окна короткого
        # слоя, детектор между прогонами слеп — карточка считает это прямо в UI.
        "ipguard_check_interval_seconds": parse_positive_int_setting(
            raw(IPGUARD_CHECK_INTERVAL_SECONDS_SETTING),
            DEFAULT_IPGUARD_CHECK_INTERVAL_SECONDS,
        ),
        # Тумблер ПУБЛИКАЦИИ суточного слоя, отдельно от главного рубильника.
        "ipguard_subnets_enabled": parse_bool_setting(
            raw(IPGUARD_SUBNETS_ENABLED_SETTING), DEFAULT_IPGUARD_SUBNETS_ENABLED
        ),
        "ipguard_alert_segment": segment,
        "ipguard_alert_segment_label": IPGUARD_SEGMENT_LABELS.get(segment, segment),
        "ipguard_subnets_per_hwid": parse_positive_int_setting(
            raw(IPGUARD_SUBNETS_PER_HWID_SETTING), DEFAULT_IPGUARD_SUBNETS_PER_HWID
        ),
        "ipguard_window_hours": parse_positive_int_setting(
            raw(IPGUARD_WINDOW_HOURS_SETTING), DEFAULT_IPGUARD_WINDOW_HOURS
        ),
        "ipguard_alert_cooldown_hours": parse_positive_int_setting(
            raw(IPGUARD_ALERT_COOLDOWN_HOURS_SETTING),
            DEFAULT_IPGUARD_ALERT_COOLDOWN_HOURS,
        ),
        "ipguard_warnings_enabled": parse_bool_setting(
            raw(IPGUARD_WARNINGS_ENABLED_SETTING), DEFAULT_IPGUARD_WARNINGS_ENABLED
        ),
        "ipguard_warning_subnets_per_hwid": parse_positive_int_setting(
            raw(IPGUARD_WARNING_SUBNETS_PER_HWID_SETTING),
            DEFAULT_IPGUARD_WARNING_SUBNETS_PER_HWID,
        ),
        "ipguard_burst_enabled": parse_bool_setting(
            raw(IPGUARD_BURST_ENABLED_SETTING), DEFAULT_IPGUARD_BURST_ENABLED
        ),
        "ipguard_burst_window_minutes": parse_positive_int_setting(
            raw(IPGUARD_BURST_WINDOW_MINUTES_SETTING),
            DEFAULT_IPGUARD_BURST_WINDOW_MINUTES,
        ),
        "ipguard_burst_ips_per_hwid": parse_positive_int_setting(
            raw(IPGUARD_BURST_IPS_PER_HWID_SETTING),
            DEFAULT_IPGUARD_BURST_IPS_PER_HWID,
        ),
        "ipguard_burst_confirmations": parse_positive_int_setting(
            raw(IPGUARD_BURST_CONFIRMATIONS_SETTING),
            DEFAULT_IPGUARD_BURST_CONFIRMATIONS,
        ),
        "ipguard_burst_min_subnets": parse_positive_int_setting(
            raw(IPGUARD_BURST_MIN_SUBNETS_SETTING),
            DEFAULT_IPGUARD_BURST_MIN_SUBNETS,
        ),
        "ipguard_geo_enabled": parse_bool_setting(
            raw(IPGUARD_GEO_ENABLED_SETTING), DEFAULT_IPGUARD_GEO_ENABLED
        ),
        "ipguard_geo_window_minutes": parse_positive_int_setting(
            raw(IPGUARD_GEO_WINDOW_MINUTES_SETTING),
            DEFAULT_IPGUARD_GEO_WINDOW_MINUTES,
        ),
        "ipguard_geo_min_regions": parse_positive_int_setting(
            raw(IPGUARD_GEO_MIN_REGIONS_SETTING), DEFAULT_IPGUARD_GEO_MIN_REGIONS
        ),
        "ipguard_autoban_enabled": parse_bool_setting(
            raw(IPGUARD_AUTOBAN_ENABLED_SETTING), DEFAULT_IPGUARD_AUTOBAN_ENABLED
        ),
        "ipguard_autoban_segment": autoban_segment,
        "ipguard_autoban_segment_label": IPGUARD_SEGMENT_LABELS.get(
            autoban_segment, autoban_segment
        ),
        "ipguard_autoban_steps_minutes": ",".join(str(step) for step in autoban_steps),
        "ipguard_autoban_steps_label": " → ".join(
            f"{step} мин" for step in autoban_steps
        ),
        "ipguard_autoban_escalation_window_hours": parse_positive_int_setting(
            raw(IPGUARD_AUTOBAN_ESCALATION_WINDOW_HOURS_SETTING),
            DEFAULT_IPGUARD_AUTOBAN_ESCALATION_WINDOW_HOURS,
        ),
        "ipguard_autoban_max_per_hour": parse_positive_int_setting(
            raw(IPGUARD_AUTOBAN_MAX_PER_HOUR_SETTING),
            DEFAULT_IPGUARD_AUTOBAN_MAX_PER_HOUR,
        ),
        # Порог автобана — абсолютный, по суточному слою; гистерезис по
        # прогонам; пробный режим по умолчанию ВКЛЮЧЁН (бан не выполняется).
        "ipguard_autoban_min_subnets": parse_positive_int_setting(
            raw(IPGUARD_AUTOBAN_MIN_SUBNETS_SETTING),
            DEFAULT_IPGUARD_AUTOBAN_MIN_SUBNETS,
        ),
        "ipguard_autoban_confirmations": parse_positive_int_setting(
            raw(IPGUARD_AUTOBAN_CONFIRMATIONS_SETTING),
            DEFAULT_IPGUARD_AUTOBAN_CONFIRMATIONS,
        ),
        "ipguard_autoban_dry_run": parse_bool_setting(
            raw(IPGUARD_AUTOBAN_DRY_RUN_SETTING), DEFAULT_IPGUARD_AUTOBAN_DRY_RUN
        ),
        "ipguard_max_alerts_per_hour": parse_positive_int_setting(
            raw(IPGUARD_MAX_ALERTS_PER_HOUR_SETTING),
            DEFAULT_IPGUARD_MAX_ALERTS_PER_HOUR,
        ),
        # Поле формы — ровно в том виде, в каком значение ляжет в БД (без
        # пробелов), чтобы повторное сохранение не переписывало строку.
        "ipguard_excluded_ips": ",".join(excluded_ips),
        "ipguard_excluded_ips_count": len(excluded_ips),
        "ipguard_excluded_ips_label": (
            ", ".join(excluded_ips) if excluded_ips else "не заданы"
        ),
        # Исключённые подписки: строка в форму (как ляжет в БД), разобранный
        # список и подпись — по аналогии с адресами.
        "ipguard_excluded_usernames": ",".join(excluded_usernames),
        "ipguard_excluded_usernames_list": list(excluded_usernames),
        "ipguard_excluded_usernames_count": len(excluded_usernames),
        "ipguard_excluded_usernames_label": (
            ", ".join(excluded_usernames) if excluded_usernames else "не заданы"
        ),
    }


def admin_antiabuse_payload(db_session):
    settings_by_key = {
        setting.key: setting
        for setting in db_session.query(SystemSetting)
        .filter(SystemSetting.key.in_(ANTIABUSE_SETTING_KEYS))
        .all()
    }
    return {
        "status": "ok",
        "settings": [
            admin_runtime_setting_payload(key, settings_by_key.get(key))
            for key in ANTIABUSE_SETTING_KEYS
        ],
        "effective": admin_antiabuse_effective(db_session),
        "strategies": [
            {"value": key, "label": label}
            for key, label in TRAFFIC_LIMIT_STRATEGY_LABELS.items()
        ],
        "segments": [
            {"value": key, "label": label}
            for key, label in IPGUARD_SEGMENT_LABELS.items()
        ],
        "managed_limits_available": managed_limits_table_available(db_session),
    }


def admin_antiabuse_save_setting(db_session, request, key, raw_value, pending=None):
    """Валидация (тип + парная) → upsert → аудит setting_save. Текст ошибки
    либо None; коммитит вызывающая сторона. Значение, совпадающее с уже
    сохранённым в БД, не перезаписывается и в аудит не пишется (формы вкладки
    предзаполнены текущими значениями и шлют все поля разом)."""
    normalized_value, error = admin_validate_runtime_setting(key, raw_value)
    if error:
        return f"{key}: {error}"
    pair_error = admin_validate_antiabuse_setting_pair(
        db_session, key, normalized_value, pending
    )
    if pair_error:
        return pair_error
    current = db_session.get(SystemSetting, key)
    if current is not None and current.value == normalized_value:
        return None
    admin_upsert_system_setting(db_session, key, normalized_value)
    admin_audit_write(
        db_session, request, "setting_save", target=key, value=normalized_value
    )
    return None


def admin_antiabuse_save_settings(db_session, request, updates):
    """Сохранить набор (key, raw) одним действием: сначала типовая валидация
    всех значений (``pending`` для парных проверок — пара порогов ip-guard
    проверяется по значениям ЭТОГО сохранения, в любом порядке полей), затем
    парная проверка и upsert. Первая ошибка — текст, иначе None."""
    pending = {}
    for key, raw_value in updates:
        normalized_value, error = admin_validate_runtime_setting(key, raw_value)
        if error:
            return f"{key}: {error}"
        pending[key] = normalized_value
    for key, raw_value in updates:
        error = admin_antiabuse_save_setting(
            db_session, request, key, raw_value, pending
        )
        if error:
            return error
    return None


def support_admin_api_antiabuse(request):
    """GET — настройки антиабьюза (сырые + действующие); POST —
    action=trial_limit_enable|trial_limit_disable|trial_limit_set
    (limit_value + limit_unit=gib|mib, strategy)|ipguard_enable|ipguard_disable|
    ipguard_master_set (check_interval_seconds)|
    ipguard_subnets_enable|ipguard_subnets_disable|
    ipguard_warnings_enable|ipguard_warnings_disable|
    ipguard_set (segment, subnets_per_hwid, warning_subnets_per_hwid,
    window_hours, cooldown_hours)|
    ipguard_burst_enable|ipguard_burst_disable|ipguard_burst_set
    (burst_window_minutes, burst_ips_per_hwid, burst_confirmations,
    burst_min_subnets)|
    ipguard_geo_enable|ipguard_geo_disable|ipguard_geo_set
    (geo_window_minutes, geo_min_regions)|
    ipguard_autoban_enable|ipguard_autoban_disable|ipguard_autoban_set
    (autoban_segment, autoban_steps_minutes, autoban_escalation_window_hours,
    autoban_max_per_hour, autoban_min_subnets, autoban_confirmations,
    max_alerts_per_hour)|
    ipguard_autoban_dry_run_enable|ipguard_autoban_dry_run_disable
    (пробный режим автобана: бан не выполняется, только помечается в алерте;
    выключение проверяет те же зависимости в БД, что и включение автобана)|
    ipguard_excluded_set (excluded_ips)|ipguard_excluded_clear|
    ipguard_excluded_usernames_set (excluded_usernames)|
    ipguard_excluded_usernames_clear.
    Пустое поле в *_set оставляет текущее значение; значение, равное
    сохранённому, не перезаписывается. Очистка списков исключений — отдельными
    действиями *_clear (пустое поле трактуется как «не трогать»).
    Ответ POST — тот же payload, что GET (UI после сохранения дополнительно
    перечитывает GET)."""
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "POST":
            action = request.POST.get("action")
            updates = []
            if action == "trial_limit_enable":
                updates.append((TRIAL_TRAFFIC_LIMIT_ENABLED_SETTING, "1"))
            elif action == "trial_limit_disable":
                updates.append((TRIAL_TRAFFIC_LIMIT_ENABLED_SETTING, "0"))
            elif action == "trial_limit_set":
                limit_value = (request.POST.get("limit_value") or "").strip()
                limit_unit = (request.POST.get("limit_unit") or "gib").strip().lower()
                if limit_value:
                    try:
                        number = float(limit_value.replace(",", "."))
                    except ValueError:
                        return JsonResponse(
                            {"status": "error", "message": "Лимит должен быть числом"},
                            status=400,
                        )
                    if limit_unit == "mib":
                        number = number / 1024
                    elif limit_unit != "gib":
                        return JsonResponse(
                            {"status": "error", "message": "Единица: ГиБ или МиБ"},
                            status=400,
                        )
                    # В БД лимит хранится в ГиБ (float); МиБ переводятся здесь.
                    updates.append((TRIAL_TRAFFIC_LIMIT_GB_SETTING, "%.10f" % number))
                strategy = (request.POST.get("strategy") or "").strip()
                if strategy:
                    updates.append((TRIAL_TRAFFIC_LIMIT_STRATEGY_SETTING, strategy))
            elif action == "ipguard_enable":
                updates.append((IPGUARD_ALERTS_ENABLED_SETTING, "1"))
            elif action == "ipguard_disable":
                updates.append((IPGUARD_ALERTS_ENABLED_SETTING, "0"))
            elif action == "ipguard_master_set":
                # Период прогона один на все слои, поэтому живёт в карточке
                # главного рубильника, а не в карточке конкретного слоя.
                value = (request.POST.get("check_interval_seconds") or "").strip()
                if value:
                    updates.append((IPGUARD_CHECK_INTERVAL_SECONDS_SETTING, value))
            elif action == "ipguard_subnets_enable":
                updates.append((IPGUARD_SUBNETS_ENABLED_SETTING, "1"))
            elif action == "ipguard_subnets_disable":
                # Выключает ТОЛЬКО суточный слой (и его предупреждения);
                # всплеск, гео и автобан продолжают работать.
                updates.append((IPGUARD_SUBNETS_ENABLED_SETTING, "0"))
            elif action == "ipguard_warnings_enable":
                updates.append((IPGUARD_WARNINGS_ENABLED_SETTING, "1"))
            elif action == "ipguard_warnings_disable":
                updates.append((IPGUARD_WARNINGS_ENABLED_SETTING, "0"))
            elif action == "ipguard_set":
                for key, form_key in (
                    (IPGUARD_ALERT_SEGMENT_SETTING, "segment"),
                    (IPGUARD_SUBNETS_PER_HWID_SETTING, "subnets_per_hwid"),
                    (
                        IPGUARD_WARNING_SUBNETS_PER_HWID_SETTING,
                        "warning_subnets_per_hwid",
                    ),
                    (IPGUARD_WINDOW_HOURS_SETTING, "window_hours"),
                    (IPGUARD_ALERT_COOLDOWN_HOURS_SETTING, "cooldown_hours"),
                ):
                    value = (request.POST.get(form_key) or "").strip()
                    if value:
                        updates.append((key, value))
            elif action == "ipguard_burst_enable":
                updates.append((IPGUARD_BURST_ENABLED_SETTING, "1"))
            elif action == "ipguard_burst_disable":
                updates.append((IPGUARD_BURST_ENABLED_SETTING, "0"))
            elif action == "ipguard_burst_set":
                for key, form_key in (
                    (IPGUARD_BURST_WINDOW_MINUTES_SETTING, "burst_window_minutes"),
                    (IPGUARD_BURST_IPS_PER_HWID_SETTING, "burst_ips_per_hwid"),
                    (IPGUARD_BURST_CONFIRMATIONS_SETTING, "burst_confirmations"),
                    (IPGUARD_BURST_MIN_SUBNETS_SETTING, "burst_min_subnets"),
                ):
                    value = (request.POST.get(form_key) or "").strip()
                    if value:
                        updates.append((key, value))
            elif action == "ipguard_geo_enable":
                updates.append((IPGUARD_GEO_ENABLED_SETTING, "1"))
            elif action == "ipguard_geo_disable":
                updates.append((IPGUARD_GEO_ENABLED_SETTING, "0"))
            elif action == "ipguard_geo_set":
                for key, form_key in (
                    (IPGUARD_GEO_WINDOW_MINUTES_SETTING, "geo_window_minutes"),
                    (IPGUARD_GEO_MIN_REGIONS_SETTING, "geo_min_regions"),
                ):
                    value = (request.POST.get(form_key) or "").strip()
                    if value:
                        updates.append((key, value))
            elif action == "ipguard_autoban_enable":
                updates.append((IPGUARD_AUTOBAN_ENABLED_SETTING, "1"))
            elif action == "ipguard_autoban_disable":
                updates.append((IPGUARD_AUTOBAN_ENABLED_SETTING, "0"))
            elif action == "ipguard_autoban_set":
                for key, form_key in (
                    (IPGUARD_AUTOBAN_SEGMENT_SETTING, "autoban_segment"),
                    (IPGUARD_AUTOBAN_STEPS_MINUTES_SETTING, "autoban_steps_minutes"),
                    (
                        IPGUARD_AUTOBAN_ESCALATION_WINDOW_HOURS_SETTING,
                        "autoban_escalation_window_hours",
                    ),
                    (IPGUARD_AUTOBAN_MAX_PER_HOUR_SETTING, "autoban_max_per_hour"),
                    (IPGUARD_AUTOBAN_MIN_SUBNETS_SETTING, "autoban_min_subnets"),
                    (IPGUARD_AUTOBAN_CONFIRMATIONS_SETTING, "autoban_confirmations"),
                    (IPGUARD_MAX_ALERTS_PER_HOUR_SETTING, "max_alerts_per_hour"),
                ):
                    value = (request.POST.get(form_key) or "").strip()
                    if value:
                        updates.append((key, value))
            elif action == "ipguard_autoban_dry_run_enable":
                updates.append((IPGUARD_AUTOBAN_DRY_RUN_SETTING, "1"))
            elif action == "ipguard_autoban_dry_run_disable":
                # С этого момента автобан (если включён) банит по-настоящему:
                # парная проверка отказывает при битых зависимостях в БД.
                updates.append((IPGUARD_AUTOBAN_DRY_RUN_SETTING, "0"))
            elif action == "ipguard_excluded_set":
                value = (request.POST.get("excluded_ips") or "").strip()
                if value:
                    updates.append((IPGUARD_EXCLUDED_IPS_SETTING, value))
            elif action == "ipguard_excluded_clear":
                # Отдельное действие: пустое поле в *_set означает «не трогать»
                # (общий контракт форм вкладки), поэтому очистить список
                # исключений иначе было бы нельзя.
                updates.append((IPGUARD_EXCLUDED_IPS_SETTING, ""))
            elif action == "ipguard_excluded_usernames_set":
                value = (request.POST.get("excluded_usernames") or "").strip()
                if value:
                    updates.append((IPGUARD_EXCLUDED_USERNAMES_SETTING, value))
            elif action == "ipguard_excluded_usernames_clear":
                updates.append((IPGUARD_EXCLUDED_USERNAMES_SETTING, ""))
            else:
                return JsonResponse(
                    {"status": "error", "message": "Неизвестное действие"}, status=400
                )
            if not updates:
                return JsonResponse(
                    {"status": "error", "message": "Нет значений для сохранения"},
                    status=400,
                )
            error = admin_antiabuse_save_settings(db_session, request, updates)
            if error:
                db_session.rollback()
                return JsonResponse({"status": "error", "message": error}, status=400)
            db_session.commit()
        elif request.method != "GET":
            return JsonResponse({"status": "error"}, status=405)

        return JsonResponse(admin_antiabuse_payload(db_session))
    finally:
        db_session.close()


# Массовые операции по лимиту трафика — по СЕГМЕНТУ (common/models/segments.py),
# порциями: один запрос обрабатывает batch_size пользователей и возвращает
# next_after_id, UI зовёт снова до done. requires_telegram=False: сайтовые
# аккаунты без Telegram тоже входят. Заблокированные (user_blocks) исключены
# самим сегментом.
ANTIABUSE_BULK_ACTIONS = {
    "apply_trial_limit": (
        "trial_active",
        "Применить лимит ко всем активным пробным без платежей",
    ),
    "remove_trial_limit": ("never_paid", "Снять лимит у всех пробных"),
    "remove_paid_limit": ("paid_any", "Снять лимит у всех, кто платил"),
}
ANTIABUSE_BULK_BATCH_DEFAULT = 50
ANTIABUSE_BULK_BATCH_MAX = 200
ANTIABUSE_BULK_PREVIEW = 20


def antiabuse_bulk_where_sql(action):
    segment_key, _label = ANTIABUSE_BULK_ACTIONS[action]
    return segment_where_sql(segment_key, requires_telegram=False)


def support_admin_api_antiabuse_bulk(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response
    if request.method != "POST":
        return JsonResponse({"status": "error"}, status=405)

    action = request.POST.get("action")
    if action not in ANTIABUSE_BULK_ACTIONS:
        return JsonResponse(
            {"status": "error", "message": "Неизвестное действие"}, status=400
        )
    dry_run = (request.POST.get("dry_run") or "") == "1"
    try:
        batch_size = int(request.POST.get("batch_size") or ANTIABUSE_BULK_BATCH_DEFAULT)
        after_id = int(request.POST.get("after_id") or 0)
    except ValueError:
        return JsonResponse(
            {"status": "error", "message": "batch_size/after_id должны быть числами"},
            status=400,
        )
    batch_size = min(max(batch_size, 1), ANTIABUSE_BULK_BATCH_MAX)
    after_id = max(after_id, 0)
    _segment_key, action_label = ANTIABUSE_BULK_ACTIONS[action]

    db_session = session_factory()
    try:
        where = antiabuse_bulk_where_sql(action)
        if not dry_run and not managed_limits_table_available(db_session):
            # Без маркеров массовые операции не различают ручные лимиты
            # владельца — не трогаем панель вовсе.
            return JsonResponse(
                {"status": "error", "message": MANAGED_LIMITS_MIGRATION_MESSAGE},
                status=503,
            )
        trial_limit = None
        if action == "apply_trial_limit":
            # Массовое ограничение — только при включённом тумблере: иначе
            # новые триалы создавались бы без лимита, а старые — с ним.
            if not trial_traffic_limit_enabled(db_session):
                return JsonResponse(
                    {
                        "status": "error",
                        "message": "Сначала включите лимит трафика пробных подписок",
                    },
                    status=400,
                )
            trial_limit = trial_traffic_limit_configured(db_session)

        if dry_run:
            total = (
                db_session.execute(
                    sa_text(f"SELECT count(*) FROM users u WHERE {where}")
                ).scalar()
                or 0
            )
            preview_rows = db_session.execute(
                sa_text(
                    f"SELECT u.id, u.username FROM users u WHERE {where} "
                    "ORDER BY u.id LIMIT :limit"
                ),
                {"limit": ANTIABUSE_BULK_PREVIEW},
            ).all()
            return JsonResponse(
                {
                    "status": "ok",
                    "result": {
                        "dry_run": True,
                        "action": action,
                        "action_label": action_label,
                        "total": int(total),
                        "preview": [row[1] for row in preview_rows],
                        "preview_limit": ANTIABUSE_BULK_PREVIEW,
                        "limit_label": (
                            admin_traffic_limit_label(trial_limit) if trial_limit else None
                        ),
                    },
                }
            )

        rows = db_session.execute(
            sa_text(
                f"SELECT u.id, u.username FROM users u WHERE {where} "
                "AND u.id > :after_id ORDER BY u.id LIMIT :limit"
            ),
            {"after_id": after_id, "limit": batch_size},
        ).all()

        # applied — панель изменена; marked — панель уже несла лимит, добавлен
        # маркер; skipped — нет подписки / лимита / уже управляемый;
        # skipped_paid — платил (лимит пробного не ставится, applied не растёт);
        # skipped_manual — ручной лимит владельца, панель не тронута;
        # skipped_admin_limit — лимит админа из бота (release_on='manual'),
        # маркер и панель не тронуты.
        processed = applied = marked = skipped = skipped_paid = skipped_manual = 0
        skipped_admin_limit = 0
        failed = 0
        last_id = after_id
        failures = []
        unavailable = None
        markers_unavailable = False
        for user_id, username in rows:
            try:
                rw_user = rwms_client.get_user_by_username_strict(username)
            except RwmsUnavailableError as error:
                unavailable = error
                break
            processed += 1
            last_id = int(user_id)
            if rw_user is None:
                skipped += 1
                continue
            try:
                if action == "apply_trial_limit":
                    if user_never_paid(db_session, user_id) is not True:
                        outcome = "skipped_paid"
                    else:
                        outcome, _message = admin_apply_managed_trial_limit(
                            db_session, user_id, rw_user, trial_limit, APPLIED_BY_SITE_BULK
                        )
                else:
                    outcome, _message = admin_release_managed_limit(
                        db_session, user_id, rw_user, APPLIED_BY_SITE_BULK
                    )
            except AdminManagedLimitsUnavailable:
                markers_unavailable = True
                processed -= 1
                last_id = int(user_id) - 1
                break
            except RuntimeError as error:
                failed += 1
                failures.append({"username": username, "message": str(error)[:200]})
                continue
            # Маркер и событие — короткой транзакцией на каждого пользователя
            # сразу после UpdateUser: сбой commit порции не должен оставить в
            # панели лимиты без маркеров (без маркера лимит считается ручным
            # и оплата его не снимет).
            db_session.commit()
            if outcome in ("applied", "released"):
                applied += 1
            elif outcome == "marked":
                marked += 1
            elif outcome == "skipped_paid":
                skipped_paid += 1
            elif outcome == "skipped_manual":
                skipped_manual += 1
            elif outcome == "skipped_admin_limit":
                skipped_admin_limit += 1
            else:
                skipped += 1

        done = unavailable is None and not markers_unavailable and len(rows) < batch_size
        if processed:
            admin_audit_write(
                db_session,
                request,
                "antiabuse_bulk_" + action,
                target=f"{applied}/{processed} users",
                after_id=after_id,
                next_after_id=last_id,
                marked=marked,
                skipped=skipped,
                skipped_paid=skipped_paid,
                skipped_manual=skipped_manual,
                skipped_admin_limit=skipped_admin_limit,
                failed=failed,
                limit_gb=trial_limit.limit_gb if trial_limit else None,
                rwms_unavailable=unavailable is not None,
            )
            db_session.commit()
        result = {
            "dry_run": False,
            "action": action,
            "action_label": action_label,
            "processed": processed,
            "applied": applied,
            "marked": marked,
            "skipped": skipped,
            "skipped_paid": skipped_paid,
            "skipped_manual": skipped_manual,
            "skipped_admin_limit": skipped_admin_limit,
            "failed": failed,
            "failures": failures[:20],
            "next_after_id": last_id,
            "done": done,
            "batch_size": batch_size,
        }
        if markers_unavailable:
            return JsonResponse(
                {
                    "status": "error",
                    "message": MANAGED_LIMITS_MIGRATION_MESSAGE,
                    "result": result,
                },
                status=503,
            )
        if unavailable is not None:
            logging.warning(
                "RWMS unavailable during antiabuse bulk %s after user id %s: %s",
                action,
                last_id,
                unavailable,
            )
            return JsonResponse(
                {
                    "status": "error",
                    "message": (
                        "RWMS/панель временно недоступны: обработка остановлена, "
                        f"прогресс сохранён ({applied} изменено, {processed} обработано). "
                        "Повторите позже — продолжится с того же места."
                    ),
                    "result": result,
                },
                status=503,
            )
        return JsonResponse({"status": "ok", "result": result})
    finally:
        db_session.close()


# Backfill маркеров: на проде лимиты пробных, поставленные фичей до появления
# таблицы managed_traffic_limits, маркера не имеют — оплата их не снимет.
# Кнопка «Пометить существующие лимиты пробных как управляемые» помечает
# ТОЛЬКО лимиты, совпадающие с текущим лимитом пробных (байты И стратегия).
# Проход — по ВСЕМ пользователям (без фильтра блокировок/Telegram: маркер
# пассивен, а заблокированному после разблокировки и оплаты лимит тоже должен
# сняться): never_paid — счётчик marked; платившие с той же сигнатурой —
# отдельный счётчик marked_paid (лимит пробного у платившего — наш, поставлен
# до миграции и не снят оплатой в окне «код v2 задеплоен, таблицы ещё нет»;
# после пометки reason=trial/release_on=payment его снимет страховка user-notify
# или следующая оплата). Ручные капы владельца (другой объём/стратегия) под
# правило не попадают; управляемые маркеры (в т.ч. release_on='manual' из
# бота) не перезаписываются (already). Порциями, как массовые операции;
# dry_run — тот же проход без записи.
ANTIABUSE_BACKFILL_LABEL = "Пометить существующие лимиты пробных как управляемые"

# Флаг «платил» считается тем же PAYS_EXISTS_SQL, что и сегменты never_paid /
# paid_any (один запрос на порцию вместо запроса на пользователя).
ANTIABUSE_BACKFILL_ROWS_SQL = (
    f"SELECT u.id, u.username, ({PAYS_EXISTS_SQL}) AS has_payment FROM users u "
    "WHERE u.id > :after_id ORDER BY u.id LIMIT :limit"
)


def support_admin_api_antiabuse_backfill(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response
    if request.method != "POST":
        return JsonResponse({"status": "error"}, status=405)

    dry_run = (request.POST.get("dry_run") or "") == "1"
    try:
        batch_size = int(request.POST.get("batch_size") or ANTIABUSE_BULK_BATCH_DEFAULT)
        after_id = int(request.POST.get("after_id") or 0)
    except ValueError:
        return JsonResponse(
            {"status": "error", "message": "batch_size/after_id должны быть числами"},
            status=400,
        )
    batch_size = min(max(batch_size, 1), ANTIABUSE_BULK_BATCH_MAX)
    after_id = max(after_id, 0)

    db_session = session_factory()
    try:
        if not managed_limits_table_available(db_session):
            return JsonResponse(
                {"status": "error", "message": MANAGED_LIMITS_MIGRATION_MESSAGE},
                status=503,
            )
        trial_limit = trial_traffic_limit_configured(db_session)
        rows = db_session.execute(
            sa_text(ANTIABUSE_BACKFILL_ROWS_SQL),
            {"after_id": after_id, "limit": batch_size},
        ).all()

        # marked — маркер записан у never_paid (dry_run: был бы записан);
        # marked_paid — то же у плативших (снимется страховкой user-notify /
        # оплатой); already — уже управляемый; skipped — лимита нет или другой
        # (ручной); missing — нет подписки в панели.
        processed = marked = marked_paid = already = skipped = missing = 0
        last_id = after_id
        unavailable = None
        markers_unavailable = False
        for user_id, username, has_payment in rows:
            try:
                rw_user = rwms_client.get_user_by_username_strict(username)
            except RwmsUnavailableError as error:
                unavailable = error
                break
            processed += 1
            last_id = int(user_id)
            if rw_user is None:
                missing += 1
                continue
            if not panel_limit_matches(rw_user, trial_limit):
                skipped += 1
                continue
            panel_bytes = admin_safe_int(getattr(rw_user, "traffic_limit_bytes", 0), 0)
            panel_strategy = getattr(rw_user, "traffic_limit_strategy", None)
            existing = get_managed_limit(db_session, user_id)
            if existing is not None and is_managed(existing, panel_bytes, panel_strategy):
                already += 1
                continue
            never_paid = user_never_paid_from_row(has_payment)
            if dry_run:
                if never_paid:
                    marked += 1
                else:
                    marked_paid += 1
                continue
            marker = upsert_managed_limit(
                db_session,
                user_id,
                trial_limit.limit_bytes,
                trial_limit.strategy_name,
                REASON_TRIAL,
                RELEASE_ON_PAYMENT,
                APPLIED_BY_BACKFILL,
            )
            if marker is None:
                markers_unavailable = True
                processed -= 1
                last_id = int(user_id) - 1
                break
            add_traffic_limit_event(
                db_session,
                user_id,
                EVENT_TRAFFIC_LIMIT_APPLIED,
                marker,
                backfill=True,
                changed=False,
                never_paid=never_paid,
                source="site",
            )
            logging.info(
                "managed traffic limit backfilled: user_id=%s username=%s limit=%s never_paid=%s",
                user_id,
                username,
                admin_traffic_limit_label(trial_limit),
                never_paid,
            )
            if never_paid:
                marked += 1
            else:
                marked_paid += 1

        done = unavailable is None and not markers_unavailable and len(rows) < batch_size
        if processed and not dry_run:
            admin_audit_write(
                db_session,
                request,
                "antiabuse_backfill",
                target=f"{marked + marked_paid}/{processed} users",
                after_id=after_id,
                next_after_id=last_id,
                marked_paid=marked_paid,
                already=already,
                skipped=skipped,
                missing=missing,
                limit_gb=trial_limit.limit_gb,
                strategy=trial_limit.strategy_key,
                rwms_unavailable=unavailable is not None,
            )
            db_session.commit()
        result = {
            "dry_run": dry_run,
            "action": "backfill_markers",
            "action_label": ANTIABUSE_BACKFILL_LABEL,
            "limit_label": admin_traffic_limit_label(trial_limit),
            "processed": processed,
            "marked": marked,
            "marked_paid": marked_paid,
            "already": already,
            "skipped": skipped,
            "missing": missing,
            "next_after_id": last_id,
            "done": done,
            "batch_size": batch_size,
        }
        if markers_unavailable:
            return JsonResponse(
                {
                    "status": "error",
                    "message": MANAGED_LIMITS_MIGRATION_MESSAGE,
                    "result": result,
                },
                status=503,
            )
        if unavailable is not None:
            logging.warning(
                "RWMS unavailable during antiabuse backfill after user id %s: %s",
                last_id,
                unavailable,
            )
            return JsonResponse(
                {
                    "status": "error",
                    "message": (
                        "RWMS/панель временно недоступны: обработка остановлена, "
                        f"прогресс сохранён ({marked + marked_paid} помечено, "
                        f"{processed} обработано). "
                        "Повторите позже — продолжится с того же места."
                    ),
                    "result": result,
                },
                status=503,
            )
        return JsonResponse({"status": "ok", "result": result})
    finally:
        db_session.close()


# Таблица «Последние алерты ip-guard»: username в ipguard_alerts — ЧИСЛОВОЙ ID
# пользователя панели (email в access-логе xray), не users.username; резолвим
# через RWMS GetUserById с кешем на запрос, при недоступности — деградация до ID.
IPGUARD_ALERTS_LIMIT = 50


def admin_ipguard_resolve_panel_ids(panel_ids, client=None):
    """{panel_id: (users.username, uuid) | None} — один RPC на уникальный ID."""
    rwms = client or rwms_client
    cache = {}
    for panel_id in panel_ids:
        if panel_id in cache:
            continue
        numeric = admin_safe_int(str(panel_id).strip())
        if numeric is None:
            cache[panel_id] = None
            continue
        try:
            rw_user = rwms.get_user_by_id(numeric)
        except Exception:
            logging.exception("support admin: failed to resolve panel id %s", panel_id)
            rw_user = None
        cache[panel_id] = (
            (getattr(rw_user, "username", None) or None, getattr(rw_user, "uuid", None) or None)
            if rw_user is not None
            else None
        )
    return cache


def admin_ipguard_alert_nodes(db_session, alerts):
    """{alert.id: [ноды]} — из ipguard_user_ips за окно алерта (в самой
    таблице ipguard_alerts нод нет)."""
    if not alerts:
        return {}
    usernames = {alert.username for alert in alerts}
    earliest = min(
        alert.created_at - timedelta(hours=alert.window_hours or 0) for alert in alerts
    )
    rows = (
        db_session.query(
            UserIpObservation.username,
            UserIpObservation.node,
            func.min(UserIpObservation.first_seen),
            func.max(UserIpObservation.last_seen),
        )
        .filter(
            UserIpObservation.username.in_(usernames),
            UserIpObservation.last_seen >= earliest,
        )
        .group_by(UserIpObservation.username, UserIpObservation.node)
        .all()
    )
    by_user = {}
    for username, node, first_seen, last_seen in rows:
        by_user.setdefault(username, []).append((node, first_seen, last_seen))
    result = {}
    for alert in alerts:
        # Нода попадает в алерт, если наблюдалась в его окне
        # [created_at - window; created_at]: появилась не позже алерта и
        # была видна не раньше начала окна.
        since = alert.created_at - timedelta(hours=alert.window_hours or 0)
        result[alert.id] = sorted(
            {
                node
                for node, first_seen, last_seen in by_user.get(alert.username, [])
                if last_seen is not None
                and last_seen >= since
                and (first_seen is None or first_seen <= alert.created_at)
            }
        )
    return result


def support_admin_api_ipguard_alerts(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response
    if request.method != "GET":
        return JsonResponse({"status": "error"}, status=405)

    db_session = session_factory()
    try:
        alerts = (
            db_session.query(IpAlert)
            .order_by(IpAlert.created_at.desc(), IpAlert.id.desc())
            .limit(IPGUARD_ALERTS_LIMIT)
            .all()
        )
        resolved = admin_ipguard_resolve_panel_ids([alert.username for alert in alerts])
        local_usernames = set()
        names = {item[0] for item in resolved.values() if item and item[0]}
        if names:
            local_usernames = {
                row[0]
                for row in db_session.query(User.username)
                .filter(User.username.in_(names))
                .all()
            }
        try:
            nodes = admin_ipguard_alert_nodes(db_session, alerts)
        except Exception:
            logging.exception("support admin: failed to load ip-guard alert nodes")
            nodes = {}
        rows = []
        for alert in alerts:
            item = resolved.get(alert.username)
            username = item[0] if item else None
            rows.append(
                {
                    "id": alert.id,
                    "created_at": admin_date_label(alert.created_at),
                    "level": alert.level,
                    "panel_id": alert.username,
                    "username": username,
                    "user_uuid": item[1] if item else None,
                    "local_user": bool(username and username in local_usernames),
                    "unique_subnet_count": alert.unique_subnet_count,
                    "unique_ip_count": alert.unique_ip_count,
                    "threshold": alert.threshold,
                    "window_hours": alert.window_hours,
                    "nodes": nodes.get(alert.id, []),
                }
            )
        return JsonResponse(
            {"status": "ok", "result": {"alerts": rows, "limit": IPGUARD_ALERTS_LIMIT}}
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
    uploaded_files = request.FILES.getlist("attachments")
    if not message:
        return redirect("support_admin_ticket_detail", ticket_id=ticket_id)
    attachment_error = validate_support_attachments(uploaded_files)
    if attachment_error:
        return support_attachment_validation_response(request, attachment_error)

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
                uploaded_files,
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

        return support_attachment_file_response(attachment, path)
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


def purchase_login_email_content(request):
    """Тема и контекст письма с постоянной ссылкой доступа (plogin_) после покупки.

    Только из request, без БД: вызывается и после commit счёта, и в ветке
    неизвестного исхода оплаты уже после rollback (FINAL-PAY-02), поэтому
    письмо в обоих случаях одинаковое.
    """
    product_name = (
        "Monkey Island VPS"
        if get_site_role(request) in ("vps", "vps_direct_sale")
        else "VPN Monkey Island"
    )
    return (
        f"Ссылка доступа {product_name}",
        {
            "title": "Доступ готов",
            "intro": f"Мы подготовили для вас доступ {product_name}.",
            "note": (
                "После оплаты зайдите по кнопке ниже: ссылка постоянная и "
                "откроет оплаченный доступ, инструкции для устройств и поддержку."
            ),
            "button_text": "Открыть доступ",
            "footer": (
                f"Если вы не оформляли {product_name}, просто "
                "проигнорируйте это письмо."
            ),
        },
    )


def wants_payment_launch_json(request):
    return (
        request.headers.get("X-Payment-Launch") == "new-tab"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )


PAYMENT_RATE_LIMITED_MESSAGE = (
    "Слишком много попыток оплаты. Подождите несколько минут и попробуйте снова."
)


def payment_error_response(message, status, payment_launch_json):
    """Отказ в запуске оплаты: JSON с message для fetch-запуска, иначе текст."""
    if payment_launch_json:
        return JsonResponse({"status": "error", "message": message}, status=status)
    return HttpResponse(message, status=status)


def payment_rate_limited_response(retry_after, payment_launch_json):
    response = payment_error_response(
        PAYMENT_RATE_LIMITED_MESSAGE, 429, payment_launch_json
    )
    response["Retry-After"] = str(retry_after)
    return response


def anonymous_payment_rate_limited(request, email):
    """Лимиты оплаты без входа. Возвращает ``(limited, retry_after)``.

    Вызывается до обращения к БД. Порядок как в send_magic_link: IP → email →
    общий; отбитый запрос следующие счётчики не расходует, иначе один IP
    выбивал бы оплату всем. Email и общий бакет считают только адреса
    валидного формата: мусор не расходует лимит чужого адреса и общий потолок
    (битый адрес ограничен IP-бакетом, новый аккаунт на него не создаётся).
    Пороги мягкие: у мобильных операторов много абонентов за одним IP (CGNAT).
    IP и email в лог попадают только отпечатком.
    """
    ip = client_ip(request) or "unknown"
    limited, retry_after = rate_limit_exceeded(
        "payment-anonymous",
        (
            (
                "ip",
                ip,
                settings.PAYMENT_ANON_IP_RATE_LIMIT,
                settings.PAYMENT_ANON_RATE_WINDOW_SECONDS,
            ),
        ),
    )
    if limited:
        logging.warning(
            "anonymous payment IP rate limit reached "
            "(ip_digest=%s limit=%s window=%ss)",
            rate_limit_log_digest(ip),
            settings.PAYMENT_ANON_IP_RATE_LIMIT,
            settings.PAYMENT_ANON_RATE_WINDOW_SECONDS,
        )
        return True, retry_after

    if not is_valid_email(email):
        return False, 0

    limited, retry_after = rate_limit_exceeded(
        "payment-anonymous",
        (
            (
                "email",
                email,
                settings.PAYMENT_ANON_EMAIL_RATE_LIMIT,
                settings.PAYMENT_ANON_RATE_WINDOW_SECONDS,
            ),
        ),
    )
    if limited:
        logging.warning(
            "anonymous payment email rate limit reached "
            "(email_digest=%s limit=%s window=%ss)",
            rate_limit_log_digest(email),
            settings.PAYMENT_ANON_EMAIL_RATE_LIMIT,
            settings.PAYMENT_ANON_RATE_WINDOW_SECONDS,
        )
        return True, retry_after

    limited, retry_after = rate_limit_exceeded(
        "payment-anonymous",
        (
            (
                "global",
                "all",
                settings.PAYMENT_ANON_GLOBAL_RATE_LIMIT,
                settings.PAYMENT_ANON_GLOBAL_RATE_WINDOW_SECONDS,
            ),
        ),
    )
    if limited:
        logging.warning(
            "anonymous payment global rate limit reached (limit=%s window=%ss)",
            settings.PAYMENT_ANON_GLOBAL_RATE_LIMIT,
            settings.PAYMENT_ANON_GLOBAL_RATE_WINDOW_SECONDS,
        )
    return limited, retry_after


def pay(request):
    if request.method == "POST":
        capture_tracking_params(request)
        email_raw = request.POST.get("email")
        tariff_id = request.POST.get("tariff_id")
        prepared_attempt = None
        prepared_attempt_id = None
        raw_status_token = None
        raw_login_token = None
        login_link = None
        send_payment_login_email = False
        payment_login_email_attempted = False
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
            return payment_error_response(
                "Email обязателен", 400, payment_launch_json
            )

        email = email_raw.lower().strip()

        if not email or not tariff_id:
            logging.warning(
                "payment request rejected: email_or_tariff_missing email_present=%s tariff_id=%s",
                bool(email),
                tariff_id,
            )
            return payment_error_response(
                "Не указан email или тариф", 400, payment_launch_json
            )

        # Прямая покупка с лендинга (решение владельца, R03): аноним вводит
        # email и сразу платит, аккаунт по email создаётся до оплаты. Email чека
        # не доказывает владение адресом, поэтому для анонима ниже: браузеру
        # отдаётся только pstatus_-ссылка статуса (никогда не авторизует),
        # ссылка входа (plogin_ или короткая magic) уходит только письмом на
        # этот email, сессия не авторизуется, пробная подписка в панели не
        # создаётся (allow_trial=False, B12). Лимиты частоты — до обращения к БД.
        # Ответ confirmation_required прежнего потока больше не выдаётся; его
        # обработка в JS лендингов и чтение pending_checkout_tariff_id кабинетом
        # оставлены для закэшированных страниц и старых сессий.
        if not getattr(request.user, "is_authenticated", False):
            limited, retry_after = anonymous_payment_rate_limited(request, email)
            if limited:
                return payment_rate_limited_response(
                    retry_after, payment_launch_json
                )

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
                return payment_error_response(
                    "Выбранный тариф не найден", 400, payment_launch_json
                )

            # Формат email. Раньше проверялась только непустота, и адрес без
            # точки в домене ('milenapanowa@yandex' — браузерный type="email"
            # такое пропускает) привязывался к аккаунту прямо здесь, а потом
            # ломал запрос к панели (инцидент 2026-09-10 в Village).
            #
            # ДЕДУШКИНА ОГОВОРКА: у существующих пользователей битый адрес уже
            # лежит в users.email, и форма кабинета отправляет его СКРЫТЫМ
            # полем (dashboard.html), а ниже стоит проверка «email не совпадает
            # с аккаунтом». Отклонять такой адрес нельзя — человек не смог бы
            # заплатить вообще ничем: битый отверг бы валидатор, валидный —
            # проверка совпадения. Поэтому строго проверяются только НОВЫЕ
            # адреса: если строка users с таким email уже есть, платёж идёт
            # как раньше, а адрес чинится отдельно.
            if not is_valid_email(email):
                known_user = (
                    db_session.query(User).filter(User.email == email).first()
                )
                if known_user is None:
                    logging.warning(
                        "payment request rejected: invalid email format email=%s "
                        "tariff_id=%s",
                        email,
                        tariff_id,
                    )
                    invalid_email_message = (
                        "Проверьте адрес электронной почты: похоже, в нём "
                        "опечатка. Он нужен для чека и входа в личный кабинет."
                    )
                    if payment_launch_json:
                        return JsonResponse(
                            {"status": "error", "message": invalid_email_message},
                            status=400,
                        )
                    return HttpResponse(invalid_email_message, status=400)

                logging.warning(
                    "payment with a malformed email that is already stored: "
                    "email=%s user_id=%s tariff_id=%s — payment is allowed so the "
                    "user is not locked out, the address needs fixing in users",
                    email,
                    known_user.id,
                    tariff_id,
                )

            logging.info(
                "payment request accepted: email=%s tariff_id=%s price=%s",
                email,
                tariff.db_tariff_id,
                tariff.price,
            )

            authenticated_user = None
            if request.user.is_authenticated:
                authenticated_user = (
                    db_session.query(User).filter(User.id == request.user.id).with_for_update().first()
                )
                if not authenticated_user:
                    logging.warning(
                        "payment request rejected: authenticated user not found user_id=%s",
                        request.user.id,
                    )
                    return HttpResponse("Аккаунт не найден", status=401)

            # Ищем или создаем пользователя. Для авторизованного аккаунта используем
            # именно текущую запись, чтобы платеж не создал дубль по email.
            payment_email_confirmation = None
            if authenticated_user:
                user = authenticated_user
                is_authenticated_payment = True
                logging.info(
                    "payment uses authenticated account and receipt email without "
                    "changing account identity: receipt_email=%s user_id=%s "
                    "username=%s tariff_id=%s",
                    email,
                    user.id,
                    user.username,
                    tariff.db_tariff_id,
                )
                # Email чека не доказывает владение адресом (B23): в users.email
                # он здесь не пишется. Аккаунту без email после создания счёта
                # отправим штатное письмо подтверждения (как update_email) —
                # адрес привяжется только по ссылке из письма.
                if not user.email and is_valid_email(email):
                    payment_email_confirmation = (user.id, email)
            else:
                # FOR UPDATE, как в авторизованной ветке: find_reusable_attempt
                # рассчитан на лок users, иначе два одновременных анонимных POST
                # на один email создали бы две попытки и два платежа
                # (FINAL-PAY-03). Для нового email сериализует advisory-лок в
                # create_site_user.
                user = (
                    db_session.query(User)
                    .filter(User.email == email)
                    .with_for_update()
                    .first()
                )
                if not user:
                    # Владение адресом ещё не подтверждено: аккаунт без пробной
                    # подписки в панели (B12); оплата продлит его по webhook.
                    user = create_site_user(
                        db_session, email, request, allow_trial=False
                    )
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
                return payment_error_response(
                    ACCOUNT_BLOCKED_MESSAGE, 403, payment_launch_json
                )

            use_permanent_purchase_link = (
                request.POST.get("login_link_kind") == "purchase_permanent"
            )
            base_url = get_current_base_url(request)
            payment_success_redirect_url = f"{base_url}/dashboard/"
            payment_fail_redirect_url = f"{base_url}/"
            login_link = None
            send_payment_login_email = should_send_payment_login_email(request, user)

            raw_status_token = create_purchase_status_token(db_session, user)
            status_token_row = get_purchase_status_token(db_session, raw_status_token)
            if status_token_row is None:
                raise RuntimeError("failed to persist purchase status token")
            logging.info(
                "created payment status token: email=%s user_id=%s tariff_id=%s",
                email,
                user.id,
                tariff.db_tariff_id,
            )

            login_token_row = None
            if use_permanent_purchase_link and send_payment_login_email:
                raw_login_token = create_purchase_login_token(db_session, user)
                login_token_row = get_purchase_login_token(db_session, raw_login_token)
                if login_token_row is None:
                    raise RuntimeError("failed to persist purchase login token")
                login_link = build_purchase_login_link(request, raw_login_token)
                logging.info(
                    "created permanent purchase login link: email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )

            payment_status_url = build_payment_status_url(request, raw_status_token)
            payment_success_redirect_url = payment_status_url
            payment_fail_redirect_url = append_query_params(
                payment_status_url,
                {"result": "failed"},
            )

            tariff, promo_discount_applied = site_apply_first_purchase_discount(
                db_session, user, tariff
            )

            gateway = "wata" if settings.PAYMENT_GATEWAY.lower() == "wata" else "yookassa"
            # Цена и промо в отпечатке: после смены цены или активации скидки
            # старая ссылка не переиспользуется.
            fingerprint = fingerprint_for(
                user.id, gateway, tariff.db_tariff_id, email,
                price=tariff.price, promo=promo_discount_applied,
            )
            # username: после merge попытка ЮKassa с metadata проигравшего не
            # переиспользуется (XSVC-01); для Wata ничего не меняется.
            previous_attempt = find_reusable_attempt(
                db_session, user.id, fingerprint, username=user.username
            )
            if previous_attempt is not None:
                previous_status_url = previous_attempt.status_url
                previous_payment_url = previous_attempt.confirmation_url or previous_status_url
                db_session.rollback()  # Discard newly prepared, unused tokens.
                if payment_launch_json:
                    return JsonResponse({"status": "ok", "payment_url": previous_payment_url,
                                         "payment_status_url": previous_status_url})
                return redirect(previous_payment_url)
            attempt = WebsitePaymentAttempt(
                id=str(uuid.uuid4()), user_id=user.id, fingerprint=fingerprint,
                gateway=gateway, tariff_id=tariff.db_tariff_id, state="prepared",
                status_token_hash=status_token_row.token_hash,
                login_token_hash=login_token_row.token_hash if login_token_row else None,
                status_url=payment_status_url, created_at=datetime.utcnow(), attempts=0,
            )
            def before_provider_send(payload):
                nonlocal prepared_attempt, prepared_attempt_id
                # Wata orderId is known before network I/O; the payment callback
                # can resolve its owner through this committed mapping.
                if gateway == "wata":
                    attempt.provider_reference = attempt.id
                    status_token_row.payment_gateway = gateway
                    status_token_row.payment_reference = attempt.id
                    if login_token_row:
                        login_token_row.payment_gateway = gateway
                        login_token_row.payment_reference = attempt.id
                # id читаем до commit: после него атрибуты ORM истекают, а в
                # ветке ошибки соединение может быть уже мёртвым.
                attempt_id = attempt.id
                persist_before_send(db_session, attempt, payload)
                prepared_attempt = attempt
                prepared_attempt_id = attempt_id

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
                    idempotency_key=attempt.id, before_send=before_provider_send,
                )

                confirmation_url = created_payment.confirmation_url
                status_token_row.payment_gateway = "wata"
                status_token_row.payment_reference = created_payment.reference
                if login_token_row is not None:
                    login_token_row.payment_gateway = "wata"
                    login_token_row.payment_reference = created_payment.reference

                finish_attempt(db_session, attempt, created_payment)

                logging.info(
                    f"an invoice for the {tariff.db_tariff_id} tariff has been created for "
                    f"user_id={user.id}"
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
                    idempotency_key=attempt.id, before_send=before_provider_send,
                    # Автоплатёж — только при email в аккаунте: yk-recurrent берёт
                    # email чека из users.email и без него не спишет. Без
                    # подтверждённого email платёж разовый.
                    save_payment_method=bool(user.email),
                    # metadata.email — email аккаунта, не чека (XSVC-01).
                    account_email=user.email or None,
                )
                finish_attempt(db_session, attempt, created_payment)
                confirmation_url = created_payment.confirmation_url
                status_token_row.payment_gateway = "yookassa"
                status_token_row.payment_reference = created_payment.reference
                if login_token_row is not None:
                    login_token_row.payment_gateway = "yookassa"
                    login_token_row.payment_reference = created_payment.reference

            invoice_event = create_invoice_event_for_tariff(tariff.db_tariff_id)
            if invoice_event:
                add_event_log(db_session, user, invoice_event)

            if use_permanent_purchase_link:
                email_subject, email_template_context = (
                    purchase_login_email_content(request)
                )
            else:
                magic = MagicToken(user_id=user.id)
                db_session.add(magic)
                db_session.flush()
                login_link = build_magic_link(
                    get_current_base_url(request), magic.token
                )
                logging.info(
                    "created short payment magic link: email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )
                email_subject = "Ссылка на личный кабинет Monkey Island"
                email_template_context = {
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
                }

            db_session.commit()
            request.session[payment_session_url_key(raw_status_token)] = (
                confirmation_url
            )
            request.session.modified = True
            logging.info(
                "payment db transaction committed: email=%s user_id=%s tariff_id=%s",
                email,
                user.id,
                tariff.db_tariff_id,
            )

            if not send_payment_login_email:
                logging.info(
                    "payment login email skipped for authenticated user: "
                    "email=%s user_id=%s tariff_id=%s",
                    email,
                    user.id,
                    tariff.db_tariff_id,
                )
            else:
                payment_login_email_attempted = True
                try:
                    send_magic_link_email(
                        email,
                        login_link,
                        subject=email_subject,
                        template_context=email_template_context,
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

            if payment_email_confirmation:
                # Счёт уже создан и закоммичен; сбой письма оплату не ломает.
                send_payment_email_confirmation(request, *payment_email_confirmation)

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

        except SiteRegistrationUnavailable as e:
            # Аккаунт под оплату подготовить нельзя. Ничего не создано и не
            # списано — не плодим сироты в Remnawave. Временная недоступность
            # панели → «повторите позже»; конфликт владельца сам не пройдёт →
            # отправляем в поддержку (ALERT в логе уже есть).
            ownership_conflict = isinstance(e, SiteRegistrationOwnershipConflict)
            error_message = (
                SITE_REGISTRATION_SUPPORT_MESSAGE
                if ownership_conflict
                else SITE_REGISTRATION_RETRY_MESSAGE
            )
            status_code = 409 if ownership_conflict else 503
            db_session.rollback()
            logging.warning(
                "payment registration %s for %s: %s",
                "ownership conflict" if ownership_conflict else "postponed",
                email,
                e,
            )
            if payment_launch_json:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": error_message,
                    },
                    status=status_code,
                )
            messages.error(request, error_message)
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
                    status=status_code,
                )
            return redirect("index")

        except Exception as e:
            db_session.rollback()
            if prepared_attempt is not None and not isinstance(e, ProviderRejected):
                # Таймаут, обрыв, 429/5xx, 202 после ретраев, неразборчивый
                # ответ: платёж мог создаться — ждём сверку, новый POST нельзя.
                logging.warning(
                    "checkout result unknown; recovery scheduled attempt_id=%s",
                    prepared_attempt_id,
                    exc_info=True,
                )
                if (
                    raw_login_token is not None
                    and login_link
                    and send_payment_login_email
                    and not payment_login_email_attempted
                ):
                    # FINAL-PAY-02: plogin_-токен уже закоммичен вместе с
                    # попыткой (persist_before_send), и после оплаты страница
                    # статуса скажет, что ссылка отправлена. plogin_ авторизует
                    # только после подтверждённой оплаты, поэтому письмо до
                    # исхода безопасно. Атрибуты ORM после rollback не читаем.
                    payment_login_email_attempted = True
                    try:
                        email_subject, email_template_context = (
                            purchase_login_email_content(request)
                        )
                        send_magic_link_email(
                            email,
                            login_link,
                            subject=email_subject,
                            template_context=email_template_context,
                        )
                        logging.info(
                            "payment login email sent after unknown checkout "
                            "outcome attempt_id=%s",
                            prepared_attempt_id,
                        )
                    except Exception:
                        logging.exception(
                            "failed to send payment login email after unknown "
                            "checkout outcome attempt_id=%s",
                            prepared_attempt_id,
                        )
                if payment_launch_json:
                    return JsonResponse({"status": "ok", "payment_url": payment_status_url,
                                         "payment_status_url": payment_status_url})
                return redirect(payment_status_url)
            if prepared_attempt is not None:
                # Провайдер однозначно отказал (400/401/403/404, у Wata ещё
                # 422): платёж не создан, попытка не должна блокировать повтор.
                logging.error(
                    "Pay error: payment provider rejected checkout attempt_id=%s",
                    prepared_attempt_id,
                    exc_info=e,
                )
                try:
                    mark_attempt_failed(db_session, prepared_attempt_id)
                except Exception:
                    try:
                        db_session.rollback()
                    except Exception:
                        pass
                    logging.error(
                        "checkout attempt could not be marked failed attempt_id=%s",
                        prepared_attempt_id,
                        exc_info=True,
                    )
            else:
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
    neutral_role = site_role in ("vps", "vps_direct_sale")
    app_name = "Monkey Island VPS" if neutral_role else "VPN Monkey Island"
    start_url = "/" if neutral_role else "/dashboard/"
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

    POST action=save|bulk_update|delete|delete_many|toggle|run.
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

            if action == "delete_many":
                # Массовое удаление отмеченных строк. Ограничение сверху —
                # защита от случайного «удалить всё» одним запросом, а не
                # бизнес-правило
                ids: list[int] = []
                for value in request.POST.getlist("check_ids") or []:
                    for part in str(value).split(","):
                        part = part.strip()
                        # isdigit() пропускает не-десятичные юникод-цифры, на
                        # которых int() падает необработанным ValueError (500
                        # вместо 400); длина ограничена, чтобы огромное число
                        # не уехало в драйвер БД
                        if part.isascii() and part.isdigit() and len(part) <= 18:
                            ids.append(int(part))
                ids = list(dict.fromkeys(ids))
                skipped = max(0, len(ids) - 500)
                ids = ids[:500]
                if not ids:
                    return JsonResponse(
                        {"status": "error",
                         "message": "Не выбрано ни одной проверки"},
                        status=400,
                    )
                db_session.query(CensorCheckRun).filter(
                    CensorCheckRun.check_id.in_(ids)
                ).delete(synchronize_session=False)
                deleted = (
                    db_session.query(CensorCheck)
                    .filter(CensorCheck.id.in_(ids))
                    .delete(synchronize_session=False)
                )
                # target обрезался бы посреди числа и оставлял в журнале
                # чужой id — полный перечень идёт отдельным полем details
                admin_audit_write(
                    db_session, request, "censor_check_delete_many",
                    target=f"{len(ids)} проверок",
                    check_ids=ids,
                    count=int(deleted or 0),
                )
                db_session.commit()
                return JsonResponse({
                    "status": "ok",
                    "deleted": int(deleted or 0),
                    # Молчаливая отсечка читалась бы как «удалено всё»
                    "skipped": skipped,
                })

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


# --- Окончания и продления подписок по тарифам («Привлечение → Окончания») ---
#
# Точной истории сроков нет: users.expire_at хранит только текущий срок, а
# событие subscription_expired из common.models.analytics_event ни один сервис
# не пишет. Концы прошлых периодов восстанавливаются из цепочки оплат: период
# = max(конец предыдущего, момент оплаты) + длительность тарифа (так продлевает
# payment-сервис: extend_user_subscription стакует срок, если он ещё не истёк).
# У последнего периода конец — реальный expire_at (в нём и бонусы, и ручные
# продления), у прошлых бонусы не видны — цифры за прошлое помечаются как
# оценка. Пробные подписки: без единой оплаты — конец по expire_at (точно);
# у плативших — старт из события subscription_created + пробные дни.
EXPIRY_TARIFF_DAYS = {
    "oneday": 1,
    "threedays": 3,
    "oneweek": 7,
    "month": 30,
    "threemonths": 90,
    "sixmonths": 180,
    "year": 360,
}
EXPIRY_TRIAL_KEY = "trial"
EXPIRY_OTHER_KEY = "other"
EXPIRY_OTHER_DAYS = 30
EXPIRY_TARIFF_ORDER = (
    EXPIRY_TRIAL_KEY,
    "oneday",
    "threedays",
    "oneweek",
    "month",
    "threemonths",
    "sixmonths",
    "year",
    EXPIRY_OTHER_KEY,
)
EXPIRY_TARIFF_LABELS = {EXPIRY_TRIAL_KEY: "Пробный", EXPIRY_OTHER_KEY: "Без тарифа"}
# Окно продления в днях после конца периода; другие значения приводятся к 30.
EXPIRY_WINDOWS = (7, 14, 30)
EXPIRY_DEFAULT_WINDOW = 30
EXPIRY_MAX_RANGE_DAYS = 366
EXPIRY_DEFAULT_TRIAL_DAYS = 7


def _expiry_tariff_key(tariff):
    return tariff if tariff in EXPIRY_TARIFF_DAYS else EXPIRY_OTHER_KEY


def _expiry_tariff_label(key):
    return EXPIRY_TARIFF_LABELS.get(key) or get_tariff_display_name(key)


def _expiry_load_payments(db_session):
    """(user_id, paid_at UTC, tariff, amount ₽, provider, autopay) по всем
    успешным оплатам. provider: yk | wata; autopay — автосписание ЮKassa."""
    return [
        (
            int(r["user_id"]),
            r["paid_at"],
            str(r["tariff"] or ""),
            float(r["amount"] or 0),
            str(r["provider"]),
            bool(r["autopay"]),
        )
        for r in _acq_rows(
            db_session,
            """
            SELECT user_id, paid_at, tariff, amount, provider, autopay FROM (
                SELECT wi.user_id AS user_id,
                       (t.payment_time AT TIME ZONE 'UTC') AS paid_at,
                       COALESCE(wi.tariff_id, '') AS tariff,
                       t.amount::numeric AS amount,
                       'wata' AS provider,
                       false AS autopay
                FROM wata_transactions t
                JOIN wata_invoices wi ON wi.order_id = t.order_id
                WHERE t.transaction_status = 'Paid'
                UNION ALL
                SELECT p.user_id, p.created_at, p.subscription_period, p.amount::numeric,
                       'yk' AS provider, COALESCE(p.is_autopay, false) AS autopay
                FROM yk_payments p
                WHERE p.status = 'succeeded'
            ) pays
            ORDER BY user_id, paid_at
            """,
        )
    ]


# Сроки нужны плативших (правда для последнего периода) и пробных без оплат,
# чей срок попадает в мыслимый диапазон: старые истёкшие триалы (старше
# EXPIRY_MAX_RANGE_DAYS + запас) ни в один отчёт не попадут — не тянем.
EXPIRY_EXPIRES_LOOKBACK_DAYS = EXPIRY_MAX_RANGE_DAYS + 30


def _expiry_load_expires(db_session):
    return {
        int(r["id"]): r["expire_at"]
        for r in _acq_rows(
            db_session,
            f"""
            SELECT u.id, u.expire_at FROM users u
            WHERE u.expire_at IS NOT NULL
              AND (
                u.expire_at >= now() AT TIME ZONE 'UTC'
                    - make_interval(days => {int(EXPIRY_EXPIRES_LOOKBACK_DAYS)})
                OR ({PAYS_EXISTS_SQL})
              )
            """,
        )
    }


def _expiry_load_autopay(db_session):
    """Кто продлится сам: есть привязка рекуррента и автоплатёж не отключён."""
    return {
        int(r["user_id"])
        for r in _acq_rows(
            db_session,
            """
            SELECT r.user_id FROM yk_recurrent_payments r
            JOIN users u ON u.id = r.user_id
            WHERE u.autopay_allow
            """,
        )
    }


def _expiry_load_trial_starts(db_session):
    """Старт пробного нужен только плативших: у остальных конец пробного —
    это их expire_at, событие не требуется."""
    return {
        int(r["user_id"]): r["started_at"]
        for r in _acq_rows(
            db_session,
            f"""
            SELECT e.user_id, min(e.timestamp) AS started_at FROM event_logs e
            WHERE e.event_type = 'subscription_created'
              AND EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id AND ({PAYS_EXISTS_SQL}))
            GROUP BY e.user_id
            """,
        )
    }


def _expiry_load_trial_days(db_session):
    setting = db_session.get(SystemSetting, BOT_TRIAL_PERIOD_DAYS_SETTING)
    return parse_positive_int_setting(
        setting.value if setting else None, EXPIRY_DEFAULT_TRIAL_DAYS
    )


def _expiry_periods(payments, expires, trial_starts, trial_days, autopay_users):
    """Периоды подписок:
    (user_id, end_utc, tariff, next_paid_at, autopay, next_tariff).

    next_paid_at — момент следующей оплаты после начала периода (None — её
    нет); по нему считается продление относительно конца периода.
    next_tariff — тариф этой следующей оплаты: по нему строится матрица
    переходов «с какого тарифа на какой пересаживаются».
    """
    periods = []
    by_user = {}
    for row in payments:
        user_id, paid_at, tariff = row[:3]
        by_user.setdefault(user_id, []).append((paid_at, tariff))
    trial_delta = timedelta(days=trial_days)
    for user_id, pays in by_user.items():
        autopay = user_id in autopay_users
        end_prev = None
        started = trial_starts.get(user_id)
        if started is not None:
            end_prev = started + trial_delta
            periods.append(
                (
                    user_id,
                    end_prev,
                    EXPIRY_TRIAL_KEY,
                    pays[0][0],
                    autopay,
                    _expiry_tariff_key(pays[0][1]),
                )
            )
        for index, (paid_at, tariff) in enumerate(pays):
            key = _expiry_tariff_key(tariff)
            days = EXPIRY_TARIFF_DAYS.get(key, EXPIRY_OTHER_DAYS)
            base = paid_at if end_prev is None or end_prev < paid_at else end_prev
            end = base + timedelta(days=days)
            is_last = index == len(pays) - 1
            if is_last:
                actual = expires.get(user_id)
                if actual is not None and actual >= paid_at:
                    end = actual
            next_paid = pays[index + 1][0] if not is_last else None
            next_key = _expiry_tariff_key(pays[index + 1][1]) if not is_last else None
            periods.append((user_id, end, key, next_paid, autopay, next_key))
            end_prev = end
    for user_id, expire_at in expires.items():
        if user_id in by_user:
            continue
        periods.append((user_id, expire_at, EXPIRY_TRIAL_KEY, None, False, None))
    return periods


def _expiry_bucket_key(end_utc, group):
    day = (end_utc + ADMIN_TZ_OFFSET).date()
    if group == "week":
        return day - timedelta(days=day.weekday())
    return day


def _expiry_bucket_keys(start, end, group):
    keys = []
    cursor = start - timedelta(days=start.weekday()) if group == "week" else start
    step = timedelta(days=7 if group == "week" else 1)
    while cursor <= end:
        keys.append(cursor)
        cursor += step
    return keys


def _expiry_aggregate(periods, start, end, group, window_days, now, today):
    """Считает бакеты диапазона [start, end] (даты МСК) из списка периодов."""
    window = timedelta(days=window_days)
    keys = _expiry_bucket_keys(start, end, group)
    index = {key: i for i, key in enumerate(keys)}
    empty = lambda: {"ending": {}, "renewed": {}, "pending": {}, "autopay": {}}  # noqa: E731
    buckets = [empty() for _ in keys]
    seen = set()
    # Уникальные подписки: у коротких тарифов (день/3 дня) один человек даёт
    # по периоду на каждую оплату, и «окончаний» в разы больше, чем подписок.
    users_by_tariff = {}
    users_all = set()
    # Матрица переходов по продлениям диапазона: с какого тарифа на какой
    # (диагональ — остались на своём). Только по бакетам с закрытым окном —
    # иначе «не продлились» ещё не окончательные.
    transitions = {}
    churned = {}

    def bump(counter, tariff, step=1):
        counter[tariff] = counter.get(tariff, 0) + step

    closed_positions = set()
    for position, key in enumerate(keys):
        last_day = key if group == "day" else key + timedelta(days=6)
        if last_day + timedelta(days=window_days) < today:
            closed_positions.add(position)

    for user_id, period_end, tariff, next_paid, autopay, next_tariff in periods:
        key = _expiry_bucket_key(period_end, group)
        position = index.get(key)
        if position is None:
            continue
        seen.add(tariff)
        users_by_tariff.setdefault(tariff, set()).add(user_id)
        users_all.add(user_id)
        bucket = buckets[position]
        bump(bucket["ending"], tariff)
        if period_end > now:
            if autopay and tariff != EXPIRY_TRIAL_KEY:
                bump(bucket["autopay"], tariff)
            continue
        renewed = next_paid is not None and next_paid <= period_end + window
        if renewed:
            bump(bucket["renewed"], tariff)
            if position in closed_positions:
                bump(transitions.setdefault(tariff, {}), next_tariff or EXPIRY_OTHER_KEY)
        elif next_paid is None and now < period_end + window:
            bump(bucket["pending"], tariff)
        elif position in closed_positions:
            bump(churned, tariff)

    tariffs = [key for key in EXPIRY_TARIFF_ORDER if key in seen]
    rows = []
    totals = {
        "ending": 0,
        "ending_past": 0,
        "ending_future": 0,
        "renewed": 0,
        "pending": 0,
        "autopay_future": 0,
        # Финальная доля продлений: только бакеты с закрытым окном.
        "ending_closed": 0,
        "renewed_closed": 0,
        "subscriptions": len(users_all),
        "by_tariff": {},
        # {from: {to: n}} и {from: не продлившиеся} — по закрытому окну.
        "transitions": transitions,
        "churned": churned,
    }
    for key, bucket in zip(keys, buckets):
        last_day = key if group == "day" else key + timedelta(days=6)
        is_past = last_day < today
        is_current = key <= today <= last_day
        # Окно закрыто у всех периодов бакета: последний день бакета + окно
        # уже прошли. Только по таким бакетам доля продлений финальна —
        # в остальных она занижена (кто-то ещё продлится).
        window_closed = last_day + timedelta(days=window_days) < today
        ending_total = sum(bucket["ending"].values())
        renewed_total = sum(bucket["renewed"].values())
        rows.append(
            {
                "key": key.isoformat(),
                "label": key.strftime("%d.%m"),
                "is_past": is_past,
                "is_current": is_current,
                "window_closed": window_closed,
                "ending": bucket["ending"],
                "renewed": bucket["renewed"],
                "pending": bucket["pending"],
                "autopay": bucket["autopay"],
                "total_ending": ending_total,
                "total_renewed": renewed_total,
                "total_pending": sum(bucket["pending"].values()),
                "total_autopay": sum(bucket["autopay"].values()),
            }
        )
        for tariff in tariffs:
            slot = totals["by_tariff"].setdefault(
                tariff,
                {
                    "ending": 0,
                    "renewed": 0,
                    "pending": 0,
                    "autopay": 0,
                    "subscriptions": len(users_by_tariff.get(tariff, ())),
                },
            )
            for name in ("ending", "renewed", "pending", "autopay"):
                slot[name] += bucket[name].get(tariff, 0)
        totals["ending"] += ending_total
        totals["renewed"] += renewed_total
        totals["pending"] += sum(bucket["pending"].values())
        totals["autopay_future"] += sum(bucket["autopay"].values())
    # Прошлое/будущее по бакетам: текущий бакет относится к прошлому — в нём
    # уже есть истёкшие, а продления по ним ещё копятся (pending).
    for row in rows:
        if row["is_past"] or row["is_current"]:
            totals["ending_past"] += row["total_ending"]
        else:
            totals["ending_future"] += row["total_ending"]
        if row["window_closed"]:
            totals["ending_closed"] += row["total_ending"]
            totals["renewed_closed"] += row["total_renewed"]
    return tariffs, rows, totals


# Сборка периодов — самая дорогая часть (все оплаты, сроки, старты пробных,
# ~сотни тысяч строк): кэшируется в памяти процесса на EXPIRY_CACHE_TTL секунд.
# Смена диапазона/шага/окна в админке пересчитывает только бакеты по готовому
# списку — мгновенно. Данные внизу меняются медленно (оплаты, сроки), пять
# минут отставания для этого отчёта не важны; ?refresh=1 сбрасывает кэш.
EXPIRY_CACHE_TTL = 300
_EXPIRY_CACHE = {"at": 0.0, "periods": None, "trial_days": None, "payments": None}
_EXPIRY_CACHE_LOCK = threading.Lock()


def _expiry_cache_clear():
    with _EXPIRY_CACHE_LOCK:
        _EXPIRY_CACHE.update(at=0.0, periods=None, trial_days=None, payments=None)
    _lifecycle_cache_clear()


def _expiry_periods_cached(db_session, refresh=False):
    now = _monotonic()
    with _EXPIRY_CACHE_LOCK:
        if (
            not refresh
            and _EXPIRY_CACHE["periods"] is not None
            and now - _EXPIRY_CACHE["at"] < EXPIRY_CACHE_TTL
        ):
            return _EXPIRY_CACHE["periods"], _EXPIRY_CACHE["trial_days"]
    started = _monotonic()
    trial_days = _expiry_load_trial_days(db_session)
    payments = _expiry_load_payments(db_session)
    periods = _expiry_periods(
        payments,
        _expiry_load_expires(db_session),
        _expiry_load_trial_starts(db_session),
        trial_days,
        _expiry_load_autopay(db_session),
    )
    logging.info(
        "acquisition expirations: %s periods rebuilt in %.2fs",
        len(periods),
        _monotonic() - started,
    )
    with _EXPIRY_CACHE_LOCK:
        _EXPIRY_CACHE.update(
            at=_monotonic(), periods=periods, trial_days=trial_days, payments=payments
        )
    return periods, trial_days


def _expiry_payments_cached(db_session, refresh=False):
    """Оплаты той же сборки, что и периоды (с суммами)."""
    _expiry_periods_cached(db_session, refresh=refresh)
    with _EXPIRY_CACHE_LOCK:
        return _EXPIRY_CACHE["payments"] or []


# --- Жизненный цикл клиента: сводка «почему выручка такая» и «Путь когорты» ---
#
# Единое определение продления на всю админку: период подписки продлён, если
# следующая оплата пришла не позже EXPIRY_DEFAULT_WINDOW дней после его конца
# (досрочная — тоже). Пробные периоды в удержание не входят, они — конверсия.
_LIFECYCLE_CACHE = {"at": 0.0, "signups": None, "connected": None}


def _lifecycle_cache_clear():
    with _EXPIRY_CACHE_LOCK:
        _LIFECYCLE_CACHE.update(at=0.0, signups=None, connected=None)


def _lifecycle_load_signups(db_session):
    """user_id -> момент создания подписки (первое subscription_created)."""
    return {
        int(r["user_id"]): r["started_at"]
        for r in _acq_rows(
            db_session,
            """
            SELECT user_id, min(timestamp) AS started_at FROM event_logs
            WHERE event_type = 'subscription_created' GROUP BY user_id
            """,
        )
    }


def _lifecycle_load_connected(db_session):
    """Кто хоть раз подключился (первый трафик — порог 0, как в воронке)."""
    return {
        int(r["user_id"])
        for r in _acq_rows(
            db_session,
            """
            SELECT DISTINCT user_id FROM event_logs
            WHERE event_type = 'traffic_threshold_reached'
              AND event_payload->>'threshold' = '0'
            """,
        )
    }


def _lifecycle_cached(db_session, refresh=False):
    now = _monotonic()
    with _EXPIRY_CACHE_LOCK:
        if (
            not refresh
            and _LIFECYCLE_CACHE["signups"] is not None
            and now - _LIFECYCLE_CACHE["at"] < EXPIRY_CACHE_TTL
        ):
            return _LIFECYCLE_CACHE["signups"], _LIFECYCLE_CACHE["connected"]
    signups = _lifecycle_load_signups(db_session)
    connected = _lifecycle_load_connected(db_session)
    with _EXPIRY_CACHE_LOCK:
        _LIFECYCLE_CACHE.update(at=_monotonic(), signups=signups, connected=connected)
    return signups, connected


def _msk_week(value_utc):
    day = (value_utc + ADMIN_TZ_OFFSET).date()
    return day - timedelta(days=day.weekday())


def _msk_month(value_utc):
    return (value_utc + ADMIN_TZ_OFFSET).date().replace(day=1)


def _paid_periods_by_user(periods, window_days, now):
    """user_id -> [(end, tariff, renewed, window_closed, next_tariff)] по
    возрастанию конца, только платные периоды."""
    window = timedelta(days=window_days)
    result = {}
    for user_id, end, tariff, next_paid, _autopay, next_tariff in periods:
        if tariff == EXPIRY_TRIAL_KEY:
            continue
        renewed = next_paid is not None and next_paid <= end + window
        closed = end + window <= now
        result.setdefault(user_id, []).append((end, tariff, renewed, closed, next_tariff))
    for items in result.values():
        items.sort(key=lambda item: item[0])
    return result


def _acq_summary_weeks(payments, periods, weeks, window_days, now, today):
    """Недельная сводка: выручка новые/повторные, покупатели, чек, доля
    продлений (закрытое окно), активная платная база на конец недели."""
    this_week = today - timedelta(days=today.weekday())
    keys = [this_week - timedelta(days=7 * i) for i in range(weeks - 1, -1, -1)]
    index = {key: i for i, key in enumerate(keys)}
    rows = [
        {
            "week": key.isoformat(),
            "label": key.strftime("%d.%m"),
            "is_current": key == this_week,
            "revenue_new": 0.0,
            "revenue_repeat": 0.0,
            "new_payers": 0,
            "payments": 0,
            "ending_closed": 0,
            "renewed_closed": 0,
            "base": 0,
        }
        for key in keys
    ]
    first_pay = {}
    for row in payments:
        user_id, paid_at = row[0], row[1]
        if user_id not in first_pay or paid_at < first_pay[user_id]:
            first_pay[user_id] = paid_at
    for row in payments:
        user_id, paid_at, _tariff, amount = row[0], row[1], row[2], (row[3] if len(row) > 3 else 0)
        position = index.get(_msk_week(paid_at))
        if position is None:
            continue
        bucket = rows[position]
        bucket["payments"] += 1
        if paid_at == first_pay[user_id]:
            bucket["revenue_new"] += amount
            bucket["new_payers"] += 1
        else:
            bucket["revenue_repeat"] += amount
    window = timedelta(days=window_days)
    week_ends_utc = [
        datetime.combine(key + timedelta(days=7), time.min) - ADMIN_TZ_OFFSET for key in keys
    ]
    for _user_id, end, tariff, next_paid, _autopay, _next_tariff in periods:
        if tariff == EXPIRY_TRIAL_KEY:
            continue
        position = index.get(_msk_week(end))
        if position is not None and end + window <= now:
            rows[position]["ending_closed"] += 1
            if next_paid is not None and next_paid <= end + window:
                rows[position]["renewed_closed"] += 1
        # Активная платная база: период покрывает конец недели. Начало
        # периода = конец − длительность тарифа (стакование делает это
        # точным; бонусы последнего периода дают небольшую погрешность).
        start = end - timedelta(days=EXPIRY_TARIFF_DAYS.get(tariff, EXPIRY_OTHER_DAYS))
        for position, week_end in enumerate(week_ends_utc):
            if start <= week_end < end:
                rows[position]["base"] += 1
    for bucket in rows:
        bucket["revenue"] = round(bucket["revenue_new"] + bucket["revenue_repeat"])
        bucket["revenue_new"] = round(bucket["revenue_new"])
        bucket["revenue_repeat"] = round(bucket["revenue_repeat"])
        bucket["avg_check"] = (
            round(bucket["revenue"] / bucket["payments"]) if bucket["payments"] else None
        )
        bucket["renewal_pct"] = (
            round(100.0 * bucket["renewed_closed"] / bucket["ending_closed"], 1)
            if bucket["ending_closed"]
            else None
        )
    return rows


def _acq_summary(db_session, weeks=None, refresh=False):
    try:
        weeks = int(weeks)
    except (TypeError, ValueError):
        weeks = 12
    weeks = max(4, min(weeks, 52))
    periods, _trial_days = _expiry_periods_cached(db_session, refresh=refresh)
    payments = _expiry_payments_cached(db_session)
    rows = _acq_summary_weeks(
        payments, periods, weeks, EXPIRY_DEFAULT_WINDOW, datetime.utcnow(), admin_msk_today()
    )
    return {"weeks": rows, "window_days": EXPIRY_DEFAULT_WINDOW, "cache_ttl": EXPIRY_CACHE_TTL}


REVENUE_MAX_RANGE_DAYS = 366


def _revenue_empty_day(key):
    return {
        "day": key.isoformat(),
        "label": key.strftime("%d.%m"),
        "weekday": key.weekday(),
        "revenue": 0.0,
        "payments": 0,
        "new_rub": 0.0,
        "new_payers": 0,
        "repeat_rub": 0.0,
        "repeat_payers": 0,
        "autopay_rub": 0.0,
        "autopay_count": 0,
        "manual_rub": 0.0,
        "manual_count": 0,
        "yk_rub": 0.0,
        "wata_rub": 0.0,
        "by_tariff": {},
    }


def _acq_revenue_days_rows(payments, start, end):
    """Выручка по дням МСК с разбивкой: новые/повторные, автоплатёж/вручную,
    провайдер, тарифы. Оплата «новая» = первая успешная оплата пользователя."""
    keys = []
    cursor = start
    while cursor <= end:
        keys.append(cursor)
        cursor += timedelta(days=1)
    index = {key: i for i, key in enumerate(keys)}
    rows = [_revenue_empty_day(key) for key in keys]
    first_pay = {}
    for row in payments:
        user_id, paid_at = row[0], row[1]
        if user_id not in first_pay or paid_at < first_pay[user_id]:
            first_pay[user_id] = paid_at
    for row in payments:
        user_id, paid_at, tariff = row[0], row[1], row[2]
        amount = row[3] if len(row) > 3 else 0.0
        provider = row[4] if len(row) > 4 else "yk"
        autopay = bool(row[5]) if len(row) > 5 else False
        position = index.get((paid_at + ADMIN_TZ_OFFSET).date())
        if position is None:
            continue
        day = rows[position]
        day["revenue"] += amount
        day["payments"] += 1
        if paid_at == first_pay[user_id]:
            day["new_rub"] += amount
            day["new_payers"] += 1
        else:
            day["repeat_rub"] += amount
            day["repeat_payers"] += 1
        if autopay:
            day["autopay_rub"] += amount
            day["autopay_count"] += 1
        else:
            day["manual_rub"] += amount
            day["manual_count"] += 1
        if provider == "wata":
            day["wata_rub"] += amount
        else:
            day["yk_rub"] += amount
        key = _expiry_tariff_key(tariff)
        slot = day["by_tariff"].setdefault(key, {"count": 0, "rub": 0.0})
        slot["count"] += 1
        slot["rub"] += amount
    for day in rows:
        for name in ("revenue", "new_rub", "repeat_rub", "autopay_rub", "manual_rub", "yk_rub", "wata_rub"):
            day[name] = round(day[name])
        day["avg_check"] = round(day["revenue"] / day["payments"]) if day["payments"] else None
        for slot in day["by_tariff"].values():
            slot["rub"] = round(slot["rub"])
    return rows


def _acq_revenue_days(db_session, start=None, end=None, refresh=False):
    today = admin_msk_today()
    end = date.fromisoformat(end) if end else today
    start = date.fromisoformat(start) if start else end - timedelta(days=44)
    if end < start or (end - start).days > REVENUE_MAX_RANGE_DAYS:
        raise ValueError("bad range")
    payments = _expiry_payments_cached(db_session, refresh=refresh)
    rows = _acq_revenue_days_rows(payments, start, end)
    seen = set()
    for row in rows:
        seen.update(row["by_tariff"])
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "today": today.isoformat(),
        "tariffs": [
            {"key": key, "label": _expiry_tariff_label(key)}
            for key in EXPIRY_TARIFF_ORDER
            if key in seen
        ],
        "days": rows,
        "cache_ttl": EXPIRY_CACHE_TTL,
    }


COHORT_PATH_GROUPS = ("week", "month")
COHORT_PATH_RENEWALS = 3
# Через сколько дней после конца когорты конверсия в покупку считается
# дозревшей: почти все первые оплаты происходят в первые две недели.
COHORT_PATH_BUY_MATURITY_DAYS = 14


def _acq_cohort_path_rows(
    signups, connected, payments, periods, group, count, window_days, now, today
):
    bucket_of = _msk_month if group == "month" else _msk_week
    if group == "month":
        first = today.replace(day=1)
        keys = []
        cursor = first
        for _ in range(count):
            keys.append(cursor)
            cursor = (cursor - timedelta(days=1)).replace(day=1)
        keys.reverse()
    else:
        this_week = today - timedelta(days=today.weekday())
        keys = [this_week - timedelta(days=7 * i) for i in range(count - 1, -1, -1)]
    index = {key: i for i, key in enumerate(keys)}

    def cohort_end(key):
        if group == "month":
            nxt = (key.replace(day=28) + timedelta(days=4)).replace(day=1)
            return nxt - timedelta(days=1)
        return key + timedelta(days=6)

    pays_by_user = {}
    for row in payments:
        pays_by_user.setdefault(row[0], []).append(row)
    paid_periods = _paid_periods_by_user(periods, window_days, now)

    rows = []
    for key in keys:
        rows.append(
            {
                "cohort": key.isoformat(),
                "label": key.strftime("%d.%m") if group == "week" else key.strftime("%m.%Y"),
                "subs": 0,
                "connected": 0,
                "buyers": 0,
                "buy_mature": cohort_end(key) + timedelta(days=COHORT_PATH_BUY_MATURITY_DAYS) < today,
                "renewals": [
                    {"eligible": 0, "matured": 0, "renewed": 0}
                    for _ in range(COHORT_PATH_RENEWALS)
                ],
                "revenue": 0.0,
                "first_tariffs": {},
                "transitions": {},
            }
        )
    for user_id, started in signups.items():
        position = index.get(bucket_of(started))
        if position is None:
            continue
        row = rows[position]
        row["subs"] += 1
        if user_id in connected:
            row["connected"] += 1
        pays = pays_by_user.get(user_id)
        if not pays:
            continue
        row["buyers"] += 1
        row["revenue"] += sum((p[3] if len(p) > 3 else 0) for p in pays)
        first_key = _expiry_tariff_key(pays[0][2])
        row["first_tariffs"][first_key] = row["first_tariffs"].get(first_key, 0) + 1
        for k, (end, tariff, renewed, closed, next_tariff) in enumerate(
            paid_periods.get(user_id, [])[:COHORT_PATH_RENEWALS]
        ):
            slot = row["renewals"][k]
            slot["eligible"] += 1
            if closed:
                slot["matured"] += 1
                if renewed:
                    slot["renewed"] += 1
                    dest = row["transitions"].setdefault(tariff, {})
                    dest[next_tariff or EXPIRY_OTHER_KEY] = dest.get(next_tariff or EXPIRY_OTHER_KEY, 0) + 1
    for row in rows:
        subs = row["subs"]
        row["connected_pct"] = round(100.0 * row["connected"] / subs, 1) if subs else None
        row["buyers_pct"] = round(100.0 * row["buyers"] / subs, 1) if subs else None
        for slot in row["renewals"]:
            slot["pct"] = (
                round(100.0 * slot["renewed"] / slot["matured"], 1) if slot["matured"] else None
            )
            slot["mature"] = slot["eligible"] > 0 and slot["matured"] == slot["eligible"]
        row["revenue"] = round(row["revenue"])
        row["revenue_per_sub"] = round(row["revenue"] / subs) if subs else None
        row["revenue_per_buyer"] = (
            round(row["revenue"] / row["buyers"]) if row["buyers"] else None
        )
    return rows


def _acq_cohort_path(db_session, group="week", count=None, refresh=False):
    group = "month" if group == "month" else "week"
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 16 if group == "week" else 12
    count = max(4, min(count, 52 if group == "week" else 24))
    periods, _trial_days = _expiry_periods_cached(db_session, refresh=refresh)
    payments = _expiry_payments_cached(db_session)
    signups, connected = _lifecycle_cached(db_session, refresh=refresh)
    rows = _acq_cohort_path_rows(
        signups,
        connected,
        payments,
        periods,
        group,
        count,
        EXPIRY_DEFAULT_WINDOW,
        datetime.utcnow(),
        admin_msk_today(),
    )
    seen = set()
    for row in rows:
        seen.update(row["first_tariffs"])
        for src, dest in row["transitions"].items():
            seen.add(src)
            seen.update(dest)
    return {
        "group": group,
        "count": count,
        "window_days": EXPIRY_DEFAULT_WINDOW,
        "buy_maturity_days": COHORT_PATH_BUY_MATURITY_DAYS,
        "renewal_steps": COHORT_PATH_RENEWALS,
        "tariffs": [
            {"key": key, "label": _expiry_tariff_label(key)}
            for key in EXPIRY_TARIFF_ORDER
            if key in seen
        ],
        "cohorts": rows,
        "cache_ttl": EXPIRY_CACHE_TTL,
    }


def _acq_expirations(
    db_session, start=None, end=None, group="day", window=None, refresh=False
):
    today = admin_msk_today()
    start = date.fromisoformat(start) if start else today - timedelta(days=30)
    end = date.fromisoformat(end) if end else today + timedelta(days=30)
    if end < start or (end - start).days > EXPIRY_MAX_RANGE_DAYS:
        raise ValueError("bad range")
    group = "week" if group == "week" else "day"
    try:
        window_days = int(window)
    except (TypeError, ValueError):
        window_days = EXPIRY_DEFAULT_WINDOW
    if window_days not in EXPIRY_WINDOWS:
        window_days = EXPIRY_DEFAULT_WINDOW

    periods, trial_days = _expiry_periods_cached(db_session, refresh=refresh)
    tariffs, rows, totals = _expiry_aggregate(
        periods, start, end, group, window_days, datetime.utcnow(), today
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "today": today.isoformat(),
        "group": group,
        "window_days": window_days,
        "trial_days": trial_days,
        "cache_ttl": EXPIRY_CACHE_TTL,
        "tariffs": [{"key": key, "label": _expiry_tariff_label(key)} for key in tariffs],
        "buckets": rows,
        "totals": totals,
    }


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
    weekly_pays = {r["week"].isoformat(): r for r in pays}
    out = []
    # Weeks with spend and no sales are the rows that most need attention.
    # Build the union of all series instead of using payments as the left side.
    week_keys = set(weekly_spend) | set(weekly_pays) | set(weekly_subs) | set(weekly_conns)
    for wk in sorted(week_keys):
        pay_row = weekly_pays.get(wk, {})
        spend = weekly_spend.get(wk, 0.0)
        new_payers = int(pay_row.get("new_payers", 0) or 0)
        new_rub = float(pay_row.get("new_rub", 0) or 0)
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


def _acq_ads_summary(db_session, start=None, end=None):
    """Сводка рекламы за произвольный диапазон дат (МСК, включительно).

    Расход по аккаунтам и суммарно, показы/клики, созданные подписки,
    подключения (первый трафик), продажи (первая оплата) и выручка новых —
    всё за один и тот же диапазон, чтобы CPC и цены подписки/подключения/
    продажи были посчитаны от одних и тех же сумм, а не усреднены по дням.
    Без дат — последние 30 дней.
    """
    today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    end_day = date.fromisoformat(end) if end else today
    start_day = date.fromisoformat(start) if start else end_day - timedelta(days=29)
    if start_day > end_day:
        start_day, end_day = end_day, start_day
    if (end_day - start_day).days > 730:
        raise ValueError("range too long")
    params = {"start": start_day, "end": end_day}
    try:
        account_rows = _acq_rows(
            db_session,
            """
            SELECT account, COALESCE(sum(amount_rub), 0) AS spend,
                   sum(impressions) AS impressions, sum(clicks) AS clicks
            FROM ad_spends
            WHERE day BETWEEN :start AND :end
            GROUP BY 1
            ORDER BY 2 DESC, 1
            """,
            **params,
        )
    except Exception:
        db_session.rollback()
        return {"needs_migration": True, "accounts": []}

    subs_rows = _acq_rows(
        db_session,
        """
        SELECT count(*) AS subs
        FROM event_logs
        WHERE event_type = 'subscription_created'
          AND ((timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date BETWEEN :start AND :end
        """,
        **params,
    )
    conn_rows = _acq_rows(
        db_session,
        """
        SELECT count(DISTINCT user_id) AS conns
        FROM event_logs
        WHERE event_type = 'traffic_threshold_reached'
          AND (event_payload->>'threshold')::int = 0
          AND ((timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date BETWEEN :start AND :end
        """,
        **params,
    )
    sale_rows = _acq_rows(
        db_session,
        f"""
        WITH {ACQ_PAYS_CTE}
        SELECT count(*) AS sales, COALESCE(sum(amount), 0) AS new_rub
        FROM pays JOIN first_pay USING (user_id)
        WHERE paid_at = first_at
          AND {ACQ_MSK_DAY} BETWEEN :start AND :end
        """,
        **params,
    )

    def ratio(spend, count):
        return round(spend / count, 2) if spend and count else None

    total_spend = 0.0
    has_traffic = False
    impressions = 0
    clicks = 0
    accounts = []
    for r in account_rows:
        spend = float(r["spend"])
        total_spend += spend
        acc_traffic = r["impressions"] is not None or r["clicks"] is not None
        acc_impr = int(r["impressions"] or 0) if acc_traffic else None
        acc_clicks = int(r["clicks"] or 0) if acc_traffic else None
        if acc_traffic:
            has_traffic = True
            impressions += acc_impr
            clicks += acc_clicks
        accounts.append({
            "account": r["account"] or "default",
            "spend": round(spend, 2),
            "impressions": acc_impr,
            "clicks": acc_clicks,
            "cpc": ratio(spend, acc_clicks) if acc_traffic else None,
        })
    for a in accounts:
        a["share"] = round(100.0 * a["spend"] / total_spend, 1) if total_spend else None
    total_spend = round(total_spend, 2)
    subs = int(subs_rows[0]["subs"]) if subs_rows else 0
    conns = int(conn_rows[0]["conns"]) if conn_rows else 0
    sales = int(sale_rows[0]["sales"]) if sale_rows else 0
    new_rub = round(float(sale_rows[0]["new_rub"]), 2) if sale_rows else 0.0
    return {
        "needs_migration": False,
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "days": (end_day - start_day).days + 1,
        "spend": total_spend,
        "impressions": impressions if has_traffic else None,
        "clicks": clicks if has_traffic else None,
        "cpc": ratio(total_spend, clicks) if has_traffic else None,
        "subs": subs,
        "cost_per_sub": ratio(total_spend, subs),
        "conns": conns,
        "cost_per_conn": ratio(total_spend, conns),
        "sales": sales,
        "cost_per_sale": ratio(total_spend, sales),
        "new_rub": new_rub,
        "drr": round(100.0 * total_spend / new_rub, 1) if total_spend and new_rub else None,
        "romi": round(new_rub / total_spend, 2) if total_spend else None,
        "accounts": accounts,
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

    today = today or admin_msk_today()
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
    "expirations": lambda s, req: _acq_expirations(
        s,
        req.GET.get("start") or None,
        req.GET.get("end") or None,
        req.GET.get("group") or "day",
        req.GET.get("window"),
        req.GET.get("refresh") == "1",
    ),
    "summary": lambda s, req: _acq_summary(
        s, req.GET.get("weeks"), req.GET.get("refresh") == "1"
    ),
    "revenue_days": lambda s, req: _acq_revenue_days(
        s, req.GET.get("start") or None, req.GET.get("end") or None,
        req.GET.get("refresh") == "1",
    ),
    # Плитки «сегодня/вчера/7/30 дней» — те же дневные строки за 61 день,
    # независимо от диапазона графика.
    "revenue_kpis": lambda s, req: _acq_revenue_days(
        s, req.GET.get("start") or None, req.GET.get("end") or None,
        req.GET.get("refresh") == "1",
    ),
    "cohort_path": lambda s, req: _acq_cohort_path(
        s, req.GET.get("group") or "week", req.GET.get("count"),
        req.GET.get("refresh") == "1",
    ),
    "funnel": lambda s, req: _acq_funnel(s, int(req.GET.get("weeks", 12))),
    "ads": lambda s, req: _acq_ads(s, int(req.GET.get("weeks", 12))),
    "ads_daily": lambda s, req: _acq_ads_daily(
        s, int(req.GET.get("days", 92)),
        "month" if req.GET.get("group") == "month" else "day",
    ),
    "ads_summary": lambda s, req: _acq_ads_summary(
        s, req.GET.get("start") or None, req.GET.get("end") or None,
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


def support_admin_api_traffic_sources(request):
    """Подписи меток трафика (traffic_sources): GET — список, POST — upsert
    по номеру метки (пустое имя без бюджета = удалить подпись)."""
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response
    db_session = session_factory()
    try:
        if request.method == "GET":
            notes = admin_traffic_source_notes(db_session)
            return JsonResponse(
                {
                    "status": "ok",
                    "result": [
                        {"id": int(key), "name": note["name"], "budget": note["budget"]}
                        for key, note in sorted(notes.items(), key=lambda item: int(item[0]))
                    ],
                }
            )
        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)
        try:
            source_id = int(request.POST.get("id") or "")
        except ValueError:
            source_id = 0
        if source_id <= 0:
            return JsonResponse(
                {"status": "error", "message": "Укажите номер метки (число из TS_N)"},
                status=400,
            )
        name = (request.POST.get("name") or "").strip()[:TRAFFIC_SOURCE_NAME_MAX]
        budget_raw = (request.POST.get("budget") or "").strip().replace(" ", "")
        budget = None
        if budget_raw:
            try:
                budget = int(budget_raw)
            except ValueError:
                budget = -1
            if budget < 0:
                return JsonResponse(
                    {"status": "error", "message": "Бюджет — целое число рублей"},
                    status=400,
                )
        row = db_session.get(TrafficSource, source_id)
        if not name and budget is None:
            # Пустая подпись — убираем строку целиком, а не храним пустоту.
            if row is not None:
                db_session.delete(row)
                admin_audit_write(
                    db_session, request, "traffic_source_note_clear", target=str(source_id)
                )
                db_session.commit()
            return JsonResponse(
                {"status": "ok", "result": {"id": source_id, "name": "", "budget": None}}
            )
        if row is None:
            row = TrafficSource(id=source_id, name=name, budget=budget)
            db_session.add(row)
        else:
            row.name = name
            row.budget = budget
        admin_audit_write(
            db_session,
            request,
            "traffic_source_note",
            target=str(source_id),
            name=name,
            budget=budget,
        )
        db_session.commit()
        return JsonResponse(
            {"status": "ok", "result": {"id": source_id, "name": name, "budget": budget}}
        )
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
    value = client_ip(request)
    return value[:64] if value else None


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
            # Лимит трафика и стратегию сброса не трогаем (антиабьюз): поля
            # не заданы → RWMS оставляет их в панели как есть.
            response = rwms_client.update_user(
                proto.UpdateUserRequest(
                    uuid=rwms_user.uuid,
                    expire_at=db_expire.replace(tzinfo=timezone.utc),
                    status=proto.UserStatus.ACTIVE
                    if db_expire > datetime.utcnow()
                    else rwms_user.status,
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


# Потолок одного count-запроса сегмента: медленный или сломанный сегмент не
# должен ронять весь пикер получателей (раньше любая ошибка = 500 и «Сегменты
# временно недоступны» на всю рассылку).
SEGMENT_COUNT_TIMEOUT_MS = 5000
# Потолок быстрого пути — все сегменты одним проходом по users (hash-join'ы
# вместо 18 отдельных запросов с коррелированными EXISTS).
SEGMENTS_FAST_COUNT_TIMEOUT_MS = 15000


def support_admin_api_segments(request):
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        counts = {}
        try:
            db_session.execute(
                sa_text(
                    f"SET LOCAL statement_timeout = {SEGMENTS_FAST_COUNT_TIMEOUT_MS}"
                )
            )
            row = db_session.execute(sa_text(segments_counts_sql())).one()
            counts = {
                key: int(value)
                for key, value in zip(ADMIN_SEGMENTS, row)
            }
        except Exception:
            logging.exception("segments fast count failed, per-segment fallback")
            db_session.rollback()

        result = []
        for key, (label, _condition) in ADMIN_SEGMENTS.items():
            if key in counts:
                count = counts[key]
            else:
                try:
                    db_session.execute(
                        sa_text(
                            f"SET LOCAL statement_timeout = {SEGMENT_COUNT_TIMEOUT_MS}"
                        )
                    )
                    count = int(
                        db_session.execute(sa_text(segment_count_sql(key))).scalar()
                        or 0
                    )
                except Exception:
                    logging.exception("segment count failed: %s", key)
                    db_session.rollback()
                    count = None
            result.append({"key": key, "label": label, "count": count})
        return JsonResponse({"status": "ok", "result": result})
    finally:
        db_session.close()


BROADCAST_TEXT_MAX_LEN = 3500
# Telegram: подпись к фото ограничена 1024 символами (у текста — 4096).
BROADCAST_CAPTION_MAX_LEN = 1024
# Telegram позволяет до ~100 кнопок на клавиатуру; 50 — запас с учётом тарифов.
BROADCAST_MAX_BUTTONS = 50
BROADCAST_BUTTON_STYLES = {"success", "danger", "primary"}
BROADCAST_MEDIA_MAX_BYTES = 3 * 1024 * 1024
BROADCAST_MEDIA_TYPES = {"image/jpeg": "photo", "image/png": "photo"}
BROADCAST_TARIFF_IDS = {"oneday", "threedays", "month", "threemonths", "sixmonths", "year"}
# Получатели промокода из кнопки claim_promo: all — всем в сегменте (повторно и
# уже активировавшим), exclude_activated — без активировавших (promo_code_uses).
BROADCAST_PROMO_RECIPIENTS = {"all", "exclude_activated"}
# Отложенные рассылки: время из админки приходит в МСК (datetime-local без
# зоны), хранится naive UTC (как всё в БД). Ближе минуты планировать нельзя —
# это уже «сразу»; дальше 90 дней — почти наверняка опечатка в дате.
BROADCAST_SCHEDULE_FORMAT = "%Y-%m-%dT%H:%M"
BROADCAST_SCHEDULE_MIN_AHEAD = timedelta(minutes=1)
BROADCAST_SCHEDULE_MAX_AHEAD = timedelta(days=90)


def admin_broadcast_parse_schedule(raw, now=None):
    """'2026-09-20T18:00' (МСК) -> naive UTC datetime; пусто -> None.

    ValueError с текстом для админа: неверный формат, прошлое, слишком далеко.
    """
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        local = datetime.strptime(value, BROADCAST_SCHEDULE_FORMAT)
    except ValueError:
        raise ValueError("Укажите дату и время отправки (МСК)")
    scheduled_at = local - ADMIN_TZ_OFFSET
    now = now or datetime.utcnow()
    if scheduled_at < now + BROADCAST_SCHEDULE_MIN_AHEAD:
        raise ValueError("Время отправки уже прошло — выберите момент в будущем")
    if scheduled_at > now + BROADCAST_SCHEDULE_MAX_AHEAD:
        raise ValueError("Рассылку можно запланировать не дальше чем на 90 дней")
    return scheduled_at


def admin_broadcast_parse_buttons(db_session, raw_buttons):
    """Валидация типизированных кнопок рассылки. Бросает ValueError с текстом
    для админа. Типы: url / claim_promo (распродажа) / tariffs (как в /sendmsg)."""
    try:
        buttons = json.loads(raw_buttons or "[]")
    except json.JSONDecodeError:
        raise ValueError("Кнопки: некорректный JSON")
    if not isinstance(buttons, list):
        raise ValueError("Кнопки: некорректный JSON")
    clean = []
    plain_count = 0
    for button in buttons:
        if not isinstance(button, dict):
            raise ValueError("Кнопки: некорректный JSON")
        button_type = str(button.get("type") or "url")
        style = button.get("style")
        style = style if style in BROADCAST_BUTTON_STYLES else None
        if button_type == "url":
            text_label = str(button.get("text") or "").strip()[:64]
            url = str(button.get("url") or "").strip()[:512]
            if not text_label or not url.startswith("https://"):
                raise ValueError("У кнопки-ссылки нужны текст и https-ссылка")
            clean.append({"type": "url", "text": text_label, "url": url, "style": style})
            plain_count += 1
        elif button_type == "claim_promo":
            text_label = str(button.get("text") or "").strip()[:64]
            try:
                promo_id = int(button.get("promo_id") or 0)
            except (TypeError, ValueError):
                promo_id = 0
            promo = db_session.get(PromoCode, promo_id) if promo_id else None
            if not text_label or promo is None:
                raise ValueError("У кнопки промокода нужны текст и существующий промокод")
            if not promo.is_active:
                raise ValueError(f"Промокод {promo.code} выключен")
            if promo.valid_until and promo.valid_until <= datetime.utcnow():
                raise ValueError(f"Срок промокода {promo.code} истёк")
            if any(entry.get("type") == "claim_promo" for entry in clean):
                raise ValueError("Кнопка промокода может быть только одна")
            clean.append(
                {
                    "type": "claim_promo",
                    "text": text_label,
                    "promo_id": promo.id,
                    "code": promo.code,
                    "style": style,
                }
            )
            plain_count += 1
        elif button_type == "tariffs":
            # Кнопки тарифов всегда идут по актуальным ценам. Промо-цена кнопки
            # (легаси-ключ price_overrides) убрана: цена ехала в callback_data,
            # бот принимал её на веру, а payment период брал из metadata и сумму
            # не сверял — «год за рубль». Скидки делаются только промокодами
            # (кнопка claim_promo: max_uses, срок, однократность на пользователя).
            # Присланный price_overrides молча игнорируем — так старые рассылки
            # с этим ключом в buttons открываются и пересохраняются без ошибок.
            tariff_ids = [
                str(item).strip().lower()
                for item in (button.get("tariff_ids") or [])
                if str(item).strip()
            ]
            unknown = [item for item in tariff_ids if item not in BROADCAST_TARIFF_IDS]
            if unknown:
                raise ValueError(f"Неизвестный тариф: {', '.join(unknown)}")
            if any(entry.get("type") == "tariffs" for entry in clean):
                raise ValueError("Кнопки тарифов можно добавить один раз")
            clean.append({"type": "tariffs", "tariff_ids": tariff_ids or None})
        else:
            raise ValueError("Неизвестный тип кнопки")
    if plain_count > BROADCAST_MAX_BUTTONS:
        raise ValueError(f"Не больше {BROADCAST_MAX_BUTTONS} кнопок (не считая тарифов)")
    return clean


def admin_broadcast_parse_media(request, text_value):
    """Фото рассылки из multipart-формы; None — без медиа. Бросает ValueError."""
    upload = request.FILES.get("media")
    if upload is None:
        return None, None
    media_type = BROADCAST_MEDIA_TYPES.get((upload.content_type or "").lower())
    if media_type is None:
        raise ValueError("Фото: только JPEG или PNG")
    if upload.size > BROADCAST_MEDIA_MAX_BYTES:
        raise ValueError("Фото: не больше 3 МБ")
    if len(text_value) > BROADCAST_CAPTION_MAX_LEN:
        raise ValueError(
            f"С фото текст ограничен {BROADCAST_CAPTION_MAX_LEN} символами (лимит Telegram)"
        )
    return upload.read(), media_type


def admin_broadcast_funnel(db_session, broadcast):
    """Воронка распродажи для рассылок с кнопкой «Забрать скидку»:
    активации промокода и покупки активировавших после активации."""
    claim = next(
        (
            button
            for button in (broadcast.buttons or [])
            if button.get("type") == "claim_promo"
        ),
        None,
    )
    if claim is None:
        return None
    params = {"promo_id": claim.get("promo_id"), "start": broadcast.created_at}
    claims = (
        db_session.execute(
            sa_text(
                """
                SELECT count(*) FROM promo_code_uses
                WHERE promo_id = :promo_id AND created_at >= :start
                """
            ),
            params,
        ).scalar()
        or 0
    )
    yk_buyers, yk_revenue = db_session.execute(
        sa_text(
            """
            SELECT count(DISTINCT uses.user_id), COALESCE(sum(p.amount), 0)
            FROM promo_code_uses uses
            JOIN yk_payments p ON p.user_id = uses.user_id
             AND p.status = 'succeeded' AND p.created_at > uses.created_at
            WHERE uses.promo_id = :promo_id AND uses.created_at >= :start
            """
        ),
        params,
    ).one()
    wata_buyers, wata_revenue = db_session.execute(
        sa_text(
            """
            SELECT count(DISTINCT uses.user_id), COALESCE(sum(t.amount), 0)
            FROM promo_code_uses uses
            JOIN wata_invoices i ON i.user_id = uses.user_id
            JOIN wata_transactions t ON t.order_id = i.order_id
             AND t.transaction_status = 'Paid' AND t.payment_time > uses.created_at
            WHERE uses.promo_id = :promo_id AND uses.created_at >= :start
            """
        ),
        params,
    ).one()
    return {
        "code": claim.get("code"),
        "claims": int(claims),
        "buyers": int(yk_buyers or 0) + int(wata_buyers or 0),
        "revenue": int(yk_revenue or 0) + int(wata_revenue or 0),
    }


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


def admin_broadcast_count_total(db_session, broadcast):
    """Число получателей сегмента сейчас — с тем же фильтром промокода, что
    у ботов (иначе покрытие и total разойдутся и рассылка не станет done)."""
    exclude_promo_id = getattr(broadcast, "exclude_promo_id", None)
    if exclude_promo_id:
        return int(
            db_session.execute(
                sa_text(segment_count_sql(broadcast.segment, exclude_promo=True)),
                {"exclude_promo_id": exclude_promo_id},
            ).scalar()
            or 0
        )
    return int(
        db_session.execute(sa_text(segment_count_sql(broadcast.segment))).scalar() or 0
    )


def admin_broadcast_exclude_promo_code(broadcast):
    """Код промокода, активировавшие который исключены из рассылки
    (exclude_promo_id всегда ссылается на промокод кнопки claim_promo,
    поэтому код берём из кнопок без лишнего запроса)."""
    promo_id = getattr(broadcast, "exclude_promo_id", None)
    if not promo_id:
        return None
    for button in broadcast.buttons or []:
        if button.get("type") == "claim_promo" and button.get("promo_id") == promo_id:
            return button.get("code")
    return None


def admin_broadcast_payload(db_session, broadcast):
    covered, sent = admin_broadcast_progress(db_session, broadcast)
    total = broadcast.total or 0
    # Рассылка исчерпана: каждый получатель сегмента имеет хотя бы одну
    # терминальную запись доставки. Помечаем done лениво при опросе.
    if broadcast.status == "running" and total and covered >= total:
        broadcast.status = "done"
        broadcast.finished_at = datetime.utcnow()
        db_session.commit()
    scheduled_at = getattr(broadcast, "scheduled_at", None)
    return {
        "id": broadcast.id,
        "title": broadcast.title,
        "segment": broadcast.segment,
        "segment_label": ADMIN_SEGMENTS.get(broadcast.segment, (broadcast.segment,))[0],
        "status": broadcast.status,
        # Отложенная: когда стартует (МСК, как остальные даты админки) и
        # значение для datetime-local при переносе.
        "scheduled_at": admin_date_label(scheduled_at) if scheduled_at else None,
        "scheduled_at_input": (
            (scheduled_at + ADMIN_TZ_OFFSET).strftime(BROADCAST_SCHEDULE_FORMAT)
            if scheduled_at
            else None
        ),
        "text": broadcast.text,
        "buttons": broadcast.buttons or [],
        "has_media": bool(broadcast.media_type),
        "is_test": bool(broadcast.test_telegram_id),
        "disable_link_preview": bool(broadcast.disable_link_preview),
        "exclude_promo_id": broadcast.exclude_promo_id,
        "exclude_promo_code": admin_broadcast_exclude_promo_code(broadcast),
        "archived": bool(broadcast.archived_at),
        "total": total,
        "covered": covered,
        "sent": sent,
        "progress_pct": round(100.0 * covered / total, 1) if total else 0,
        "funnel": admin_broadcast_funnel(db_session, broadcast),
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
            # По умолчанию — только неархивные; ?archived=1 показывает архив
            # (тестовые прогоны и старые кампании, убранные из основного списка).
            show_archived = request.GET.get("archived") == "1"
            broadcasts = (
                db_session.query(Broadcast)
                .filter(
                    Broadcast.archived_at.isnot(None)
                    if show_archived
                    else Broadcast.archived_at.is_(None)
                )
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
            # stop работает и для отложенной: она отменяется, не стартовав.
            if broadcast.status in ("running", "scheduled"):
                was_scheduled = broadcast.status == "scheduled"
                broadcast.status = "stopped"
                broadcast.finished_at = datetime.utcnow()
                admin_audit_write(
                    db_session,
                    request,
                    "broadcast_cancel" if was_scheduled else "broadcast_stop",
                    target=str(broadcast.id),
                )
                db_session.commit()
            return JsonResponse(
                {"status": "ok", "result": admin_broadcast_payload(db_session, broadcast)}
            )

        if action in ("reschedule", "send_now"):
            # Только для ещё не стартовавшей: running/stopped/done перенести
            # нельзя — боты уже прошли (или проходят) по получателям.
            broadcast = db_session.get(Broadcast, int(request.POST.get("id") or 0))
            if not broadcast:
                return JsonResponse({"status": "not_found"}, status=404)
            if broadcast.status != "scheduled":
                return JsonResponse(
                    {"status": "error", "message": "Рассылка уже не запланирована"},
                    status=400,
                )
            if action == "reschedule":
                try:
                    scheduled_at = admin_broadcast_parse_schedule(
                        request.POST.get("scheduled_at")
                    )
                except ValueError as error:
                    return JsonResponse(
                        {"status": "error", "message": str(error)}, status=400
                    )
                if scheduled_at is None:
                    return JsonResponse(
                        {"status": "error", "message": "Укажите новое время отправки"},
                        status=400,
                    )
                broadcast.scheduled_at = scheduled_at
                admin_audit_write(
                    db_session,
                    request,
                    "broadcast_reschedule",
                    target=str(broadcast.id),
                    scheduled_at=scheduled_at.isoformat(timespec="minutes"),
                )
            else:
                # «Отправить сейчас»: стартуем отсюда же, не дожидаясь бота.
                # total пересчитывается на момент старта — как делает бот.
                broadcast.status = "running"
                if not broadcast.test_telegram_id:
                    broadcast.total = admin_broadcast_count_total(db_session, broadcast)
                admin_audit_write(
                    db_session,
                    request,
                    "broadcast_send_now",
                    target=str(broadcast.id),
                    total=int(broadcast.total or 0),
                )
            db_session.commit()
            return JsonResponse(
                {"status": "ok", "result": admin_broadcast_payload(db_session, broadcast)}
            )

        if action in ("archive", "unarchive"):
            broadcast = db_session.get(Broadcast, int(request.POST.get("id") or 0))
            if not broadcast:
                return JsonResponse({"status": "not_found"}, status=404)
            if action == "archive":
                if broadcast.status in ("running", "scheduled"):
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": "Сначала остановите рассылку",
                        },
                        status=400,
                    )
                broadcast.archived_at = datetime.utcnow()
            else:
                broadcast.archived_at = None
            admin_audit_write(
                db_session,
                request,
                f"broadcast_{action}",
                target=str(broadcast.id),
            )
            db_session.commit()
            return JsonResponse(
                {"status": "ok", "result": admin_broadcast_payload(db_session, broadcast)}
            )

        if action in ("create", "test"):
            is_test = action == "test"
            title = (request.POST.get("title") or "").strip()[:256]
            text_value = (request.POST.get("text") or "").strip()
            segment = (request.POST.get("segment") or "").strip()
            if is_test and not title:
                title = "Тест рассылки"
            if is_test and not segment:
                segment = "all"
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
                clean_buttons = admin_broadcast_parse_buttons(
                    db_session, request.POST.get("buttons")
                )
                media_bytes, media_type = admin_broadcast_parse_media(
                    request, text_value
                )
            except ValueError as error:
                return JsonResponse(
                    {"status": "error", "message": str(error)}, status=400
                )

            # Режим получателей промокода — явный выбор админа в форме:
            # all — слать всем в сегменте (и уже активировавшим, повторно);
            # exclude_activated — исключить тех, у кого есть активация
            # промокода кнопки claim_promo (promo_code_uses). Режим привязан
            # к кнопке — без неё исключать нечего.
            exclude_promo_id = None
            promo_recipients = (request.POST.get("promo_recipients") or "all").strip()
            if promo_recipients not in BROADCAST_PROMO_RECIPIENTS:
                return JsonResponse(
                    {"status": "error", "message": "Неизвестный режим получателей промокода"},
                    status=400,
                )
            if promo_recipients == "exclude_activated":
                promo_button = next(
                    (
                        entry
                        for entry in clean_buttons
                        if entry.get("type") == "claim_promo"
                    ),
                    None,
                )
                if promo_button is None:
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": "Исключение активировавших требует кнопку промокода",
                        },
                        status=400,
                    )
                exclude_promo_id = int(promo_button["promo_id"])

            # Отложенный запуск: время (МСК) из формы; тест всегда сразу —
            # проверять письмо «через неделю» незачем.
            scheduled_at = None
            if not is_test:
                try:
                    scheduled_at = admin_broadcast_parse_schedule(
                        request.POST.get("scheduled_at")
                    )
                except ValueError as error:
                    return JsonResponse(
                        {"status": "error", "message": str(error)}, status=400
                    )

            test_telegram_id = None
            if is_test:
                try:
                    test_telegram_id = int(request.POST.get("telegram_id") or 0)
                except ValueError:
                    test_telegram_id = 0
                if test_telegram_id <= 0:
                    return JsonResponse(
                        {"status": "error", "message": "Укажите Telegram ID для теста"},
                        status=400,
                    )
                known = (
                    db_session.query(User.id)
                    .filter(User.telegram_id == test_telegram_id)
                    .first()
                )
                if known is None:
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": "Этого Telegram ID нет среди пользователей ботов",
                        },
                        status=400,
                    )
                total = 1
            elif exclude_promo_id:
                total = (
                    db_session.execute(
                        sa_text(segment_count_sql(segment, exclude_promo=True)),
                        {"exclude_promo_id": exclude_promo_id},
                    ).scalar()
                    or 0
                )
                if not total:
                    return JsonResponse(
                        {
                            "status": "error",
                            "message": "В сегменте нет получателей без активации этого промокода",
                        },
                        status=400,
                    )
            else:
                total = (
                    db_session.execute(sa_text(segment_count_sql(segment))).scalar() or 0
                )
                if not total:
                    return JsonResponse(
                        {"status": "error", "message": "В сегменте нет получателей"},
                        status=400,
                    )

            broadcast = Broadcast(
                title=title if not is_test else f"[тест] {title}"[:256],
                text=text_value,
                segment=segment,
                status="scheduled" if scheduled_at else "running",
                scheduled_at=scheduled_at,
                buttons=clean_buttons,
                media=media_bytes,
                media_type=media_type,
                test_telegram_id=test_telegram_id,
                # Отключить превью ссылок (как /sendmsg): чекбокс в админке.
                disable_link_preview=request.POST.get("disable_preview") == "1",
                exclude_promo_id=exclude_promo_id,
                total=int(total),
                created_by=str(support_admin_actor(request))[:128],
            )
            db_session.add(broadcast)
            admin_audit_write(
                db_session,
                request,
                "broadcast_test"
                if is_test
                else ("broadcast_schedule" if scheduled_at else "broadcast_create"),
                target=title,
                segment=segment,
                total=int(total),
                exclude_promo_id=exclude_promo_id,
                scheduled_at=(
                    scheduled_at.isoformat(timespec="minutes") if scheduled_at else None
                ),
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
BULK_ACTIONS = {
    "extend_days",
    "block",
    "unblock",
    "referral_block",
    "referral_unblock",
    # Антиабьюз: лимит трафика пробных (по списку пользователей)
    "apply_trial_limit",
    "remove_traffic_limit",
}


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
    # Проверка «есть @» пропускала домен без точки, а панель валидирует email
    # через EmailStr — такой запрос падал целиком (инцидент 2026-09-10).
    # Невалидный адрес не задаём: панель оставит свой.
    user_email = usable_panel_email(
        rwms_user.email, getattr(rwms_user, "username", "?"), "UpdateUser"
    )
    # Лимит трафика и стратегию сброса не трогаем (антиабьюз): поля не заданы
    # → RWMS оставляет их в панели как есть.
    response = rwms_client.update_user(
        proto.UpdateUserRequest(
            uuid=rwms_user.uuid,
            email=user_email,
            telegram_id=rwms_user.telegram_id,
            expire_at=target_expire,
            status=proto.UserStatus.ACTIVE,
            active_internal_squads=[
                squad.uuid for squad in rwms_user.active_internal_squads
            ],
        )
    )
    if response is None:
        raise RuntimeError("RWMS не принял продление")
    return f"продлено до {target_expire + ADMIN_TZ_OFFSET:%Y-%m-%d %H:%M} МСК"


def admin_account_block_payload(db_session, user):
    """Состояние полной блокировки аккаунта (user_blocks) для карточки клиента."""
    block = db_session.get(UserBlock, user.id)
    return {
        "blocked": block is not None,
        "reason": block.reason if block else None,
        "created_at": admin_date_label(block.created_at) if block else None,
    }


def admin_block_account(db_session, request, user, reason):
    """Полная блокировка аккаунта — 1:1 с /block-user в боте: user_blocks
    (UserBlockMiddleware бота отвечает только ACCOUNT_BLOCKED), блок рефералки,
    автоплатёж выключен и сохранённый рекуррент удалён, подписка в RWMS →
    DISABLED. Возвращает (удалено рекуррентов, обновлён ли RWMS)."""
    block = db_session.get(UserBlock, user.id)
    if block is None:
        db_session.add(UserBlock(user_id=user.id, reason=reason))
    else:
        block.reason = reason
    ref_block = db_session.get(ReferralProgramBlock, user.id)
    if ref_block is None:
        db_session.add(ReferralProgramBlock(user_id=user.id, reason=reason))
    else:
        ref_block.reason = reason
    user.autopay_allow = False
    removed_recurrents = (
        db_session.query(YkRecurrentPayment)
        .filter(YkRecurrentPayment.user_id == user.id)
        .delete(synchronize_session=False)
    )
    admin_audit_write(
        db_session,
        request,
        "account_block",
        target=user.username,
        reason=reason,
        removed_recurrents=removed_recurrents,
    )
    db_session.commit()

    rwms_updated = False
    rwms_user = rwms_client.get_user_by_username(user.username)
    if rwms_user is not None:
        response = rwms_client.update_user(
            proto.UpdateUserRequest(
                uuid=rwms_user.uuid, status=proto.UserStatus.DISABLED
            )
        )
        rwms_updated = response is not None
    logging.info(
        "account blocked from admin panel: username=%s reason=%r "
        "removed_recurrents=%s rwms_updated=%s",
        user.username,
        reason,
        removed_recurrents,
        rwms_updated,
    )
    return removed_recurrents, rwms_updated


def admin_unblock_account(db_session, request, user):
    """Снятие полной блокировки — как /unblock-user: user_blocks и блок
    рефералки удаляются, RWMS → ACTIVE только если срок подписки не истёк;
    автоплатёж остаётся выключенным (пользователь включит сам при оплате).
    Возвращает, активирован ли RWMS (None — срок истёк, не трогали)."""
    db_session.query(UserBlock).filter(UserBlock.user_id == user.id).delete(
        synchronize_session=False
    )
    db_session.execute(
        sa_delete(ReferralProgramBlock).where(ReferralProgramBlock.user_id == user.id)
    )
    admin_audit_write(db_session, request, "account_unblock", target=user.username)
    db_session.commit()

    rwms_updated = None
    db_expire = admin_dt(user.expire_at)
    if db_expire and db_expire > datetime.utcnow():
        rwms_user = rwms_client.get_user_by_username(user.username)
        if rwms_user is not None:
            response = rwms_client.update_user(
                proto.UpdateUserRequest(
                    uuid=rwms_user.uuid, status=proto.UserStatus.ACTIVE
                )
            )
            rwms_updated = response is not None
    logging.info(
        "account unblocked from admin panel: username=%s rwms_updated=%s",
        user.username,
        rwms_updated,
    )
    return rwms_updated


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


def admin_bulk_apply_trial_limit(db_session, user, limit):
    """Лимит пробного по списку → (outcome, текст): платившим не ставится
    (skipped_paid — applied не растёт), ручной лимит владельца не трогается
    (skipped_manual), маркер site:bulk пишется в той же транзакции. Панель
    читается строго (RwmsUnavailableError всплывает — строка помечается
    ошибкой)."""
    if user_never_paid(db_session, user.id) is not True:
        return "skipped_paid", "платил — лимит пробного не применён"
    rwms_user = rwms_client.get_user_by_username_strict(user.username)
    if rwms_user is None:
        return "skipped", "подписки нет в RWMS (панель не тронута)"
    outcome, message = admin_apply_managed_trial_limit(
        db_session, user.id, rwms_user, limit, APPLIED_BY_SITE_BULK
    )
    if outcome == "unchanged":
        return outcome, f"без изменений: {message}"
    return outcome, message


def admin_bulk_remove_traffic_limit(db_session, user):
    """Снятие лимита по списку → (outcome, текст): только управляемый
    (released); ручной лимит владельца — skipped_manual, панель не тронута."""
    rwms_user = rwms_client.get_user_by_username_strict(user.username)
    if rwms_user is None:
        return "skipped", "подписки нет в RWMS (панель не тронута)"
    outcome, message = admin_release_managed_limit(
        db_session, user.id, rwms_user, APPLIED_BY_SITE_BULK
    )
    if outcome == "unchanged":
        return outcome, f"без изменений: {message}"
    return outcome, message


BULK_TRAFFIC_LIMIT_ACTIONS = {"apply_trial_limit", "remove_traffic_limit"}


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

        if (
            action in BULK_TRAFFIC_LIMIT_ACTIONS
            and not dry_run
            and not managed_limits_table_available(db_session)
        ):
            return JsonResponse(
                {"status": "error", "message": MANAGED_LIMITS_MIGRATION_MESSAGE},
                status=503,
            )
        trial_limit = (
            trial_traffic_limit_configured(db_session)
            if action == "apply_trial_limit"
            else None
        )
        results = []
        applied = 0
        # Антиабьюз: пропуски не считаются применёнными (applied не растёт).
        skipped_paid = skipped_manual = skipped_admin_limit = skipped = 0
        for token, user in resolved:
            if dry_run:
                current = admin_dt(user.expire_at)
                preview = {
                    "extend_days": (
                        f"будет продлён на {days} дн. "
                        f"(сейчас до {current + ADMIN_TZ_OFFSET:%Y-%m-%d %H:%M} МСК)"
                        if current
                        else f"будет продлён на {days} дн. (сейчас без подписки)"
                    ),
                    "block": "будет заблокирован (DISABLED, автоплатёж снят)",
                    "unblock": "будет разблокирован",
                    "referral_block": "рефералка будет заблокирована",
                    "referral_unblock": "рефералка будет разблокирована",
                    "apply_trial_limit": (
                        f"будет применён лимит {admin_traffic_limit_label(trial_limit)} "
                        "(только если не платил; ручной лимит панели не трогается)"
                        if trial_limit
                        else ""
                    ),
                    "remove_traffic_limit": (
                        "лимит трафика будет снят (0, NO_RESET, ACTIVE; "
                        "сквады не меняются) — только управляемый, ручной лимит "
                        "панели не трогается"
                    ),
                }[action]
                results.append(
                    {"token": token, "username": user.username, "ok": True,
                     "message": preview}
                )
                continue
            outcome = "applied"
            try:
                if action == "extend_days":
                    message = admin_bulk_extend(db_session, user, days)
                elif action == "block":
                    message = admin_bulk_block(db_session, user, reason)
                elif action == "unblock":
                    message = admin_bulk_unblock(db_session, user)
                elif action == "apply_trial_limit":
                    outcome, message = admin_bulk_apply_trial_limit(
                        db_session, user, trial_limit
                    )
                elif action == "remove_traffic_limit":
                    outcome, message = admin_bulk_remove_traffic_limit(db_session, user)
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
                if outcome in ("applied", "released", "marked"):
                    applied += 1
                elif outcome == "skipped_paid":
                    skipped_paid += 1
                elif outcome == "skipped_manual":
                    skipped_manual += 1
                elif outcome == "skipped_admin_limit":
                    skipped_admin_limit += 1
                else:
                    skipped += 1
                results.append(
                    {"token": token, "username": user.username, "ok": True,
                     "outcome": outcome, "message": message}
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
                limit_gb=trial_limit.limit_gb if trial_limit else None,
                missing=len(missing),
                skipped_paid=skipped_paid,
                skipped_manual=skipped_manual,
                skipped_admin_limit=skipped_admin_limit,
                skipped=skipped,
            )
            db_session.commit()

        return JsonResponse(
            {
                "status": "ok",
                "result": {
                    "dry_run": dry_run,
                    "applied": applied,
                    "skipped_paid": skipped_paid,
                    "skipped_manual": skipped_manual,
                    "skipped_admin_limit": skipped_admin_limit,
                    "skipped": skipped,
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


def admin_promo_batch_payload(batch, sample, codes, used):
    """Сводка партии с правилами первого купона (в партии они одинаковые)."""
    return {
        "id": batch.id,
        "name": batch.name,
        "comment": batch.comment,
        "codes": int(codes or 0),
        "used": int(used or 0),
        "created_at": admin_date_label(batch.created_at),
        "promo_type": sample.promo_type if sample else None,
        "value": int(sample.value or 0) if sample else 0,
        "first_purchase_only": bool(sample.first_purchase_only) if sample else False,
        "valid_until": admin_date_label(sample.valid_until) if sample else None,
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
            batch_ids = [batch.id for batch in batches]
            batch_samples = {}
            if batch_ids:
                sample_ids = [
                    row[0]
                    for row in (
                        db_session.query(func.min(PromoCode.id))
                        .filter(PromoCode.batch_id.in_(batch_ids))
                        .group_by(PromoCode.batch_id)
                        .all()
                    )
                ]
                batch_samples = {
                    promo.batch_id: promo
                    for promo in (
                        db_session.query(PromoCode)
                        .filter(PromoCode.id.in_(sample_ids))
                        .all()
                    )
                }
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
                            admin_promo_batch_payload(
                                batch,
                                batch_samples.get(batch.id),
                                batch_counts.get(batch.id, 0),
                                batch_used.get(batch.id, 0),
                            )
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

        if action == "delete":
            # Удаление кода вместе с историей его активаций и выданными скидками
            # (для тестовых/ошибочных кодов). У активированных кодов UI требует
            # подтверждение с количеством активаций: воронка распродажи по ним
            # после удаления перестанет считаться.
            promo = db_session.get(PromoCode, int(request.POST.get("id") or 0))
            if not promo:
                return JsonResponse({"status": "not_found"}, status=404)
            uses = (
                db_session.query(PromoCodeUse)
                .filter(PromoCodeUse.promo_id == promo.id)
                .delete()
            )
            discounts = (
                db_session.query(UserDiscount)
                .filter(UserDiscount.source_promo_id == promo.id)
                .delete()
            )
            admin_audit_write(
                db_session, request, "promo_delete", target=promo.code,
                uses=int(uses), discounts=int(discounts),
            )
            db_session.delete(promo)
            db_session.commit()
            return JsonResponse(
                {"status": "ok", "result": {"uses": int(uses), "discounts": int(discounts)}}
            )

        return JsonResponse(
            {"status": "error", "message": "Неизвестное действие"}, status=400
        )
    finally:
        db_session.close()


def admin_promo_cohort_tariffs(db_session, promo_ids):
    """Тарифы успешных оплат после активации выбранных промокодов.

    ЮKassa и Wata хранят тариф в разных таблицах, поэтому агрегируем их
    отдельно через ORM, а затем объединяем по внутреннему tariff_id.
    """
    yk_rows = (
        db_session.query(
            YkPayment.subscription_period,
            func.count(YkPayment.id),
            func.coalesce(func.sum(YkPayment.amount), 0),
        )
        .select_from(PromoCodeUse)
        .join(YkPayment, YkPayment.user_id == PromoCodeUse.user_id)
        .filter(PromoCodeUse.promo_id.in_(promo_ids))
        .filter(YkPayment.status == "succeeded")
        .filter(YkPayment.created_at > PromoCodeUse.created_at)
        .group_by(YkPayment.subscription_period)
        .all()
    )
    wata_rows = (
        db_session.query(
            WataInvoice.tariff_id,
            func.count(WataTransaction.id),
            func.coalesce(func.sum(WataTransaction.amount), 0),
        )
        .select_from(PromoCodeUse)
        .join(WataInvoice, WataInvoice.user_id == PromoCodeUse.user_id)
        .join(WataTransaction, WataTransaction.order_id == WataInvoice.order_id)
        .filter(PromoCodeUse.promo_id.in_(promo_ids))
        .filter(WataTransaction.transaction_status == "Paid")
        .filter(WataTransaction.payment_time > PromoCodeUse.created_at)
        .group_by(WataInvoice.tariff_id)
        .all()
    )

    merged = {}
    for tariff_id, payments, revenue in [*yk_rows, *wata_rows]:
        key = tariff_id or ""
        tariff = merged.setdefault(
            key,
            {
                "id": key,
                "name": get_tariff_display_name(key),
                "payments": 0,
                "revenue": 0,
            },
        )
        tariff["payments"] += int(payments or 0)
        tariff["revenue"] += admin_money(revenue)

    total_payments = sum(tariff["payments"] for tariff in merged.values())
    tariffs = []
    for tariff in merged.values():
        tariffs.append(
            {
                **tariff,
                "share_pct": (
                    round(100.0 * tariff["payments"] / total_payments, 1)
                    if total_payments
                    else 0
                ),
            }
        )
    return sorted(
        tariffs,
        key=lambda tariff: (get_tariff_order(tariff["name"]), tariff["name"]),
    )


PROMO_COHORT_USERS_LIMIT = 500


def support_admin_api_promo_cohort(request):
    """Когорта по промокоду (или партии): судьба активировавших код.

    Когорта = строки promo_code_uses; для каждого пользователя считаем
    платежи ПОСЛЕ активации (ЮKassa + Wata), текущий статус подписки и
    автоплатёж. Отдельно — конверсия рассылок с кнопкой claim_promo этого
    кода: сколько получателей активировали. Только чтение, ничего не мутирует.
    """
    auth_response = require_support_admin_any(request, ANALYTICS_ROLES)
    if auth_response:
        return auth_response
    if request.method != "GET":
        return JsonResponse({"status": "error"}, status=405)

    def parse_id(name):
        try:
            return int(request.GET.get(name) or 0)
        except ValueError:
            return 0

    promo_id, batch_id = parse_id("promo_id"), parse_id("batch_id")
    if bool(promo_id) == bool(batch_id):
        return JsonResponse(
            {"status": "error", "message": "Нужен promo_id или batch_id"}, status=400
        )

    db_session = session_factory()
    try:
        if promo_id:
            promo = db_session.get(PromoCode, promo_id)
            if not promo:
                return JsonResponse({"status": "not_found"}, status=404)
            promo_ids = [promo.id]
            subject = {
                "kind": "code",
                "label": promo.code,
                "promo_type": promo.promo_type,
                "value": promo.value,
            }
        else:
            batch = db_session.get(PromoBatch, batch_id)
            if not batch:
                return JsonResponse({"status": "not_found"}, status=404)
            codes = (
                db_session.query(PromoCode.id, PromoCode.promo_type, PromoCode.value)
                .filter(PromoCode.batch_id == batch_id)
                .all()
            )
            promo_ids = [row[0] for row in codes]
            subject = {
                "kind": "batch",
                "label": batch.name,
                "promo_type": codes[0][1] if codes else None,
                "value": codes[0][2] if codes else None,
            }
        if not promo_ids:
            return JsonResponse(
                {"status": "ok", "result": {"subject": subject, "cohort": None}}
            )

        params = {"promo_ids": promo_ids}
        rows = db_session.execute(
            sa_text(
                """
                SELECT
                    uses.user_id,
                    u.telegram_id,
                    uses.created_at AS activated_at,
                    u.expire_at,
                    EXISTS (
                        SELECT 1 FROM yk_recurrent_payments r WHERE r.user_id = u.id
                    ) AS has_autopay,
                    EXISTS (
                        SELECT 1 FROM user_blocks b WHERE b.user_id = u.id
                    ) AS is_blocked,
                    yk.cnt AS yk_cnt, yk.total AS yk_total, yk.first_at AS yk_first,
                    wt.cnt AS wt_cnt, wt.total AS wt_total, wt.first_at AS wt_first
                FROM promo_code_uses uses
                JOIN users u ON u.id = uses.user_id
                LEFT JOIN LATERAL (
                    SELECT count(*) AS cnt,
                           coalesce(sum(p.amount), 0) AS total,
                           min(p.created_at) AS first_at
                    FROM yk_payments p
                    WHERE p.user_id = uses.user_id AND p.status = 'succeeded'
                      AND p.created_at > uses.created_at
                ) yk ON TRUE
                LEFT JOIN LATERAL (
                    -- wata_transactions.payment_time это timestamptz, а
                    -- promo_code_uses.created_at и yk_payments.created_at —
                    -- naive UTC. Приводим здесь, а не в питоне: иначе граница
                    -- «после активации» зависела бы от TimeZone сессии БД, а
                    -- min() ниже сравнивал бы aware с naive и падал с
                    -- TypeError — ровно это ломало отчёт по когорте
                    SELECT count(*) AS cnt,
                           coalesce(sum(t.amount), 0) AS total,
                           min(t.payment_time AT TIME ZONE 'UTC') AS first_at
                    FROM wata_invoices i
                    JOIN wata_transactions t ON t.order_id = i.order_id
                     AND t.transaction_status = 'Paid'
                    WHERE i.user_id = uses.user_id
                      AND t.payment_time AT TIME ZONE 'UTC' > uses.created_at
                ) wt ON TRUE
                WHERE uses.promo_id = ANY(:promo_ids)
                ORDER BY uses.created_at DESC
                """
            ),
            params,
        ).all()

        now = datetime.utcnow()
        users, days_to_purchase = [], []
        totals = {
            "activations": len(rows),
            "buyers": 0,
            "repeat_buyers": 0,
            "payments": 0,
            "revenue": 0,
            "active_now": 0,
            "active_paid": 0,
            "active_without_purchase": 0,
            "expired_without_purchase": 0,
            "returned_then_churned": 0,
            "with_autopay": 0,
            "blocked": 0,
        }
        activations_by_day = {}
        first_payments_by_day = {}
        for row in rows:
            paid_count = int(row.yk_cnt or 0) + int(row.wt_cnt or 0)
            revenue = admin_money(row.yk_total) + admin_money(row.wt_total)
            # Страховка поверх нормализации в SQL: min() по смеси naive и
            # aware падает с TypeError и роняет весь отчёт в 500
            first_paid_at = min(
                (
                    admin_naive_utc(value)
                    for value in (row.yk_first, row.wt_first)
                    if value
                ),
                default=None,
            )
            is_active = bool(row.expire_at and row.expire_at > now)
            if row.is_blocked:
                status = "blocked"
            elif is_active:
                status = "active"
            else:
                status = "expired"
            totals["payments"] += paid_count
            totals["revenue"] += revenue
            if paid_count:
                totals["buyers"] += 1
                if paid_count > 1:
                    totals["repeat_buyers"] += 1
                if not is_active and not row.is_blocked:
                    totals["returned_then_churned"] += 1
            if is_active:
                totals["active_now"] += 1
            if not row.is_blocked:
                if is_active and paid_count:
                    totals["active_paid"] += 1
                elif is_active:
                    totals["active_without_purchase"] += 1
                elif not paid_count:
                    totals["expired_without_purchase"] += 1
            if row.has_autopay:
                totals["with_autopay"] += 1
            if row.is_blocked:
                totals["blocked"] += 1
            naive_paid = first_paid_at
            if first_paid_at:
                paid_day = (naive_paid + ADMIN_TZ_OFFSET).date().isoformat()
                first_payments_by_day[paid_day] = (
                    first_payments_by_day.get(paid_day, 0) + 1
                )
            if naive_paid and row.activated_at:
                delta_days = (naive_paid - row.activated_at).total_seconds() / 86400
                if delta_days >= 0:
                    days_to_purchase.append(delta_days)
            if row.activated_at:
                day = (row.activated_at + ADMIN_TZ_OFFSET).date().isoformat()
                activations_by_day[day] = activations_by_day.get(day, 0) + 1
            if len(users) < PROMO_COHORT_USERS_LIMIT:
                users.append(
                    {
                        "user_id": row.user_id,
                        "telegram_id": row.telegram_id,
                        "activated_at": admin_date_label(row.activated_at),
                        "status": status,
                        "payments": paid_count,
                        "revenue": revenue,
                        "first_paid_at": admin_date_label(first_paid_at),
                        "expire_at": admin_date_label(row.expire_at),
                        "has_autopay": bool(row.has_autopay),
                    }
                )

        tariffs = admin_promo_cohort_tariffs(db_session, promo_ids)

        pending_discounts = (
            db_session.execute(
                sa_text(
                    """
                    SELECT count(*) FROM user_discounts d
                    WHERE d.source_promo_id = ANY(:promo_ids)
                      AND d.valid_until > (now() AT TIME ZONE 'UTC')
                    """
                ),
                params,
            ).scalar()
            or 0
        )

        broadcasts = db_session.execute(
            sa_text(
                """
                SELECT b.id, b.title, b.total, b.created_at,
                       (
                           SELECT count(DISTINCT d.user_id)
                           FROM broadcast_deliveries d
                           WHERE d.broadcast_id = b.id AND d.status = 'sent'
                       ) AS sent,
                       (
                           SELECT count(DISTINCT uses.user_id)
                           FROM promo_code_uses uses
                           JOIN broadcast_deliveries d ON d.user_id = uses.user_id
                            AND d.broadcast_id = b.id AND d.status = 'sent'
                           WHERE uses.promo_id = ANY(:promo_ids)
                             AND uses.created_at >= b.created_at
                       ) AS activated
                FROM broadcasts b
                WHERE b.test_telegram_id IS NULL
                  AND EXISTS (
                      -- jsonb_array_elements роняет весь SELECT на строке,
                      -- где buttons не массив (объект, скаляр, jsonb 'null'), а
                      -- условие сканирует ВСЕ рассылки. Каст promo_id тоже
                      -- защищаем: порядок вычисления AND в Postgres не
                      -- гарантирован, и нечисловая строка дала бы
                      -- ProgrammingError вместо отчёта
                      SELECT 1 FROM jsonb_array_elements(b.buttons) btn
                      WHERE jsonb_typeof(b.buttons) = 'array'
                        AND btn->>'type' = 'claim_promo'
                        AND btn->>'promo_id' ~ '^[0-9]+$'
                        AND (btn->>'promo_id')::bigint = ANY(:promo_ids)
                  )
                ORDER BY b.created_at DESC
                """
            ),
            params,
        ).all()

        return JsonResponse(
            {
                "status": "ok",
                "result": {
                    "subject": subject,
                    "cohort": {
                        **totals,
                        "pending_discounts": int(pending_discounts),
                        "avg_days_to_purchase": (
                            round(sum(days_to_purchase) / len(days_to_purchase), 1)
                            if days_to_purchase
                            else None
                        ),
                        "conversion_pct": (
                            round(100.0 * totals["buyers"] / totals["activations"], 1)
                            if totals["activations"]
                            else 0
                        ),
                        "second_purchase_pct": (
                            round(100.0 * totals["repeat_buyers"] / totals["buyers"], 1)
                            if totals["buyers"]
                            else 0
                        ),
                        "revenue_per_activation": (
                            round(totals["revenue"] / totals["activations"])
                            if totals["activations"]
                            else 0
                        ),
                    },
                    "activations_by_day": sorted(activations_by_day.items()),
                    "first_payments_by_day": sorted(first_payments_by_day.items()),
                    "tariffs": tariffs,
                    "broadcasts": [
                        {
                            "id": row.id,
                            "title": row.title,
                            "total": int(row.total or 0),
                            "sent": int(row.sent or 0),
                            "activated": int(row.activated or 0),
                            "activation_pct": (
                                round(100.0 * int(row.activated or 0) / int(row.sent), 1)
                                if row.sent
                                else 0
                            ),
                            "created_at": admin_date_label(row.created_at),
                        }
                        for row in broadcasts
                    ],
                    "users": users,
                    "users_truncated": len(rows) > len(users),
                },
            }
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


# ---------------------------------------------------------------------------
# Установка нод через админку: публичный bootstrap-API (для обёртки на
# сервере) и admin-API (скрипты установки + заявки). Логика — в
# engine/node_provisioning.py, эти view только парсят запрос и держат сессию.
# ---------------------------------------------------------------------------


def _node_bootstrap_reject_host(request):
    """Bootstrap-API отвечает только на доменах из NODE_BOOTSTRAP_DOMAINS.

    Пустой список = API выключено. На остальных доменах отдаём 404 без
    тела, чтобы эндпоинты не светились на VPN/VPS/cabinet доменах.
    """
    if not settings.NODE_BOOTSTRAP_DOMAINS:
        return HttpResponse(status=404)
    if normalize_host(request.get_host()) not in settings.NODE_BOOTSTRAP_DOMAINS:
        return HttpResponse(status=404)
    return None


def _node_bootstrap_token(request):
    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer "):].strip()
    return ""


def _node_bootstrap_error(db_session, error):
    db_session.rollback()
    return JsonResponse(
        {"status": "error", "message": str(error)}, status=error.http_status
    )


def _node_bootstrap_find(db_session, request):
    """Заявка по Bearer-токену + проверка привязки к IP (после claim)."""
    provision_request = node_provisioning.find_request_by_token(
        db_session, _node_bootstrap_token(request)
    )
    node_provisioning.check_claimed_ip(provision_request, admin_client_ip(request))
    return provision_request


def node_bootstrap_runner(request):
    host_response = _node_bootstrap_reject_host(request)
    if host_response:
        return host_response
    return render(
        request,
        "node_bootstrap_runner.sh",
        {"base_url": f"{request.scheme}://{request.get_host()}"},
        content_type="text/plain; charset=utf-8",
    )


@csrf_exempt
def node_bootstrap_claim(request):
    host_response = _node_bootstrap_reject_host(request)
    if host_response:
        return host_response
    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "Только POST"}, status=405
        )

    db_session = session_factory()
    try:
        provision_request = node_provisioning.find_request_by_token(
            db_session, _node_bootstrap_token(request), for_update=True
        )
        payload = node_provisioning.claim_request(
            db_session, provision_request, admin_client_ip(request), rwms_client
        )
        db_session.commit()
        return JsonResponse({"status": "ok", "result": payload})
    except node_provisioning.ProvisionError as error:
        return _node_bootstrap_error(db_session, error)
    finally:
        db_session.close()


def node_bootstrap_script(request):
    host_response = _node_bootstrap_reject_host(request)
    if host_response:
        return host_response

    db_session = session_factory()
    try:
        provision_request = _node_bootstrap_find(db_session, request)
        node_provisioning.check_stage_allowed(provision_request)
        script = db_session.query(NodeInstallScript).get(provision_request.script_id)
        return HttpResponse(
            script.content, content_type="text/plain; charset=utf-8"
        )
    except node_provisioning.ProvisionError as error:
        return _node_bootstrap_error(db_session, error)
    finally:
        db_session.close()


@csrf_exempt
def node_bootstrap_progress(request):
    host_response = _node_bootstrap_reject_host(request)
    if host_response:
        return host_response
    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "Только POST"}, status=405
        )

    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse(
            {"status": "error", "message": "Невалидный JSON"}, status=400
        )

    db_session = session_factory()
    try:
        provision_request = _node_bootstrap_find(db_session, request)
        node_provisioning.record_progress(
            db_session,
            provision_request,
            stage=str(body.get("stage") or ""),
            status=str(body.get("status") or ""),
            message=(str(body.get("message")) if body.get("message") else None),
            log_tail=(str(body.get("log_tail")) if body.get("log_tail") else None),
        )
        db_session.commit()
        return JsonResponse({"status": "ok"})
    except node_provisioning.ProvisionError as error:
        return _node_bootstrap_error(db_session, error)
    finally:
        db_session.close()


@csrf_exempt
def node_bootstrap_complete(request):
    host_response = _node_bootstrap_reject_host(request)
    if host_response:
        return host_response
    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "Только POST"}, status=405
        )

    try:
        body = json.loads(request.body or b"{}")
        exit_code = int(body.get("exit_code"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return JsonResponse(
            {"status": "error", "message": "Невалидный exit_code"}, status=400
        )

    db_session = session_factory()
    try:
        provision_request = _node_bootstrap_find(db_session, request)
        node_provisioning.complete_request(db_session, provision_request, exit_code)
        db_session.commit()
        return JsonResponse({"status": "ok"})
    except node_provisioning.ProvisionError as error:
        return _node_bootstrap_error(db_session, error)
    finally:
        db_session.close()


def _admin_node_script_payload(script, with_content=False):
    payload = {
        "id": script.id,
        "node_type": script.node_type,
        "version": script.version,
        "is_active": script.is_active,
        "comment": script.comment,
        "created_by": script.created_by,
        "created_at": admin_dt(script.created_at),
        "size": len(script.content),
        "sha256": node_provisioning.script_sha256(script.content),
    }
    if with_content:
        payload["content"] = script.content
    return payload


def _admin_node_script_groups(db_session):
    """Динамические именованные группы; version=0 — внутренний черновик."""
    all_scripts = (
        db_session.query(NodeInstallScript)
        .order_by(NodeInstallScript.node_type, NodeInstallScript.version.desc())
        .all()
    )
    by_name = {}
    for script in all_scripts:
        by_name.setdefault(script.node_type, []).append(script)

    groups = []
    for name, rows in by_name.items():
        versions = [script for script in rows if script.version > 0]
        active = next((script for script in versions if script.is_active), None)
        groups.append(
            {
                "key": name,
                "label": node_provisioning.script_display_name(name),
                "has_active": active is not None,
                "active": (
                    _admin_node_script_payload(active, with_content=True)
                    if active
                    else None
                ),
                "versions": [
                    _admin_node_script_payload(script) for script in versions
                ],
            }
        )
    return sorted(groups, key=lambda group: group["label"].casefold())


def support_admin_api_node_scripts(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "POST":
            action = request.POST.get("action") or ""
            try:
                if action == "create":
                    script = node_provisioning.create_script_group(
                        db_session,
                        request.POST.get("name") or "",
                        str(support_admin_actor(request))[:128],
                    )
                    admin_audit_write(
                        db_session,
                        request,
                        "node_script_create",
                        target=script.node_type,
                    )
                    db_session.commit()
                    return JsonResponse(
                        {
                            "status": "ok",
                            "result": {
                                "key": script.node_type,
                                "label": node_provisioning.script_display_name(
                                    script.node_type
                                ),
                            },
                        }
                    )

                if action == "rename":
                    script = node_provisioning.rename_script_group(
                        db_session,
                        request.POST.get("node_type") or "",
                        request.POST.get("name") or "",
                    )
                    admin_audit_write(
                        db_session,
                        request,
                        "node_script_rename",
                        target=script.node_type,
                    )
                    db_session.commit()
                    return JsonResponse(
                        {
                            "status": "ok",
                            "result": {
                                "key": script.node_type,
                                "label": node_provisioning.script_display_name(
                                    script.node_type
                                ),
                            },
                        }
                    )

                if action == "delete":
                    node_type = request.POST.get("node_type") or ""
                    deleted_versions = node_provisioning.delete_script_group(
                        db_session, node_type
                    )
                    admin_audit_write(
                        db_session,
                        request,
                        "node_script_delete",
                        target=node_type,
                        versions=deleted_versions,
                    )
                    db_session.commit()
                    return JsonResponse({"status": "ok"})

                if action == "save":
                    node_type = request.POST.get("node_type") or ""
                    content = request.POST.get("content") or ""
                    script = node_provisioning.save_script_version(
                        db_session,
                        node_type,
                        content,
                        request.POST.get("comment") or "",
                        str(support_admin_actor(request))[:128],
                    )
                    admin_audit_write(
                        db_session, request, "node_script_save",
                        target=node_type, version=script.version,
                    )
                    db_session.commit()
                    return JsonResponse(
                        {
                            "status": "ok",
                            "result": _admin_node_script_payload(script),
                            "warnings": node_provisioning.script_warnings(content),
                        }
                    )

                if action == "activate":
                    script = node_provisioning.activate_script_version(
                        db_session, int(request.POST.get("script_id") or 0)
                    )
                    admin_audit_write(
                        db_session, request, "node_script_activate",
                        target=script.node_type, version=script.version,
                    )
                    db_session.commit()
                    return JsonResponse(
                        {"status": "ok", "result": _admin_node_script_payload(script)}
                    )
            except node_provisioning.ProvisionError as error:
                db_session.rollback()
                return JsonResponse(
                    {"status": "error", "message": str(error)},
                    status=error.http_status,
                )

            return JsonResponse(
                {"status": "error", "message": "Неизвестное действие"}, status=400
            )

        script_id = request.GET.get("script_id")
        if script_id:
            script = db_session.query(NodeInstallScript).get(int(script_id))
            if script is None:
                return JsonResponse(
                    {"status": "error", "message": "Версия не найдена"}, status=404
                )
            return JsonResponse(
                {
                    "status": "ok",
                    "result": _admin_node_script_payload(script, with_content=True),
                }
            )

        groups = _admin_node_script_groups(db_session)
        # `types` остаётся на время совместимости со старой админкой.
        return JsonResponse(
            {"status": "ok", "result": {"scripts": groups, "types": groups}}
        )
    finally:
        db_session.close()


def _admin_provision_request_payload(provision_request, script_versions=None):
    script_meta = (script_versions or {}).get(provision_request.script_id)
    if isinstance(script_meta, dict):
        script_version = script_meta.get("version")
        script_name = script_meta.get("name")
    else:
        script_version = script_meta
        script_name = node_provisioning.script_display_name(
            provision_request.node_type
        )
    return {
        "id": provision_request.id,
        "node_name": provision_request.node_name,
        "node_type": provision_request.node_type,
        "status": provision_request.status.value,
        "script_name": script_name,
        "script_version": script_version,
        "claimed_ip": provision_request.claimed_ip,
        "ssh_port": provision_request.ssh_port,
        "remnawave_node_uuid": provision_request.remnawave_node_uuid,
        "error": provision_request.error,
        "created_by": provision_request.created_by,
        "created_at": admin_dt(provision_request.created_at),
        "expires_at": admin_dt(provision_request.expires_at),
        "finished_at": admin_dt(provision_request.finished_at),
    }


def support_admin_api_node_provision(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "POST":
            action = request.POST.get("action") or ""
            try:
                if action == "create":
                    nodes_response = rwms_client.get_nodes()
                    if nodes_response is None:
                        return JsonResponse(
                            {"status": "error", "message": "RWMS недоступен"},
                            status=502,
                        )
                    source_uuid = request.POST.get("source_node_uuid") or ""
                    source_node = next(
                        (n for n in nodes_response.nodes if n.uuid == source_uuid),
                        None,
                    )
                    if source_node is None:
                        return JsonResponse(
                            {
                                "status": "error",
                                "message": "Нода-образец не найдена в панели",
                            },
                            status=400,
                        )
                    provision_request, token = node_provisioning.create_request(
                        db_session,
                        node_name=request.POST.get("node_name") or "",
                        node_type=(
                            request.POST.get("script_name")
                            or request.POST.get("node_type")
                            or ""
                        ),
                        source_node=source_node,
                        country_code=(
                            (request.POST.get("country_code") or "").upper()[:2]
                            or None
                        ),
                        created_by=str(support_admin_actor(request))[:128],
                        ttl_minutes=settings.NODE_BOOTSTRAP_TOKEN_TTL_MINUTES,
                    )
                    admin_audit_write(
                        db_session, request, "node_provision_create",
                        target=provision_request.node_name,
                        node_type=provision_request.node_type,
                    )
                    db_session.commit()
                    bootstrap_domain = (
                        settings.NODE_BOOTSTRAP_DOMAINS[0]
                        if settings.NODE_BOOTSTRAP_DOMAINS
                        else normalize_host(request.get_host())
                    )
                    one_liner = (
                        f"curl -fsSL https://{bootstrap_domain}/node-bootstrap/runner/ "
                        f"| bash -s -- {token}"
                    )
                    return JsonResponse(
                        {
                            "status": "ok",
                            "result": {
                                "request": _admin_provision_request_payload(
                                    provision_request
                                ),
                                # Токен показывается ровно один раз
                                "one_liner": one_liner,
                                "bootstrap_domains_configured": bool(
                                    settings.NODE_BOOTSTRAP_DOMAINS
                                ),
                            },
                        }
                    )

                provision_request = (
                    db_session.query(NodeProvisionRequest)
                    .filter(NodeProvisionRequest.id == int(request.POST.get("id") or 0))
                    .with_for_update()
                    .first()
                )
                if provision_request is None:
                    return JsonResponse(
                        {"status": "error", "message": "Заявка не найдена"},
                        status=404,
                    )

                if action == "revoke":
                    node_provisioning.revoke_request(provision_request)
                    admin_audit_write(
                        db_session, request, "node_provision_revoke",
                        target=provision_request.node_name,
                    )
                    db_session.commit()
                    return JsonResponse({"status": "ok"})
            except node_provisioning.ProvisionError as error:
                db_session.rollback()
                return JsonResponse(
                    {"status": "error", "message": str(error)},
                    status=error.http_status,
                )

            return JsonResponse(
                {"status": "error", "message": "Неизвестное действие"}, status=400
            )

        requests_rows = (
            db_session.query(NodeProvisionRequest)
            .order_by(NodeProvisionRequest.created_at.desc())
            .limit(100)
            .all()
        )
        script_rows = db_session.query(NodeInstallScript).all()
        script_versions = {
            script.id: {
                "version": script.version,
                "name": node_provisioning.script_display_name(script.node_type),
            }
            for script in script_rows
        }
        script_names = sorted(
            {script.node_type for script in script_rows},
            key=lambda name: node_provisioning.script_display_name(name).casefold(),
        )
        scripts_ready = {
            name: any(
                script.node_type == name and script.is_active
                for script in script_rows
            )
            for name in script_names
        }
        scripts_payload = [
            {
                "key": name,
                "label": node_provisioning.script_display_name(name),
                "has_active": scripts_ready[name],
                "active_version": next(
                    (
                        script.version
                        for script in script_rows
                        if script.node_type == name and script.is_active
                    ),
                    None,
                ),
            }
            for name in script_names
        ]

        nodes_payload = []
        nodes_response = rwms_client.get_nodes()
        if nodes_response is not None:
            nodes_payload = [
                {
                    "uuid": node.uuid,
                    "name": node.name,
                    "address": node.address,
                    "is_connected": node.is_connected,
                    "has_config_profile": bool(node.config_profile_uuid),
                }
                for node in nodes_response.nodes
            ]

        return JsonResponse(
            {
                "status": "ok",
                "result": {
                    "requests": [
                        _admin_provision_request_payload(row, script_versions)
                        for row in requests_rows
                    ],
                    "nodes": nodes_payload,
                    "rwms_available": nodes_response is not None,
                    "scripts": scripts_payload,
                    # Старое поле сохраняем для обратной совместимости API.
                    "node_types": [
                        {"key": script["key"], "label": script["label"]}
                        for script in scripts_payload
                    ],
                    "scripts_ready": scripts_ready,
                    "bootstrap_domains_configured": bool(
                        settings.NODE_BOOTSTRAP_DOMAINS
                    ),
                },
            }
        )
    finally:
        db_session.close()


def support_admin_api_node_provision_detail(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        provision_request = (
            db_session.query(NodeProvisionRequest)
            .get(int(request.GET.get("id") or 0))
        )
        if provision_request is None:
            return JsonResponse(
                {"status": "error", "message": "Заявка не найдена"}, status=404
            )

        # Пока заявка в installed, каждый poll детали проверяет коннект ноды
        node_provisioning.refresh_connect_status(
            db_session, provision_request, rwms_client
        )
        db_session.commit()

        stages_rows = (
            db_session.query(NodeProvisionStage)
            .filter(NodeProvisionStage.request_id == provision_request.id)
            .all()
        )
        stages_by_name = {row.stage: row for row in stages_rows}
        stages_payload = []
        for stage_name in node_provisioning.STAGES:
            row = stages_by_name.get(stage_name)
            stages_payload.append(
                {
                    "stage": stage_name,
                    "status": row.status if row else "pending",
                    "message": row.message if row else None,
                    "updated_at": admin_dt(row.updated_at) if row else None,
                }
            )

        return JsonResponse(
            {
                "status": "ok",
                "result": {
                    "request": _admin_provision_request_payload(provision_request),
                    "stages": stages_payload,
                    "install_log": provision_request.install_log or "",
                },
            }
        )
    finally:
        db_session.close()


# --- Инфраструктура → Серверы ------------------------------------------------


def _infra_error_response(db_session, error):
    db_session.rollback()
    return JsonResponse(
        {"status": "error", "message": error.message}, status=error.http_status
    )


def support_admin_api_infra_servers(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "GET":
            include_archived = request.GET.get("archived") == "1"
            try:
                payload = infra.server_list_payload(
                    db_session, include_archived=include_archived
                )
            except SQLAlchemyError:
                # Код мог приехать раньше alembic-миграции таблиц infra_*
                db_session.rollback()
                logging.exception("infra servers list failed")
                return JsonResponse(
                    {
                        "status": "error",
                        "message": "Таблицы инфраструктуры недоступны — "
                        "применена ли миграция common?",
                    },
                    status=503,
                )
            return JsonResponse({"status": "ok", "result": payload})

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        action = request.POST.get("action") or ""
        server_id = request.POST.get("id")
        actor = str(support_admin_actor(request))[:128]
        try:
            if action == "update":
                fields = {
                    "display_name": request.POST.get("display_name"),
                    "bandwidth_limit_mbps": request.POST.get(
                        "bandwidth_limit_mbps"
                    ),
                    "notes": request.POST.get("notes"),
                }
                if "country_code" in request.POST:
                    fields["country_code"] = request.POST.get("country_code")
                server = infra.update_server(db_session, server_id, fields)
                admin_audit_write(
                    db_session, request, "infra_server_update",
                    target=server.node_name,
                    bandwidth_limit_mbps=server.bandwidth_limit_mbps,
                )
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action in ("archive", "unarchive"):
                server = infra.update_server(
                    db_session, server_id, {"is_archived": action == "archive"}
                )
                admin_audit_write(
                    db_session, request, f"infra_server_{action}",
                    target=server.node_name,
                )
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action == "add_ip":
                row = infra.add_manual_ip(
                    db_session,
                    server_id,
                    request.POST.get("ip") or "",
                    request.POST.get("prefix"),
                    request.POST.get("comment") or "",
                )
                admin_audit_write(
                    db_session, request, "infra_ip_add",
                    target=row.ip, server_id=int(server_id or 0),
                )
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action == "delete_ip":
                infra.delete_ip(db_session, server_id, request.POST.get("ip_id"))
                admin_audit_write(
                    db_session, request, "infra_ip_delete",
                    target=request.POST.get("ip_id"),
                    server_id=int(server_id or 0),
                )
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action in ("block_ip", "unblock_ip"):
                row = infra.set_ip_blocked(
                    db_session,
                    server_id,
                    request.POST.get("ip_id"),
                    action == "block_ip",
                )
                admin_audit_write(
                    db_session, request, f"infra_{action}", target=row.ip,
                    server_id=int(server_id or 0),
                )
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action == "add_domain":
                row = infra.add_domain(
                    db_session, server_id, request.POST.get("domain") or "",
                    request.POST.get("client_snis") or "",
                )
                admin_audit_write(
                    db_session, request, "infra_domain_add", target=row.domain,
                    server_id=int(server_id or 0),
                    value=", ".join(row.client_snis or []),
                )
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action == "set_server_snis":
                server = infra.set_server_snis(
                    db_session, server_id, request.POST.get("client_snis") or "",
                )
                admin_audit_write(
                    db_session, request, "infra_server_snis",
                    target=server.node_name, server_id=int(server_id or 0),
                    value=", ".join(server.client_snis or []),
                )
                db_session.commit()
                return JsonResponse({
                    "status": "ok",
                    "client_snis": list(server.client_snis or []),
                })

            if action == "set_domain_snis":
                apply_all = str(
                    request.POST.get("apply_all") or ""
                ).strip().lower() in ("1", "true", "yes", "on")
                rows = infra.set_domain_snis(
                    db_session, server_id, request.POST.get("domain") or "",
                    request.POST.get("client_snis") or "",
                    apply_all=apply_all,
                )
                admin_audit_write(
                    db_session, request, "infra_domain_snis",
                    target=", ".join(row.domain for row in rows),
                    server_id=int(server_id or 0),
                    value=", ".join(rows[0].client_snis or []) if rows else "",
                    apply_all=apply_all,
                )
                db_session.commit()
                # По строкам значения различаются: домену, совпавшему с
                # единственным именем, пишется NULL. Отдаём состояние каждой
                return JsonResponse({
                    "status": "ok",
                    "domains": [row.domain for row in rows],
                    "client_snis": {
                        row.domain: list(row.client_snis or []) for row in rows
                    },
                })

            if action == "delete_domain":
                infra.delete_domain(
                    db_session, server_id, request.POST.get("domain") or ""
                )
                admin_audit_write(
                    db_session, request, "infra_domain_delete",
                    target=request.POST.get("domain"),
                    server_id=int(server_id or 0),
                )
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action == "ensure_ip":
                server = infra.get_server(db_session, server_id)
                ip_row = db_session.get(
                    InfraServerIp, int(request.POST.get("ip_id") or 0)
                )
                if ip_row is None or ip_row.server_id != server.id:
                    raise infra.InfraError("IP не найден", 404)
                command = infra.create_ensure_ip_command(
                    db_session, server, ip_row, created_by=actor
                )
                admin_audit_write(
                    db_session, request, "infra_ensure_ip", target=ip_row.ip,
                    command_id=command.id,
                )
                db_session.commit()
                return JsonResponse(
                    {"status": "ok", "result": {"command_id": command.id}}
                )

            if action == "replace_ip":
                replacement = infra.request_replacement(
                    db_session,
                    server_id,
                    request.POST.get("ip") or "",
                    created_by=f"manual:{actor}",
                )
                admin_audit_write(
                    db_session, request, "infra_replace_ip",
                    target=request.POST.get("ip"),
                    replacement_id=replacement.id,
                )
                db_session.commit()
                return JsonResponse(
                    {"status": "ok", "result": {"replacement_id": replacement.id}}
                )

            if action in ("snooze", "unsnooze"):
                if action == "snooze":
                    server = infra.snooze_anomaly_detector(
                        db_session, server_id, request.POST.get("hours")
                    )
                else:
                    server = infra.unsnooze_anomaly_detector(
                        db_session, server_id
                    )
                admin_audit_write(
                    db_session, request, f"infra_anomaly_{action}",
                    target=server.node_name,
                    until=str(server.anomaly_suppressed_until),
                )
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action in ("tspu_enable", "tspu_disable"):
                server = infra.set_tspu_checks_enabled(
                    db_session, server_id, action == "tspu_enable"
                )
                admin_audit_write(
                    db_session, request, f"infra_{action}",
                    target=server.node_name,
                )
                db_session.commit()
                return JsonResponse({"status": "ok"})

            if action == "force_check":
                # Полная диагностика: сначала адреса контрольным именем,
                # затем имена на живом адресе. Вердикт и алерт с журналом
                # проверок выдаст обычный конвейер обработки аномалий.
                server = infra.get_server(db_session, server_id)
                result = infra.start_manual_diagnosis(
                    db_session, server, actor=actor
                )
                admin_audit_write(
                    db_session, request, "infra_force_tspu_check",
                    target=server.node_name,
                    run_ids=list(result.get("runs", {}).values()),
                )
                db_session.commit()
                return JsonResponse({"status": "ok", "result": result})

            return JsonResponse(
                {"status": "error", "message": "Неизвестное действие"}, status=400
            )
        except infra.InfraError as error:
            return _infra_error_response(db_session, error)
    finally:
        db_session.close()


def support_admin_api_infra_server_detail(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        try:
            payload = infra.server_detail_payload(
                db_session, request.GET.get("id")
            )
        except infra.InfraError as error:
            return _infra_error_response(db_session, error)
        return JsonResponse({"status": "ok", "result": payload})
    finally:
        db_session.close()


def support_admin_api_infra_telemetry(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        try:
            payload = infra.telemetry_series_payload(
                db_session,
                request.GET.get("id"),
                request.GET.get("period") or "24h",
            )
        except infra.InfraError as error:
            return _infra_error_response(db_session, error)
        return JsonResponse({"status": "ok", "result": payload})
    finally:
        db_session.close()


def support_admin_api_infra_settings(request):
    auth_response = require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)
    if auth_response:
        return auth_response

    db_session = session_factory()
    try:
        if request.method == "GET":
            values = infra.get_settings(db_session)
            items = [
                {
                    "key": key,
                    "value": values[key],
                    "default": default,
                    "description": description,
                    "allowed_values": allowed or [],
                    "type": (
                        "bool" if cast is bool
                        else "float" if cast is float
                        else "str" if cast is str
                        else "int"
                    ),
                }
                for key, (default, cast, description, allowed)
                in infra.INFRA_SETTINGS.items()
            ]
            return JsonResponse({"status": "ok", "settings": items})

        if request.method != "POST":
            return JsonResponse({"status": "error"}, status=405)

        key = (request.POST.get("key") or "").strip()
        try:
            normalized = infra.validate_setting(key, request.POST.get("value"))
        except infra.InfraError as error:
            return _infra_error_response(db_session, error)
        admin_upsert_system_setting(db_session, key, normalized)
        admin_audit_write(
            db_session, request, "infra_setting_save", target=key,
            value=normalized,
        )
        db_session.commit()
        return JsonResponse({"status": "ok"})
    finally:
        db_session.close()
