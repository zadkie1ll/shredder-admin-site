from engine.test_template_source import template_source
import hashlib
import hmac
import json
import tempfile
import subprocess
import shutil
import os
import re
import time
from contextlib import ExitStack
from urllib.parse import urlsplit
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.test import RequestFactory
from datetime import timezone as dt_timezone
from django.test import SimpleTestCase
from django.test import override_settings
from sqlalchemy import create_engine
from sqlalchemy import event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from common.models.db import Base
from common.models.db import WataInvoice
from common.models.db import WataTransaction
from common.models.db import YkPayment

from common.models.settings import RUNTIME_SETTING_KEYS
from common.models.settings import BOT_APPLE_RECOMMENDED_APP_SETTING
from common.models.settings import BOT_TARIFF_PRICE_ONEDAY_SETTING
from common.models.settings import BOT_TARIFF_PRICE_THREEDAYS_SETTING
from common.models.settings import BOT_TARIFF_PRICE_MONTH_SETTING
from common.models.settings import BOT_TARIFF_PRICE_THREEMONTHS_SETTING
from common.models.settings import BOT_TARIFF_PRICE_YEAR_SETTING
from common.models.db import ClientUaRule
from common.models.db import CensorCheck
from common.models.db import CensorCheckRun
from common.models.db import CustomConfigTemplate
from common.models.db import MagicToken
from common.models.db import RipeApiKey
from common.models.db import User
from common.rwms_client import RwmsUnavailableError
from engine.rwms_helpers import deterministic_username
from engine.sql_helpers import lock_registration_email
from engine.sql_helpers import lock_registration_telegram_id
from engine.payments import create_wata_payment_sync
from engine.payments import create_yk_payment_sync
from engine.payments import fetch_wata_transaction_status
from engine.views import active_wata_status_for_token
from engine.views import admin_clamp_cohort_window
from engine.views import admin_runtime_setting_payload
from engine.views import admin_runtime_setting_type
from engine.views import admin_validate_runtime_setting
from engine.views import admin_stats_row_bucket_key
from engine.views import support_admin_api_cohort_stats
from engine.views import support_admin_api_censor_checks
from engine.views import auth_by_telegram_widget
from engine.views import SITE_REGISTRATION_RETRY_MESSAGE
from engine.views import SITE_REGISTRATION_SUPPORT_MESSAGE
from engine.views import SiteRegistrationOwnershipConflict
from engine.views import SiteRegistrationUnavailable
from engine.views import create_site_user
from engine.views import get_purchase_payment_status
from engine.views import resolve_existing_site_subscription
from engine.views import send_magic_link
from engine.views import update_email
from engine.views import site_registration_username
from engine.views import custom_config_template_payload
from engine.views import form_bool_enabled
from engine.views import get_runtime_actual_tariffs
from engine.views import get_runtime_offer_tariffs
from engine.views import get_telegram_auth_bot
from engine.views import get_telegram_web_login_start_code
from engine.views import cancel_autopay
from engine.views import pay
from engine.views import payment_session_url_key
from engine.views import ACQ_PUSH_ATTRIBUTION_CTE
from engine.views import ACQ_PAYS_TARIFF_CTE
from engine.views import ACQ_TIMING_LABELS
from engine.views import _acq_ads
from engine.views import _acq_ads_daily
from engine.views import _acq_ads_summary
from engine.views import _acq_pushes
from engine.views import _acq_renewal_ladder
from engine.views import _parse_direct_number
from engine.views import _parse_yandex_direct_csv
from engine.views import _acq_tariff_paths
from engine.views import _acq_trial_timing
from engine.views import _acq_trials
from engine.views import payment_retry
from engine.views import render_login
from engine.views import should_create_trial_for_channel
from engine.views import should_send_payment_login_email
from engine.views import site_trial_registration_enabled
from engine.views import config_pins_payload
from engine.views import remove_config_template_id_from_pins
from engine.views import support_admin_api_config_pins
from engine.views import support_admin_api_config_templates
from engine.views import support_admin_api_ua_rules
from engine.views import client_ua_rule_payload
from engine.views import client_ua_rule_scenarios
from engine.views import validate_client_ua_rule_fields
from engine.views import validate_config_template_json
from engine.views import verify_telegram_widget_auth
from engine.views import verify_telegram_webapp_init_data
from engine.views import get_telegram_webapp_user_id
from web_app.settings import telegram_web_login_start_codes


class _FakeProtoTimestamp:
    """Stand-in для protobuf Timestamp (нужен только ``ToDatetime``)."""

    def __init__(self, value):
        self._value = value

    def ToDatetime(self):  # noqa: N802 - mirrors protobuf API
        return self._value


class _FakeSiteRwUser(SimpleNamespace):
    """Stand-in for proto.UserResponse в тестах сайтовой регистрации.

    ``HasField`` отвечает по фактически переданным атрибутам, поэтому запись
    панели можно создать как с email (проверка владельца), так и без него
    (неоднозначная запись, которую ownership guard обязан отвергнуть)."""

    def HasField(self, field_name):  # noqa: N802 - mirrors protobuf API
        return getattr(self, field_name, None) is not None


class _SiteRegistrationFakeSession:
    """Минимальная сессия для unit-тестов ``create_site_user``.

    ``get_bind`` отдаёт не-postgres диалект: транзакционный advisory-лок по
    email в такой сессии — no-op (как и на SQLite в тестах), сама регистрация
    проверяется без базы."""

    next_user_id = 1

    def __init__(self):
        self.added = []
        self.queries = []

    def get(self, model, key):
        return None

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, User):
            obj.id = self.next_user_id

    def flush(self):
        return None

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def query(self, *args, **kwargs):
        self.queries.append(args)
        return _EmptyQuery()


class _EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def one_or_none(self):
        return None


class _ExistingUserQuery(_EmptyQuery):
    def __init__(self, user):
        self.__user = user

    def first(self):
        return self.__user

    def one_or_none(self):
        return self.__user


class _SiteRegistrationSessionWithExistingUser(_SiteRegistrationFakeSession):
    """Сессия, в которой строка users под вычисленным username уже есть —
    например, у пользователя, сменившего почту уже после регистрации."""

    def __init__(self, existing_user):
        super().__init__()
        self.existing_user = existing_user

    def query(self, *args, **kwargs):
        self.queries.append(args)
        return _ExistingUserQuery(self.existing_user)


class StaticAssetVersioningTests(SimpleTestCase):
    def test_staticfiles_use_django_6_manifest_storage(self):
        settings_source = Path("web_app/settings.py").read_text()

        self.assertIn(
            "ManifestStaticFilesStorage",
            settings.PRODUCTION_STATICFILES_BACKEND,
        )
        self.assertEqual(
            settings.STORAGES["staticfiles"]["BACKEND"],
            "django.contrib.staticfiles.storage.StaticFilesStorage",
        )
        self.assertNotIn("STATICFILES_STORAGE =", settings_source)


class DashboardSetupTemplateTests(SimpleTestCase):
    def test_compact_setup_uses_connection_hero(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("Для подключения", template)
        self.assertIn("Подключиться в 1 клик!", template)
        self.assertIn("min-width: min(100%, 420px);", template)
        self.assertIn("align-self: flex-start;", template)
        self.assertIn("align-self: stretch;", template)
        self.assertIn('data-tab="setup"><i class="fas fa-bolt"></i> Установка</button>', template)
        self.assertIn("<span>Устройства</span>", template)
        self.assertIn("installInstruction", template)
        self.assertIn("formatInstallLinkLabel", template)
        self.assertNotIn("setup-connect-extra-list", template)
        self.assertNotIn("setup-connect-step-title", template)
        self.assertNotIn("setup-connect-step-text", template)
        self.assertNotIn("const mainSteps =", template)
        self.assertNotIn("#9cff1a", template)
        self.assertIn("color: #6b7280;", template)
        self.assertIn("font-size: 14px;", template)
        self.assertIn("font-weight: 700;", template)
        self.assertIn("line-height: 1.625;", template)
        self.assertIn("font-size: 15px !important;", template)
        self.assertIn("text-transform: none !important;", template)
        self.assertIn("box-shadow: 0 10px 28px rgba(255, 199, 0, 0.28)", template)
        self.assertIn("background: #ffc700 !important;", template)
        self.assertIn(".setup-compact-copybtn", template)
        self.assertIn("dashboard-action-btn", template)
        self.assertIn("quick-access-btn", template)
        self.assertIn("copyInputText", template)
        self.assertIn("clearCopySelection", template)
        self.assertIn("setup-cta-btn dashboard-action-btn !w-full", template)
        self.assertNotIn("setup-cta-btn !w-full !py-5 !mb-0 !mr-0", template)
        self.assertNotIn("setup-link-btn.setup-secondary-btn", template)
        self.assertNotIn("setup-cta-btn.setup-secondary-btn", template)
        self.assertNotIn('${link.text}</a>`).join(\' или \');', template)
        self.assertNotIn('id="tab-subscription"', template)
        self.assertNotIn("showTab('subscription')", template)

    def test_setup_wizard_orb_returns_on_phones_and_miniapp(self):
        template = template_source("engine/templates/dashboard.html")

        # Мастер с орбом-«крутилкой» включается клиентски на телефонах и в Mini App.
        self.assertIn("function shouldUseSetupWizard()", template)
        self.assertIn("function applySetupFlowMode()", template)
        self.assertIn("applySetupFlowMode();", template)
        self.assertIn("document.body.classList.contains('tg-webapp')", template)
        self.assertIn("return platform === 'ios' || platform === 'android';", template)
        # USE_NEW_SETUP_FLOW остаётся серверным override для всех устройств.
        self.assertIn(
            "const forceSetupWizard = {% if use_new_setup_flow %}true{% else %}false{% endif %};",
            template,
        )
        # Плоский флоу и его шапка скрываются, когда активен мастер.
        self.assertIn('id="setup-flat-header"', template)
        self.assertIn('id="setup-logic-placeholder"', template)
        # Кольца крутилки с плавными переходами и учётом prefers-reduced-motion.
        self.assertIn('class="setup-hero-ring setup-hero-ring-1"', template)
        self.assertIn("transition: transform 0.7s ease, opacity 0.7s ease;", template)
        self.assertIn("prefers-reduced-motion", template)

    def test_setup_wizard_follows_competitor_layout(self):
        template = template_source("engine/templates/dashboard.html")

        # Старт: крутилка с иконкой платформы сверху, заголовок и две кнопки.
        self.assertIn("updateNewSetupHero('setup-hero-step-start', meta.icon, 0);", template)
        self.assertIn("Настройка на ${app.platformLabel}", template)
        self.assertIn("3 шага для завершения настройки", template)
        # Индикатор шага «N из 3» над заголовком каждого шага.
        self.assertIn('<div class="setup-step-kicker">1 из 3</div>', template)
        self.assertIn('<div class="setup-step-kicker">2 из 3</div>', template)
        self.assertIn('<div class="setup-step-kicker">3 из 3</div>', template)
        # На каждом шаге две кнопки: цветная CTA + приглушённая «Далее».
        self.assertIn(".setup-wizard-stage .setup-secondary-btn", template)
        self.assertIn(".setup-wizard-stage .setup-share-btn", template)
        # Финал — одна кнопка действия на главный экран.
        self.assertIn("Подключение и использование</h2>", template)
        self.assertIn('newSetupButton(`Завершить`, "showTab(\'home\')"', template)
        # Экран «Другое устройство»: чипы по центру, копирование/шаринг и QR.
        self.assertIn("Выберите вашу платформу", template)
        self.assertIn("shareNewSetupLink", template)
        self.assertIn('id="new-setup-qr"', template)
        self.assertIn("Отсканируйте на другом устройстве", template)
        # Старый блок «Ваше устройство» удалён вместе со стилями.
        self.assertNotIn("setup-detected", template)
        # Крутилка — постоянный DOM-блок с кольцами, сужающимися к финалу,
        # и плавной прогресс-дугой (@property), как у Akenai.
        self.assertIn('id="new-setup-hero"', template)
        self.assertIn("setup-hero-ring-4", template)
        self.assertIn(".setup-hero-step-done .setup-hero-ring", template)
        self.assertIn("@property --setup-progress", template)
        self.assertIn("function hideNewSetupHero()", template)
        # «Назад» сверху на старте и на выборе платформы, а не под таб-баром.
        self.assertIn("setup-devices-topbar", template)
        # Нижний таб-бар скрыт на экране мастера.
        self.assertIn("body.dashboard-v2.setup-wizard-active.is-setup-tab .nav-mobile", template)
        self.assertIn("document.body.classList.toggle('is-setup-tab', tabId === 'setup');", template)
        # В Mini App «Назад» — нативная кнопка Telegram; свои ссылки «Назад»
        # и «Предыдущий шаг» там скрыты.
        self.assertIn("function handleTgBackButton()", template)
        self.assertIn("updateTgBackButton", template)
        self.assertIn(".tg-webapp .setup-devices-topbar", template)
        # На сайте чип «Назад» (в стиле кнопки со страницы тарифов) есть на
        # всех экранах мастера; нижних «Предыдущий шаг» больше нет.
        self.assertIn("function newSetupBackChip(", template)
        self.assertEqual(template.count("newSetupBackChip("), 6)
        self.assertNotIn("Предыдущий шаг", template)
        # Шрифт кнопок мастера — спокойный medium, как у Akenai.
        self.assertIn("font-weight: 500 !important;", template)
        # «Скопировать ссылку подписки» убран с шага подписки.
        self.assertNotIn("Не сработало? Скопировать ссылку подписки", template)

    def test_happ_app_store_recommendation_uses_single_ru_link(self):
        template = template_source("engine/templates/dashboard.html")

        # Актуальная рекомендация на скачивание Happ (iOS/macOS) — только RU App Store.
        self.assertIn("https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6788279553", template)
        self.assertNotIn("id6746188973", template)
        # Кнопка/ссылка «для других регионов» (US App Store) убрана.
        self.assertNotIn("id6504287215", template)
        self.assertNotIn("других регионов", template)

    def test_dashboard_customer_copy_uses_respectful_tone(self):
        template = template_source("engine/templates/dashboard.html")

        informal_fragments = [
            "Управление твоей",
            "Подключись ",
            "Выбери ",
            "Установи ",
            "Добавь ",
            "Нажми ",
            "Тебе ",
            "Теперь ты",
            "Отправь ",
            "получай ",
            "Привяжи ",
            "Продли ",
            "Начни ",
            "по твоей ссылке",
        ]

        for fragment in informal_fragments:
            self.assertNotIn(fragment, template)


class DashboardReferralTemplateTests(SimpleTestCase):
    def test_referral_page_shows_registration_bonus(self):
        template = template_source("engine/templates/dashboard.html")

        # Бонус типа REGISTRATION теперь начисляется за подключение
        # (первый трафик), а не за создание аккаунта.
        registration_bonus = '+{{ join_referrer_bonus_days|default:"3" }} дня за подключение'
        self.assertIn("Приглашения и бонусы", template)
        self.assertIn(registration_bonus, template)
        self.assertIn("когда друг начнет пользоваться сервисом", template)
        self.assertNotIn("за регистрацию", template)
        # Проверяем саму компактную статистику рефералов, а не случайный
        # grid-класс из другого раздела главной страницы.
        for label, value in (
            ("Приглашено", '{{ ref_invited_count|default:"0" }}'),
            ("Активных", '{{ ref_connected_count|default:"0" }}'),
            ("Покупок", '{{ ref_purchased_count|default:"0" }}'),
        ):
            self.assertIn(label, template)
            self.assertIn(value, template)
        self.assertIn("padding: 22px 24px !important;", template)
        self.assertIn("padding: 18px 22px !important;", template)
        self.assertIn(".referral-stat-card p:last-child", template)
        self.assertNotIn("Добыча за друзей", template)


class DashboardPwaLayoutTemplateTests(SimpleTestCase):
    def test_ios_standalone_pwa_uses_safe_area_layout_fix(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("document.documentElement.classList.add('standalone-pwa')", template)
        self.assertIn("document.documentElement.classList.add('ios-device')", template)
        self.assertIn("html.standalone-pwa.ios-device #app-container", template)
        self.assertIn("html.standalone-pwa.ios-device body.dashboard-v2 #tariff-selection-view", template)
        self.assertIn("padding-top: calc(18px + env(safe-area-inset-top)) !important;", template)
        self.assertIn("padding-top: calc(20px + env(safe-area-inset-top)) !important;", template)
        self.assertIn("height: 72px;", template)
        self.assertIn("padding-bottom: 88px !important;", template)

    def test_mobile_renewal_cta_is_short(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertNotIn("Оплатить и получить доступ", template)


class DashboardDesktopSkinTemplateTests(SimpleTestCase):
    """Десктопный скин кабинета в мире island-лендинга (docs/cabinet-desktop/).

    Скин действует только на >=1025px вне Telegram Mini App; мобильная
    вёрстка и Mini App сохраняют систему docs/mobile-cabinet/DESIGN.md.
    """

    def test_desktop_skin_assets_are_scoped_to_desktop_only(self):
        template = template_source("engine/templates/dashboard.html")

        # Скин подключается вне Mini App; CSS — без media-атрибута, чтобы
        # его мобильный блок прятал .dt-элементы и на узких экранах.
        self.assertIn("{% if not tg_webapp_mode %}", template)
        self.assertIn("{% static 'css/cabinet-desktop.css' %}?v=6\">", template)
        self.assertNotIn(
            "cabinet-desktop.css' %}?v=6\" media=",
            template,
        )
        # Десктопные шрифты не применяются на мобильных.
        self.assertIn(
            "family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono",
            template,
        )
        self.assertIn('media="(min-width: 1025px)"', template)

    def test_desktop_skin_css_never_touches_mobile_or_miniapp(self):
        css = Path("engine/static/css/cabinet-desktop.css").read_text()

        # Все правила скина завёрнуты в десктопный media-запрос и
        # исключают Telegram Mini App.
        self.assertIn("@media (min-width: 1025px)", css)
        self.assertIn("body.dashboard-v2:not(.tg-webapp)", css)
        self.assertNotIn(".tg-mini-", css)
        self.assertNotIn(".mi3-", css)
        self.assertNotIn(".nav-mobile", css)
        # Новые элементы скрыты вне десктопа.
        self.assertIn("@media (max-width: 1024.98px)", css)
        mobile_hide_block = css.split("@media (max-width: 1024.98px)")[1]
        for selector in (".dt-expire-topbar", ".dt-cabinet-footer", ".dt-access-grid", ".dt-final", ".dt-qr-card"):
            self.assertIn(selector, mobile_hide_block)
        self.assertIn("display: none !important;", mobile_hide_block)
        # Мир лендинга: золото, мята, моно-счётчик, чип статуса.
        self.assertIn("--dt-gold: #ffc700;", css)
        self.assertIn("--dt-mint: #38d996;", css)
        self.assertIn(".dt-days-num", css)
        self.assertIn("'JetBrains Mono'", css)

    def test_expire_topbar_sells_renewal_with_price(self):
        template = template_source("engine/templates/dashboard.html")

        # Полоса истечения показывается только при истекающем оплаченном
        # доступе, вне Mini App, и ведёт в существующий выбор тарифов.
        self.assertIn(
            "{% if show_expiring_banner and has_subscription_access and not tg_webapp_mode %}",
            template,
        )
        self.assertIn('class="dt-expire-topbar" role="status"', template)
        self.assertIn(
            "{% if tariffs %}Продлить за {{ tariffs.0.price }} ₽{% else %}Продлить доступ{% endif %}",
            template,
        )
        self.assertIn(
            '<div class="dt-expire-topbar" role="status">\n                <span>Доступ <span class="warn-txt">истекает',
            template,
        )

    def test_desktop_footer_reuses_landing_links(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn('class="dt-cabinet-footer"', template)
        self.assertIn("{% url 'offer' %}", template)
        self.assertIn("{% url 'terms' %}", template)
        self.assertIn("{% url 'privacy' %}", template)
        # Футер не рендерится в Mini App.
        footer_index = template.index('class="dt-cabinet-footer"')
        guard_index = template.rindex("{% if not tg_webapp_mode %}", 0, footer_index)
        self.assertLess(footer_index - guard_index, 300)

    def test_secnum_plates_number_the_tabs_like_landing(self):
        css = Path("engine/static/css/cabinet-desktop.css").read_text()

        self.assertIn('.dt-devices-tab .desktop-devices-header h2::before { content: "01"; }', css)
        self.assertIn('#setup-flat-header h1::before { content: "02"; }', css)
        self.assertIn('#tab-profile .referral-mobile-header h1::before { content: "03"; }', css)
        self.assertIn('#tab-support > header h1::before { content: "04"; }', css)
        self.assertIn('#settings-header h1::before { content: "05"; }', css)

    def test_concept_compositions_are_wired_to_existing_functionality(self):
        template = template_source("engine/templates/dashboard.html")

        # Главная: карточка доступа 2:1 со счётчиком и прогрессом на старых id.
        self.assertIn('class="dt-access-card"', template)
        self.assertIn('id="days-left-count" class="dt-days-num"', template)
        self.assertIn('<div id="subscription-progress-bar" class="status-progress-bar"></div>', template)
        self.assertIn('id="dt-stat-devices"', template)
        # Финальный призыв с брендовым SVG лендинга.
        self.assertIn("{% static 'brand/flagship.svg' %}", template)
        # Вкладка «Устройства» — только десктоп, панель со старыми id переехала в неё.
        self.assertIn('<div id="tab-devices" class="tab-content">', template)
        self.assertIn('data-tab="devices"', template)
        self.assertIn('id="desktop-devices-list"', template)
        # Установка: этапный мастер с QR на шаге подписки.
        self.assertIn('id="dt-setup-wizard"', template)
        # Рефералы: карточка + плитки + шаги, существующие share/copy-механизмы.
        self.assertIn('class="dt-ref-grid"', template)
        self.assertIn('data-mi3-copy="{{ referral_link }}"', template)
        self.assertIn("openRefShare('tg')", template)
        # Поддержка: статус-полоса и две карточки, FAQ через существующий суб-экран.
        self.assertIn('class="dt-status-strip"', template)
        self.assertIn('class="dt-support-grid"', template)
        self.assertIn('onclick="openSettingsFaq()" class="desktop-secondary-action"', template)
        # Быстрые действия Главной: установка, текущее устройство, платежи.
        self.assertIn('class="dt-quick-grid"', template)
        self.assertIn('class="dt-quick-card" onclick="quickAccessInstall()"', template)
        self.assertIn('class="dt-quick-card" onclick="openPaymentsHistorySheet()"', template)
        # Этапный мастер установки (UX мобильного визарда): платформа →
        # приложение → установка → подписка с QR → готово; данные installData.
        self.assertIn('id="dt-setup-wizard"', template)
        self.assertIn("dtSetupWizard", template)
        self.assertIn('id="dt-wiz-body"', template)
        self.assertIn("ШАГ ' + state.step + ' ИЗ ' + TOTAL", template)
        self.assertIn("Подключиться в 1 клик", template)
        self.assertIn('id="dt-wiz-qr"', template)
        self.assertIn("copyInputValueBtn(event, \\'dt-wiz-key\\')", template)
        self.assertIn("showTab(\\'devices\\')", template)
        # Логотип шапки с маркой лендинга и подсветка курсора.
        self.assertIn('class="dt-brand-mark"', template)
        self.assertIn("{% static 'icons/monkey-island-logo-animated.webp' %}", template)
        self.assertIn('id="dt-cursor-glow"', template)

    def test_home_status_uses_rwms_panel_data(self):
        template = template_source("engine/templates/dashboard.html")

        # Статус панели говорит прямо: DISABLED — подписка отключена,
        # LIMITED — достигнут лимит трафика (+ когда сбросится автоматически).
        self.assertIn('{% if rw_status == "DISABLED" %}Подписка отключена', template)
        self.assertIn("ваша подписка отключена", template)
        self.assertIn("Достигнут лимит трафика", template)
        self.assertIn("Лимит сбросится автоматически {{ rw_traffic_reset_at|date:\"j E\" }}", template)
        self.assertIn("Автосброса нет — лимит снимется после оплаты подписки.", template)
        # Стат-плитки: трафик (лимит/всего), email-аккаунт.
        self.assertIn("{{ rw_traffic_limit_used_gb|floatformat:1 }}", template)
        self.assertIn("использовано всего {{ rw_traffic_total_gb|floatformat:1 }} ГБ", template)
        self.assertIn('class="dt-stat-num dt-stat-email"', template)

    def test_traffic_limit_next_reset_math(self):
        from engine.views import traffic_limit_next_reset

        now = datetime(2026, 9, 10, 12, 0, tzinfo=dt_timezone.utc)  # четверг

        self.assertIsNone(traffic_limit_next_reset("no_reset", now=now))
        self.assertIsNone(traffic_limit_next_reset(None, now=now))
        self.assertEqual(
            traffic_limit_next_reset("day", now=now).date(), date(2026, 9, 11)
        )
        # Ближайший понедельник.
        self.assertEqual(
            traffic_limit_next_reset("week", now=now).date(), date(2026, 9, 14)
        )
        self.assertEqual(
            traffic_limit_next_reset("month", now=now).date(), date(2026, 10, 1)
        )
        # month_rolling: по числу даты создания; 31-е клампится к концу месяца.
        self.assertEqual(
            traffic_limit_next_reset(
                "month_rolling", created_at_date=date(2026, 1, 15), now=now
            ).date(),
            date(2026, 9, 15),
        )
        self.assertEqual(
            traffic_limit_next_reset(
                "month_rolling", created_at_date=date(2026, 1, 5), now=now
            ).date(),
            date(2026, 10, 5),
        )
        self.assertEqual(
            traffic_limit_next_reset(
                "month_rolling",
                created_at_date=date(2026, 1, 31),
                now=datetime(2026, 2, 10, tzinfo=dt_timezone.utc),
            ).date(),
            date(2026, 2, 28),
        )

    def test_desktop_footer_sticks_to_viewport_bottom(self):
        css = Path("engine/static/css/cabinet-desktop.css").read_text()

        wrapper_rule = css.split("#interface-wrapper {")[1].split("}")[0]
        self.assertIn("min-height: 100vh", wrapper_rule)
        # hideTariffs снимает inline-display (''), а не ставит 'block' —
        # инлайновый block перебивал десктопный flex и отклеивал футер.
        template = template_source("engine/templates/dashboard.html")
        self.assertIn(
            "document.getElementById('interface-wrapper').style.display = '';",
            template,
        )
        self.assertNotIn(
            "document.getElementById('interface-wrapper').style.display = 'block';",
            template,
        )
        self.assertIn("flex-direction: column", wrapper_rule)
        self.assertIn(".dt-cabinet-footer { margin-top: auto;", css)
        # На десктопе плоский флоу установки скрыт — работает этапный мастер.
        self.assertIn("#setup-logic-placeholder { display: none !important; }", css)
        self.assertIn(".dt-wiz-shell", css)
        self.assertIn(".dt-wiz-chip.is-active", css)
        self.assertIn("#dt-cursor-glow", css)


class AdminDashboardTemplateTests(SimpleTestCase):
    def test_large_loading_state_uses_payment_status_orb_spinner(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn(".admin-loading-state", template)
        self.assertIn(".admin-loading-orb", template)
        self.assertIn(".admin-loading-orb-pulse", template)
        self.assertIn(".admin-loading-orb-ring-outer", template)
        self.assertIn(".admin-loading-orb-ring-inner", template)
        self.assertIn(".admin-loading-orb-center", template)
        self.assertIn(".admin-loading-caption", template)
        self.assertIn("admin-orb-spin-cw", template)
        self.assertIn("admin-orb-spin-ccw", template)
        self.assertIn('role="status"', template)
        self.assertIn("const safeText = escapeHtml(text);", template)
        self.assertIn('<p class="admin-loading-caption">${safeText}</p>', template)
        self.assertNotIn("card info-card admin-loading", template)
        self.assertNotIn("Запрос отправлен", template)
        self.assertNotIn("Ожидаем ответ сервера", template)
        self.assertNotIn("Обновляем данные", template)
        self.assertNotIn(
            '<div class="card info-card muted">${text}</div>',
            template,
        )

    def test_apple_recommended_app_is_grouped_with_common_runtime_settings(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("'apple_recommended_app'", template)
        self.assertIn(
            "'technical_work_enabled', 'apple_recommended_app'",
            template,
        )

    def test_admin_dashboard_loads_black_gold_control_skin(self):
        template = template_source("engine/templates/admin_dashboard.html")
        stylesheet = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn("family=Manrope", template)
        self.assertIn("{% static 'css/admin_dashboard.css' %}", template)
        self.assertIn("/* Black Gold Control", stylesheet)

        for token in (
            "--admin-accent: #ffc700;",
            "--admin-highlight: #fff2b3;",
            "--admin-surface: #111217;",
            "--admin-text: #fff;",
        ):
            self.assertIn(token, stylesheet)

        for selector in (
            ".admin-table-row.table-head",
            ".sources-table-row",
            ".payments-list-row",
            ".user-payments-row",
            ".node-traffic-row",
            ".cohort-table th",
            ".sources-row-action",
        ):
            self.assertIn(selector, stylesheet)

        self.assertIn("min-height: 50px;", stylesheet)
        self.assertIn("font-size: 13px;", stylesheet)
        self.assertIn("font-variant-numeric: tabular-nums;", stylesheet)
        self.assertIn("text-transform: uppercase;", stylesheet)
        self.assertIn("position: sticky;", stylesheet)
        self.assertIn("border: 1px solid rgba(var(--accent-rgb), .34);", stylesheet)
        self.assertIn("@media (max-width: 640px)", stylesheet)
        self.assertIn(":where(button, a, input, select, textarea):focus-visible", stylesheet)

    def test_light_theme_table_headers_have_distinct_surface(self):
        stylesheet = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn(
            'html[data-admin-theme="light"] .admin-table-row.table-head,',
            stylesheet,
        )
        self.assertIn("background: #e5e9ee;", stylesheet)
        self.assertIn("color: #59616d;", stylesheet)
        self.assertIn("border-color: rgba(29, 34, 42, .18);", stylesheet)
        self.assertIn(
            "box-shadow: inset 0 -1px 0 rgba(29, 34, 42, .08);",
            stylesheet,
        )

    def test_censor_checks_have_dedicated_mobile_layout(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn(
            "#panel-censor-checks .censor-check-form { grid-template-columns: minmax(0, 1fr); }",
            template,
        )
        self.assertIn("#panel-censor-checks .censor-check-row > span::before", template)
        self.assertIn("#panel-censor-checks .censor-key-row > span::before", template)
        self.assertIn('data-label="Последний замер"', template)
        self.assertIn('class="censor-row-actions" data-label="Действия"', template)

    def test_censor_check_form_fields_do_not_inherit_row_flex_basis(self):
        """Старые flex-правила инпутов формы замеров ТСПУ задавали flex-basis
        130-240 px для строчной раскладки; внутри колоночного label концепта
        это превращалось в высоту поля. Концептный CSS обязан это гасить."""
        stylesheet = Path("engine/static/css/admin-concept-infrastructure.css").read_text()
        template = template_source("engine/templates/admin_dashboard.html")
        concept_index = Path("engine/static/css/admin-concept.css").read_text()

        self.assertIn('.censor-check-form .input[name="mode"] { flex: 2 1 240px;', template)
        self.assertIn(".infra-concept-field {\n    display: flex; flex-direction: column;", stylesheet)
        rule_start = stylesheet.index("#panel-censor-checks .infra-concept-field .input,")
        rule = stylesheet[rule_start:stylesheet.index("}", rule_start)]
        self.assertIn("flex: 0 0 auto;", rule)
        self.assertIn("height: auto;", rule)
        self.assertIn("@import url('./admin-concept-infrastructure.css?v=13');", concept_index)

    def test_censor_checks_allow_selecting_rows_and_deleting_them(self):
        template = template_source("engine/templates/admin_dashboard.html")
        for marker in (
            'data-censor-select="${check.id}"',
            "data-censor-select-all",
            "data-censor-bulk-delete",
            "data-censor-bulk-clear",
            "body.append('action', 'delete_many');",
            "function censorSyncBulkBar(container)",
            '<label class="censor-pick" data-label="Выделить все">',
            "#panel-censor-checks .censor-check-row.table-head > span:not(.censor-pick) { display: none; }",
            ".censor-check-row { grid-template-columns: 34px minmax(130px, .8fr)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, template)

    def test_censor_checks_offer_safe_bulk_settings_form(self):
        template = template_source("engine/templates/admin_dashboard.html")
        stylesheet = Path("engine/static/css/admin_dashboard.css").read_text()

        censor_panel = template[template.index('<section id="panel-censor-checks"'):]
        bulk_position = censor_panel.index('<details class="card censor-bulk-card">')
        single_form_position = censor_panel.index('<form id="censor-check-form"')
        self.assertLess(bulk_position, single_form_position)
        self.assertIn('<summary class="censor-bulk-summary">', censor_panel)
        self.assertNotIn('<details class="card censor-bulk-card" open>', censor_panel)
        self.assertIn('id="censor-bulk-form"', template)
        for flag_name in (
            "apply_mode",
            "apply_api_key",
            "apply_interval",
            "apply_alerts",
            "apply_enabled",
        ):
            self.assertIn(f'name="{flag_name}"', template)
        self.assertIn('id="censor-bulk-key"', template)
        self.assertIn("function submitCensorBulk(event)", template)
        self.assertIn("body.append('action', 'bulk_update')", template)
        self.assertIn("Текущие и завершённые замеры не изменятся", template)
        self.assertIn(".censor-bulk-grid", stylesheet)
        self.assertIn(".censor-bulk-field.is-selected", stylesheet)
        self.assertIn(".censor-bulk-card[open] > .censor-bulk-summary", stylesheet)

    def test_runtime_setting_actions_use_modern_tonal_buttons(self):
        template = template_source("engine/templates/admin_dashboard.html")
        stylesheet = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn('class="setting-action setting-action-save"', template)
        self.assertIn('class="setting-action setting-action-reset"', template)
        self.assertIn('<i class="fas fa-check"></i><span>Сохранить</span>', template)
        self.assertIn('<i class="fas fa-arrow-rotate-left"></i><span>Сбросить</span>', template)
        self.assertNotIn('value="save" title="Сохранить"><i class="fas fa-floppy-disk"', template)
        self.assertIn(".setting-action-save:hover", stylesheet)
        self.assertIn(".setting-action-reset:hover", stylesheet)
        self.assertIn(".setting-row .setting-action", stylesheet)

    def test_runtime_settings_use_responsive_cross_browser_property_grid(self):
        template = template_source("engine/templates/admin_dashboard.html")
        stylesheet = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn("function settingControlHtml(setting)", template)
        self.assertIn("function settingsListHtml(rows)", template)
        self.assertIn("function updateSettingDirtyState(control)", template)
        self.assertIn('class="settings-list-head"', template)
        self.assertIn("Источник и действия", template)
        self.assertIn("data-setting-control", template)
        self.assertIn("data-initial-value", template)
        self.assertIn("setting-select-shell", template)
        self.assertIn("setting.sensitive ? 'password'", template)
        self.assertIn("form.classList.toggle('is-dirty', isDirty)", template)
        self.assertIn("saveButton.disabled = !isDirty", template)
        self.assertIn("container.classList.toggle('has-settings'", template)

        self.assertIn(".settings-list.has-settings", stylesheet)
        self.assertIn(
            "grid-template-columns: minmax(220px, 1.15fr) minmax(220px, 1fr) minmax(270px, .9fr);",
            stylesheet,
        )
        self.assertIn("-webkit-appearance: none;", stylesheet)
        self.assertIn("-moz-appearance: none;", stylesheet)
        self.assertIn('.setting-control[type="number"]::-webkit-inner-spin-button', stylesheet)
        self.assertIn("-webkit-backdrop-filter: blur(20px);", stylesheet)
        self.assertIn("@media (max-width: 900px)", stylesheet)
        self.assertIn("box-shadow: inset 2px 0 0 var(--admin-accent);", stylesheet)
        self.assertIn(".setting-value-editor {\n    padding-right: 18px;", stylesheet)
        self.assertIn(".setting-value-editor {\n        padding-right: 0;", stylesheet)

    def test_payment_search_result_is_rendered_before_payment_history(self):
        template = template_source("engine/templates/admin_dashboard.html")

        # Порядок вкладки «Платежи»: форма поиска → результат поиска →
        # сворачиваемая история платежей (детали найденного платежа важнее
        # общей ленты).
        payment_panel = template[template.index('<section id="panel-payment-info"'):]
        search_position = payment_panel.index('<form id="payment-info-form"')
        result_position = payment_panel.index('<div id="payment-info-result"')
        history_position = payment_panel.index('<details id="payments-history-details"')

        self.assertLess(search_position, result_position)
        self.assertLess(result_position, history_position)

    def test_expandable_controls_have_consistent_chevron_affordances(self):
        template = template_source("engine/templates/admin_dashboard.html")
        stylesheet = Path("engine/static/css/admin_dashboard.css").read_text()

        for selector in (
            "details > summary::marker",
            "details > summary::-webkit-details-marker",
            "details[class] > summary::after",
            "details[class][open] > summary::after",
            "details[class] > summary:focus-visible",
            ".date-trigger::after",
            ".date-field.open .date-trigger::after",
            "select:not(.setting-control)",
        ):
            self.assertIn(selector, stylesheet)

        self.assertIn("function setDatePickerExpanded(field, isExpanded)", template)
        self.assertIn("trigger.setAttribute('aria-controls', popover.id)", template)
        self.assertIn("trigger.setAttribute('aria-haspopup', 'dialog')", template)
        self.assertIn("setDatePickerExpanded(field, shouldOpen)", template)
        self.assertIn("background-image: url(\"data:image/svg+xml", stylesheet)

    def test_client_tab_has_session_only_start_state_and_recent_searches(self):
        template = template_source("engine/templates/admin_dashboard.html")
        stylesheet = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn("function renderClientStartState()", template)
        self.assertIn("Начните с поиска клиента", template)
        self.assertIn("Недавно просмотренные", template)
        self.assertIn("Как искать", template)
        self.assertIn("<kbd>Enter</kbd>", template)
        self.assertIn("CLIENT_RECENT_STORAGE_KEY", template)
        self.assertIn("sessionStorage.getItem(CLIENT_RECENT_STORAGE_KEY)", template)
        self.assertIn("sessionStorage.setItem(CLIENT_RECENT_STORAGE_KEY", template)
        self.assertIn("sessionStorage.removeItem(CLIENT_RECENT_STORAGE_KEY)", template)
        self.assertNotIn("localStorage.getItem(CLIENT_RECENT_STORAGE_KEY)", template)
        self.assertIn("clients.slice(0, 5)", template)
        self.assertIn("rememberRecentClient(result.user)", template)
        self.assertIn("data-client-recent-query", template)
        self.assertIn("form.requestSubmit()", template)
        self.assertIn("renderClientStartState();", template)

        for selector in (
            ".client-start-state",
            ".client-start-hero",
            ".client-start-grid",
            ".client-recent-button",
            ".client-search-hints",
            ".client-enter-hint kbd",
        ):
            self.assertIn(selector, stylesheet)

    def test_acquisition_explains_new_revenue_calculation(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('id="acq-new-revenue-help"', template)
        self.assertIn("Как считаются «Новые покупатели» и «Выручка новых»", template)
        self.assertIn("самый ранний успешный платёж за всю доступную историю Wata и YooKassa", template)
        self.assertIn("Это не LTV пришедших за период", template)
        self.assertIn("не хранится как зафиксированный снимок", template)
        self.assertIn("Это сопоставление недельных итогов, а не строгая когортная конверсия", template)

    def test_winback_table_shows_conversion_segments(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("'Новые', 'Повторные', 'Медиана до оплаты', 'Выручка', 'Чеки'", template)
        self.assertIn("Last-touch атрибуция", template)
        self.assertIn("«Новые» — это была первая оплата пользователя за всю историю", template)


YANDEX_DIRECT_CSV_SAMPLE = """День,Показы,Клики,"Расход, ₽",Конверсии,"CR, %","CPA, ₽","CPC, ₽"
Итого,406662,32520,204336.59,0,0.00,,6.28
29.06.2026,13773,1023,5030.16,0,0.00,-,4.92
30.06.2026,13422,1057,5154.73,0,0.00,-,4.88
01.07.2026,17860,1478,7261.73,0,0.00,-,4.91
"""


class YandexDirectCsvParserTests(SimpleTestCase):
    def test_parses_daily_rows_and_skips_totals(self):
        rows = _parse_yandex_direct_csv(YANDEX_DIRECT_CSV_SAMPLE)

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {
            "day": date(2026, 6, 29),
            "amount_rub": 5030.16,
            "impressions": 13773,
            "clicks": 1023,
        })
        self.assertEqual(rows[2]["day"], date(2026, 7, 1))
        self.assertEqual(rows[2]["amount_rub"], 7261.73)

    def test_reparse_is_deterministic_for_overwrite(self):
        # Повторная загрузка того же файла должна дать ровно те же строки —
        # upsert по (day, channel) перезапишет дни теми же значениями.
        self.assertEqual(
            _parse_yandex_direct_csv(YANDEX_DIRECT_CSV_SAMPLE),
            _parse_yandex_direct_csv(YANDEX_DIRECT_CSV_SAMPLE),
        )

    def test_parses_russian_number_formats_and_report_preamble(self):
        text = (
            "Статистика по дням\n"
            "\n"
            "Дата,Показы,Клики,\"Расход (руб.)\"\n"
            "05.07.2026,\"1 500\",\"1 023\",\"5 030,16\"\n"
        )
        rows = _parse_yandex_direct_csv(text)

        self.assertEqual(rows, [{
            "day": date(2026, 7, 5),
            "amount_rub": 5030.16,
            "impressions": 1500,
            "clicks": 1023,
        }])

    def test_returns_empty_without_direct_header(self):
        self.assertEqual(_parse_yandex_direct_csv("a,b,c\n1,2,3\n"), [])

    def test_number_parser_edge_cases(self):
        self.assertEqual(_parse_direct_number("204336.59"), 204336.59)
        self.assertEqual(_parse_direct_number("5 030,16"), 5030.16)
        self.assertEqual(_parse_direct_number("1,234.56"), 1234.56)
        self.assertIsNone(_parse_direct_number("-"))
        self.assertIsNone(_parse_direct_number(""))
        self.assertIsNone(_parse_direct_number(None))


class AcquisitionTrialsTests(SimpleTestCase):
    @mock.patch("engine.views._acq_rows")
    def test_window_passed_to_sql_and_echoed(self, rows_mock):
        rows_mock.return_value = [
            {"day": date(2026, 7, 10), "trials": 655, "converted": 14},
        ]

        result = _acq_trials(object(), 60, 30)

        self.assertEqual(result["window_days"], 30)
        _, kwargs = rows_mock.call_args
        self.assertEqual(kwargs["window_days"], 30)
        sql = rows_mock.call_args[0][1]
        self.assertIn("make_interval(days => :window_days)", sql)
        self.assertEqual(result["days"][0]["conv_pct"], 2.1)

    @mock.patch("engine.views._acq_rows")
    def test_unknown_window_falls_back_to_10(self, rows_mock):
        rows_mock.return_value = []

        result = _acq_trials(object(), 60, 45)

        self.assertEqual(result["window_days"], 10)
        _, kwargs = rows_mock.call_args
        self.assertEqual(kwargs["window_days"], 10)

    @mock.patch("engine.views._acq_rows")
    def test_default_window_is_10(self, rows_mock):
        rows_mock.return_value = []

        result = _acq_trials(object(), 60)

        self.assertEqual(result["window_days"], 10)

    @mock.patch("engine.views._acq_rows")
    def test_zero_trials_does_not_divide_by_zero(self, rows_mock):
        rows_mock.return_value = [
            {"day": date(2026, 7, 10), "trials": 0, "converted": 0},
        ]

        result = _acq_trials(object(), 60, 10)

        self.assertEqual(result["days"][0]["conv_pct"], 0)


class AcquisitionExpiryTests(SimpleTestCase):
    """«Привлечение → Окончания и продления»: концы периодов восстанавливаются
    из цепочки оплат со стакованием, последний — по реальному expire_at;
    продление — следующая оплата не позже окна после конца (в том числе
    досрочная); пробные — по expire_at без оплат и по subscription_created."""

    NOW = datetime(2026, 9, 15, 12, 0)  # UTC; МСК-сегодня 15.09
    TODAY = date(2026, 9, 15)

    def _periods(self, payments, expires=None, trial_starts=None, autopay=()):
        from engine.views import _expiry_periods

        return _expiry_periods(payments, expires or {}, trial_starts or {}, 7, set(autopay))

    def test_periods_stack_from_previous_end_and_use_real_expire_for_last(self):
        payments = [
            (1, datetime(2026, 6, 1), "month"),  # до 01.07
            (1, datetime(2026, 6, 28), "month"),  # досрочно: 01.07 + 30 = 31.07
            (1, datetime(2026, 8, 5), "threemonths"),  # после паузы: 05.08 + 90
        ]
        expires = {1: datetime(2026, 11, 10)}  # реальный срок с бонусом
        periods = self._periods(payments, expires)
        ends = [(p[1], p[2], p[3], p[5]) for p in periods]
        self.assertEqual(
            ends,
            [
                (datetime(2026, 7, 1), "month", datetime(2026, 6, 28), "month"),
                (datetime(2026, 7, 31), "month", datetime(2026, 8, 5), "threemonths"),
                (datetime(2026, 11, 10), "threemonths", None, None),
            ],
        )

    def test_real_expire_before_last_payment_is_ignored(self):
        # expire_at раньше последней оплаты — рассинхрон, берём симуляцию.
        periods = self._periods([(1, datetime(2026, 9, 1), "month")], {1: datetime(2026, 8, 1)})
        self.assertEqual(periods[0][1], datetime(2026, 10, 1))

    def test_trial_periods_for_payers_and_non_payers(self):
        payments = [(1, datetime(2026, 9, 5), "month")]
        expires = {1: datetime(2026, 10, 5), 2: datetime(2026, 9, 20)}
        trial_starts = {1: datetime(2026, 9, 1)}
        periods = self._periods(payments, expires, trial_starts)
        trial = [p for p in periods if p[2] == "trial"]
        self.assertEqual(
            sorted((p[0], p[1], p[3], p[5]) for p in trial),
            [(1, datetime(2026, 9, 8), datetime(2026, 9, 5), "month"), (2, datetime(2026, 9, 20), None, None)],
        )
        # Оплата в пробный период стакуется от конца пробного: 08.09 + 30.
        self.assertIn((1, datetime(2026, 10, 5), "month", None, False, None), periods)

    def test_unknown_tariff_falls_back_to_other(self):
        periods = self._periods([(1, datetime(2026, 9, 1), "")])
        self.assertEqual(periods[0][2], "other")
        self.assertEqual(periods[0][1], datetime(2026, 10, 1))

    def test_aggregate_renewed_pending_future_and_autopay(self):
        from engine.views import _expiry_aggregate

        periods = [
            (1, datetime(2026, 9, 1, 10), "month", datetime(2026, 8, 30), False, "month"),  # досрочное продление
            (2, datetime(2026, 9, 1, 11), "month", datetime(2026, 9, 20), False, "year"),  # позже, но в окне 30
            (3, datetime(2026, 9, 1, 12), "month", None, False, None),  # окно 30 ещё открыто (сегодня 15.09)
            (4, datetime(2026, 8, 1, 12), "month", None, False, None),  # окно закрыто — отвал
            (5, datetime(2026, 8, 1, 12), "month", datetime(2026, 9, 10), False, "month"),  # позже окна — возврат, не продление
            (6, datetime(2026, 9, 20, 12), "year", None, True, None),  # будущее с автоплатежом
            (7, datetime(2026, 9, 20, 12), "trial", None, True, None),  # пробный: автоплатёж не считается
            (8, datetime(2026, 12, 1), "month", None, False, None),  # вне диапазона
            (9, datetime(2026, 8, 2, 12), "trial", datetime(2026, 8, 3), False, "month"),  # пробный -> месяц, окно закрыто
        ]
        tariffs, rows, totals = _expiry_aggregate(
            periods, date(2026, 8, 1), date(2026, 9, 30), "day", 30, self.NOW, self.TODAY
        )
        self.assertEqual(tariffs, ["trial", "month", "year"])
        # Переходы — только по закрытому окну: 01.09 ещё открыт, туда не попадает.
        self.assertEqual(totals["transitions"], {"trial": {"month": 1}})
        self.assertEqual(totals["churned"], {"month": 2})
        by_key = {r["key"]: r for r in rows}
        self.assertEqual(len(rows), 61)
        sep1 = by_key["2026-09-01"]
        self.assertEqual(sep1["ending"], {"month": 3})
        self.assertEqual(sep1["renewed"], {"month": 2})
        self.assertEqual(sep1["pending"], {"month": 1})
        self.assertTrue(sep1["is_past"])
        aug1 = by_key["2026-08-01"]
        self.assertEqual(aug1["ending"], {"month": 2})
        self.assertEqual(aug1["renewed"], {})
        self.assertEqual(aug1["pending"], {})
        sep20 = by_key["2026-09-20"]
        self.assertFalse(sep20["is_past"])
        self.assertEqual(sep20["ending"], {"year": 1, "trial": 1})
        self.assertEqual(sep20["autopay"], {"year": 1})
        self.assertTrue(by_key["2026-09-15"]["is_current"])
        # Окно 30 дн.: у 01.08 закрыто (01.08+30 < 15.09), у 01.09 — нет.
        self.assertTrue(aug1["window_closed"])
        self.assertFalse(sep1["window_closed"])
        self.assertEqual(totals["ending_closed"], 3)
        self.assertEqual(totals["renewed_closed"], 1)
        self.assertEqual(totals["ending"], 8)
        self.assertEqual(totals["ending_past"], 6)
        self.assertEqual(totals["ending_future"], 2)
        self.assertEqual(totals["renewed"], 3)
        self.assertEqual(totals["pending"], 1)
        self.assertEqual(totals["autopay_future"], 1)
        self.assertEqual(
            totals["by_tariff"]["month"],
            {"ending": 5, "renewed": 2, "pending": 1, "autopay": 0, "subscriptions": 5,
             # Закрытое окно у month: два окончания 01.08, оба без продления;
             # единственное закрытое продление — пробный → month.
             "ending_closed": 2, "renewed_closed": 0},
        )
        # Чипы и строка «Все платные тарифы» строятся из этих же полей.
        self.assertEqual(
            sum(slot["ending_closed"] for slot in totals["by_tariff"].values()),
            totals["ending_closed"],
        )
        self.assertEqual(
            sum(slot["renewed_closed"] for slot in totals["by_tariff"].values()),
            totals["renewed_closed"],
        )
        # 8 периодов в диапазоне у 8 разных пользователей.
        self.assertEqual(totals["subscriptions"], 8)

    def test_window_changes_verdict(self):
        from engine.views import _expiry_aggregate

        periods = [(2, datetime(2026, 9, 1, 11), "month", datetime(2026, 9, 20), False, "month")]
        _, rows, _ = _expiry_aggregate(periods, date(2026, 9, 1), date(2026, 9, 1), "day", 7, self.NOW, self.TODAY)
        self.assertEqual(rows[0]["renewed"], {})
        self.assertEqual(rows[0]["pending"], {})

    def test_msk_day_and_week_buckets(self):
        from engine.views import _expiry_aggregate

        # 31.08 22:30 UTC = 01.09 01:30 МСК; неделя 31.08–06.09 (понедельник 31.08).
        periods = [(1, datetime(2026, 8, 31, 22, 30), "month", None, False, None)]
        _, rows, _ = _expiry_aggregate(periods, date(2026, 9, 1), date(2026, 9, 1), "day", 30, self.NOW, self.TODAY)
        self.assertEqual(rows[0]["ending"], {"month": 1})
        _, rows, _ = _expiry_aggregate(periods, date(2026, 9, 2), date(2026, 9, 16), "week", 30, self.NOW, self.TODAY)
        self.assertEqual([r["key"] for r in rows], ["2026-08-31", "2026-09-07", "2026-09-14"])
        self.assertEqual(rows[0]["ending"], {"month": 1})
        self.assertTrue(rows[0]["is_past"])
        self.assertTrue(rows[2]["is_current"])
        self.assertFalse(rows[2]["is_past"])

    @mock.patch("engine.views._expiry_load_trial_days", return_value=7)
    @mock.patch("engine.views._expiry_load_autopay", return_value=set())
    @mock.patch("engine.views._expiry_load_trial_starts", return_value={})
    @mock.patch("engine.views._expiry_load_expires", return_value={})
    @mock.patch("engine.views._expiry_load_payments", return_value=[])
    def test_section_defaults_and_param_validation(self, *_mocks):
        from engine.views import ACQ_SECTIONS, _acq_expirations, _expiry_cache_clear

        _expiry_cache_clear()
        self.addCleanup(_expiry_cache_clear)
        self.assertIn("expirations", ACQ_SECTIONS)
        res = _acq_expirations(object())
        self.assertEqual(res["cache_ttl"], 300)
        self.assertEqual(res["group"], "day")
        self.assertEqual(res["window_days"], 30)
        self.assertEqual(len(res["buckets"]), 61)
        self.assertEqual(res["tariffs"], [])
        res = _acq_expirations(object(), "2026-09-01", "2026-09-30", "week", "x")
        self.assertEqual(res["group"], "week")
        self.assertEqual(res["window_days"], 30)
        self.assertEqual(_acq_expirations(object(), None, None, "day", "7")["window_days"], 7)
        with self.assertRaises(ValueError):
            _acq_expirations(object(), "2026-09-30", "2026-09-01")
        with self.assertRaises(ValueError):
            _acq_expirations(object(), "2025-01-01", "2026-09-01")
        with self.assertRaises(ValueError):
            _acq_expirations(object(), "вчера", "2026-09-01")

    @mock.patch("engine.views._expiry_load_trial_days", return_value=7)
    @mock.patch("engine.views._expiry_load_autopay", return_value=set())
    @mock.patch("engine.views._expiry_load_trial_starts", return_value={})
    @mock.patch("engine.views._expiry_load_expires", return_value={})
    @mock.patch("engine.views._expiry_load_payments", return_value=[])
    def test_periods_are_cached_between_requests(self, payments_mock, *_mocks):
        """Дорогая сборка периодов делается раз в EXPIRY_CACHE_TTL: смена
        диапазона/шага не ходит в БД, refresh=1 пересобирает."""
        from engine.views import _acq_expirations, _expiry_cache_clear

        _expiry_cache_clear()
        self.addCleanup(_expiry_cache_clear)
        _acq_expirations(object(), "2026-09-01", "2026-09-30")
        _acq_expirations(object(), "2026-08-01", "2026-09-30", "week", "7")
        self.assertEqual(payments_mock.call_count, 1)
        _acq_expirations(object(), "2026-09-01", "2026-09-30", refresh=True)
        self.assertEqual(payments_mock.call_count, 2)

    def test_template_has_expiry_subtab_chart_and_table(self):
        template = template_source("engine/templates/admin_dashboard.html")
        for needle in (
            'data-subtab="acq-expiry"',
            'id="subpanel-acq-expiry"',
            'id="acq-expiry-chart"',
            'id="acq-expiry-table"',
            'id="acq-expiry-tariffs"',
            'id="acq-expiry-window"',
            'data-expiry-group="week"',
            'data-expiry-preset="around30"',
            'data-help="expiry"',
            "'acq-expiry': loadExpiry",
            "acqFetch('expirations'",
            "marker: todayIndex >= 0",
            "shade: {from: 0, to: pastEnd}",
            "hideLegend: true",
            "hidden: new Set(['trial'])",
            'id="acq-expiry-loading"',
            'id="acq-expiry-transitions"',
            "function renderExpiryTransitions(res)",
            "res.totals.transitions",
            "loading.hidden = false;",
            "row.window_closed ? expiryPct(renewed, ending)",
            "expiry: {",
            "Окончания и продления по тарифам",
            # Матрица переходов не зависит от чипов легенды графика: строка
            # есть у каждого тарифа с окончаниями за дни с закрытым окном
            # (иначе спрятанные для читаемости 3/6/12 месяцев пропадали).
            "const fromKeys = order.filter((k) => (transitions[k] && Object.keys(transitions[k]).length) || churned[k]);",
            "function expiryClosedTo(res)",
            "acq-expiry-matrix-note",
            "уже закрыто); тарифы, чьи периоды за эти дни не заканчивались, строкой не показаны.",
            "Чипы легенды графика на матрицу не влияют",
            # Процент продлений тарифа в чипе и общий по всем тарифам.
            "acq-expiry-chip-pct",
            "expiryPct(total.renewed_closed, total.ending_closed)",
            "function expiryOverallHtml(res)",
            "Все платные тарифы: <b>",
            "с пробным: <b>",
            "card('Продлились по выбранным тарифам'",
        ):
            self.assertIn(needle, template, needle)
        self.assertNotIn("order.filter((k) => !expiryState.hidden.has(k) && ((transitions[k]", template)
        css = template_source("engine/static/css/admin-concept-sections.css")
        self.assertIn(".acq-expiry-table tr.is-today", css)
        self.assertIn('.acq-expiry-chip[aria-pressed="false"]', css)
        # Заголовки числовых колонок над цифрами: общее правило таблиц
        # «Привлечения» (два id в селекторе) ставило им text-align: left.
        self.assertIn("#panel-acquisition .acq-expiry-table thead th:not(:first-child) { text-align: right !important; }", css)
        self.assertIn(".acq-expiry-matrix-note", css)


class AcquisitionCohortPathTests(SimpleTestCase):
    """«Привлечение → Путь когорты»: один человек в одной строке на всех
    шагах — подписка, подключение, покупка, 1/2/3-е продление, деньги."""

    NOW = datetime(2026, 9, 15, 12, 0)
    TODAY = date(2026, 9, 15)

    def test_cohort_rows_follow_users_through_steps(self):
        from engine.views import _acq_cohort_path_rows

        signups = {
            1: datetime(2026, 7, 6, 10),  # неделя 06.07: купил, продлил 1-й, 2-й не продлил
            2: datetime(2026, 7, 7, 10),  # неделя 06.07: купил, не продлил
            3: datetime(2026, 7, 8, 10),  # неделя 06.07: подключился, не купил
            4: datetime(2026, 7, 9, 10),  # неделя 06.07: ничего
            5: datetime(2026, 9, 14, 10),  # текущая неделя: не дозрела
        }
        connected = {1, 2, 3}
        payments = [
            (1, datetime(2026, 7, 7), "month", 249.0, "yk", False),
            (1, datetime(2026, 8, 5), "threemonths", 599.0, "yk", False),
            (2, datetime(2026, 7, 8), "month", 249.0, "wata", False),
        ]
        periods = [
            (1, datetime(2026, 8, 6), "month", datetime(2026, 8, 5), False, "threemonths"),  # 1-й период: продлён досрочно, окно закрыто
            (1, datetime(2026, 11, 4), "threemonths", None, False, None),  # 2-й период: ещё идёт
            (2, datetime(2026, 8, 7), "month", None, False, None),  # 1-й период: окно закрыто, отвал
            (1, datetime(2026, 7, 6, 10) + timedelta(days=7), "trial", datetime(2026, 7, 7), False, "month"),  # пробный не считается
        ]
        rows = _acq_cohort_path_rows(signups, connected, payments, periods, "week", 12, 30, self.NOW, self.TODAY)
        self.assertEqual(len(rows), 12)
        by = {r["cohort"]: r for r in rows}
        july = by["2026-07-06"]
        self.assertEqual((july["subs"], july["connected"], july["buyers"]), (4, 3, 2))
        self.assertEqual((july["connected_pct"], july["buyers_pct"]), (75.0, 50.0))
        self.assertTrue(july["buy_mature"])
        r1, r2, r3 = july["renewals"]
        self.assertEqual((r1["eligible"], r1["matured"], r1["renewed"], r1["pct"], r1["mature"]), (2, 2, 1, 50.0, True))
        self.assertEqual((r2["eligible"], r2["matured"], r2["renewed"], r2["pct"], r2["mature"]), (1, 0, 0, None, False))
        self.assertEqual(r3["eligible"], 0)
        self.assertEqual(july["revenue"], 1097)
        self.assertEqual(july["revenue_per_sub"], 274)
        self.assertEqual(july["revenue_per_buyer"], 548)
        self.assertEqual(july["first_tariffs"], {"month": 2})
        self.assertEqual(july["transitions"], {"month": {"threemonths": 1}})
        current = by["2026-09-14"]
        self.assertEqual(current["subs"], 1)
        self.assertFalse(current["buy_mature"])

    def test_month_cohorts_and_labels(self):
        from engine.views import _acq_cohort_path_rows

        signups = {1: datetime(2026, 8, 31, 22, 30)}  # 01.09 01:30 МСК -> когорта сентября
        rows = _acq_cohort_path_rows(signups, set(), [], [], "month", 3, 30, self.NOW, self.TODAY)
        self.assertEqual([r["cohort"] for r in rows], ["2026-07-01", "2026-08-01", "2026-09-01"])
        self.assertEqual(rows[-1]["label"], "09.2026")
        self.assertEqual(rows[-1]["subs"], 1)
        self.assertEqual(rows[-2]["subs"], 0)

    @mock.patch("engine.views._lifecycle_load_connected", return_value=set())
    @mock.patch("engine.views._lifecycle_load_signups", return_value={})
    @mock.patch("engine.views._expiry_load_trial_days", return_value=7)
    @mock.patch("engine.views._expiry_load_autopay", return_value=set())
    @mock.patch("engine.views._expiry_load_trial_starts", return_value={})
    @mock.patch("engine.views._expiry_load_expires", return_value={})
    @mock.patch("engine.views._expiry_load_payments", return_value=[])
    def test_section_defaults_and_cache(self, _p, _e, _t, _a, _d, signups_mock, _c):
        from engine.views import _acq_cohort_path, _expiry_cache_clear

        _expiry_cache_clear()
        self.addCleanup(_expiry_cache_clear)
        res = _acq_cohort_path(object())
        self.assertEqual((res["group"], res["count"], res["renewal_steps"]), ("week", 16, 3))
        self.assertEqual(len(res["cohorts"]), 16)
        res = _acq_cohort_path(object(), "month", "999")
        self.assertEqual((res["group"], res["count"]), ("month", 24))
        _acq_cohort_path(object(), "week", "8")
        self.assertEqual(signups_mock.call_count, 1)

    def test_template_replaces_funnel_with_cohort_path(self):
        template = template_source("engine/templates/admin_dashboard.html")
        for needle in (
            'data-subtab="acq-cohortpath"',
            'id="subpanel-acq-cohortpath"',
            'id="acq-cohortpath-chart"',
            'id="acq-cohortpath-table"',
            'data-cohortpath-group="month"',
            'id="acq-cohortpath-count"',
            'data-help="cohortpath"',
            "'acq-cohortpath': loadCohortPathTab",
            "acqFetch('cohort_path'",
            "function cohortDetailsHtml(row, res)",
            "cohortpath: {",
            # Недельная воронка и конверсия по дням живут на той же вкладке,
            # ниже когорт (владелец пользуется таблицей воронки и графиком).
            'id="acq-funnel-chart"',
            'id="acq-funnel-table"',
            'id="acq-trials-chart"',
            'id="acq-trials-window"',
            '<option value="10" selected>',
            'data-help="funnel"',
            'data-help="trials"',
            "Promise.allSettled([loadCohortPath(), loadFunnel(), loadTrials()])",
            "acqFetch('funnel', {weeks: 12})",
            "acqFetch('trials', {days: 60, window: windowDays})",
            "'Инвойс→Оплата'",
            "funnelPct(r.payments, r.invoice_clicks)",
            "Подкл.→100 МБ",
            "Инвойс→Оплата в воронке",
            "<b>Инвойс→Оплата</b> = все оплаты недели ÷ инвойсы той же недели.",
            "            funnel: {",
            "            trials: {",
        ):
            self.assertIn(needle, template, needle)
        # Отдельной под-вкладки «Воронка» нет.
        self.assertNotIn('data-subtab="acq-funnel"', template)
        self.assertNotIn('id="subpanel-acq-funnel"', template)

    def test_ads_cost_formatters_are_module_scoped(self):
        # fmtCost/fmtInt использует «Реклама» (сводка за период, таблица
        # аккаунтов) — раньше они жили внутри loadFunnel и после его удаления
        # сводка падала с «fmtCost is not defined».
        script = Path("engine/static/scripts/admin_dashboard-1.js").read_text()
        self.assertIn("        const fmtCost = (v) => v == null ? '—' : v.toFixed(2) + ' ₽';", script)
        self.assertIn("        const fmtInt = (v) => v == null ? '—' : v.toLocaleString('ru-RU');", script)
        self.assertIn("tile('CPC', fmtCost(res.cpc)", script)


class AdminTrafficSourceNotesTests(SimpleTestCase):
    """Подписи меток трафика: traffic_sources.name/budget подмешиваются к
    источникам в stats и правятся эндпоинтом support-admin/api/traffic-sources/."""

    class FakeSession:
        def __init__(self, rows=()):
            self.rows = {row.id: row for row in rows}
            self.added = []
            self.deleted = []
            self.committed = 0

        def query(self, model):
            rows = list(self.rows.values())

            class Q:
                def all(_self):
                    return rows

            return Q()

        def get(self, model, key):
            return self.rows.get(key)

        def add(self, row):
            if not hasattr(row, "id") or row.id is None:
                row.id = len(self.rows) + 1
            self.rows[row.id] = row
            self.added.append(row)

        def delete(self, row):
            self.rows.pop(row.id, None)
            self.deleted.append(row)

        def commit(self):
            self.committed += 1

        def close(self):
            pass

    def _request(self, method="POST", data=None):
        factory = RequestFactory()
        request = factory.post("/support-admin/api/traffic-sources/", data or {}) if method == "POST" else factory.get("/support-admin/api/traffic-sources/", data or {})
        request.session = {}
        return request

    def test_notes_attached_to_sources(self):
        from engine.views import admin_attach_source_notes

        session = self.FakeSession([SimpleNamespace(id=217, name=" Сайт, лендинг ", budget=15000)])
        sources = [{"traffic_source": "217"}, {"traffic_source": "4"}, {"traffic_source": None}]
        admin_attach_source_notes(session, sources)
        self.assertEqual((sources[0]["name"], sources[0]["budget"]), ("Сайт, лендинг", 15000))
        self.assertEqual((sources[1]["name"], sources[1]["budget"]), ("", None))
        self.assertEqual((sources[2]["name"], sources[2]["budget"]), ("", None))

    def test_endpoint_upserts_clears_and_validates(self):
        from engine import views

        session = self.FakeSession()
        with mock.patch("engine.views.require_support_admin_any", return_value=None), mock.patch(
            "engine.views.session_factory", return_value=session
        ), mock.patch("engine.views.admin_audit_write") as audit:
            response = views.support_admin_api_traffic_sources(
                self._request(data={"id": "217", "name": "Трафик с сайта", "budget": "12 000"})
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.content)["result"], {"id": 217, "name": "Трафик с сайта", "budget": 12000})
            self.assertEqual((session.rows[217].name, session.rows[217].budget), ("Трафик с сайта", 12000))
            self.assertEqual(audit.call_args[0][2], "traffic_source_note")
            # Правка существующей: имя меняется, бюджет пустой -> None.
            views.support_admin_api_traffic_sources(self._request(data={"id": "217", "name": "Директ №3"}))
            self.assertEqual((session.rows[217].name, session.rows[217].budget), ("Директ №3", None))
            # Список.
            listing = json.loads(views.support_admin_api_traffic_sources(self._request("GET")).content)
            self.assertEqual(listing["result"], [{"id": 217, "name": "Директ №3", "budget": None}])
            # Пустая подпись без бюджета удаляет строку.
            response = views.support_admin_api_traffic_sources(self._request(data={"id": "217", "name": "  "}))
            self.assertEqual(json.loads(response.content)["result"]["name"], "")
            self.assertNotIn(217, session.rows)
            self.assertEqual(audit.call_args[0][2], "traffic_source_note_clear")
            # Валидация.
            for data in ({"id": "abc", "name": "x"}, {"id": "0", "name": "x"}, {"id": "5", "name": "x", "budget": "-1"}, {"id": "5", "name": "x", "budget": "много"}):
                self.assertEqual(views.support_admin_api_traffic_sources(self._request(data=data)).status_code, 400, data)
            self.assertNotIn(5, session.rows)

    def test_template_has_inline_note_editor(self):
        template = template_source("engine/templates/admin_dashboard.html")
        for needle in (
            'data-traffic-sources-url="{% url \'support_admin_api_traffic_sources\' %}"',
            "function sourceNoteHtml(source, compact = false)",
            "function openSourceNoteEditor(value)",
            "async function saveSourceNote(form)",
            "data-source-note-edit=",
            "data-source-note-form=",
            "Что это за метка? Добавьте описание",
            "${sourceNoteHtml(source)}",
            "sourceNoteHtml(source, true)",
        ):
            self.assertIn(needle, template, needle)
        css = template_source("engine/static/css/admin-concept-customers.css")
        self.assertIn(".source-note-form", css)

    def test_route_registered(self):
        from django.urls import reverse

        self.assertTrue(reverse("support_admin_api_traffic_sources"))


class AcquisitionRevenueTests(SimpleTestCase):
    """«Привлечение → Выручка»: дневные разрезы оплат, недельная сводка с
    единым определением продления и активной базой; старый «Отвал базы»
    (45 дней по когорте месяца) удалён — определение продления одно."""

    NOW = datetime(2026, 9, 15, 12, 0)
    TODAY = date(2026, 9, 15)

    def test_revenue_days_splits_new_repeat_autopay_provider_tariff(self):
        from engine.views import _acq_revenue_days_rows

        payments = [
            (1, datetime(2026, 9, 1, 10), "month", 249.0, "yk", False),  # первая оплата
            (1, datetime(2026, 9, 1, 20, 30), "year", 1799.0, "yk", True),  # 23:30 МСК 01.09, повтор, автоплатёж
            (2, datetime(2026, 9, 1, 21, 30), "month", 249.0, "wata", False),  # 00:30 МСК 02.09
            (3, datetime(2026, 8, 1), "month", 249.0, "yk", False),  # вне диапазона, но делает 3 «старым»
            (3, datetime(2026, 9, 2, 5), "", 100.0, "wata", False),
        ]
        rows = _acq_revenue_days_rows(payments, date(2026, 9, 1), date(2026, 9, 2))
        first, second = rows
        self.assertEqual(first["revenue"], 2048)
        self.assertEqual(first["payments"], 2)
        self.assertEqual(first["avg_check"], 1024)
        self.assertEqual((first["new_rub"], first["new_payers"]), (249, 1))
        self.assertEqual((first["repeat_rub"], first["repeat_payers"]), (1799, 1))
        self.assertEqual((first["autopay_rub"], first["autopay_count"]), (1799, 1))
        self.assertEqual((first["yk_rub"], first["wata_rub"]), (2048, 0))
        self.assertEqual(first["by_tariff"], {"month": {"count": 1, "rub": 249}, "year": {"count": 1, "rub": 1799}})
        self.assertEqual(second["revenue"], 349)
        self.assertEqual((second["new_rub"], second["repeat_rub"]), (249, 100))
        self.assertEqual(second["wata_rub"], 349)
        self.assertEqual(second["by_tariff"], {"month": {"count": 1, "rub": 249}, "other": {"count": 1, "rub": 100}})
        self.assertEqual(second["weekday"], 2)

    @mock.patch("engine.views._expiry_load_trial_days", return_value=7)
    @mock.patch("engine.views._expiry_load_autopay", return_value=set())
    @mock.patch("engine.views._expiry_load_trial_starts", return_value={})
    @mock.patch("engine.views._expiry_load_expires", return_value={})
    @mock.patch("engine.views._expiry_load_payments", return_value=[])
    def test_revenue_sections_registered_and_validated(self, *_mocks):
        from engine.views import ACQ_SECTIONS, _acq_revenue_days, _expiry_cache_clear

        _expiry_cache_clear()
        self.addCleanup(_expiry_cache_clear)
        for key in ("revenue_days", "revenue_kpis", "summary", "cohort_path"):
            self.assertIn(key, ACQ_SECTIONS)
        self.assertNotIn("renew45", ACQ_SECTIONS)
        res = _acq_revenue_days(object())
        self.assertEqual(len(res["days"]), 45)
        self.assertEqual(res["tariffs"], [])
        with self.assertRaises(ValueError):
            _acq_revenue_days(object(), "2026-09-30", "2026-09-01")

    @mock.patch("engine.views._expiry_load_trial_days", return_value=7)
    @mock.patch("engine.views._expiry_load_autopay", return_value=set())
    @mock.patch("engine.views._expiry_load_trial_starts", return_value={})
    @mock.patch("engine.views._expiry_load_expires", return_value={})
    @mock.patch("engine.views._expiry_load_payments", return_value=[])
    def test_cold_revenue_loads_only_payments_and_reuses_them(self, payments_mock, expires_mock, *_mocks):
        """Холодный старт «Выручки»: график ждёт только запрос оплат;
        периоды (users + event_logs) собираются отдельно и переиспользуют
        уже загруженные оплаты, а не грузят их второй раз."""
        from engine.views import _acq_revenue_days, _acq_summary, _expiry_cache_clear

        _expiry_cache_clear()
        self.addCleanup(_expiry_cache_clear)
        _acq_revenue_days(object())
        self.assertEqual(payments_mock.call_count, 1)
        self.assertEqual(expires_mock.call_count, 0)
        _acq_summary(object())
        self.assertEqual(expires_mock.call_count, 1)
        self.assertEqual(payments_mock.call_count, 1)

    @override_settings(ACQ_CACHE_BACKGROUND=True)
    @mock.patch("engine.views._expiry_load_trial_days", return_value=7)
    @mock.patch("engine.views._expiry_load_autopay", return_value=set())
    @mock.patch("engine.views._expiry_load_trial_starts", return_value={})
    @mock.patch("engine.views._expiry_load_expires", return_value={})
    @mock.patch("engine.views._expiry_load_payments", return_value=[])
    def test_stale_cache_is_served_and_refreshed_in_background(self, payments_mock, *_mocks):
        """После TTL устаревший кэш отдаётся сразу; пересборка — одна на
        процесс, в фоне, со своей сессией (stale-while-revalidate)."""
        from engine import views

        views._expiry_cache_clear()
        self.addCleanup(views._expiry_cache_clear)
        runs = []
        with mock.patch.object(views, "_ACQ_THREAD_RUNNER", runs.append):
            views._acq_summary(object())
            self.assertEqual(payments_mock.call_count, 1)
            with views._EXPIRY_CACHE_LOCK:
                views._EXPIRY_CACHE["at"] -= views.EXPIRY_CACHE_TTL + 1
                views._EXPIRY_CACHE["payments_at"] -= views.EXPIRY_CACHE_TTL + 1
            views._acq_summary(object())
            views._acq_revenue_days(object())
            self.assertEqual(payments_mock.call_count, 1)
            self.assertEqual(len(runs), 1)
            with mock.patch.object(views, "session_factory", return_value=mock.MagicMock()):
                runs[0]()
            self.assertEqual(payments_mock.call_count, 2)
            views._acq_summary(object())
            self.assertEqual(len(runs), 1)

    @override_settings(ACQ_CACHE_BACKGROUND=True)
    def test_dashboard_open_warms_caches_once(self):
        import inspect

        from engine import views

        views._expiry_cache_clear()
        self.addCleanup(views._expiry_cache_clear)
        runs = []
        with mock.patch.object(views, "_ACQ_THREAD_RUNNER", runs.append):
            views._acq_cache_warm()
            views._acq_cache_warm()
        # Периоды и жизненный цикл — по одному фоновому прогреву, повтор не плодит.
        self.assertEqual(len(runs), 2)
        self.assertIn("_acq_cache_warm()", inspect.getsource(views.support_admin_tickets))

    def test_background_refresh_is_disabled_under_tests(self):
        # В тестах фон выключен настройкой — потоки с реальной сессией не стартуют,
        # устаревший кэш пересобирается синхронно.
        from engine import views

        self.assertFalse(settings.ACQ_CACHE_BACKGROUND)
        self.assertFalse(views._acq_background("expiry", lambda _s: None))

    def test_template_renders_revenue_blocks_independently(self):
        template = template_source("engine/templates/admin_dashboard.html")
        # График и плитки не ждут недельную сводку: три запроса рисуются
        # по мере ответов, ошибка одного блока не гасит остальные.
        self.assertIn("Promise.allSettled([daysReq, kpisReq, weeklyReq])", template)
        self.assertIn("Недельная сводка считается по периодам подписок", template)
        self.assertNotIn("Собираем оплаты и периоды — первый раз до минуты", template)

    def test_summary_weeks_revenue_renewal_and_base(self):
        from engine.views import _acq_summary_weeks

        payments = [
            (1, datetime(2026, 8, 3, 10), "month", 249.0, "yk", False),  # неделя 03.08, новая
            (1, datetime(2026, 8, 31, 10), "month", 299.0, "yk", True),  # неделя 31.08, повторная
            (2, datetime(2026, 9, 14, 10), "year", 1799.0, "yk", False),  # текущая неделя, новая
        ]
        periods = [
            (1, datetime(2026, 8, 5, 10), "month", datetime(2026, 8, 31, 10), False, "month"),  # неделя 03.08, продлён (окно закрыто)
            (5, datetime(2026, 8, 6, 10), "month", None, False, None),  # неделя 03.08, не продлён
            (1, datetime(2026, 9, 30, 10), "month", None, False, None),  # активен: покрывает конец недель 31.08, 07.09, 14.09
            (9, datetime(2026, 8, 20, 10), "trial", None, False, None),  # пробный — не в удержании и не в базе
        ]
        rows = _acq_summary_weeks(payments, periods, 8, 30, self.NOW, self.TODAY)
        by = {r["week"]: r for r in rows}
        self.assertEqual([r["week"] for r in rows][-1], "2026-09-14")
        w0803 = by["2026-08-03"]
        self.assertEqual((w0803["revenue_new"], w0803["revenue_repeat"], w0803["new_payers"]), (249, 0, 1))
        self.assertEqual((w0803["ending_closed"], w0803["renewed_closed"], w0803["renewal_pct"]), (2, 1, 50.0))
        w0831 = by["2026-08-31"]
        self.assertEqual((w0831["revenue_new"], w0831["revenue_repeat"], w0831["avg_check"]), (0, 299, 299))
        # Период 31.08→30.09 покрывает концы недель 31.08, 07.09 и 14.09.
        self.assertEqual([by[k]["base"] for k in ("2026-08-24", "2026-08-31", "2026-09-07", "2026-09-14")], [0, 1, 1, 1])
        self.assertIsNone(by["2026-09-14"]["renewal_pct"])
        self.assertTrue(by["2026-09-14"]["is_current"])

    def test_template_has_revenue_tab_and_no_renew45(self):
        template = template_source("engine/templates/admin_dashboard.html")
        for needle in (
            'class="subtab active" data-subtab="acq-revenue"',
            'id="subpanel-acq-revenue" class="subtab-panel active"',
            'id="acq-revenue-kpis"',
            'id="acq-revenue-chart"',
            'id="acq-weekly-chart"',
            'id="acq-revenue-table"',
            'data-revenue-mode="newrep"',
            'data-revenue-preset="45"',
            'data-help="revenue"',
            'data-help="weekly"',
            "'acq-revenue': loadRevenue",
            "acqFetch('revenue_days'",
            "acqFetch('revenue_kpis'",
            "acqFetch('summary'",
            "sharedAxis: true",
            "barLabels: true, totalLabel: 'Всего за день, ₽'",
            "acq-tooltip-row is-total",
            "|| 'acq-revenue'",
            "revenue: {",
            "weekly: {",
        ):
            self.assertIn(needle, template, needle)
        # «Отвал базы» и отдельная под-вкладка «Новые vs повторные» убраны:
        # разрез новые/повторные живёт режимом графика на «Выручке».
        for gone in (
            "acq-renew45-chart", "loadRenew45", "Отвал базы: % продливших", 'data-help="renew45"',
            'data-subtab="acq-newrep"', 'id="subpanel-acq-newrep"', 'id="acq-newrep-month"',
            "loadNewRepeat", "acqFetch('new_repeat'", "            newrep: {",
        ):
            self.assertNotIn(gone, template, gone)


class AcquisitionAdsDailyTests(SimpleTestCase):
    @mock.patch("engine.views._acq_rows")
    def test_daily_rows_merge_spend_funnel_and_costs(self, rows_mock):
        rows_mock.side_effect = [
            [{"day": date(2026, 7, 1), "spend": Decimal("7261.73"),
              "impressions": 17860, "clicks": 1478}],
            [{"day": date(2026, 7, 1), "subs": 100}],
            [{"day": date(2026, 7, 1), "conns": 40}],
            [{"day": date(2026, 7, 1), "sales": 10}],
        ]

        result = _acq_ads_daily(object(), 92, "day")

        self.assertFalse(result["needs_migration"])
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["period"], "2026-07-01")
        self.assertEqual(row["spend"], 7261.73)
        self.assertEqual(row["cpc"], round(7261.73 / 1478, 2))
        self.assertEqual(row["cost_per_sub"], round(7261.73 / 100, 2))
        self.assertEqual(row["cost_per_conn"], round(7261.73 / 40, 2))
        self.assertEqual(row["cost_per_sale"], round(7261.73 / 10, 2))

    @mock.patch("engine.views._acq_rows")
    def test_month_grouping_divides_sums_not_daily_ratios(self, rows_mock):
        rows_mock.side_effect = [
            [
                {"day": date(2026, 7, 1), "spend": Decimal("100"),
                 "impressions": 1000, "clicks": 10},
                {"day": date(2026, 7, 2), "spend": Decimal("300"),
                 "impressions": 3000, "clicks": 30},
            ],
            [
                {"day": date(2026, 7, 1), "subs": 5},
                {"day": date(2026, 7, 2), "subs": 15},
            ],
            [{"day": date(2026, 7, 1), "conns": 8}],
            [{"day": date(2026, 7, 2), "sales": 4}],
        ]

        result = _acq_ads_daily(object(), 92, "month")

        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["period"], "2026-07")
        self.assertEqual(row["spend"], 400.0)
        self.assertEqual(row["impressions"], 4000)
        self.assertEqual(row["clicks"], 40)
        self.assertEqual(row["cpc"], 10.0)
        self.assertEqual(row["subs"], 20)
        self.assertEqual(row["cost_per_sub"], 20.0)
        self.assertEqual(row["conns"], 8)
        self.assertEqual(row["cost_per_conn"], 50.0)
        self.assertEqual(row["sales"], 4)
        self.assertEqual(row["cost_per_sale"], 100.0)

    @mock.patch("engine.views._acq_rows")
    def test_day_without_traffic_data_hides_cpc(self, rows_mock):
        rows_mock.side_effect = [
            [{"day": date(2026, 7, 1), "spend": Decimal("500"),
              "impressions": None, "clicks": None}],
            [{"day": date(2026, 7, 1), "subs": 10}],
            [],
            [],
        ]

        result = _acq_ads_daily(object(), 92, "day")

        row = result["rows"][0]
        self.assertIsNone(row["impressions"])
        self.assertIsNone(row["clicks"])
        self.assertIsNone(row["cpc"])
        self.assertEqual(row["cost_per_sub"], 50.0)
        self.assertIsNone(row["cost_per_conn"])
        self.assertIsNone(row["cost_per_sale"])

    @mock.patch("engine.views._acq_rows")
    def test_reports_missing_migration(self, rows_mock):
        rows_mock.side_effect = Exception("no such column: impressions")

        result = _acq_ads_daily(mock.Mock(), 92, "day")

        self.assertTrue(result["needs_migration"])
        self.assertEqual(result["rows"], [])

    @mock.patch("engine.views._acq_rows")
    def test_weekly_ads_split_cpa_into_stage_costs(self, rows_mock):
        week = date(2026, 7, 6)
        rows_mock.side_effect = [
            [{"id": 1, "day": date(2026, 7, 7), "channel": "yandex-direct",
              "account": "default",
              "amount_rub": Decimal("1000"), "impressions": 5000, "clicks": 200,
              "comment": "csv-import"}],
            [{"account": "default"}],
            [{"week": week, "new_payers": 4, "new_rub": Decimal("2000")}],
            [{"week": week, "subs": 50}],
            [{"week": week, "conns": 20}],
        ]

        result = _acq_ads(object(), 12)

        row = result["weeks"][0]
        self.assertEqual(row["cost_per_sub"], 20.0)
        self.assertEqual(row["conns"], 20)
        self.assertEqual(row["cost_per_conn"], 50.0)
        self.assertEqual(row["cost_per_sale"], 250.0)
        self.assertNotIn("cpa", row)
        spend_row = result["spends"][0]
        self.assertEqual(spend_row["impressions"], 5000)
        self.assertEqual(spend_row["clicks"], 200)

    @mock.patch("engine.views._acq_rows")
    def test_weekly_ads_keeps_spend_week_with_zero_sales(self, rows_mock):
        rows_mock.side_effect = [
            [{"id": 1, "day": date(2026, 7, 7), "channel": "yandex-direct",
              "account": "default", "amount_rub": Decimal("1000"),
              "impressions": 5000, "clicks": 200, "comment": ""}],
            [{"account": "default"}],
            [],
            [{"week": date(2026, 7, 6), "subs": 10}],
            [{"week": date(2026, 7, 6), "conns": 4}],
        ]

        result = _acq_ads(object(), 12)

        self.assertEqual(len(result["weeks"]), 1)
        row = result["weeks"][0]
        self.assertEqual(row["spend"], 1000.0)
        self.assertEqual(row["new_payers"], 0)
        self.assertEqual(row["new_rub"], 0.0)
        self.assertIsNone(row["cost_per_sale"])
        self.assertIsNone(row["drr"])


class AcquisitionAdsSummaryTests(SimpleTestCase):
    """Сводка рекламы за диапазон дат: расход по аккаунтам, суммарный
    расход и цены за те же самые даты."""

    @mock.patch("engine.views._acq_rows")
    def test_summary_sums_accounts_and_prices_stages(self, rows_mock):
        rows_mock.side_effect = [
            [
                {"account": "default", "spend": Decimal("3000"), "impressions": 30000, "clicks": 300},
                {"account": "second@yandex.ru", "spend": Decimal("1000"), "impressions": 10000, "clicks": 100},
            ],
            [{"subs": 200}],
            [{"conns": 80}],
            [{"sales": 16, "new_rub": Decimal("8000")}],
        ]

        result = _acq_ads_summary(object(), "2026-08-21", "2026-09-09")

        self.assertFalse(result["needs_migration"])
        self.assertEqual((result["start"], result["end"], result["days"]), ("2026-08-21", "2026-09-09", 20))
        self.assertEqual(result["spend"], 4000.0)
        self.assertEqual(result["impressions"], 40000)
        self.assertEqual(result["clicks"], 400)
        self.assertEqual(result["cpc"], 10.0)
        self.assertEqual(result["cost_per_sub"], 20.0)
        self.assertEqual(result["cost_per_conn"], 50.0)
        self.assertEqual(result["cost_per_sale"], 250.0)
        self.assertEqual(result["new_rub"], 8000.0)
        self.assertEqual(result["drr"], 50.0)
        self.assertEqual(result["romi"], 2.0)
        self.assertEqual([a["account"] for a in result["accounts"]], ["default", "second@yandex.ru"])
        self.assertEqual([a["share"] for a in result["accounts"]], [75.0, 25.0])
        self.assertEqual(result["accounts"][1]["cpc"], 10.0)
        # Все четыре запроса получают один и тот же диапазон.
        for call in rows_mock.call_args_list:
            self.assertEqual(call.kwargs["start"], date(2026, 8, 21))
            self.assertEqual(call.kwargs["end"], date(2026, 9, 9))
            self.assertIn("BETWEEN :start AND :end", call.args[1])

    @mock.patch("engine.views._acq_rows")
    def test_summary_swaps_reversed_dates_and_hides_cpc_without_traffic(self, rows_mock):
        rows_mock.side_effect = [
            [{"account": "default", "spend": Decimal("500"), "impressions": None, "clicks": None}],
            [{"subs": 0}],
            [{"conns": 0}],
            [{"sales": 0, "new_rub": Decimal("0")}],
        ]

        result = _acq_ads_summary(object(), "2026-09-09", "2026-09-01")

        self.assertEqual((result["start"], result["end"], result["days"]), ("2026-09-01", "2026-09-09", 9))
        self.assertIsNone(result["cpc"])
        self.assertIsNone(result["impressions"])
        self.assertIsNone(result["cost_per_sub"])
        self.assertIsNone(result["cost_per_sale"])
        self.assertIsNone(result["drr"])
        self.assertEqual(result["romi"], 0.0)
        self.assertEqual(result["accounts"][0]["share"], 100.0)

    @mock.patch("engine.views._acq_rows")
    def test_summary_defaults_to_last_30_days_and_reports_migration(self, rows_mock):
        rows_mock.side_effect = Exception("relation ad_spends does not exist")

        result = _acq_ads_summary(mock.Mock(), None, None)

        self.assertTrue(result["needs_migration"])
        call = rows_mock.call_args
        self.assertEqual((call.kwargs["end"] - call.kwargs["start"]).days, 29)

    def test_summary_rejects_bad_dates_and_huge_ranges(self):
        with self.assertRaises(ValueError):
            _acq_ads_summary(object(), "2026-13-01", "2026-09-09")
        with self.assertRaises(ValueError):
            _acq_ads_summary(object(), "2020-01-01", "2026-09-09")

    def test_summary_section_is_wired_into_admin_page(self):
        from engine.views import ACQ_SECTIONS

        self.assertIn("ads_summary", ACQ_SECTIONS)
        template = template_source("engine/templates/admin_dashboard.html")
        css = Path("engine/static/css/admin-concept-sections.css").read_text()
        for marker in (
            'id="acq-ads-summary"', 'id="acq-ads-sum-start"', 'id="acq-ads-sum-end"',
            'data-ads-summary-preset="30"', 'id="acq-ads-summary-apply"',
            "async function loadAdsSummary()", "acqFetch('ads_summary', params)",
            "loaders['acq-ads'] = () => Promise.all([original(), loadAdsSummary()])",
            "Расход по аккаунтам", "Цена подключения", "Цена продажи",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, template)
        self.assertIn(".acq-ads-summary-tiles", css)


class AcquisitionAdsTemplateTests(SimpleTestCase):
    def test_ads_tab_has_csv_import_and_daily_analytics(self):
        template = template_source("engine/templates/admin_dashboard.html")
        stylesheet = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn('id="acq-csv-form"', template)
        self.assertIn("action', 'import_csv'", template.replace('"', "'"))
        self.assertIn('id="acq-ads-daily-chart"', template)
        self.assertIn('id="acq-ads-cost-chart"', template)
        self.assertIn('id="acq-ads-group"', template)
        self.assertIn('data-help="ads_daily"', template)
        self.assertIn("'Подключения', 'Цена подключения', 'Продажи', 'Цена продажи'", template)
        self.assertIn('class="acq-chart-data-section"', template)
        self.assertIn('id="acq-ads-scroll-hint"', template)
        self.assertIn('updateAdsDailyTableFrame(group, rows.length);', template)
        self.assertIn('scrollbar-gutter: stable both-edges;', stylesheet)
        self.assertIn('.acq-chart-data-scroll thead th {', stylesheet)
        self.assertNotIn("'CPA, ₽'", template)


class AcquisitionPushAttributionTests(SimpleTestCase):
    def test_attribution_cte_uses_last_touch(self):
        # Оплата привязывается к последнему продающему пушу перед ней,
        # а пуш засчитывается сконвертившим не более одного раза.
        self.assertIn("SELECT DISTINCT ON (p.user_id, p.paid_at)", ACQ_PUSH_ATTRIBUTION_CTE)
        self.assertIn("ORDER BY p.user_id, p.paid_at, ev.ts DESC", ACQ_PUSH_ATTRIBUTION_CTE)
        self.assertIn("SELECT DISTINCT ON (a.event_id) a.*", ACQ_PUSH_ATTRIBUTION_CTE)
        self.assertIn("interval '72 hours'", ACQ_PUSH_ATTRIBUTION_CTE)

    @mock.patch("engine.views._acq_rows")
    def test_acq_pushes_sent_deduplicates_bot_copies(self, rows_mock):
        # Один пуш логируется каждым ботом (vpn/vps) отдельным событием, поэтому
        # «Отправлено» и дневной график считают уникальные (юзер, тип/день),
        # а не сырые строки event_logs — иначе метрики задваиваются.
        rows_mock.side_effect = [[], [], [], []]

        _acq_pushes(object(), 30)

        daily_sql = rows_mock.call_args_list[0].args[1]
        self.assertIn(
            "count(DISTINCT (user_id, event_payload->>'notification_type'))",
            daily_sql,
        )
        winback_sql = rows_mock.call_args_list[2].args[1]
        self.assertIn("count(DISTINCT (user_id, ts::date)) AS sent", winback_sql)

    @mock.patch("engine.views._acq_rows")
    def test_acq_pushes_builds_winback_segments(self, rows_mock):
        rows_mock.side_effect = [
            [{"day": date(2026, 7, 1), "selling": 5, "other": 2}],
            [{"day": date(2026, 7, 1), "rub": Decimal("1000")}],
            [{
                "ntype": "winback-1", "sent": 100, "paid_72h": 3,
                "new_payers": 1, "repeat_payers": 2,
                "rub": Decimal("1497"), "median_hours": 5.25,
            }],
            [
                {"ntype": "winback-1", "amount": Decimal("499"), "cnt": 2},
                {"ntype": "winback-1", "amount": Decimal("199"), "cnt": 1},
            ],
        ]

        result = _acq_pushes(object(), 30)

        self.assertEqual(len(result["winback"]), 1)
        row = result["winback"][0]
        self.assertEqual(row["type"], "winback-1")
        self.assertEqual(row["conv_pct"], 3.0)
        self.assertEqual(row["new_payers"], 1)
        self.assertEqual(row["repeat_payers"], 2)
        self.assertEqual(row["rub"], 1497.0)
        self.assertEqual(row["median_hours"], 5.2)
        self.assertEqual(
            row["amounts"],
            [{"amount": 499.0, "count": 2}, {"amount": 199.0, "count": 1}],
        )

    @mock.patch("engine.views._acq_rows")
    def test_acq_pushes_keeps_zero_conversion_rows(self, rows_mock):
        rows_mock.side_effect = [
            [],
            [],
            [{
                "ntype": "winback-4", "sent": 50, "paid_72h": 0,
                "new_payers": 0, "repeat_payers": 0,
                "rub": Decimal("0"), "median_hours": None,
            }],
            [],
        ]

        result = _acq_pushes(object(), 30)

        row = result["winback"][0]
        self.assertEqual(row["conv_pct"], 0)
        self.assertIsNone(row["median_hours"])
        self.assertEqual(row["amounts"], [])


class AdminRuntimeSettingsTests(SimpleTestCase):
    def test_apple_recommended_app_setting_is_enum(self):
        self.assertEqual(
            admin_runtime_setting_type(BOT_APPLE_RECOMMENDED_APP_SETTING),
            "enum",
        )

        normalized_value, error = admin_validate_runtime_setting(
            BOT_APPLE_RECOMMENDED_APP_SETTING,
            "INCY",
        )

        self.assertIsNone(error)
        self.assertEqual(normalized_value, "incy")

    def test_apple_recommended_app_payload_exposes_allowed_values(self):
        payload = admin_runtime_setting_payload(BOT_APPLE_RECOMMENDED_APP_SETTING)

        self.assertEqual(payload["allowed_values"], ["happ", "incy"])
        self.assertIn("iOS/macOS", payload["description"])

    def test_apple_recommended_app_rejects_unknown_value(self):
        normalized_value, error = admin_validate_runtime_setting(
            BOT_APPLE_RECOMMENDED_APP_SETTING,
            "streisand",
        )

        self.assertIsNone(normalized_value)
        self.assertIn("Допустимые значения", error)


class OfferTemplateTests(SimpleTestCase):
    def test_offer_has_current_prices_and_gateway_autopay_terms(self):
        template = template_source("engine/templates/offer.html")

        self.assertIn("Дата вступления в силу: 01.08.2026", template)
        self.assertIn("{{ offer_tariffs.oneday.price }} ₽", template)
        self.assertIn("{{ offer_tariffs.month.price }} ₽", template)
        self.assertIn("{{ offer_tariffs.threemonths.price }} ₽", template)
        self.assertIn("{{ offer_tariffs.year.price }} ₽", template)
        # Условия автопродления сформулированы через платёжный сервис, без
        # внутренних технических деталей (настроек БД) в юридическом тексте.
        self.assertIn("через платёжный сервис YooKassa", template)
        self.assertIn("автоматически подключается", template)
        self.assertIn("через иную платёжную систему (не YooKassa)", template)
        self.assertNotIn("payment_gateway", template)
        self.assertNotIn("299 ₽", template)
        self.assertNotIn("19 ₽", template)
        self.assertIn("покупка является разовой", template)

    def test_offer_tariff_table_lists_all_paid_tariffs(self):
        """Таблица 2.1 обязана перечислять ВСЕ платные тарифы, включая
        «Пробный период на 3 дня» (10 ₽) и «Подписку на 1 день» (19 ₽) —
        они продаются в боте и подключают автопродление YooKassa (раздел 4),
        поэтому не могут отсутствовать в перечне тарифов оферты."""
        import inspect

        from engine import views

        src = inspect.getsource(views.offer)
        self.assertIn("for tariff in OFFER_TARIFFS", src)
        self.assertNotIn("for tariff in ACTUAL_TARIFFS", src)
        # OFFER_TARIFFS = trial 3 дня + 1 день + витрина сайта
        ids = [tariff.db_tariff_id for tariff in views.OFFER_TARIFFS]
        self.assertEqual(ids, ["threedays", "oneday", "month", "threemonths", "year"])

        template = template_source("engine/templates/offer.html")
        # 2.2 не дублирует таблицу ценой, а говорит, где какие тарифы доступны.
        self.assertIn("доступны для оплаты в Telegram-боте Сервиса", template)
        self.assertNotIn("также доступен тариф «Подписка на 1 день» стоимостью", template)

    def test_offer_brand_follows_site_role(self):
        # Шапка оферты обязана подстраиваться под тип домена: на VPS-доменах
        # "MONKEY ISLAND VPS", на VPN/кабинетных — "MONKEY ISLAND VPN".
        # Регресс: бренд был захардкожен как VPS и светился на VPN-доменах.
        template = template_source("engine/templates/offer.html")

        self.assertIn(
            "{% if site_role == 'vps' or site_role == 'vps_direct_sale' %}"
            " VPS{% else %} VPN{% endif %}",
            template,
        )
        self.assertNotIn('<span class="text-[#ffc700]"> VPS</span>', template)

    def test_terms_page_wired_and_follows_site_role(self):
        # Пользовательское соглашение доступно по /terms/, а формулировка
        # назначения бота подстраивается под тип домена: VPS-лендинги говорят
        # про аренду VPS, VPN/кабинетные — про VPN-сервис.
        from django.urls import reverse

        self.assertEqual(reverse("terms"), "/terms/")

        template = template_source("engine/templates/terms.html")
        self.assertIn("Пользовательское соглашение", template)
        self.assertIn(
            "{% if site_role == 'vps' or site_role == 'vps_direct_sale' %}"
            "аренды пользователями VPS серверов"
            "{% else %}предоставления пользователям доступа к VPN-сервису{% endif %}",
            template,
        )
        # Контакт поддержки и ссылка на политику конфиденциальности.
        self.assertIn("https://t.me/monkeyislandsupportbot", template)
        self.assertIn('<a href="/privacy/">Политике конфиденциальности</a>', template)

    def test_terms_linked_from_landing_footers(self):
        for name in ("index_vpn.html", "index_vps.html", "index_vps_direct_sale.html"):
            template = Path(f"engine/templates/{name}").read_text()
            self.assertIn('href="/terms/"', template, name)

    def test_privacy_effective_date_is_current(self):
        template = template_source("engine/templates/privacy.html")
        self.assertIn("Дата вступления в силу: 04.08.2026", template)
        self.assertNotIn("03.05.2026", template)

    def test_runtime_offer_tariffs_use_database_prices(self):
        class FakeSession:
            values = {
                    BOT_TARIFF_PRICE_THREEDAYS_SETTING: "11",
                    BOT_TARIFF_PRICE_ONEDAY_SETTING: "19",
                    BOT_TARIFF_PRICE_MONTH_SETTING: "299",
                    BOT_TARIFF_PRICE_THREEMONTHS_SETTING: "609",
                    BOT_TARIFF_PRICE_YEAR_SETTING: "1809",
            }

            def query(self, model):
                return self

            def filter(self, *args):
                return self

            def all(self):
                return [
                    SimpleNamespace(key=key, value=value)
                    for key, value in self.values.items()
                ]

        tariffs = get_runtime_offer_tariffs(FakeSession())

        self.assertEqual(
            {tariff_id: tariff.price for tariff_id, tariff in tariffs.items()},
            {
                "threedays": 11,
                "oneday": 19,
                "month": 299,
                "threemonths": 609,
                "year": 1809,
            },
        )


class WataPaymentFlowTests(SimpleTestCase):
    def test_public_landing_headers_use_animated_brand_logo(self):
        animated_logo = "icons/monkey-island-logo-animated.webp"
        self.assertTrue(Path(f"engine/static/{animated_logo}").is_file())

        for template_name in (
            "engine/templates/index_vpn.html",
            "engine/templates/index_vps.html",
            "engine/templates/index_vps_direct_sale.html",
        ):
            with self.subTest(template=template_name):
                template = Path(template_name).read_text()
                self.assertIn(animated_logo, template)

        # У каждого лендинга свой класс логотипа: index_vpn после редизайна
        # использует .mi-logo-mark, index_vps — .landing-brand-mark с
        # уменьшением на узких экранах.
        vpn_template = template_source("engine/templates/index_vpn.html")
        self.assertIn(".mi-logo-mark", vpn_template)

        vps_template = template_source("engine/templates/index_vps.html")
        self.assertIn(".landing-brand-mark", vps_template)
        self.assertIn("@media (max-width: 380px)", vps_template)

    def test_public_landing_hero_art_has_lightweight_animation(self):
        # index_vpn.html переехал на собственный hero без плавающей иллюстрации.
        for template_name in (
            "engine/templates/index_vps.html",
            "engine/templates/index_vps_direct_sale.html",
        ):
            with self.subTest(template=template_name):
                template = Path(template_name).read_text()
                self.assertIn("hero-art-scene", template)
                self.assertIn("island-art-float", template)
                self.assertIn("prefers-reduced-motion: reduce", template)

    def test_neutral_vps_landings_avoid_forbidden_wording(self):
        # Нейтральные VPS-домены существуют ради рекламы, которой нельзя
        # упоминать VPN, обход блокировок, шифрование и т.п. (см. README,
        # раздел «Лендинги и платный flow»).
        forbidden = (
            "vpn",
            "обход",
            "блокир",
            "заблокир",
            "цензур",
            "шифрован",
            "приватн",
            "любые сайты",
            "роутер",
        )
        for template_name in (
            "engine/templates/index_vps.html",
            "engine/templates/index_vps_direct_sale.html",
        ):
            template = Path(template_name).read_text().lower()
            for word in forbidden:
                with self.subTest(template=template_name, word=word):
                    self.assertNotIn(word, template)

    def test_vps_landing_uses_redesigned_theme(self):
        # Редизайн 2026-08-28: структура подписочного лендинга (группы секций,
        # карточки, Golos Text), фирменная чёрно-жёлтая палитра.
        template = template_source("engine/templates/index_vps.html")

        self.assertIn("Golos+Text", template)
        self.assertIn("--island-accent: #ffc700;", template)
        self.assertNotIn("#3183ff", template)
        self.assertIn('class="light-group"', template)
        self.assertIn('class="dark-group"', template)
        self.assertIn("cta-banner", template)
        # Email-поля обязаны быть 16px, иначе iOS Safari зумит при фокусе.
        self.assertIn("font-size: 16px;", template)
        # Ключевые механики покупки сохранены после редизайна.
        self.assertIn("data-payment-form", template)
        self.assertIn('name="login_link_kind" value="purchase_permanent"', template)
        self.assertIn("data-known-user-cta", template)
        self.assertIn("mobile-buybar", template)

    def test_vps_landings_show_client_ip_topbar_with_neutral_wording(self):
        # Топ-бар с IP посетителя (как у конкурентов), но формулировка строго
        # нейтральная: «личный сервер не подключён», без слов про защиту.
        for template_name in (
            "engine/templates/index_vps.html",
            "engine/templates/index_vps_direct_sale.html",
        ):
            with self.subTest(template=template_name):
                template = Path(template_name).read_text()
                self.assertIn('{% if client_ip %}', template)
                self.assertIn("ip-topbar", template)
                self.assertIn("Ваш IP:", template)
                self.assertIn("личный сервер не подключён", template)
                self.assertIn("{{ client_ip_country }}", template)
                self.assertNotIn("не защищ", template)

    def test_vpn_landing_shows_client_ip_topbar_with_direct_wording(self):
        # На VPN-доменах подача прямая, поэтому топ-бар говорит «Вы не
        # защищены» — в отличие от нейтральных VPS-доменов.
        template = template_source("engine/templates/index_vpn.html")

        self.assertIn('{% if client_ip %}', template)
        self.assertIn("ip-topbar", template)
        self.assertIn("Ваш IP:", template)
        self.assertIn("Вы не защищены!", template)
        self.assertIn("{{ client_ip_country }}", template)

    def test_direct_sale_landing_sells_immediately_after_hero(self):
        # Смысл direct-sale-лендинга — сразу продавать: блок тарифов идёт
        # первым после hero, до всех остальных секций.
        template = template_source("engine/templates/index_vps_direct_sale.html")

        prices = template.index('<section id="prices"')
        self.assertLess(prices, template.index('<section id="features"'))
        self.assertLess(prices, template.index('<section id="how"'))
        self.assertLess(prices, template.index('<section id="faq"'))

    def test_vps_landings_hero_art_is_palette_native_server_mock(self):
        # Вместо растровой золотой иллюстрации hero использует собранный в
        # вёрстке макет карточки сервера — он не конфликтует с синей палитрой.
        for template_name in (
            "engine/templates/index_vps.html",
            "engine/templates/index_vps_direct_sale.html",
        ):
            with self.subTest(template=template_name):
                template = Path(template_name).read_text()
                self.assertIn("server-mock", template)
                self.assertIn("mock-card", template)
                self.assertNotIn("personal-server-hub", template)

    def test_wata_views_do_not_embed_hosted_payment_page_in_iframe(self):
        views = Path("engine/views.py").read_text()

        self.assertNotIn('"wata_payment.html"', views)
        self.assertNotIn("payment rendering wata widget", views)

    def test_payment_forms_open_provider_tab_and_status_in_current_tab(self):
        for template_name in (
            "engine/templates/index_vpn.html",
            "engine/templates/index_vps.html",
            "engine/templates/index_vps_direct_sale.html",
            "engine/templates/dashboard.html",
        ):
            with self.subTest(template=template_name):
                template = Path(template_name).read_text()
                self.assertIn("window.open('', '_blank')", template)
                self.assertIn("'X-Payment-Launch': 'new-tab'", template)
                self.assertIn("payload.payment_url", template)
                self.assertIn("payload.payment_status_url", template)

    def test_payment_status_has_open_payment_button_for_pending_tab(self):
        template = template_source("engine/templates/payment_status.html")

        self.assertIn("Продолжить оплату", template)
        self.assertNotIn("Проверить статус вручную", template)
        self.assertNotIn("manual-status-action", template)
        self.assertIn('target="_blank" rel="noopener"', template)

    def test_payment_status_can_return_to_cabinet_without_canceling_payment(self):
        template = template_source("engine/templates/payment_status.html")

        self.assertIn('id="cabinet-action" href="{% url \'dashboard\' %}"', template)
        self.assertIn("Вернуться в кабинет", template)
        self.assertIn("window.location.replace(cabinetUrl)", template)
        self.assertIn("tg.BackButton.onClick(returnToCabinet)", template)
        self.assertIn("tg.BackButton.show()", template)
        self.assertIn("tg.BackButton.hide()", template)
        # Возврат не вызывает API отмены и не останавливает polling платежа.
        self.assertNotIn("cancelPayment", template)
        self.assertTrue("setTimeout(pollStatus, delay)" in template)

    def test_payment_status_matches_mobile_dashboard_typography_and_buttons(self):
        template = template_source("engine/templates/payment_status.html")

        self.assertIn('font-family: -apple-system, BlinkMacSystemFont, "Segoe UI"', template)
        self.assertNotIn("fonts.googleapis.com", template)
        self.assertIn("font-size: 27px;", template)
        self.assertIn("font-weight: 600;", template)
        self.assertIn("text-transform: none;", template)
        self.assertIn(".btn-cabinet", template)
        self.assertIn(".btn-support", template)

    @override_settings(PAYMENT_GATEWAY="wata", WATA_HOST="https://wata.example", WATA_TOKEN="token")
    def test_pay_rejects_blocked_user(self):
        """Полностью заблокированный (user_blocks) не может создать платёж —
        иначе успешная оплата реактивировала бы отключённую подписку."""
        tariff = SimpleNamespace(
            price=100,
            db_tariff_id="month",
            description="1 месяц",
        )
        user = SimpleNamespace(
            id=42,
            email="user@example.com",
            username="user-42",
            telegram_id=None,
        )

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeSession:
            def query(self, model):
                return FakeQuery()

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        request = RequestFactory().post(
            "/pay/",
            {"email": "user@example.com", "tariff_id": "month"},
            HTTP_HOST="example.com",
        )
        request.user = SimpleNamespace(is_authenticated=True, id=42, email="user@example.com")
        request.session = SessionDict()

        with (
            mock.patch("engine.views.find_reusable_attempt", return_value=None),
            mock.patch("engine.views.site_apply_first_purchase_discount", return_value=(tariff, False)),
            mock.patch("engine.views.finish_attempt"),
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.is_user_blocked", return_value=True),
            mock.patch("engine.views.get_runtime_actual_tariffs", return_value=[tariff]),
            mock.patch(
                "engine.views.get_registration_context",
                return_value={"traffic_source": None, "ymid": None},
            ),
            mock.patch("engine.views.sync_existing_user_tracking"),
        ):
            response = pay(request)

        self.assertEqual(response.status_code, 403)

    @override_settings(PAYMENT_GATEWAY="wata", WATA_HOST="https://wata.example", WATA_TOKEN="token")
    def test_ajax_payment_launch_returns_provider_and_status_urls(self):
        tariff = SimpleNamespace(
            price=100,
            db_tariff_id="month",
            description="1 месяц",
        )
        user = SimpleNamespace(
            id=42,
            email="user@example.com",
            username="user-42",
            telegram_id=None,
        )
        status_token = SimpleNamespace(token_hash="test-hash", payment_gateway=None, payment_reference=None)
        login_token = SimpleNamespace(token_hash="test-hash", payment_gateway=None, payment_reference=None)

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeSession:
            def __init__(self):
                self.added = []

            def query(self, model):
                return FakeQuery()

            def add(self, obj):
                # Токен проставляется во flush(), как в настоящей сессии:
                # питоновский column default применяется на INSERT. Дубль,
                # ставивший его в add(), скрывал регресс 2026-09-12
                # (/login/magic/None/ в письме).
                self.added.append(obj)

            def flush(self):
                for pending in self.added:
                    if isinstance(pending, MagicToken) and pending.token is None:
                        pending.token = "magic-token"

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        request = RequestFactory().post(
            "/pay/",
            {
                "email": "user@example.com",
                "tariff_id": "month",
                "login_link_kind": "purchase_permanent",
            },
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_X_PAYMENT_LAUNCH="new-tab",
            HTTP_HOST="example.com",
        )
        request.user = SimpleNamespace(is_authenticated=True, id=42, email="user@example.com")
        request.session = SessionDict()

        created_payment = SimpleNamespace(
            confirmation_url="https://wata.example/pay",
            reference="order-1",
            payload={"id": "invoice-1"},
        )

        with (
            mock.patch("engine.views.find_reusable_attempt", return_value=None),
            mock.patch("engine.views.site_apply_first_purchase_discount", return_value=(tariff, False)),
            mock.patch("engine.views.finish_attempt"),
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.is_user_blocked", return_value=False),
            mock.patch("engine.views.get_runtime_actual_tariffs", return_value=[tariff]),
            mock.patch("engine.views.get_registration_context", return_value={"traffic_source": None, "ymid": None}),
            mock.patch("engine.views.sync_existing_user_tracking"),
            mock.patch("engine.views.create_purchase_status_token", return_value="pstatus_token"),
            mock.patch("engine.views.get_purchase_status_token", return_value=status_token),
            mock.patch("engine.views.create_purchase_login_token", return_value="plogin_token"),
            mock.patch("engine.views.get_purchase_login_token", return_value=login_token),
            mock.patch("engine.views.create_wata_payment_sync", return_value=created_payment),
            mock.patch("engine.views.save_wata_invoice"),
            mock.patch("engine.views.add_event_log"),
            mock.patch("engine.views.send_magic_link_email"),
        ):
            response = pay(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["payment_url"], "https://wata.example/pay")
        self.assertTrue(
            payload["payment_status_url"].endswith(
                "/payment/status/pstatus_token/"
            )
        )
        self.assertEqual(status_token.payment_gateway, "wata")
        self.assertEqual(status_token.payment_reference, "order-1")
        self.assertIsNone(login_token.payment_gateway)
        self.assertIsNone(login_token.payment_reference)
        self.assertEqual(
            request.session[payment_session_url_key("pstatus_token")],
            "https://wata.example/pay",
        )
        self.assertTrue(request.session.modified)

    @override_settings(
        PAYMENT_GATEWAY="yookassa",
        YOOKASSA_SHOP_ID="shop",
        YOOKASSA_SECRET_KEY="secret",
    )
    def test_ajax_yookassa_payment_launch_uses_status_token(self):
        tariff = SimpleNamespace(
            price=100,
            db_tariff_id="month",
            description="1 месяц",
        )
        user = SimpleNamespace(
            id=42,
            email="user@example.com",
            username="user-42",
            telegram_id=None,
        )
        status_token = SimpleNamespace(token_hash="test-hash", payment_gateway=None, payment_reference=None)

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeSession:
            def __init__(self):
                self.added = []

            def query(self, model):
                return FakeQuery()

            def add(self, obj):
                # Токен проставляется во flush(), как в настоящей сессии:
                # питоновский column default применяется на INSERT. Дубль,
                # ставивший его в add(), скрывал регресс 2026-09-12
                # (/login/magic/None/ в письме).
                self.added.append(obj)

            def flush(self):
                for pending in self.added:
                    if isinstance(pending, MagicToken) and pending.token is None:
                        pending.token = "magic-token"

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        request = RequestFactory().post(
            "/pay/",
            {
                "email": "user@example.com",
                "tariff_id": "month",
            },
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_X_PAYMENT_LAUNCH="new-tab",
            HTTP_HOST="example.com",
        )
        request.user = SimpleNamespace(is_authenticated=True, id=42, email="user@example.com")
        request.session = SessionDict()

        created_payment = SimpleNamespace(
            confirmation_url="https://yookassa.example/pay",
            reference="yk-payment-1",
        )

        with (
            mock.patch("engine.views.find_reusable_attempt", return_value=None),
            mock.patch("engine.views.site_apply_first_purchase_discount", return_value=(tariff, False)),
            mock.patch("engine.views.finish_attempt"),
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.is_user_blocked", return_value=False),
            mock.patch("engine.views.get_runtime_actual_tariffs", return_value=[tariff]),
            mock.patch(
                "engine.views.get_registration_context",
                return_value={"traffic_source": None, "ymid": None},
            ),
            mock.patch("engine.views.sync_existing_user_tracking"),
            mock.patch("engine.views.create_purchase_status_token", return_value="pstatus_token"),
            mock.patch("engine.views.get_purchase_status_token", return_value=status_token),
            mock.patch("engine.views.create_yk_payment_sync", return_value=created_payment),
            mock.patch("engine.views.add_event_log"),
            mock.patch("engine.views.send_magic_link_email"),
        ):
            response = pay(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["payment_url"], "https://yookassa.example/pay")
        self.assertTrue(
            payload["payment_status_url"].endswith(
                "/payment/status/pstatus_token/"
            )
        )
        self.assertEqual(status_token.payment_gateway, "yookassa")
        self.assertEqual(status_token.payment_reference, "yk-payment-1")

    def _authenticated_yookassa_pay(self, account_email, receipt_email):
        tariff = SimpleNamespace(price=100, db_tariff_id="month", description="1 месяц")
        user = SimpleNamespace(
            id=42, email=account_email, username="user-42", telegram_id=777
        )
        status_token = SimpleNamespace(
            token_hash="test-hash", payment_gateway=None, payment_reference=None
        )
        events = []

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeSession:
            def __init__(self):
                self.added = []

            def query(self, model):
                return FakeQuery()

            def add(self, obj):
                # Токен проставляется во flush(), как в настоящей сессии:
                # питоновский column default применяется на INSERT. Дубль,
                # ставивший его в add(), скрывал регресс 2026-09-12
                # (/login/magic/None/ в письме).
                self.added.append(obj)

            def flush(self):
                for pending in self.added:
                    if isinstance(pending, MagicToken) and pending.token is None:
                        pending.token = "magic-token"

            def commit(self):
                events.append("commit")

            def rollback(self):
                return None

            def close(self):
                return None

        request = RequestFactory().post(
            "/pay/",
            {"email": receipt_email, "tariff_id": "month"},
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_HOST="example.com",
        )
        request.user = SimpleNamespace(is_authenticated=True, id=42, email=account_email)
        request.session = SessionDict()
        created_payment = SimpleNamespace(
            confirmation_url="https://yookassa.example/pay", reference="yk-payment-1"
        )

        def confirmation_sent(*args):
            events.append("confirmation")
            return True

        with (
            override_settings(
                PAYMENT_GATEWAY="yookassa",
                YOOKASSA_SHOP_ID="shop",
                YOOKASSA_SECRET_KEY="secret",
            ),
            mock.patch("engine.views.find_reusable_attempt", return_value=None),
            mock.patch("engine.views.site_apply_first_purchase_discount", return_value=(tariff, False)),
            mock.patch("engine.views.finish_attempt"),
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.is_user_blocked", return_value=False),
            mock.patch("engine.views.get_runtime_actual_tariffs", return_value=[tariff]),
            mock.patch("engine.views.create_purchase_status_token", return_value="pstatus_token"),
            mock.patch("engine.views.get_purchase_status_token", return_value=status_token),
            mock.patch("engine.views.create_yk_payment_sync", return_value=created_payment) as create_payment,
            mock.patch("engine.views.add_event_log"),
            mock.patch("engine.views.send_magic_link_email"),
            mock.patch(
                "engine.views.send_payment_email_confirmation",
                side_effect=confirmation_sent,
            ) as confirmation,
        ):
            response = pay(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["status"], "ok")
        return user, create_payment, confirmation, events

    def test_authenticated_account_without_email_pays_one_off_and_gets_confirmation(self):
        user, create_payment, confirmation, events = self._authenticated_yookassa_pay(
            None, "receipt@example.com"
        )

        # Чек — на введённый адрес, но без email в аккаунте карта не сохраняется:
        # yk-recurrent без users.email молча не списал бы автоплатёж.
        self.assertEqual(create_payment.call_args.kwargs["email"], "receipt@example.com")
        self.assertIs(create_payment.call_args.kwargs["save_payment_method"], False)
        # Непроверенный адрес не пишется в аккаунт (B23) — только письмо подтверждения
        # после commit счёта.
        self.assertIsNone(user.email)
        confirmation.assert_called_once()
        self.assertEqual(confirmation.call_args.args[1:], (42, "receipt@example.com"))
        self.assertLess(events.index("commit"), events.index("confirmation"))

    def test_authenticated_account_with_email_keeps_autopay_without_confirmation(self):
        user, create_payment, confirmation, _ = self._authenticated_yookassa_pay(
            "user@example.com", "user@example.com"
        )

        self.assertIs(create_payment.call_args.kwargs["save_payment_method"], True)
        confirmation.assert_not_called()
        self.assertEqual(user.email, "user@example.com")

    def test_wata_payment_retry_redirects_to_hosted_invoice_url(self):
        request = RequestFactory().get("/pay/retry/token/")
        invoice = SimpleNamespace(url="https://wata.example/pay", order_id="order-1")

        class FakeSession:
            def close(self):
                return None

        with (
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.get_purchase_status_token", return_value=object()),
            mock.patch("engine.views.get_purchase_wata_invoice", return_value=invoice),
            mock.patch("engine.views.is_wata_invoice_expired", return_value=False),
        ):
            response = payment_retry(request, "token")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://wata.example/pay")


class CustomConfigTemplatePayloadTests(SimpleTestCase):
    def test_custom_config_template_model_allows_multiple_active_rows(self):
        self.assertNotIn(
            "uix_custom_config_templates_single_active",
            {index.name for index in CustomConfigTemplate.__table__.indexes},
        )

    def test_form_bool_enabled_handles_checkbox_values(self):
        self.assertTrue(form_bool_enabled("1"))
        self.assertTrue(form_bool_enabled("on"))
        self.assertFalse(form_bool_enabled("0"))
        self.assertTrue(form_bool_enabled(None, default=True))

    def test_payload_contains_dialer_proxy_options(self):
        template = SimpleNamespace(
            id=1,
            name="default",
            template_json="{}",
            entry_name="proxy",
            enable_dialer_proxy=False,
            dialer_proxy_name="CUSTOM-ROUTING",
            announce_text=None,
            support_url=None,
            profile_update_interval=None,
            additional_headers=None,
            is_active=True,
        )

        payload = custom_config_template_payload(template)

        self.assertFalse(payload["enable_dialer_proxy"])
        self.assertEqual(payload["dialer_proxy_name"], "CUSTOM-ROUTING")

    def test_config_template_save_does_not_deactivate_other_active_templates(self):
        template = SimpleNamespace(
            id=1,
            name="old",
            template_json="{}",
            entry_name=None,
            enable_dialer_proxy=True,
            dialer_proxy_name=None,
            announce_text=None,
            support_url=None,
            profile_update_interval=None,
            additional_headers={},
            is_active=True,
            updated_at=None,
        )

        class FakeUaRuleQuery:
            def filter(self, *args, **kwargs):
                return self

            def order_by(self, *args, **kwargs):
                return self

            def all(self):
                return []

        class FakeSession:
            def get(self, model, template_id):
                return template

            def query(self, model):
                # Валидация шаблона читает UA-правила — это разрешено.
                if model is ClientUaRule:
                    return FakeUaRuleQuery()
                raise AssertionError("saving an active template must not deactivate others")

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        request = RequestFactory().post(
            "/support-admin/api/config-templates/",
            data={
                "template_id": "1",
                "name": "active-a",
                "template_json": "{}",
                "is_active": "1",
            },
        )

        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
        ):
            response = support_admin_api_config_templates(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(template.is_active)


class ConfigTemplateJinjaValidationTests(SimpleTestCase):
    JINJA_TEMPLATE = """{
      "dns": {
        "servers": [
          {% if CLIENT == "happ" %}
          "1.1.1.1",
          {% else %}
          "https://dns.google/dns-query",
          {% endif %}
          "77.88.8.8"
        ]
      },
      "user": "{{VLESS_USER}}"
    }"""

    def test_plain_json_passes(self):
        self.assertIsNone(validate_config_template_json('{"log": {}}'))

    def test_plain_invalid_json_reports_default_scenario(self):
        error = validate_config_template_json('{"log": }')
        self.assertIn("без клиентских переменных", error)

    def test_jinja_branches_validated_per_scenario(self):
        scenarios = [
            ("CLIENT=happ", {"CLIENT": "happ"}),
            ("CLIENT=incy", {"CLIENT": "incy"}),
        ]
        self.assertIsNone(
            validate_config_template_json(self.JINJA_TEMPLATE, scenarios)
        )

    def test_broken_branch_reports_scenario_label(self):
        template = """{
          {% if CLIENT == "incy" %}
          "servers": [,]
          {% else %}
          "servers": []
          {% endif %}
        }"""
        error = validate_config_template_json(
            template, [("CLIENT=incy", {"CLIENT": "incy"})]
        )
        self.assertIn("CLIENT=incy", error)

    def test_jinja_syntax_error_reported(self):
        error = validate_config_template_json('{"a": {% if %}}')
        self.assertIn("Jinja", error)

    def test_reserved_variables_substituted(self):
        # Ветка else рендерится без клиентских переменных, {{VLESS_USER}}
        # подставляется тестовым значением — шаблон валиден как есть.
        self.assertIsNone(validate_config_template_json(self.JINJA_TEMPLATE))


class ClientUaRuleTests(SimpleTestCase):
    def test_rule_fields_validation(self):
        self.assertIsNotNone(validate_client_ua_rule_fields("", "CLIENT", "incy"))
        self.assertIsNotNone(validate_client_ua_rule_fields("incy", "", "incy"))
        self.assertIsNotNone(
            validate_client_ua_rule_fields("incy", "2CLIENT", "incy")
        )
        self.assertIsNotNone(
            validate_client_ua_rule_fields("incy", "CLI-ENT", "incy")
        )
        self.assertIsNotNone(validate_client_ua_rule_fields("incy", "CLIENT", ""))
        for reserved in ("VLESS_USER", "REMARKS", "ENTRY_NAME"):
            self.assertIsNotNone(
                validate_client_ua_rule_fields("incy", reserved, "incy")
            )
        self.assertIsNone(validate_client_ua_rule_fields("incy", "CLIENT", "incy"))

    def test_scenarios_built_from_rules(self):
        rules = [
            SimpleNamespace(variable_name="CLIENT", value="happ"),
            SimpleNamespace(variable_name="CLIENT", value="incy"),
        ]
        self.assertEqual(
            client_ua_rule_scenarios(rules),
            [
                ("CLIENT=happ", {"CLIENT": "happ"}),
                ("CLIENT=incy", {"CLIENT": "incy"}),
            ],
        )

    def test_rule_payload(self):
        rule = SimpleNamespace(
            id=7,
            match_substring="incy",
            variable_name="CLIENT",
            value="incy",
            priority=100,
            is_active=True,
        )
        payload = client_ua_rule_payload(rule)
        self.assertEqual(payload["match_substring"], "incy")
        self.assertEqual(payload["variable_name"], "CLIENT")

    def test_config_template_validate_action_accepts_jinja_branches(self):
        template = '{"a": {% if CLIENT == "happ" %}1{% else %}2{% endif %}}'
        request = RequestFactory().post(
            "/support-admin/api/config-templates/",
            data={"action": "validate", "template_json": template},
        )

        rules = [SimpleNamespace(variable_name="CLIENT", value="happ")]
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=mock.MagicMock()),
            mock.patch("engine.views.load_client_ua_rules", return_value=rules),
        ):
            response = support_admin_api_config_templates(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["scenarios_checked"], 2)

    def test_config_template_validate_action_reports_broken_branch(self):
        template = '{"a": {% if CLIENT == "happ" %}broken{% else %}2{% endif %}}'
        request = RequestFactory().post(
            "/support-admin/api/config-templates/",
            data={"action": "validate", "template_json": template},
        )

        rules = [SimpleNamespace(variable_name="CLIENT", value="happ")]
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=mock.MagicMock()),
            mock.patch("engine.views.load_client_ua_rules", return_value=rules),
        ):
            response = support_admin_api_config_templates(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("CLIENT=happ", json.loads(response.content)["message"])

    def test_save_rejects_reserved_variable_name(self):
        request = RequestFactory().post(
            "/support-admin/api/ua-rules/",
            data={
                "match_substring": "incy",
                "variable_name": "VLESS_USER",
                "value": "incy",
            },
        )

        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=mock.MagicMock()),
        ):
            response = support_admin_api_ua_rules(request)

        self.assertEqual(response.status_code, 400)

    def test_save_creates_rule_with_default_variable(self):
        session = mock.MagicMock()
        request = RequestFactory().post(
            "/support-admin/api/ua-rules/",
            data={
                "match_substring": "Incy",
                "value": "incy",
                "priority": "50",
            },
        )

        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
            mock.patch("engine.views.load_client_ua_rules", return_value=[]),
            mock.patch(
                "engine.views.collect_ua_rule_template_warnings", return_value=[]
            ),
        ):
            response = support_admin_api_ua_rules(request)

        self.assertEqual(response.status_code, 200)
        session.add.assert_called_once()
        session.commit.assert_called_once()
        added_rule = session.add.call_args[0][0]
        self.assertEqual(added_rule.variable_name, "CLIENT")
        self.assertEqual(added_rule.match_substring, "Incy")
        self.assertEqual(added_rule.priority, 50)


class AdminSalesSeriesTests(SimpleTestCase):
    def test_bucket_key_matches_selected_granularity(self):
        value = datetime(2026, 5, 20, 12, 0)

        self.assertEqual(admin_stats_row_bucket_key(value, "day"), "2026-05-20")
        self.assertEqual(admin_stats_row_bucket_key(value, "week"), "2026-05-18")
        self.assertEqual(admin_stats_row_bucket_key(value, "month"), "2026-05-01")


class AdminCohortWindowClampTests(SimpleTestCase):
    def test_clamps_cohort_window_into_period(self):
        start, end, error = admin_clamp_cohort_window(
            date(2025, 1, 1),
            date(2025, 12, 31),
            date(2024, 12, 1),
            date(2025, 2, 15),
        )

        self.assertIsNone(error)
        self.assertEqual(start, date(2025, 1, 1))
        self.assertEqual(end, date(2025, 2, 15))

    def test_keeps_cohort_window_when_already_inside(self):
        start, end, error = admin_clamp_cohort_window(
            date(2025, 1, 1),
            date(2025, 12, 31),
            date(2025, 3, 1),
            date(2025, 3, 31),
        )

        self.assertIsNone(error)
        self.assertEqual(start, date(2025, 3, 1))
        self.assertEqual(end, date(2025, 3, 31))

    def test_rejects_inverted_cohort_window(self):
        start, end, error = admin_clamp_cohort_window(
            date(2025, 1, 1),
            date(2025, 12, 31),
            date(2025, 5, 1),
            date(2025, 4, 1),
        )

        self.assertIsNone(start)
        self.assertIsNone(end)
        self.assertEqual(error, "Начало когорты больше её конца")

    def test_rejects_cohort_window_outside_period(self):
        start, end, error = admin_clamp_cohort_window(
            date(2025, 1, 1),
            date(2025, 3, 31),
            date(2025, 6, 1),
            date(2025, 6, 30),
        )

        self.assertIsNone(start)
        self.assertIsNone(end)
        self.assertEqual(error, "Окно когорты вне выбранного диапазона")


class AdminCohortStatsEndpointTests(SimpleTestCase):
    class FakeSession:
        def close(self):
            return None

    def test_endpoint_does_not_run_builder_without_admin_role(self):
        request = RequestFactory().get(
            "/support-admin/api/cohort-stats/",
            {"start": "2025-01-01", "end": "2025-12-31"},
        )
        request.session = {}

        with mock.patch(
            "engine.views.build_admin_cohort_retention_stats"
        ) as builder:
            response = support_admin_api_cohort_stats(request)

        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(builder.called)

    def test_invalid_period_returns_400(self):
        request = RequestFactory().get(
            "/support-admin/api/cohort-stats/",
            {"start": "2025-12-31", "end": "2025-01-01"},
        )

        with (
            mock.patch(
                "engine.views.require_support_admin_any", return_value=None
            ),
            mock.patch(
                "engine.views.build_admin_cohort_retention_stats"
            ) as builder,
        ):
            response = support_admin_api_cohort_stats(request)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(builder.called)
        self.assertEqual(json.loads(response.content)["status"], "error")

    def test_passes_clamped_cohort_window_to_builder(self):
        request = RequestFactory().get(
            "/support-admin/api/cohort-stats/",
            {
                "start": "2025-01-01",
                "end": "2025-12-31",
                # Когорта частично выходит за начало диапазона — должна зажаться.
                "cohort_start": "2024-12-01",
                "cohort_end": "2025-01-31",
                "granularity": "month",
            },
        )

        with (
            mock.patch(
                "engine.views.require_support_admin_any", return_value=None
            ),
            mock.patch(
                "engine.views.session_factory",
                return_value=self.FakeSession(),
            ),
            mock.patch(
                "engine.views.build_admin_cohort_retention_stats",
                return_value={"sentinel": True},
            ) as builder,
        ):
            response = support_admin_api_cohort_stats(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["result"], {"sentinel": True})

        args = builder.call_args.args
        # args: (db_session, period_start, period_end, cohort_start, cohort_end, granularity)
        self.assertEqual(args[1], date(2025, 1, 1))
        self.assertEqual(args[2], date(2025, 12, 31))
        self.assertEqual(args[3], date(2025, 1, 1))
        self.assertEqual(args[4], date(2025, 1, 31))
        self.assertEqual(args[5], "month")


class AdminReferralActivityEndpointTests(SimpleTestCase):
    def test_requires_analytics_role(self):
        request = RequestFactory().get(
            "/support-admin/api/referral-activity/",
            {"start": "2026-09-01", "end": "2026-09-10"},
        )
        request.session = {}
        with mock.patch("engine.views.admin_referral_activity") as builder:
            response = views.support_admin_api_referral_activity(request)
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(builder.called)

    def test_invalid_period_returns_400(self):
        request = RequestFactory().get(
            "/support-admin/api/referral-activity/",
            {"start": "2026-09-10", "end": "2026-09-01"},
        )
        request.session = {}
        with mock.patch(
            "engine.views.require_support_admin_any", return_value=None
        ), mock.patch("engine.views.admin_referral_activity") as builder:
            response = views.support_admin_api_referral_activity(request)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(builder.called)


class AdminStatsSalesModeTests(SimpleTestCase):
    def test_absolute_mode_replaces_cash_kpis_and_tariffs(self):
        from engine.views import admin_stats_apply_sales_mode

        cohort_totals = {
            "subscriptions": 100,
            "connections": 50,
            "unique_paying_users": 4,
            "payments": 5,
            "revenue": 1200,
        }
        sales_series = {
            "mode": "absolute",
            "totals": {
                "unique_paying_users": 18,
                "payments": 27,
                "revenue": 9400,
                "tariffs": {"1 месяц": 20, "1 год": 7},
            },
        }

        totals, tariffs, cohort_payers = admin_stats_apply_sales_mode(
            cohort_totals, {"1 месяц": 5}, sales_series
        )

        self.assertEqual(totals["subscriptions"], 100)
        self.assertEqual(totals["connections"], 50)
        self.assertEqual(totals["unique_paying_users"], 18)
        self.assertEqual(totals["payments"], 27)
        self.assertEqual(totals["revenue"], 9400)
        self.assertEqual(tariffs, {"1 месяц": 20, "1 год": 7})
        self.assertEqual(cohort_payers, 4)

    def test_series_totals_match_chart_buckets(self):
        from engine.views import admin_stats_sales_series_totals

        totals = admin_stats_sales_series_totals(
            [
                {
                    "payments": 2,
                    "revenue": 600,
                    "tariffs": [{"name": "1 месяц", "count": 2}],
                },
                {
                    "payments": 3,
                    "revenue": 1500,
                    "tariffs": [
                        {"name": "1 месяц", "count": 1},
                        {"name": "1 год", "count": 2},
                    ],
                },
            ],
            unique_paying_users=4,
        )

        self.assertEqual(totals["payments"], 5)
        self.assertEqual(totals["revenue"], 2100)
        self.assertEqual(totals["unique_paying_users"], 4)
        self.assertEqual(totals["tariffs"], {"1 месяц": 3, "1 год": 2})


class AdminCohortDashboardTemplateTests(SimpleTestCase):
    def test_analytics_dense_views_use_readable_typography(self):
        template = template_source("engine/templates/admin_dashboard.html")

        def css_rule(selector):
            start = template.index(f"{selector} {{")
            return template[start:template.index("}", start) + 1]

        expected_sizes = {
            "#panel-stats .sources-th": "font-size: 10px",
            "#panel-stats .sources-cell": "font-size: 12.5px",
            "#panel-stats .sources-cell-name-copy b": "font-size: 13px",
            "#panel-stats .analytics-insight-heading h3": "font-size: 16px",
            "#panel-stats .analytics-tariff-name": "font-size: 12px",
            "#source-users-modal .source-user-head": "font-size: 10px",
            "#source-users-modal .source-user-identity-copy b": "font-size: 12.5px",
            "#source-users-modal .source-user-status": "font-size: 11px",
            "#source-users-modal .modal-page-info": "font-size: 11px",
        }
        for selector, font_size in expected_sizes.items():
            with self.subTest(selector=selector):
                self.assertIn(font_size, css_rule(selector))

        self.assertIn("min-width: 1260px", css_rule("#panel-stats .sources-table-row"))
        self.assertIn("min-height: 60px", css_rule("#panel-stats .sources-table-row"))
        self.assertIn("min-width: 980px", css_rule("#source-users-modal .source-users-table"))
        self.assertIn("min-height: 64px", css_rule("#source-users-modal .source-user-row"))

    def test_source_details_button_keeps_dark_text_on_gold_background(self):
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn(
            ".source-details-btn {\n    color: #171300;\n}",
            css,
        )
        self.assertIn(
            ".source-details-btn:hover {\n    color: #050505;\n}",
            css,
        )
        self.assertIn(".source-details-btn:focus-visible {", css)
        self.assertIn(
            ".source-details-btn i {\n    color: currentColor;\n}",
            css,
        )
        self.assertNotIn(
            'html[data-admin-theme="light"] .source-details-btn,',
            css,
        )

    def test_sources_panel_has_totals_summary_above_rows(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("function sourceTotals(sources)", template)
        self.assertIn("function renderSourcesSummary(sources)", template)
        self.assertIn('class="sources-summary"', template)
        self.assertIn("${renderSourcesSummary(sources)}\n                ${body}", template)
        for label in (
            "Подписки",
            "Подключения",
            "Покупатели",
            "Платежи",
            "Выручка",
            "В подключение",
            "В продажу",
        ):
            self.assertIn(f"<span>{label}</span>", template)

    def test_cohort_analytics_section_is_present(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("data-cohort-stats-url", template)
        self.assertIn('id="cohort-form"', template)
        self.assertIn("Когортный анализ", template)
        self.assertIn('name="cohort_start"', template)
        self.assertIn('name="cohort_end"', template)
        self.assertIn("data-cohort-period", template)
        self.assertIn("data-cohort-window", template)
        self.assertIn("function loadCohortStats", template)
        self.assertIn("applyCohortPeriodPreset", template)
        # Когортный график переиспользует тот же интерактивный renderer, что и
        # сквозная аналитика, не дублируя отрисовку и tooltip.
        self.assertIn("drawSalesSeriesChart(chart, series);", template)
        self.assertIn("bindSalesChartTooltip(\n                    chart,", template)

    def test_both_analytics_charts_use_acquisition_style_renderer(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("const acquisitionChartColors = Object.freeze", template)
        # Серии берут разнотонную палитру: серо-жёлтый вариант делал соседние
        # серии неразличимыми (см. AdminChartPaletteTests).
        self.assertIn("repeat: CHART_TONES.indigo.dark,", template)
        self.assertIn("new: CHART_TONES.amber.dark,", template)
        self.assertIn("payers: CHART_TONES.green.dark,", template)
        self.assertIn("const tariffChartColors = tariffChartTones.map", template)
        self.assertNotIn("['#22c55e', '#84cc16', '#a3e635'", template)
        self.assertIn("function renderSalesChartLegend(series)", template)
        self.assertIn("Покупатели (правая ось)", template)
        self.assertIn("Number(tariff.revenue || 0)", template)
        self.assertIn("Number(row.unique_paying_users || 0)", template)
        self.assertIn("canvas.__salesChartMeta", template)
        self.assertIn("meta.render(index);", template)
        self.assertIn('id="stats-chart"', template)
        self.assertIn('id="cohort-chart"', template)
        self.assertNotIn("function bindCohortChartTooltip", template)

    def test_admin_theme_switch_defaults_dark_and_persists_light_choice(self):
        template = template_source("engine/templates/admin_dashboard.html")
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn('<meta name="color-scheme" content="dark light">', template)
        # Тема должна выставляться до загрузки стилей, иначе при светлой
        # теме будет вспышка тёмного фона. Привязываться к конкретному
        # CDN нельзя — состав подключаемых стилей меняется.
        self.assertLess(
            template.index('monkey_island_admin_theme_v1'),
            template.index('href="https://cdnjs.cloudflare.com'),
        )
        self.assertIn("let theme = 'dark'", template)
        self.assertIn("localStorage.getItem(storageKey) === 'light'", template)
        self.assertEqual(template.count('data-admin-theme-toggle'), 4)
        self.assertEqual(template.count('role="switch" aria-checked="false" aria-label="Включить светлую тему"'), 2)
        self.assertEqual(template.count('class="admin-theme-switch-label"'), 2)
        desktop_actions_start = template.index('<div class="admin-topbar-actions">')
        desktop_actions = template[desktop_actions_start:template.index('</header>', desktop_actions_start)]
        self.assertLess(
            desktop_actions.index('data-admin-theme-toggle'),
            desktop_actions.index('class="admin-pill role"'),
        )
        self.assertIn("function applyAdminTheme(theme, persist = false)", template)
        self.assertIn("localStorage.setItem(ADMIN_THEME_STORAGE_KEY, normalizedTheme)", template)
        self.assertIn("button.setAttribute('aria-label', actionLabel)", template)
        self.assertIn("visibleLabel.textContent = isLight ? 'Светлая' : 'Тёмная'", template)
        self.assertIn("window.addEventListener('storage'", template)
        self.assertIn("configJsonEditor.setOption('theme', isLight ? 'default' : 'material-darker')", template)
        self.assertIn("canvas.__salesChartMeta.render(null)", template)
        self.assertIn("canvas.__acqMeta.render(null)", template)
        self.assertIn(
            "tipEl.className = 'sales-chart-tooltip acquisition-chart-tooltip';",
            template,
        )
        self.assertNotIn("display:none;background:#15161c", template)
        self.assertIn('html[data-admin-theme="light"]', css)
        self.assertIn("color-scheme: light", css)
        self.assertIn('html[data-admin-theme="light"] .sales-chart-tooltip', css)
        self.assertIn("background: rgba(255, 255, 255, .98);", css)
        self.assertIn('.admin-theme-switch[aria-checked="true"] .admin-theme-thumb', css)
        self.assertIn("min-width: 136px;", css)
        self.assertIn(".admin-theme-switch-label", css)
        self.assertIn("border-color: rgba(var(--accent-rgb), .42);", css)

    def test_light_theme_chart_help_uses_readable_content_colors(self):
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        for selector in (
            'html[data-admin-theme="light"] body .chart-help-title',
            'html[data-admin-theme="light"] body .chart-help-list li',
            'html[data-admin-theme="light"] body .chart-help-list li b',
            'html[data-admin-theme="light"] body .chart-help-list li code',
            'html[data-admin-theme="light"] body .chart-help-close',
            'html[data-admin-theme="light"] body .chart-help-sec.calc .chart-help-sec-label',
            'html[data-admin-theme="light"] body .chart-help-sec.warn .chart-help-sec-label',
        ):
            self.assertIn(selector, css)
        self.assertIn("color: rgba(34, 39, 47, .78);", css)
        self.assertIn("color: #1f242c;", css)
        self.assertIn("background: #edf0f4;", css)

    def test_analytics_has_overview_and_cohort_subtabs(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('id="subpanel-overview"', template)
        self.assertIn('id="subpanel-cohort"', template)
        self.assertIn('data-subtab="overview"', template)
        self.assertIn('data-subtab="cohort"', template)
        self.assertIn("function showSubtab", template)
        self.assertIn("setupSubtabs('panel-stats')", template)

    def test_cohort_form_groups_period_and_cohort_ranges(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('class="card form-card cohort-builder"', template)
        self.assertIn('aria-label="Диапазон графика"', template)
        self.assertIn('aria-label="Окно когорты"', template)
        self.assertIn("Быстрый диапазон", template)
        self.assertIn("Окно от начала диапазона", template)
        self.assertIn('data-cohort-window="range-7"', template)
        self.assertIn('data-cohort-window="range-30"', template)
        self.assertIn('data-cohort-window="range-90"', template)
        self.assertIn('data-cohort-window="range-month-end"', template)
        self.assertIn("До конца месяца", template)
        self.assertIn("cohort-submit-row", template)
        self.assertIn("clearCohortPresetActiveForField", template)
        self.assertNotIn("Диапазон — начало", template)
        self.assertNotIn("Когорта — начало", template)
        self.assertNotIn("Месяц старта", template)

    def test_system_tab_grouped_into_subtabs(self):
        # Вкладка «Система» организована подвкладками (как «Аналитика»),
        # вместо жёсткой двухколоночной сетки, вылезавшей за экран.
        template = template_source("engine/templates/admin_dashboard.html")

        for slug in ("sys-tariffs", "sys-winback", "sys-payment", "sys-referral", "sys-alerts", "sys-antiabuse", "sys-general"):
            self.assertIn(f'data-subtab="{slug}"', template)
            self.assertIn(f'id="subpanel-{slug}"', template)
        # Runtime-настройки раскладываются по контейнерам групп.
        for group in ("tariffs", "winback", "payment", "referral", "alerts", "general", "other"):
            self.assertIn(f'data-settings-group="{group}"', template)
        # Тумблер win-back живёт в группе «winback»: без него ключ уехал бы в
        # «Прочие», а выключить цепочку из админки было бы неочевидно.
        self.assertIn(
            "{slug: 'winback', keys: ['winback_enabled', 'winback_price_month'",
            template,
        )
        self.assertIn(
            'data-subtab="sys-payment" role="tab"><i class="fas fa-credit-card"></i>Платёжные шлюзы',
            template,
        )
        self.assertIn('<h2 class="system-card-title">Платёжные шлюзы</h2>', template)
        self.assertNotIn('</i>Платёж</button>', template)
        self.assertIn("setupSubtabs('panel-system')", template)
        # Во вкладке остаются глобальные бонусы и антифрод. Ручная блокировка
        # относится к конкретному найденному клиенту и сюда не дублируется.
        self.assertIn('id="referral-antifraud-form"', template)
        self.assertIn('class="card system-card system-settings-card referral-bonus-card"', template)
        self.assertIn('class="card system-card referral-antifraud-card"', template)
        self.assertNotIn('id="referral-block-form"', template)
        self.assertNotIn("function submitReferralBlock", template)
        self.assertNotIn('data-subtab="sys-operations"', template)
        self.assertNotIn('id="subpanel-sys-operations"', template)
        self.assertNotIn('data-recurrents-url=', template)
        self.assertNotIn("function loadRecurrents", template)
        # Таблица «Топ клиентов по платежам» убрана из вкладки «Платежи».
        self.assertNotIn('data-top-payments-url=', template)
        self.assertNotIn("loadTopPayments", template)
        # Сетка рефералки не должна использовать фиксированную минимальную ширину колонок,
        # из-за которой контент вылезал за экран.
        self.assertNotIn(".system-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px", template)
        self.assertIn(".system-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", template)

    def test_referral_block_management_is_part_of_found_client(self):
        template = template_source("engine/templates/admin_dashboard.html")
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn("function clientReferralControlHtml", template)
        self.assertIn("function clientOverviewSectionHtml(result, refResult)", template)
        self.assertIn("const controlHtml = clientReferralControlHtml(refResult);", template)
        self.assertIn('data-client-section-link="referrals"', template)
        self.assertIn("Действия с клиентом", template)
        self.assertIn('data-client-referral-block-action="block"', template)
        self.assertIn('data-client-referral-block-action="unblock"', template)
        self.assertIn("function clientReferralBlockAction", template)
        self.assertIn("formData.set('q', clientCardState.q)", template)
        self.assertIn("await renderClientCard();", template)
        self.assertNotIn("await renderClientCard('referrals')", template)
        self.assertNotIn("function clientSubscriptionManageHtml", template)
        self.assertIn("Заблокировать", template)
        self.assertIn("Разблокировать", template)
        self.assertIn(".client-referral-manage-card", css)
        self.assertIn(".client-ref-control-actions", css)

    def test_referral_settings_match_standard_system_width_without_duplicate_antifraud_rows(self):
        template = template_source("engine/templates/admin_dashboard.html")
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn("{slug: 'referral', keys: ['join_referrer_bonus_days', 'traffic_referrer_bonus_days', 'purchase_referrer_bonus_days', 'referral_bonus_days']}", template)
        self.assertIn("{slug: 'referral-antifraud', keys: ['referral_registration_autoblock_enabled', 'referral_registration_burst_limit', 'referral_registration_burst_window_minutes']}", template)
        self.assertIn("#subpanel-sys-referral .referral-bonus-card", css)
        self.assertIn(".referral-settings-stack {\n    display: grid;\n    gap: 16px;\n    width: 100%;\n    max-width: 960px;", css)
        # Антифрод — единый блок в стиле таблицы настроек, без дублирования
        # лимита и окна в отдельных статус-карточках.
        self.assertIn(".antifraud-panel", css)
        self.assertIn(".antifraud-row", css)
        self.assertNotIn(".referral-antifraud-layout", css)
        self.assertNotIn(".referral-antifraud-fields", css)
        self.assertNotIn(".referral-antifraud-stat {", css)

    def test_cohort_shows_invited_referrals_metrics(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("invited_referrals", template)
        self.assertIn("invited_referrals_active", template)
        self.assertIn("invited_referrals_paid", template)
        self.assertIn("Привела рефералов", template)

    def test_cohort_all_time_preset_starts_at_business_start_not_2020(self):
        template = template_source("engine/templates/admin_dashboard.html")

        # «Всё время» в когорте отсчитывается от старта бизнеса (апрель 2025),
        # а не от условного 2020 года, по которому нет данных.
        self.assertIn("COHORT_BUSINESS_START = new Date(2025, 3, 1)", template)
        self.assertNotIn("start = new Date(2020, 0, 1);\n                end = todayOnly;", template)

    def test_cohort_tooltip_shows_month_name_for_monthly_granularity(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("function cohortBucketLabel", template)
        self.assertIn("cohortBucketLabel(row.label, series.granularity)", template)

    def test_cohort_panel_has_metrics_legend(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('class="metrics-legend"', template)
        self.assertIn("Как читать показатели", template)
        # Определения ключевых показателей присутствуют.
        for term in ("ARPU", "ARPPU", "Размер когорты", "Привела рефералов"):
            self.assertIn(term, template)

    def test_existing_stats_form_is_untouched(self):
        template = template_source("engine/templates/admin_dashboard.html")

        # Старый блок аналитики и его контракт остаются на месте.
        self.assertIn('id="stats-form"', template)
        self.assertIn('name="sales_mode"', template)
        self.assertIn("function loadStats", template)
        self.assertIn('<option value="auto" selected>Авто</option>', template)
        self.assertIn('<option value="week">По неделям</option>', template)

    def test_stats_period_presets_are_grouped_and_include_calendar_ranges(self):
        template = template_source("engine/templates/admin_dashboard.html")

        for label in (
            "Текущие",
            "Эта неделя",
            "Этот месяц",
            "Этот год",
            "Прошлые",
            "Прошлый месяц",
            "Позапрошлый месяц",
            "Скользящие",
            "Быстро",
        ):
            self.assertIn(label, template)

        for preset in (
            "this-week",
            "this-month",
            "last-month",
            "prev-month",
            "this-year",
        ):
            self.assertIn(f'data-period-preset="{preset}"', template)

        self.assertIn("function startOfIsoWeek", template)
        self.assertIn("preset === 'last-month'", template)
        self.assertIn("preset === 'prev-month'", template)


class TelegramAuthBotTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(
        TELEGRAM_AUTH_BOTS={
            "monkey-island-vps.com": {
                "username": "vps_auth_bot",
                "token": "111:first-token",
            },
            "monkey-island-vpn.com": {
                "username": "vpn_auth_bot",
                "token": "222:second-token",
            },
        },
        TG_BOT_USERNAME="legacy_bot",
        TELEGRAM_AUTH_BOT_TOKEN="333:legacy-token",
    )
    def test_selects_telegram_auth_bot_by_host(self):
        request = self.factory.get("/login/", HTTP_HOST="monkey-island-vpn.com")

        bot = get_telegram_auth_bot(request.get_host())

        self.assertEqual(bot["username"], "vpn_auth_bot")
        self.assertEqual(bot["token"], "222:second-token")

    @override_settings(
        TELEGRAM_WEB_LOGIN_START_CODES={
            "monkeyislandvpn.com": "webv2",
            "monkey-island-vpn.com": "webv3",
        }
    )
    def test_selects_telegram_web_login_start_code_by_host(self):
        self.assertEqual(
            get_telegram_web_login_start_code("monkeyislandvpn.com"),
            "webv2",
        )
        self.assertEqual(
            get_telegram_web_login_start_code("monkey-island-vpn.com"),
            "webv3",
        )
        self.assertEqual(get_telegram_web_login_start_code("mnk-island.org"), "web")

    def test_parses_telegram_web_login_start_codes_in_both_orders(self):
        self.assertEqual(
            telegram_web_login_start_codes(
                "mnk-island.org|webv1,webv2|https://monkeyislandvpn.com"
            ),
            {
                "mnk-island.org": "webv1",
                "monkeyislandvpn.com": "webv2",
            },
        )

    @override_settings(
        TELEGRAM_AUTH_BOTS={},
        TG_BOT_USERNAME="@legacy_bot",
        TELEGRAM_AUTH_BOT_TOKEN="333:legacy-token",
    )
    def test_falls_back_to_legacy_telegram_auth_bot(self):
        request = self.factory.get("/login/", HTTP_HOST="monkey-island-vpn.com")

        bot = get_telegram_auth_bot(request.get_host())

        self.assertEqual(bot["username"], "legacy_bot")
        self.assertEqual(bot["token"], "333:legacy-token")

    def test_verify_telegram_widget_auth_uses_selected_bot_token(self):
        auth_data = {
            "id": "123456",
            "first_name": "Alex",
            "auth_date": str(int(time.time())),
        }
        bot_token = "222:second-token"
        data_check_string = "\n".join(
            f"{key}={auth_data[key]}" for key in sorted(auth_data)
        )
        secret_key = hashlib.sha256(bot_token.encode()).digest()
        auth_data["hash"] = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        self.assertTrue(verify_telegram_widget_auth(auth_data, bot_token))
        self.assertFalse(verify_telegram_widget_auth(auth_data, "111:first-token"))

    def test_verify_telegram_widget_auth_stale_auth_date_fails(self):
        auth_data = {
            "id": "123456",
            "first_name": "Alex",
            "auth_date": str(int(time.time()) - 90000),
        }
        bot_token = "222:second-token"
        data_check_string = "\n".join(
            f"{key}={auth_data[key]}" for key in sorted(auth_data)
        )
        secret_key = hashlib.sha256(bot_token.encode()).digest()
        auth_data["hash"] = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        self.assertFalse(verify_telegram_widget_auth(auth_data, bot_token))

    @override_settings(
        ALLOWED_HOSTS=["monkey-island-vpn.com"],
        CABINET_DOMAINS=["monkey-island-vpn.com"],
        DEFAULT_CABINET_DOMAIN="https://monkey-island-vpn.com",
        TELEGRAM_AUTH_BOTS={
            "monkey-island-vpn.com": {
                "username": "vpn_auth_bot",
                "token": "222:second-token",
            },
        },
    )
    def test_empty_telegram_callback_renders_login_without_error(self):
        request = self.factory.get(
            "/login/telegram-auth/",
            HTTP_HOST="monkey-island-vpn.com",
            secure=True,
        )
        request.session = {}

        response = auth_by_telegram_widget(request)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            "Не удалось подтвердить вход через Telegram",
            response.content.decode(),
        )

    @override_settings(
        ALLOWED_HOSTS=["monkey-island-vpn.com"],
        CABINET_DOMAINS=["monkey-island-vpn.com"],
        DEFAULT_CABINET_DOMAIN="https://monkey-island-vpn.com",
        GOOGLE_OAUTH_CLIENT_ID="",
        GOOGLE_OAUTH_CLIENT_SECRET="",
        YANDEX_OAUTH_CLIENT_ID="",
        YANDEX_OAUTH_CLIENT_SECRET="",
        TELEGRAM_AUTH_BOTS={
            "monkey-island-vpn.com": {
                "username": "vpn_auth_bot",
                "token": "222:second-token",
            },
        },
    )
    def test_telegram_login_points_to_auth_bot(self):
        request = self.factory.get(
            "/login/",
            HTTP_HOST="monkey-island-vpn.com",
            secure=True,
        )
        request.session = {}

        response = render_login(request)
        content = response.content.decode()

        self.assertIn("https://t.me/vpn_auth_bot?start=web", content)


class TelegramWebappAuthTests(SimpleTestCase):
    """Авторизация Telegram Mini App (кабинет в WebView из кнопки меню бота)."""

    BOT_TOKEN = "222:second-token"

    def build_init_data(self, bot_token=None, auth_date=None, user_id=123456):
        from urllib.parse import urlencode

        fields = {
            "auth_date": str(auth_date or int(time.time())),
            "query_id": "AAE-test",
            "user": json.dumps({"id": user_id, "first_name": "Alex"}),
        }
        data_check_string = "\n".join(
            f"{key}={fields[key]}" for key in sorted(fields)
        )
        secret_key = hmac.new(
            b"WebAppData",
            (bot_token or self.BOT_TOKEN).encode(),
            hashlib.sha256,
        ).digest()
        fields["hash"] = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        return urlencode(fields)

    def test_valid_init_data_passes_verification(self):
        init_data = self.build_init_data()

        fields = verify_telegram_webapp_init_data(init_data, self.BOT_TOKEN)

        self.assertIsNotNone(fields)
        self.assertEqual(get_telegram_webapp_user_id(fields), 123456)

    def test_wrong_bot_token_fails_verification(self):
        init_data = self.build_init_data()

        self.assertIsNone(
            verify_telegram_webapp_init_data(init_data, "111:first-token")
        )

    def test_stale_auth_date_fails_verification(self):
        init_data = self.build_init_data(auth_date=int(time.time()) - 90000)

        self.assertIsNone(verify_telegram_webapp_init_data(init_data, self.BOT_TOKEN))

    def test_missing_hash_fails_verification(self):
        self.assertIsNone(
            verify_telegram_webapp_init_data("auth_date=123", self.BOT_TOKEN)
        )
        self.assertIsNone(verify_telegram_webapp_init_data("", self.BOT_TOKEN))

    def test_tampered_user_fails_verification(self):
        init_data = self.build_init_data(user_id=123456)
        tampered = init_data.replace("123456", "999999")

        self.assertIsNone(verify_telegram_webapp_init_data(tampered, self.BOT_TOKEN))

    def test_invalid_user_payload_returns_none_id(self):
        self.assertIsNone(get_telegram_webapp_user_id({}))
        self.assertIsNone(get_telegram_webapp_user_id({"user": "not-json"}))
        self.assertIsNone(get_telegram_webapp_user_id({"user": "{}"}))

    @override_settings(
        TELEGRAM_AUTH_BOTS={
            "monkey-island-vpn.com": {
                "username": "vpn_auth_bot",
                "token": "222:second-token",
            },
            "monkey-island-vps.com": {
                "username": "vps_auth_bot",
                "token": "111:first-token",
            },
        },
        TG_BOT_USERNAME="legacy_bot",
        TELEGRAM_AUTH_BOT_TOKEN="333:legacy-token",
    )
    def test_all_auth_bots_are_tried_domain_bot_first(self):
        """Mini App обоих ботов открывает один кабинетный домен, но initData
        подписан токеном того бота, из которого открыли. Регресс-гард: вход
        из vps-бота на vpn-домене падал с «Не удалось подтвердить вход»."""
        from engine.views import get_all_telegram_auth_bots

        bots = get_all_telegram_auth_bots("monkey-island-vpn.com")

        tokens = [bot["token"] for bot in bots]
        # Доменный бот первым, все остальные (включая legacy) — тоже в списке
        self.assertEqual(tokens[0], "222:second-token")
        self.assertIn("111:first-token", tokens)
        self.assertIn("333:legacy-token", tokens)
        self.assertEqual(len(tokens), len(set(tokens)))  # без дублей

        # initData, подписанный «чужим» для домена (vps) ботом, проходит
        # проверку одним из доверенных токенов.
        init_data = self.build_init_data(bot_token="111:first-token")
        verified_by = [
            bot["username"]
            for bot in bots
            if verify_telegram_webapp_init_data(init_data, bot["token"]) is not None
        ]
        self.assertEqual(verified_by, ["vps_auth_bot"])

    def test_webapp_auth_view_iterates_over_all_bots(self):
        import inspect

        from engine.views import auth_by_telegram_webapp

        src = inspect.getsource(auth_by_telegram_webapp)
        self.assertIn("get_all_telegram_auth_bots", src)
        self.assertIn("for telegram_bot in telegram_bots", src)

    @override_settings(
        ALLOWED_HOSTS=["mnk-island.org"],
        TELEGRAM_AUTH_BOTS={
            "mnk-island.org": {
                "username": "monkeyislandvpnbot",
                "token": "222:second-token",
            },
            "monkey-island-vps.com": {
                "username": "monkeyislandvpsbot",
                "token": "111:first-token",
            },
        },
    )
    def test_webapp_login_succeeds_with_init_data_from_vps_bot_on_vpn_domain(self):
        """Сквозной регресс-гард продовой раскладки: webapp_url обоих ботов
        указывает на mnk-island.org (домен vpn-бота), а initData подписан
        vps-ботом. Старый код отвечал логин-страницей с «Не удалось
        подтвердить вход»; новый должен дологинить и средиректить в кабинет."""
        from engine.views import auth_by_telegram_webapp

        init_data = self.build_init_data(bot_token="111:first-token", user_id=777)
        request = RequestFactory().post(
            "/tg-webapp/auth/",
            {"init_data": init_data},
            HTTP_HOST="mnk-island.org",
            secure=True,
        )
        request.session = {}

        fake_user = SimpleNamespace(id=1, telegram_id=777)

        class FakeBegin:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return fake_user

        class FakeSession:
            def begin(self):
                return FakeBegin()

            def query(self, model):
                return FakeQuery()

            def close(self):
                pass

        with mock.patch(
            "engine.views.session_factory", return_value=FakeSession()
        ), mock.patch("engine.views.add_event_log_once"), mock.patch(
            "engine.views.authorize_user_session"
        ) as authorize_mock:
            response = auth_by_telegram_webapp(request)

        # Подпись принята чужим для домена (vps) токеном: логин состоялся
        authorize_mock.assert_called_once()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/dashboard/")
        self.assertTrue(request.session.get("tg_webapp_mode"))


class PaymentRedirectTests(SimpleTestCase):
    def test_site_trial_registration_flag_falls_back_to_env_setting(self):
        class FakeSession:
            def get(self, model, key):
                return None

        with override_settings(SITE_TRIAL_REGISTRATION_ENABLED=True):
            self.assertTrue(site_trial_registration_enabled(FakeSession()))

    @override_settings(SITE_TRIAL_REGISTRATION_ENABLED=True)
    def test_site_trial_registration_database_setting_overrides_env(self):
        class FakeSession:
            def get(self, model, key):
                return SimpleNamespace(value="false")

        self.assertFalse(site_trial_registration_enabled(FakeSession()))

    @override_settings(SITE_TRIAL_REGISTRATION_ENABLED=False)
    def test_site_trial_registration_flag_controls_all_site_channels(self):
        class FakeSession:
            def get(self, model, key):
                return None

        self.assertFalse(
            should_create_trial_for_channel(FakeSession(), "site_telegram_widget")
        )
        self.assertFalse(should_create_trial_for_channel(FakeSession(), "site"))

    @override_settings(SITE_TRIAL_REGISTRATION_ENABLED=False)
    def test_create_site_user_without_trial_creates_local_user_without_rwms(self):
        class FakeSession(_SiteRegistrationFakeSession):
            pass

        session = FakeSession()
        request = SimpleNamespace()

        with mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": 42, "ymid": 1001},
        ), mock.patch(
            "engine.views.find_rwms_user_by_identity",
            return_value=None,
        ) as find_rwms, mock.patch(
            "engine.views.create_user",
        ) as create_rwms_user, mock.patch(
            "engine.views.add_user_to_traffic_progress",
        ), mock.patch(
            "engine.views.add_event_log",
        ) as add_event_log:
            user = create_site_user(
                session,
                "new@example.com",
                request,
                creation_channel="site_magic_link",
            )

        self.assertEqual(user.email, "new@example.com")
        self.assertIsNone(user.telegram_id)
        self.assertIsNone(user.expire_at)
        self.assertEqual(user.ymid, 1001)
        self.assertIn(user, session.added)
        find_rwms.assert_called_once_with(
            email="new@example.com",
            telegram_id=None,
            username=site_registration_username("new@example.com", None),
        )
        create_rwms_user.assert_not_called()
        add_event_log.assert_not_called()

    def test_site_rwms_client_uses_configured_default_deadline(self):
        from engine import views

        self.assertEqual(
            views.rwms_client._RwmsClientSync__timeout,
            settings.RWMS_RPC_TIMEOUT_SECONDS,
        )

    @override_settings(SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=False)
    def test_identity_lookup_without_legacy_scan_never_downloads_panel(self):
        from engine import views

        with mock.patch("engine.views.rwms_client") as rwms:
            rwms.get_user_by_username_strict.return_value = None
            self.assertIsNone(
                views.find_rwms_user_by_identity(
                    email="new@example.com", username="site-user"
                )
            )

        rwms.get_all_users.assert_not_called()

    @override_settings(
        SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=True,
        RWMS_BULK_RPC_TIMEOUT_SECONDS=31.0,
    )
    def test_legacy_scan_without_panel_reply_is_ambiguous_not_absent(self):
        """TR-05: None от GetAllUsers — сбой панели, а не «подписки нет»."""
        from engine import views

        with mock.patch("engine.views.rwms_client") as rwms, self.assertLogs(
            level="ERROR"
        ):
            rwms.get_user_by_username_strict.return_value = None
            rwms.get_all_users.return_value = None
            with self.assertRaises(SiteRegistrationUnavailable):
                views.find_rwms_user_by_identity(
                    email="legacy@example.com", username="site-user"
                )

        rwms.get_all_users.assert_called_once_with(timeout=31.0)

    @override_settings(
        SITE_TRIAL_REGISTRATION_ENABLED=False,
        SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=True,
    )
    def test_create_site_user_postpones_registration_when_legacy_scan_fails(self):
        session = _SiteRegistrationFakeSession()

        with mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": None, "ymid": None},
        ), mock.patch("engine.views.rwms_client") as rwms, mock.patch(
            "engine.views.create_user"
        ) as create_rwms_user, self.assertLogs(level="ERROR"):
            rwms.get_user_by_username_strict.return_value = None
            rwms.get_all_users.return_value = None
            with self.assertRaises(SiteRegistrationUnavailable):
                create_site_user(
                    session,
                    "legacy@example.com",
                    SimpleNamespace(),
                    creation_channel="site_magic_link",
                )

        # Ни локального аккаунта «без подписки», ни новой подписки в панели.
        self.assertEqual(session.added, [])
        create_rwms_user.assert_not_called()

    def _create_with_rwms_outage(self, email, **kwargs):
        session = _SiteRegistrationFakeSession()
        with mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": None, "ymid": None},
        ), mock.patch("engine.views.rwms_client") as rwms, mock.patch(
            "engine.views.create_user"
        ) as create_rwms_user, mock.patch(
            "engine.views.add_user_to_traffic_progress"
        ), self.assertLogs(level="ERROR") as logs:
            rwms.get_user_by_username_strict.side_effect = RwmsUnavailableError(
                deterministic_username(email), None, "deadline exceeded"
            )
            try:
                user = create_site_user(session, email, SimpleNamespace(), **kwargs)
            except SiteRegistrationUnavailable as error:
                user = error
        create_rwms_user.assert_not_called()
        rwms.add_user.assert_not_called()
        return session, user, rwms, logs

    @override_settings(
        SITE_TRIAL_REGISTRATION_ENABLED=False,
        SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=False,
    )
    def test_anonymous_purchase_creates_local_account_when_rwms_is_unavailable(self):
        """FINAL-PAY-01: анонимная покупка (allow_trial=False) при недоступной
        панели, как в HEAD, получает локальный аккаунт без подписки и ALERT;
        подписку по детерминированному username сведёт payment."""
        email = "buyer@example.com"
        session, user, rwms, logs = self._create_with_rwms_outage(
            email, allow_trial=False
        )

        self.assertIsInstance(user, User)
        self.assertEqual(session.added, [user])
        self.assertEqual(
            (user.email, user.username, user.expire_at),
            (email, deterministic_username(email), None),
        )
        rwms.get_all_users.assert_not_called()
        self.assertTrue(
            any(
                "ALERT: RWMS unavailable during anonymous purchase" in line
                for line in logs.output
            )
        )

    @override_settings(
        SITE_TRIAL_REGISTRATION_ENABLED=False,
        SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=False,
    )
    def test_magic_link_registration_still_fails_closed_when_rwms_is_unavailable(self):
        """FINAL-PAY-01: регистрация с входом (magic link, OAuth, Telegram) при
        недоступной панели по-прежнему отказывает, ничего не создавая."""
        for channel in ("site_magic_link", "site_google_oauth"):
            with self.subTest(channel=channel):
                session, result, _, logs = self._create_with_rwms_outage(
                    "reader@example.com", creation_channel=channel
                )
                self.assertIsInstance(result, SiteRegistrationUnavailable)
                self.assertEqual(session.added, [])
                self.assertFalse(any("ALERT" in line for line in logs.output))

    @override_settings(
        SITE_TRIAL_REGISTRATION_ENABLED=False,
        SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=True,
    )
    def test_anonymous_purchase_fails_closed_when_legacy_scan_cannot_run(self):
        """Включённый legacy-скан (TR-05) при недоступной панели выполнить
        нельзя: локальный аккаунт не создаётся и для анонимной покупки."""
        session, result, rwms, _ = self._create_with_rwms_outage(
            "legacy@example.com", allow_trial=False
        )
        self.assertIsInstance(result, SiteRegistrationUnavailable)
        self.assertEqual(session.added, [])
        rwms.get_all_users.assert_not_called()

    @override_settings(SITE_TRIAL_REGISTRATION_ENABLED=True, SITE_TRIAL_PERIOD_DAYS=7)
    def test_create_site_user_with_trial_flag_keeps_rwms_trial_path(self):
        class FakeSession(_SiteRegistrationFakeSession):
            next_user_id = 2

        session = FakeSession()
        request = SimpleNamespace()
        rw_user = _FakeSiteRwUser(username="rw-user")

        with mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": 42, "ymid": None},
        ), mock.patch(
            "engine.views.resolve_existing_site_subscription",
            return_value=None,
        ), mock.patch(
            "engine.views.create_user",
            return_value=rw_user,
        ) as create_rwms_user, mock.patch(
            "engine.views.add_user_to_traffic_progress",
        ), mock.patch(
            "engine.views.add_event_log",
        ) as add_event_log:
            user = create_site_user(
                session,
                "trial@example.com",
                request,
                creation_channel="site_magic_link",
            )

        self.assertEqual(user.email, "trial@example.com")
        self.assertIsNone(user.expire_at)
        create_rwms_user.assert_called_once()
        self.assertEqual(create_rwms_user.call_args.kwargs["trial_period_days"], 7)
        add_event_log.assert_called_once()

    @override_settings(SITE_TRIAL_REGISTRATION_ENABLED=True, SITE_TRIAL_PERIOD_DAYS=7)
    def test_create_site_user_without_email_confirmation_never_creates_trial(self):
        """B12/R03: анонимная покупка с лендинга создаёт аккаунт до
        подтверждения email — пробная подписка в панели не выдаётся даже при
        включённом site trial; существующая подписка ищется тем же strict-путём."""
        session = _SiteRegistrationFakeSession()

        with mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": 42, "ymid": None},
        ), mock.patch(
            "engine.views.should_create_trial_for_channel", return_value=True
        ), mock.patch(
            "engine.views.find_rwms_user_by_identity", return_value=None
        ) as find_rwms, mock.patch(
            "engine.views.resolve_existing_site_subscription"
        ) as resolve_for_trial, mock.patch(
            "engine.views.create_user"
        ) as create_rwms_user, mock.patch(
            "engine.views.add_user_to_traffic_progress"
        ), mock.patch(
            "engine.views.add_event_log"
        ) as add_event_log:
            user = create_site_user(
                session,
                "buyer@example.com",
                SimpleNamespace(),
                allow_trial=False,
            )

        self.assertEqual(user.email, "buyer@example.com")
        self.assertIsNone(user.expire_at)
        self.assertIn(user, session.added)
        find_rwms.assert_called_once_with(
            email="buyer@example.com",
            telegram_id=None,
            username=site_registration_username("buyer@example.com", None),
        )
        resolve_for_trial.assert_not_called()
        create_rwms_user.assert_not_called()
        add_event_log.assert_not_called()

    def test_runtime_actual_tariffs_use_database_prices(self):
        class FakeSession:
            values = {
                    BOT_TARIFF_PRICE_MONTH_SETTING: "199",
                    BOT_TARIFF_PRICE_YEAR_SETTING: "bad-value",
            }

            def query(self, model):
                return self

            def filter(self, *args):
                return self

            def all(self):
                return [
                    SimpleNamespace(key=key, value=value)
                    for key, value in self.values.items()
                ]

        tariffs = {
            tariff.db_tariff_id: tariff
            for tariff in get_runtime_actual_tariffs(FakeSession())
        }

        self.assertEqual(tariffs["month"].price, 199)
        self.assertEqual(tariffs["threemonths"].price, 599)
        self.assertEqual(tariffs["year"].price, 1799)

    def test_authenticated_payment_does_not_send_login_email(self):
        request = SimpleNamespace(
            user=SimpleNamespace(is_authenticated=True, id=42),
        )
        payment_user = SimpleNamespace(id=42)

        self.assertFalse(should_send_payment_login_email(request, payment_user))

    def test_anonymous_payment_sends_login_email(self):
        request = SimpleNamespace(
            user=SimpleNamespace(is_authenticated=False, id=None),
        )
        payment_user = SimpleNamespace(id=42)

        self.assertTrue(should_send_payment_login_email(request, payment_user))

    def test_yookassa_payment_uses_return_url(self):
        tariff = SimpleNamespace(
            price=100,
            db_tariff_id="one_month",
            description="1 месяц",
        )
        confirmation = SimpleNamespace(confirmation_url="https://yk.example/pay")
        payment = SimpleNamespace(id="yk-payment-id", confirmation=confirmation)

        with mock.patch(
            "engine.payments.Payment.create", return_value=payment
        ) as create:
            created_payment = create_yk_payment_sync(
                shop_id="shop-id",
                secret="secret",
                tariff=tariff,
                username="user-1",
                telegram_id=0,
                return_url="https://example.com/login/purchase/token/",
            )

        payload = create.call_args.args[0]
        self.assertEqual(created_payment.confirmation_url, "https://yk.example/pay")
        self.assertEqual(created_payment.reference, "yk-payment-id")
        self.assertEqual(
            payload["confirmation"]["return_url"],
            "https://example.com/login/purchase/token/",
        )

    def test_yookassa_payment_includes_receipt_when_email_given(self):
        # Фискализация (54-ФЗ): с email платёж обязан содержать чек.
        tariff = SimpleNamespace(
            price=100,
            db_tariff_id="one_month",
            description="1 месяц",
        )
        confirmation = SimpleNamespace(confirmation_url="https://yk.example/pay")
        payment = SimpleNamespace(id="yk-payment-id", confirmation=confirmation)

        with mock.patch(
            "engine.payments.Payment.create", return_value=payment
        ) as create:
            create_yk_payment_sync(
                shop_id="shop-id",
                secret="secret",
                tariff=tariff,
                username="user-1",
                telegram_id=0,
                return_url="https://example.com/login/purchase/token/",
                email="user@example.com",
            )

        payload = create.call_args.args[0]
        receipt = payload["receipt"]
        self.assertEqual(receipt["customer"], {"email": "user@example.com"})
        item = receipt["items"][0]
        self.assertEqual(item["amount"], {"value": "100.00", "currency": "RUB"})
        self.assertEqual(item["vat_code"], 1)  # без НДС (УСН)
        self.assertEqual(item["payment_subject"], "service")

    def test_yookassa_payment_saves_card_by_default_and_can_be_one_off(self):
        tariff = SimpleNamespace(
            price=100,
            db_tariff_id="one_month",
            description="1 месяц",
        )
        confirmation = SimpleNamespace(confirmation_url="https://yk.example/pay")
        payment = SimpleNamespace(id="yk-payment-id", confirmation=confirmation)

        for kwargs, expected in (({}, True), ({"save_payment_method": False}, False)):
            with self.subTest(kwargs=kwargs), mock.patch(
                "engine.payments.Payment.create", return_value=payment
            ) as create:
                create_yk_payment_sync(
                    shop_id="shop-id",
                    secret="secret",
                    tariff=tariff,
                    username="user-1",
                    telegram_id=0,
                    return_url="https://example.com/login/purchase/token/",
                    email="user@example.com",
                    **kwargs,
                )
                self.assertIs(create.call_args.args[0]["save_payment_method"], expected)

    def test_wata_payment_uses_success_and_fail_redirect_urls(self):
        tariff = SimpleNamespace(
            price=100,
            db_tariff_id="one_month",
            description="1 месяц",
        )
        captured = {}

        class FakeResponse:
            content = b'{"url":"https://wata.example/pay","orderId":"order-1"}'

            def raise_for_status(self):
                return None

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return None

            def post(self, url, headers, json):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return FakeResponse()

        with mock.patch(
            "engine.payments.httpx.Client", return_value=FakeClient()
        ) as client_class:
            created_payment = create_wata_payment_sync(
                wata_host="https://wata.example",
                wata_token="token",
                tariff=tariff,
                success_redirect_url="https://example.com/login/purchase/token/",
                fail_redirect_url="https://example.com/",
            )

        from engine import payments

        client_class.assert_called_once_with(
            timeout=payments.PAYMENT_HTTP_TIMEOUT_SECONDS
        )

        self.assertEqual(created_payment.confirmation_url, "https://wata.example/pay")
        self.assertEqual(created_payment.reference, "order-1")
        self.assertEqual(captured["url"], "https://wata.example/links")
        self.assertEqual(
            captured["json"]["successRedirectUrl"],
            "https://example.com/login/purchase/token/",
        )
        self.assertEqual(captured["json"]["failRedirectUrl"], "https://example.com/")

    def test_yookassa_transport_sets_network_timeout(self):
        from engine import payments

        response = SimpleNamespace(
            content=b"{}",
            headers={},
        )
        session = mock.Mock()
        session.request.return_value = response
        client = payments.TimeoutApiClient.__new__(payments.TimeoutApiClient)
        client.endpoint = "https://api.yookassa.test"
        client.configuration = SimpleNamespace(verify=True)
        client.get_session = mock.Mock(return_value=session)
        client.log_request = mock.Mock()
        client.log_response = mock.Mock()
        client.get_response_info = mock.Mock(return_value={})

        result = client.execute({}, "POST", "/payments", None, {})

        self.assertIs(result, response)
        self.assertEqual(
            session.request.call_args.kwargs["timeout"],
            payments.PAYMENT_HTTP_TIMEOUT_SECONDS,
        )
        session.close.assert_called_once()


class RegistrationAdvisoryLockTests(SimpleTestCase):
    """``lock_registration_email`` — общий транзакционный лок регистрации.

    Его берут и сайтовые flow (``create_site_user``), и мобильный email-логин
    (``mobile_api.auth.lock_email``), причём ПО ОДНОМУ И ТОМУ ЖЕ ключу: иначе
    параллельные сайтовая и мобильная регистрации на один новый email обе
    дошли бы до AddUser, и подписка проигравшего осталась бы сиротой."""

    def _session(self, dialect_name):
        session = mock.Mock()
        session.get_bind.return_value = SimpleNamespace(
            dialect=SimpleNamespace(name=dialect_name)
        )
        return session

    def test_locks_on_postgres_with_normalized_email(self):
        session = self._session("postgresql")

        lock_registration_email(session, "  User@Example.COM ")

        session.execute.assert_called_once()
        statement, params = session.execute.call_args.args
        self.assertIn("pg_advisory_xact_lock(hashtext(:email))", str(statement))
        self.assertEqual(params, {"email": "user@example.com"})

    def test_noop_on_non_postgres_backend(self):
        session = self._session("sqlite")

        lock_registration_email(session, "user@example.com")

        session.execute.assert_not_called()

    def test_noop_without_email(self):
        session = self._session("postgresql")

        lock_registration_email(session, None)
        lock_registration_email(session, "   ")

        session.execute.assert_not_called()

    def test_mobile_lock_email_delegates_to_the_same_helper(self):
        from mobile_api import auth as mobile_auth

        session = self._session("postgresql")
        mobile_auth.lock_email(session, "User@Example.com")

        session.execute.assert_called_once()
        _statement, params = session.execute.call_args.args
        self.assertEqual(params, {"email": "user@example.com"})

    def test_telegram_registration_has_its_own_postgres_lock(self):
        session = self._session("postgresql")

        lock_registration_telegram_id(session, 123456789)

        session.execute.assert_called_once()
        statement, params = session.execute.call_args.args
        self.assertIn("pg_advisory_xact_lock(hashtext(:identity))", str(statement))
        self.assertEqual(params, {"identity": "telegram:123456789"})

    def test_telegram_lock_is_noop_without_id_or_postgres(self):
        postgres = self._session("postgresql")
        sqlite = self._session("sqlite")

        lock_registration_telegram_id(postgres, None)
        lock_registration_telegram_id(sqlite, 42)

        postgres.execute.assert_not_called()
        sqlite.execute.assert_not_called()


@override_settings(SITE_TRIAL_REGISTRATION_ENABLED=True, SITE_TRIAL_PERIOD_DAYS=7)
class SiteRegistrationOrphanTests(SimpleTestCase):
    """P1: обычная сайтовая регистрация больше не плодит сирот в Remnawave.

    Было: ``username = uuid4().hex`` → AddUser в панель → только потом строка
    users. Крэш между AddUser и commit'ом оставлял активную подписку навсегда,
    а повтор регистрации брал НОВЫЙ uuid и создавал ЕЩЁ ОДНУ подписку.

    Стало (как в mobile_api): детерминированное имя от email → advisory-лок по
    email → строгое чтение панели → adoption найденной подписки / создание при
    достоверном NOT_FOUND / отказ при недоступности панели."""

    EMAIL = "new@example.com"
    CONTEXT = {"referrer": None, "traffic_source": 42, "ymid": None}

    def _run(self, session, email, client, create_user_return=None):
        request = SimpleNamespace()
        with mock.patch(
            "engine.views.get_registration_context",
            return_value=dict(self.CONTEXT),
        ), mock.patch(
            "engine.views.rwms_client", client
        ), mock.patch(
            "engine.views.create_user",
            return_value=create_user_return,
        ) as create_rwms_user, mock.patch(
            "engine.views.add_user_to_traffic_progress",
        ), mock.patch(
            "engine.views.add_event_log",
        ):
            user = create_site_user(
                session,
                email,
                request,
                creation_channel="site_magic_link",
            )
        return user, create_rwms_user

    def test_email_registration_reuses_the_mobile_deterministic_generator(self):
        from mobile_api.provisioning import deterministic_username as mobile_name

        # Один генератор на оба flow: подписку-сироту, оставленную сайтом,
        # сможет принять мобильный вход и наоборот.
        self.assertEqual(
            site_registration_username(self.EMAIL),
            mobile_name(self.EMAIL),
        )
        self.assertEqual(
            site_registration_username(" New@Example.COM "),
            site_registration_username(self.EMAIL),
        )

    def test_two_different_emails_get_different_usernames(self):
        self.assertNotEqual(
            site_registration_username("a@example.com"),
            site_registration_username("b@example.com"),
        )
        self.assertRegex(site_registration_username("a@example.com"), r"^m[0-9a-f]{31}$")

    def test_telegram_only_registration_uses_bot_deterministic_username(self):
        # Тот же telegram_id всегда выводит то же имя, совпадающее с ботом.
        first = site_registration_username(None, telegram_id=42)
        second = site_registration_username(None, telegram_id=42)

        self.assertEqual(first, "42")
        self.assertEqual(second, "42")

    def test_telegram_only_registration_strict_reads_before_add_user(self):
        session = _SiteRegistrationFakeSession()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = None
        rw_user = _FakeSiteRwUser(username="42", telegram_id=42)

        request = SimpleNamespace()
        with mock.patch(
            "engine.views.get_registration_context",
            return_value=dict(self.CONTEXT),
        ), mock.patch("engine.views.rwms_client", client), mock.patch(
            "engine.views.create_user", return_value=rw_user
        ) as create_rwms_user, mock.patch(
            "engine.views.add_user_to_traffic_progress"
        ), mock.patch("engine.views.add_event_log"):
            user = create_site_user(
                session,
                None,
                request,
                telegram_id=42,
                creation_channel="site_telegram_widget",
            )

        client.get_user_by_username_strict.assert_called_once_with("42")
        create_rwms_user.assert_called_once()
        self.assertEqual(create_rwms_user.call_args.kwargs["username"], "42")
        self.assertIsNotNone(user)

    def test_telegram_crash_retry_adopts_without_second_adduser(self):
        session = _SiteRegistrationFakeSession()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            username="42",
            telegram_id=42,
            expire_at=_FakeProtoTimestamp(datetime(2030, 1, 1)),
        )
        request = SimpleNamespace()

        with mock.patch(
            "engine.views.get_registration_context",
            return_value=dict(self.CONTEXT),
        ), mock.patch("engine.views.rwms_client", client), mock.patch(
            "engine.views.create_user"
        ) as create_rwms_user, mock.patch(
            "engine.views.add_user_to_traffic_progress"
        ), mock.patch("engine.views.add_event_log"):
            user = create_site_user(
                session,
                None,
                request,
                telegram_id=42,
                creation_channel="site_telegram_widget",
            )

        create_rwms_user.assert_not_called()
        client.add_user.assert_not_called()
        self.assertEqual(user.username, "42")
        self.assertEqual(user.telegram_id, 42)

    def test_telegram_adoption_refuses_missing_or_foreign_owner(self):
        for panel_telegram_id in (None, 777):
            with self.subTest(panel_telegram_id=panel_telegram_id):
                session = _SiteRegistrationFakeSession()
                client = mock.Mock()
                client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
                    username="42", telegram_id=panel_telegram_id
                )
                request = SimpleNamespace()
                with mock.patch(
                    "engine.views.get_registration_context",
                    return_value=dict(self.CONTEXT),
                ), mock.patch("engine.views.rwms_client", client), mock.patch(
                    "engine.views.create_user"
                ) as create_rwms_user, self.assertLogs(level="CRITICAL"):
                    with self.assertRaises(SiteRegistrationUnavailable):
                        create_site_user(
                            session,
                            None,
                            request,
                            telegram_id=42,
                            creation_channel="site_telegram_widget",
                        )

                create_rwms_user.assert_not_called()

    def test_crash_window_retry_adopts_subscription_without_second_adduser(self):
        """Панель создала подписку, БД не успела: повтор регистрации выводит ТО
        ЖЕ имя, находит подписку и ПРИНИМАЕТ её. Второго AddUser нет."""
        session = _SiteRegistrationFakeSession()
        username = deterministic_username(self.EMAIL)
        expire = datetime(2030, 1, 1, 12, 0, 0)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            username=username,
            email=self.EMAIL,
            expire_at=_FakeProtoTimestamp(expire),
        )

        user, create_rwms_user = self._run(session, self.EMAIL, client)

        client.get_user_by_username_strict.assert_called_once_with(username)
        # КРИТИЧНО: панель не тронута — ни второго AddUser, ни recreate.
        create_rwms_user.assert_not_called()
        client.add_user.assert_not_called()
        self.assertEqual(user.username, username)
        self.assertEqual(user.email, self.EMAIL)
        self.assertEqual(user.expire_at, expire)

    def test_adoption_refuses_panel_record_without_email(self):
        session = _SiteRegistrationFakeSession()
        username = deterministic_username(self.EMAIL)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            username=username
        )

        with self.assertLogs(level="CRITICAL"):
            with self.assertRaises(SiteRegistrationUnavailable):
                self._run(session, self.EMAIL, client)

        client.add_user.assert_not_called()
        self.assertEqual(session.added, [])

    def test_foreign_subscription_is_never_adopted_and_raises_alert(self):
        """P2 (тот же guard на сайтовом adoption): подписка под вычисленным
        именем с ЧУЖИМ email — неоднозначное состояние. Ни adoption, ни запись
        в панель: ALERT и отказ."""
        session = _SiteRegistrationFakeSession()
        username = deterministic_username(self.EMAIL)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            username=username,
            email="someone-else@example.com",
        )

        with self.assertLogs(level="CRITICAL") as captured_logs:
            with self.assertRaises(SiteRegistrationUnavailable):
                self._run(session, self.EMAIL, client)

        self.assertTrue(any("ALERT:" in line for line in captured_logs.output))
        self.assertTrue(
            any("someone-else@example.com" in line for line in captured_logs.output)
        )
        client.add_user.assert_not_called()

    def test_adoption_refuses_local_row_owned_by_another_email(self):
        """Захват аккаунта по «освобождённому» email. Жертва зарегистрировалась
        на A, позже сменила почту на B; панель осталась с A (её обновление
        best-effort и на блипе RWMS молча пропускается). Новый владелец адреса A
        вводит его в форму: имя детерминировано от A, панельный guard проходит
        (email панели == A), но строка users принадлежит уже адресу B. Без
        проверки владельца локальной строки регистрация вернула бы аккаунт
        жертвы (magic-link и purchase-token выдались бы чужому человеку)."""
        username = deterministic_username(self.EMAIL)
        victim = User(email="victim-new@example.com", username=username)
        victim.id = 777
        session = _SiteRegistrationSessionWithExistingUser(victim)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            username=username,
            email=self.EMAIL,  # панель осталась со старым адресом жертвы
        )

        with self.assertLogs(level="CRITICAL") as captured_logs:
            with self.assertRaises(SiteRegistrationUnavailable):
                self._run(session, self.EMAIL, client)

        self.assertTrue(any("ALERT:" in line for line in captured_logs.output))
        # Ни панель, ни БД не тронуты, чужой аккаунт не отдан
        client.add_user.assert_not_called()
        self.assertEqual(session.added, [])
        self.assertEqual(victim.email, "victim-new@example.com")

    def test_adoption_reuses_local_row_of_the_same_email(self):
        """Обратная сторона guard'а: строка того же владельца принимается как
        раньше (crash-window recovery не сломан)."""
        username = deterministic_username(self.EMAIL)
        existing = User(email=self.EMAIL, username=username)
        existing.id = 42
        session = _SiteRegistrationSessionWithExistingUser(existing)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            username=username,
            email=self.EMAIL,
        )

        user, create_rwms_user = self._run(session, self.EMAIL, client)

        create_rwms_user.assert_not_called()
        client.add_user.assert_not_called()
        self.assertIs(user, existing)
        client.update_user.assert_not_called()
        self.assertEqual(session.added, [])

    def test_rwms_unavailable_aborts_registration_without_touching_panel(self):
        session = _SiteRegistrationFakeSession()
        username = deterministic_username(self.EMAIL)
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = RwmsUnavailableError(
            username, None, "panel down"
        )

        with self.assertRaises(SiteRegistrationUnavailable):
            self._run(session, self.EMAIL, client)

        client.add_user.assert_not_called()
        # Ни локальной строки: «зарегистрировали без подписки» разошло бы БД и
        # панель, если подписка на самом деле уже существует.
        self.assertEqual(session.added, [])

    def test_confirmed_not_found_creates_subscription_with_deterministic_name(self):
        session = _SiteRegistrationFakeSession()
        username = deterministic_username(self.EMAIL)
        expire = datetime(2030, 6, 1, 9, 0, 0)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = None  # достоверный NOT_FOUND

        user, create_rwms_user = self._run(
            session,
            self.EMAIL,
            client,
            create_user_return=_FakeSiteRwUser(
                username=username, expire_at=_FakeProtoTimestamp(expire)
            ),
        )

        create_rwms_user.assert_called_once()
        self.assertEqual(create_rwms_user.call_args.kwargs["username"], username)
        self.assertEqual(create_rwms_user.call_args.kwargs["trial_period_days"], 7)
        self.assertEqual(user.username, username)
        self.assertEqual(user.expire_at, expire)

    def test_advisory_lock_is_taken_before_any_panel_call(self):
        order = []
        session = _SiteRegistrationFakeSession()
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = lambda name: order.append(
            "strict_read"
        )
        request = SimpleNamespace()

        with mock.patch(
            "engine.views.lock_registration_email",
            side_effect=lambda *args: order.append("advisory_lock"),
        ), mock.patch(
            "engine.views.get_registration_context",
            side_effect=lambda *args: order.append("context") or dict(self.CONTEXT),
        ), mock.patch(
            "engine.views.rwms_client", client
        ), mock.patch(
            "engine.views.create_user",
            side_effect=lambda **kwargs: order.append("add_user")
            or _FakeSiteRwUser(username=kwargs["username"]),
        ), mock.patch(
            "engine.views.add_user_to_traffic_progress",
        ), mock.patch(
            "engine.views.add_event_log",
        ):
            create_site_user(session, self.EMAIL, request)

        self.assertEqual(order, ["advisory_lock", "context", "strict_read", "add_user"])

    def test_resolve_helper_translates_outage_and_conflict(self):
        username = deterministic_username(self.EMAIL)
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = RwmsUnavailableError(
            username, None, "down"
        )
        with mock.patch("engine.views.rwms_client", client):
            with self.assertRaises(SiteRegistrationUnavailable):
                resolve_existing_site_subscription(username, self.EMAIL)

        client = mock.Mock()
        client.get_user_by_username_strict.return_value = None
        with mock.patch("engine.views.rwms_client", client):
            self.assertIsNone(
                resolve_existing_site_subscription(username, self.EMAIL)
            )


class SiteRegistrationViewFallbackTests(SimpleTestCase):
    """Пользовательская реакция на отказ регистрации: «попробуйте позже», а не
    молчаливый «ok» и не «регистрация не удалась навсегда»."""

    class _FakeBegin:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_session(self, existing_user):
        outer = self

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return existing_user

        class FakeSession:
            def __init__(self):
                self.closed = False
                self.added = []

            def begin(self):
                return outer._FakeBegin()

            def query(self, *args, **kwargs):
                return FakeQuery()

            def add(self, obj):
                # Токен здесь НЕ проставляем: питоновский column default
                # (`MagicToken.token = Column(default=uuid.uuid4)`) настоящая
                # сессия применяет на INSERT, то есть во flush(). Пока дубль
                # делал это в add(), код без flush() проходил тесты и слал
                # письма со ссылкой /login/magic/None/ (инцидент 2026-09-12).
                self.added.append(obj)

            def flush(self):
                for obj in self.added:
                    if isinstance(obj, MagicToken) and obj.token is None:
                        obj.token = "magic-token"

            def close(self):
                self.closed = True

        return FakeSession()

    def _post(self, email):
        request = RequestFactory().post("/magic/", {"email": email})
        request.session = {}
        return request

    def test_magic_link_defers_registration_until_email_link_is_opened(self):
        session = self._fake_session(None)

        with mock.patch(
            "engine.views.session_factory", return_value=session
        ), mock.patch(
            "engine.views.create_site_user",
            side_effect=SiteRegistrationUnavailable("rwms unavailable"),
        ) as create_site, mock.patch(
            "engine.views.send_magic_link_email"
        ) as send_email:
            response = send_magic_link(self._post("new@example.com"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"status": "ok"})
        create_site.assert_not_called()
        sent_link = send_email.call_args.args[1]
        self.assertIn("/login/register/", sent_link)
        self.assertTrue(session.closed)

    def test_confirmed_registration_asks_to_retry_when_rwms_is_unavailable(self):
        session = self._fake_session(None)
        request = RequestFactory().get("/login/register/token/")
        request.session = {}
        request.user = SimpleNamespace(is_authenticated=False)
        token = views.build_site_registration_token("new@example.com", {})

        with mock.patch(
            "engine.views.session_factory", return_value=session
        ), mock.patch(
            "engine.views.create_site_user",
            side_effect=SiteRegistrationUnavailable("rwms unavailable"),
        ):
            response = views.auth_by_registration_link(request, token)

        self.assertEqual(response.status_code, 503)
        self.assertIn(SITE_REGISTRATION_RETRY_MESSAGE, response.content.decode())
        self.assertTrue(session.closed)

    def test_legacy_random_username_user_logs_in_unchanged(self):
        """Аккаунты, заведённые старым ``uuid4().hex``-генератором, не
        пересчитываются нигде: вход идёт по существующей строке users."""
        legacy_username = "0123456789abcdef0123456789abcdef"
        legacy_user = SimpleNamespace(
            id=7,
            email="legacy@example.com",
            username=legacy_username,
        )
        session = self._fake_session(legacy_user)

        with mock.patch(
            "engine.views.session_factory", return_value=session
        ), mock.patch(
            "engine.views.create_site_user"
        ) as create_site, mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": None, "ymid": None},
        ), mock.patch(
            "engine.views.sync_existing_user_tracking"
        ), mock.patch(
            "engine.views.send_magic_link_email"
        ) as send_email:
            response = send_magic_link(self._post("legacy@example.com"))

        create_site.assert_not_called()
        self.assertEqual(legacy_user.username, legacy_username)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"status": "ok"})
        send_email.assert_called_once()

    def test_magic_link_in_letter_actually_opens_login(self):
        """Регресс 2026-09-12: в письме уходила ссылка /login/magic/None/.

        Утверждать «письмо отправлено» мало — именно так баг и доехал до прода:
        отправка была, а ссылка в ней вела в 404, и войти не мог никто.
        Поэтому проверяем не факт отправки, а то, что путь из письма
        РАЗРЕЗОЛВИТСЯ Django в обработчик входа — то есть по нему реально
        откроется кабинет.
        """
        user = SimpleNamespace(id=7, email="legacy@example.com", username="u7")
        session = self._fake_session(user)

        with mock.patch(
            "engine.views.session_factory", return_value=session
        ), mock.patch("engine.views.create_site_user"), mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": None, "ymid": None},
        ), mock.patch(
            "engine.views.sync_existing_user_tracking"
        ), mock.patch(
            "engine.views.send_magic_link_email"
        ) as send_email:
            response = send_magic_link(self._post("legacy@example.com"))

        self.assertEqual(response.status_code, 200)
        link = send_email.call_args.args[1]

        # Токен обязан быть настоящим: «None» в пути — ровно тот инцидент.
        self.assertNotIn("/magic/None/", link)
        path = urlsplit(link).path
        self.assertRegex(path, r"^/login/magic/[^/]+/$")
        self.assertNotIn("None", path)

    @override_settings(PAYMENT_GATEWAY="wata")
    def test_pay_asks_to_retry_instead_of_creating_orphan(self):
        """Деньги: если аккаунт под анонимную оплату подготовить нельзя, форма
        оплаты не открывается и в панели ничего не создаётся."""
        tariff = SimpleNamespace(price=100, db_tariff_id="month", description="1 месяц")

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def with_for_update(self, **kwargs):
                # FINAL-PAY-03: анонимная ветка читает users FOR UPDATE.
                return self

            def first(self):
                return None

        class FakeSession:
            def query(self, model):
                return FakeQuery()

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        request = RequestFactory().post(
            "/pay/",
            {"email": "new@example.com", "tariff_id": "month"},
            HTTP_HOST="example.com",
            HTTP_X_PAYMENT_LAUNCH="new-tab",
        )
        request.user = SimpleNamespace(is_authenticated=False, id=None)
        request.session = SessionDict()

        with (
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.get_runtime_actual_tariffs", return_value=[tariff]),
            mock.patch(
                "engine.views.create_site_user",
                side_effect=SiteRegistrationUnavailable("rwms unavailable"),
            ) as create_site,
            mock.patch("engine.views.create_wata_payment_sync") as create_invoice,
        ):
            response = pay(request)

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], SITE_REGISTRATION_RETRY_MESSAGE)
        create_invoice.assert_not_called()
        # R03/B12: аккаунт под анонимную покупку — без пробной подписки в панели.
        self.assertIs(create_site.call_args.kwargs["allow_trial"], False)


class LoginOnboardingTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(
        ALLOWED_HOSTS=["cab.example.com"],
        CABINET_DOMAINS=["cab.example.com"],
        VPN_DOMAINS=[],
        VPS_DOMAINS=[],
        VPS_DIRECT_SALE_DOMAINS=[],
        DEFAULT_CABINET_DOMAIN="https://cab.example.com",
        TELEGRAM_AUTH_BOTS={},
    )
    def test_cabinet_domain_hides_login_onboarding(self):
        request = self.factory.get("/login/", HTTP_HOST="cab.example.com", secure=True)
        request.session = {}

        content = render_login(request).content.decode()

        self.assertNotIn('id="loginOnboarding"', content)

    @override_settings(
        ALLOWED_HOSTS=["vpn.example.com"],
        CABINET_DOMAINS=[],
        VPN_DOMAINS=["vpn.example.com"],
        VPS_DOMAINS=[],
        VPS_DIRECT_SALE_DOMAINS=[],
        DEFAULT_CABINET_DOMAIN="https://vpn.example.com",
        TELEGRAM_AUTH_BOTS={},
    )
    def test_vpn_domain_shows_login_onboarding(self):
        request = self.factory.get("/login/", HTTP_HOST="vpn.example.com", secure=True)
        request.session = {}

        content = render_login(request).content.decode()

        self.assertIn('id="loginOnboarding"', content)


def _fake_wata_http_client(status_code=200, body_obj=None):
    payload = json.dumps(body_obj or {}).encode()

    class FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self.content = payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            return FakeResponse()

    return FakeClient()


class WataActiveStatusTests(SimpleTestCase):
    def test_fetch_returns_paid_when_any_item_paid(self):
        client = _fake_wata_http_client(body_obj={"items": [{"status": "Paid"}]})
        with mock.patch("engine.payments.httpx.Client", return_value=client):
            result = fetch_wata_transaction_status("https://h", "tok", "order-1")
        self.assertEqual(result, "Paid")

    def test_fetch_returns_declined_when_all_declined(self):
        client = _fake_wata_http_client(
            body_obj={"items": [{"status": "Declined"}, {"status": "Declined"}]}
        )
        with mock.patch("engine.payments.httpx.Client", return_value=client):
            result = fetch_wata_transaction_status("https://h", "tok", "order-2")
        self.assertEqual(result, "Declined")

    def test_fetch_returns_none_while_pending(self):
        client = _fake_wata_http_client(body_obj={"items": [{"status": "Pending"}]})
        with mock.patch("engine.payments.httpx.Client", return_value=client):
            result = fetch_wata_transaction_status("https://h", "tok", "order-3")
        self.assertIsNone(result)

    def test_fetch_returns_none_on_empty_or_error(self):
        empty = _fake_wata_http_client(body_obj={"items": []})
        with mock.patch("engine.payments.httpx.Client", return_value=empty):
            self.assertIsNone(
                fetch_wata_transaction_status("https://h", "tok", "order-4")
            )

        err = _fake_wata_http_client(status_code=429, body_obj={})
        with mock.patch("engine.payments.httpx.Client", return_value=err):
            self.assertIsNone(
                fetch_wata_transaction_status("https://h", "tok", "order-5")
            )

    def test_fetch_returns_none_when_missing_args(self):
        self.assertIsNone(fetch_wata_transaction_status("", "tok", "order"))
        self.assertIsNone(fetch_wata_transaction_status("https://h", "", "order"))
        self.assertIsNone(fetch_wata_transaction_status("https://h", "tok", ""))

    @override_settings(WATA_HOST="https://h", WATA_TOKEN="tok")
    def test_active_status_is_throttled_per_order(self):
        token = SimpleNamespace(
            payment_gateway="wata", payment_reference="order-throttle-1"
        )
        with mock.patch(
            "engine.views.fetch_wata_transaction_status", return_value="Paid"
        ) as fetch:
            first = active_wata_status_for_token(token)
            second = active_wata_status_for_token(token)

        self.assertEqual(first, ("succeeded", "Платеж прошел успешно"))
        self.assertIsNone(second)  # второй вызов в пределах 30с — троттлинг
        self.assertEqual(fetch.call_count, 1)

    def test_active_status_ignores_non_wata_token(self):
        token = SimpleNamespace(payment_gateway="yookassa", payment_reference="x")
        with mock.patch("engine.views.fetch_wata_transaction_status") as fetch:
            self.assertIsNone(active_wata_status_for_token(token))
        fetch.assert_not_called()


class WebsiteDockerRuntimeTests(SimpleTestCase):
    def test_geoip_database_is_downloaded_by_app_into_persistent_volume(self):
        compose = Path("docker/website/docker-compose.yml").read_text()
        env_example = Path("docker/website/.env.example").read_text()

        app_section = compose[compose.index("  app:") : compose.index("  nginx:")]

        self.assertIn(
            "GEOIP_CITY_DB_PATH: "
            "/var/lib/monkey-island/geoip/DBIP-City-Lite.mmdb",
            app_section,
        )
        self.assertIn(
            "geoipdata:/var/lib/monkey-island/geoip",
            app_section,
        )
        self.assertNotIn("geoipupdate:", compose)
        self.assertNotIn("ghcr.io/maxmind/geoipupdate", compose)

        self.assertNotIn("GEOIPUPDATE_ACCOUNT_ID", env_example)
        self.assertNotIn("GEOIPUPDATE_LICENSE_KEY", env_example)
        self.assertIn("GEOIPUPDATE_INTERVAL_HOURS=168", env_example)
        self.assertIn("DB-IP City Lite скачивается без аккаунта и ключей", env_example)
        self.assertIn("INFRA_GEOIP_CACHE_SECONDS=60", env_example)
        self.assertIn("\n  geoipdata:\n", compose)

    def test_origin_renew_keeps_compose_stack_running(self):
        renew_script = Path("docker/website/renew-certs.sh").read_text()

        self.assertNotIn('docker compose -f "${COMPOSE_FILE}" down', renew_script)
        self.assertNotIn("--standalone", renew_script)
        self.assertNotIn("-p 80:80", renew_script)
        self.assertIn("--webroot", renew_script)
        self.assertIn("--webroot-path /var/www/certbot", renew_script)
        self.assertIn(
            '-v "${SCRIPT_DIR}/certbot-www:/var/www/certbot"',
            renew_script,
        )

    def test_origin_nginx_serves_acme_challenge_webroot(self):
        compose = Path("docker/website/docker-compose.yml").read_text()
        nginx = Path("docker/website/nginx.conf.template").read_text()

        self.assertIn("./certbot-www:/var/www/certbot:ro", compose)
        self.assertIn(
            "location ^~ /.well-known/acme-challenge/",
            nginx,
        )
        self.assertIn("root /var/www/certbot;", nginx)
        self.assertIn("try_files $uri =404;", nginx)

    def test_origin_issue_certs_restores_running_stack_on_failure(self):
        issue_script = Path("docker/website/issue-certs.sh").read_text()

        self.assertNotIn('docker compose -f "${COMPOSE_FILE}" down', issue_script)
        self.assertIn('docker compose -f "${COMPOSE_FILE}" stop nginx', issue_script)
        self.assertIn("restore_stack()", issue_script)
        self.assertIn("trap restore_stack EXIT", issue_script)
        self.assertIn("--standalone", issue_script)

    def test_origin_deploy_does_not_remove_running_stack_before_image_load(self):
        deploy_script = Path("docker/website/deploy.sh").read_text()

        self.assertNotIn("docker compose -f docker-compose.yml down", deploy_script)
        self.assertNotIn("docker image rm '${REMOTE_IMAGE_TAG}'", deploy_script)
        self.assertIn("docker load -i '${IMAGE_TAR_NAME}'", deploy_script)
        self.assertIn(
            "docker compose -f docker-compose.yml up -d --no-build --force-recreate",
            deploy_script,
        )
        self.assertIn("'${REMOTE_DIR}/certbot-www'", deploy_script)


class CancelAutopayViewTests(SimpleTestCase):
    def _build_session(self, db_user, deleted_count):
        recorded = {"deleted": False}

        class FakeQuery:
            def __init__(self, kind):
                self.kind = kind

            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return db_user

            def delete(self, synchronize_session=False):
                recorded["deleted"] = True
                return deleted_count

        class FakeSession:
            def query(self, model):
                if model is User:
                    return FakeQuery("user")
                return FakeQuery("recurrent")

            def add(self, obj):
                recorded.setdefault("added", []).append(obj)

            def commit(self):
                recorded["committed"] = True

            def rollback(self):
                recorded["rolled_back"] = True

            def close(self):
                recorded["closed"] = True

        return FakeSession(), recorded

    def test_cancel_autopay_removes_recurrents_and_disables_flag(self):
        db_user = SimpleNamespace(id=42, autopay_allow=True)
        session, recorded = self._build_session(db_user, deleted_count=1)

        request = RequestFactory().post("/cancel-autopay/")
        request.user = SimpleNamespace(is_authenticated=True, id=42)

        with mock.patch("engine.views.session_factory", return_value=session):
            response = cancel_autopay(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["removed_recurrents"], 1)
        self.assertFalse(db_user.autopay_allow)
        self.assertTrue(recorded["deleted"])
        self.assertTrue(recorded["committed"])
        self.assertTrue(recorded["closed"])
        # Отмена из кабинета пишет то же событие, что и бот, — иначе она
        # невидима для churn-аналитики и ежедневного отчёта.
        added_types = [event.event_type for event in recorded.get("added", [])]
        self.assertIn("confirm_cancel_autopay_clicked", added_types)

    def test_cancel_autopay_rejects_get_requests(self):
        request = RequestFactory().get("/cancel-autopay/")
        request.user = SimpleNamespace(is_authenticated=True, id=42)

        response = cancel_autopay(request)
        self.assertEqual(response.status_code, 403)

    def test_cancel_autopay_requires_authentication(self):
        request = RequestFactory().post("/cancel-autopay/")
        request.user = SimpleNamespace(is_authenticated=False, id=None)

        response = cancel_autopay(request)
        self.assertEqual(response.status_code, 403)

    def test_cancel_autopay_handles_missing_user(self):
        session, recorded = self._build_session(db_user=None, deleted_count=0)

        request = RequestFactory().post("/cancel-autopay/")
        request.user = SimpleNamespace(is_authenticated=True, id=999)

        with mock.patch("engine.views.session_factory", return_value=session):
            response = cancel_autopay(request)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(recorded["closed"])


class PricingFilterTests(SimpleTestCase):
    def test_floor_div_gives_marketing_monthly_price(self):
        from engine.templatetags.pricing import floor_div

        # widthratio округлял бы 599/3 к 200 — на витрине нужно 199/149.
        self.assertEqual(floor_div(599, 3), 199)
        self.assertEqual(floor_div(1799, 12), 149)
        self.assertEqual(floor_div(249, 1), 249)
        self.assertEqual(floor_div("599", "3"), 199)
        self.assertEqual(floor_div(None, 3), "")
        self.assertEqual(floor_div(599, 0), "")

    def test_templates_use_floor_div_for_monthly_price(self):
        for name in (
            "dashboard.html",
            "index_vpn.html",
            "index_vps.html",
            "index_vps_direct_sale.html",
        ):
            template = Path(f"engine/templates/{name}").read_text()
            self.assertIn("floor_div", template, name)
            self.assertNotIn("widthratio tariff.price", template, name)


class SettingsTabTemplateTests(SimpleTestCase):
    def test_settings_tab_present_with_autopay_and_faq(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn('data-tab="settings"', template)
        self.assertIn('id="tab-settings"', template)
        # Пользовательское название и иконка соответствуют содержимому раздела;
        # внутренний settings-id сохраняется для обратной совместимости ссылок.
        # Мобильный кабинет: профиль открывается аватаром на главной («Аккаунт»),
        # нижней навигации больше нет.
        self.assertIn('class="cm-avatar" data-cm-go="account" aria-label="Аккаунт"', template)
        self.assertNotIn('<nav class="nav-mobile"', template)
        self.assertIn('tracking-tighter mb-3">Профиль</h1>', template)
        self.assertNotIn('<i class="fas fa-cog"></i><span>Настройки</span>', template)
        self.assertIn("Отключить автопродление", template)
        self.assertIn("openAutopaySheet()", template)
        self.assertIn("confirmCancelAutopay", template)
        self.assertIn("{% url 'cancel_autopay' %}", template)
        # FAQ по схеме Akenai: разделы -> вопросы -> статья в bottom-sheet.
        # Данные — в FAQ_DATA, разделы согласованы с разделом «Вопросы» бота.
        self.assertIn("const FAQ_DATA", template)
        for title in (
            "Подключение",
            "Блокировки и «белые списки»",
            "Подписка и оплата",
            "Безопасность и приватность",
            "Скорость и стабильность",
        ):
            self.assertIn(f"title: '{title}'", template)
        self.assertIn("Как подключить VPN?", template)
        self.assertIn("Может ли кто-то узнать, что я пользуюсь VPN?", template)
        self.assertIn('id="faq-article-sheet"', template)
        self.assertIn("Следующая статья", template)
        self.assertIn("Не нашли ответ?", template)
        # Документы и webapp-режим
        self.assertIn("{% url 'offer' %}", template)
        self.assertIn("{% url 'privacy' %}", template)
        self.assertIn("tg_webapp_mode", template)

    def test_autopay_button_always_clickable_with_nothing_to_cancel_sheet(self):
        template = template_source("engine/templates/dashboard.html")

        # Отключение автопродления убрано с видных мест и живёт ссылкой
        # внутри листа «История платежей» (открывает прежний confirm-флоу).
        self.assertIn("setTimeout(onAutopayButtonClick, 320)", template)
        # Клиент решает по has_recurrent, какой лист открыть.
        self.assertIn(
            "const HAS_RECURRENT = {% if has_recurrent %}true{% else %}false{% endif %};",
            template,
        )
        # Информационный лист «отменять нечего».
        self.assertIn('id="no-autopay-sheet"', template)
        self.assertIn("openNoAutopaySheet", template)
        self.assertIn("Автопродление не подключено", template)
        self.assertIn("отменять нечего", template)
        # Заглушки старого варианта быть не должно.
        self.assertNotIn("Не подключено · оформляется при оплате", template)


class MobileDashboardHomeTemplateTests(SimpleTestCase):
    def test_desktop_visual_language_uses_concept_sidebar_buttons_and_referral_strip(self):
        import inspect

        from engine import views

        template = template_source("engine/templates/dashboard.html")
        dashboard_source = inspect.getsource(views.dashboard)

        self.assertIn("@media (min-width: 1025px)", template)
        self.assertIn("font-family: 'Golos Text'", template)
        self.assertIn(".sidebar-desktop .nav-btn.active::before", template)
        self.assertIn("background: rgba(255, 255, 255, 0.065);", template)
        self.assertIn("background: #ffc700;", template)
        self.assertIn("border-radius: 8px !important;", template)
        self.assertIn("dt-ref-card", template)
        self.assertIn("Получайте дни доступа", template)
        self.assertIn("до {{ max_referral_bonus_days }} дней", template)
        self.assertNotIn('id="ref-pill"', template)
        self.assertIn('"max_referral_bonus_days": (', dashboard_source)
        self.assertIn("join_referrer_bonus_days", dashboard_source)
        self.assertIn("traffic_referrer_bonus_days", dashboard_source)
        self.assertIn("purchase_referrer_bonus_days", dashboard_source)

    def test_desktop_home_uses_inline_devices_without_summary_or_detail_panel(self):
        template = template_source("engine/templates/dashboard.html")
        desktop_home = template[
            template.index('<div class="standard-dashboard-home">'):
            template.index("{% endif %}\n                </div>\n            </div>", template.index('<div class="standard-dashboard-home">'))
        ]

        self.assertNotIn("home-quick-actions", desktop_home)
        self.assertNotIn("quick-action-card", desktop_home)
        self.assertNotIn("Установка на ваши устройства", desktop_home)
        self.assertNotIn("Помощь в Telegram", desktop_home)
        self.assertNotIn("+40 дней за друга", desktop_home)
        self.assertNotIn("Кратко", desktop_home)
        self.assertNotIn("Переподключить", desktop_home)
        self.assertNotIn("desktop-renewal-card", desktop_home)
        self.assertNotIn("mi3-desktop-devices-note", desktop_home)
        self.assertIn("dt-access-card", desktop_home)
        self.assertIn("dt-stat-devices", desktop_home)
        self.assertIn('onclick="quickAccessInstall()"', desktop_home)
        self.assertIn('class="dt-quick-grid"', desktop_home)
        self.assertIn('class="dt-quick-card" onclick="quickAccessInstall()"', desktop_home)
        self.assertIn("Быстрый доступ", desktop_home)
        self.assertIn('onclick="showTab(\'setup\')"', desktop_home)
        self.assertIn("Добавить устройство", desktop_home)
        self.assertIn('onclick="openPaymentsHistorySheet()"', desktop_home)
        self.assertIn("История платежей", desktop_home)
        self.assertIn("fas fa-receipt", desktop_home)

    def test_mobile_home_uses_white_monkey_face_without_tint(self):
        template = template_source("engine/templates/dashboard.html")
        logo_styles = template[
            template.index(".tg-mini-brand-mark img {"):
            template.index("}", template.index(".tg-mini-brand-mark img {")) + 1
        ]

        self.assertIn("{% static 'icons/logo-face-white.png' %}", template)
        self.assertIn("filter: none;", logo_styles)
        self.assertNotIn("sepia", logo_styles)

    def test_desktop_home_uses_full_width_compact_surface_palette(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn(".desktop-cabinet-home", template)
        self.assertIn("width: min(100%, 1280px);", template)
        self.assertIn(".desktop-subscription-card", template)
        self.assertIn(".desktop-devices-panel", template)
        self.assertIn(".desktop-device-row", template)
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(210px, 0.46fr) auto;", template)
        self.assertIn("linear-gradient(90deg, #35c966, #62df85);", template)
        self.assertIn("rgba(255, 190, 67, 0.18)", template)
        self.assertIn("linear-gradient(105deg, rgba(31, 26, 17, 0.90), rgba(18, 18, 20, 0.92));", template)
        self.assertIn(".desktop-quick-actions", template)

    def test_desktop_devices_render_from_existing_api_and_delete_safely(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("function renderDesktopDevices(data)", template)
        self.assertIn("function bindDeviceDeleteButtons(root)", template)
        self.assertIn("data-desktop-device-status", template)
        self.assertIn("data-mi3-hwid", template)
        self.assertIn("this.classList.contains('is-armed')", template)
        self.assertIn("/api/cabinet/devices/delete/", template)
        self.assertIn("updateHomeDevices(payload)", template)
        self.assertIn("desktopDeviceFilter === 'online'", template)

    def test_mobile_home_uses_compact_state_driven_layout(self):
        template = template_source("engine/templates/dashboard.html")

        # Компактная главная всегда есть в DOM: на сайте её включает mobile
        # breakpoint, а Mini App использует её независимо от ширины.
        self.assertIn('<div class="tg-mini-home cabinet-home">', template)
        self.assertIn('{% include "includes/cabinet_mobile.html" %}', template)
        self.assertIn("body.tg-webapp .tg-mini-home", template)
        self.assertIn("{% if not tg_webapp_mode %}", template)
        self.assertIn('<div class="standard-dashboard-home">', template)
        # Карточка подписки: статус, остаток, трафик, устройства, главная CTA.
        self.assertIn('<section class="cm-card cm-sub" aria-label="Состояние подписки">', template)
        self.assertIn("{% if not has_subscription_access %}Не активна{% elif show_expiring_banner %}Истекает{% else %}Активна{% endif %}", template)
        self.assertIn('{{ user.expire_at|date:"d.m.Y" }}', template)
        self.assertIn(
            '<button type="button" class="cm-btn cm-btn-primary" data-cm-go="buy">'
            "{% if not has_subscription_access %}Купить подписку"
            "{% elif has_recurrent %}Продлить заранее"
            "{% else %}Продлить подписку{% endif %}</button>",
            template,
        )
        self.assertIn('data-cm-go="devices"', template)
        self.assertIn('id="cm-devices-count"', template)
        # Без докупки трафика — у Monkey Island его нет.
        self.assertNotIn("Докупить трафик", template)
        self.assertNotIn("cm-page=\"traffic\"", template)

    def test_mobile_home_has_restrained_visual_hierarchy(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("body.tg-webapp {", template)
        # Кабинет и Mini App используют фирменный Golos Text (как лендинги);
        # системный стек остаётся фолбэком.
        self.assertIn(
            "font-family: 'Golos Text', -apple-system, BlinkMacSystemFont, \"Segoe UI\"",
            template,
        )
        self.assertIn(".tg-mini-expiry h1", template)
        self.assertIn("font-size: 27px !important;", template)
        self.assertIn(".tg-mini-primary", template)
        self.assertIn("min-height: 52px;", template)
        self.assertIn("body.dashboard-v2 .nav-mobile .nav-btn.active", template)
        self.assertIn("background: transparent;", template)

    def test_mobile_breakpoint_replaces_standard_home_for_regular_website(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("@media (max-width: 1024px)", template)
        self.assertIn(".tg-mini-home {\n                display: block;", template)
        self.assertIn(".standard-dashboard-home {\n                display: none;", template)
        self.assertIn("body.dashboard-v2 .main-content", template)
        self.assertIn("body.dashboard-v2 .nav-mobile", template)

    def test_desktop_referral_dialog_matches_approved_concept(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn('id="ref-desktop-content" class="ref-desktop-dialog"', template)
        self.assertIn('role="dialog" aria-modal="true"', template)
        self.assertIn('aria-labelledby="ref-desktop-title"', template)
        self.assertIn("Отправьте ссылку другу — бонусы начислятся автоматически", template)
        self.assertIn("Как получить до {{ max_referral_bonus_days|default:\"40\" }} дней", template)
        self.assertIn("Друг подключился", template)
        self.assertIn("Стал активным пользователем", template)
        self.assertIn("Оплатил подписку", template)
        self.assertIn('aria-label="Статистика приглашений"', template)
        self.assertIn('id="ref-desktop-telegram-link"', template)
        self.assertIn('id="ref-desktop-site-link"', template)
        self.assertIn("Рекомендуем", template)
        self.assertIn('onclick="shareTelegramReferral()">Отправить</button>', template)
        self.assertIn("Бонусы начисляются автоматически после выполнения условий", template)
        self.assertIn("body.dashboard-v2 #ref-sheet #ref-content", template)
        self.assertIn("body.dashboard-v2 .ref-desktop-dialog.is-open", template)

    def test_referral_texts_do_not_expose_traffic_threshold(self):
        """Порог «100 МБ трафика от друга» — внутренняя механика начисления,
        в пользовательских текстах кабинета он не раскрывается («активный
        пользователь»), как и в текстах бота. Админка (admin_dashboard.html)
        порог показывает — это ожидаемо."""
        template = template_source("engine/templates/dashboard.html").lower()

        self.assertNotIn("100 мб", template)
        self.assertNotIn("100мб", template)
        self.assertNotIn("100 mb", template)
        self.assertNotIn("трафика от друга", template)

    def test_mobile_referral_bottom_sheet_is_preserved(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn('class="bottom-sheet-content fixed inset-x-0 bottom-0', template)
        self.assertIn('id="ref-content"', template)
        self.assertIn('id="site-ref-link-input-sheet"', template)
        self.assertIn('id="ref-link-input-sheet"', template)
        self.assertIn(".ref-desktop-dialog {\n            display: none;", template)
        self.assertIn("const isDesktop = window.matchMedia('(min-width: 1025px)').matches;", template)
        self.assertIn("content.classList.replace('translate-y-full', 'translate-y-0')", template)

    def test_mobile_referral_copy_buttons_have_click_handler(self):
        """Кнопки «Скопировать» мобильного экрана рефералов (data-mi3-copy)
        должны иметь обработчик: раньше его не было и кнопки не кликались."""
        template = template_source("engine/templates/dashboard.html")

        # В HTML-атрибуте работает автоэскейпинг Django; |escapejs здесь
        # портил ссылку литеральными =-последовательностями.
        self.assertIn('data-mi3-copy="{{ referral_link }}"', template)
        self.assertIn('data-mi3-copy="{{ site_referral_link }}"', template)
        self.assertIn("document.querySelectorAll('[data-mi3-copy]')", template)
        self.assertIn("navigator.clipboard.writeText(value)", template)
        # Fallback для webview без clipboard API
        self.assertIn("document.execCommand('copy')", template)

    def test_quick_access_waits_for_deferred_install_prompt(self):
        """Первый клик по «Быстрому доступу» не должен сваливаться в
        инструкцию, если beforeinstallprompt ещё не успел прийти."""
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("function waitForInstallPrompt(", template)
        self.assertIn("const installPrompt = await waitForInstallPrompt(1500);", template)
        self.assertIn("installPromptWaiters.splice(0).forEach((resolve) => resolve(event));", template)

    def test_faq_back_returns_to_origin_tab(self):
        """«Назад» из FAQ возвращает на вкладку, с которой FAQ открыли
        (например, «Поддержка»), а не всегда в «Профиль»."""
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("let faqReturnTab = 'settings';", template)
        self.assertIn("faqReturnTab = activeTab ? activeTab.id.replace('tab-', '') : 'settings';", template)
        self.assertIn("function backFromSettingsFaq()", template)
        self.assertIn("showTab(faqReturnTab || 'settings');", template)
        self.assertIn('onclick="backFromSettingsFaq()"', template)

    def test_referral_dialog_supports_escape_focus_and_safe_telegram_share(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn('aria-hidden="true"', template)
        self.assertIn("let refSheetTrigger = null;", template)
        # Inline-JS кабинета держим на ES2019 (без optional chaining), см.
        # engine/tests_frontend_review.LegacyBrowserSyntaxGuardTests.
        self.assertIn("if (appContainer) appContainer.setAttribute('inert', '');", template)
        self.assertIn("if (appContainer) appContainer.removeAttribute('inert');", template)
        self.assertIn("const refDesktopClose = document.getElementById('ref-desktop-close');", template)
        self.assertIn("if (refDesktopClose) refDesktopClose.focus();", template)
        self.assertIn("event.key === 'Escape'", template)
        self.assertIn("function shareTelegramReferral()", template)
        self.assertIn("https://t.me/share/url?url=${encodeURIComponent(input.value)}", template)
        self.assertIn("telegramWebApp.openTelegramLink(shareUrl)", template)
        self.assertIn("window.open(shareUrl, '_blank', 'noopener,noreferrer')", template)

    def test_desktop_referral_icons_stay_centered_and_links_have_no_input_bars(self):
        template = template_source("engine/templates/dashboard.html")

        # Text rules must not override the inline-flex icon container.
        self.assertIn(".ref-desktop-step > div > strong", template)
        self.assertIn(".ref-desktop-step > div > span", template)
        self.assertNotIn(".ref-desktop-step strong,\n            body.dashboard-v2 .ref-desktop-step span", template)
        # The general dashboard input style uses !important; the readonly link
        # fields explicitly reset it so no black rounded bars remain.
        self.assertIn("body.dashboard-v2 .ref-desktop-link-input", template)
        self.assertIn("background: transparent !important;", template)
        self.assertIn("border: 0 !important;", template)
        self.assertIn("border-radius: 0 !important;", template)
        self.assertIn("box-shadow: none !important;", template)

    def test_desktop_referral_earned_card_sits_below_close_button(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("grid-template-columns: 50px minmax(0, 1fr);", template)
        self.assertIn("min-height: 96px;", template)
        self.assertNotIn("min-height: 136px;", template)
        self.assertIn("position: absolute;\n                top: 60px;\n                right: 28px;", template)
        self.assertIn("right: 28px;\n                min-width: 150px;", template)
        self.assertNotIn("margin-top: 48px;", template)
        self.assertIn("text-align: center;", template)
        self.assertIn("position: absolute;\n                top: 24px;\n                right: 28px;", template)

    def test_referral_terms_use_dedicated_responsive_dialog(self):
        template = template_source("engine/templates/dashboard.html")
        open_terms_source = template[
            template.index("function openReferralTerms()"):
            template.index("function closeReferralTerms()")
        ]

        self.assertIn('id="referral-terms-modal" class="referral-terms-modal hidden"', template)
        self.assertIn('id="referral-terms-panel" class="referral-terms-panel"', template)
        self.assertIn('role="dialog" aria-modal="true"', template)
        self.assertIn('id="referral-terms-title">Условия программы</h2>', template)
        self.assertIn("До {{ max_referral_bonus_days|default:\"40\" }} дней за одного друга", template)
        self.assertIn("Друг зарегистрировался и начал пользоваться сервисом", template)
        self.assertIn("Друг стал активным пользователем сервиса", template)
        self.assertIn("Друг оплатил подписку от 1 месяца", template)
        self.assertNotIn('id="referral-terms-content"', template)
        self.assertNotIn("openQuickAccessModal", open_terms_source)
        self.assertIn("modal.classList.remove('hidden')", open_terms_source)

    def test_referral_terms_match_desktop_and_mobile_concept(self):
        template = template_source("engine/templates/dashboard.html")

        # Mobile-first bottom sheet with drag handle, timeline and brand CTA.
        self.assertIn("align-items: flex-end;", template)
        self.assertIn("border-radius: 24px 24px 0 0;", template)
        self.assertIn('id="referral-terms-drag-zone"', template)
        self.assertIn("grid-template-columns: 44px minmax(0, 1fr);", template)
        self.assertIn("background: #ffc700;", template)
        self.assertIn("color: #0b0c0f;", template)
        # Desktop becomes a centered compact modal with rows and neutral CTA.
        self.assertIn("body.dashboard-v2 .referral-terms-modal", template)
        self.assertIn("width: min(820px, calc(100vw - 48px));", template)
        self.assertIn("grid-template-columns: 46px 104px minmax(0, 1fr);", template)
        self.assertIn("body.dashboard-v2 .referral-terms-confirm", template)
        self.assertIn("background: rgba(255, 255, 255, 0.02);", template)
        self.assertIn("color: rgba(255, 255, 255, 0.88);", template)

    def test_referral_surfaces_use_black_and_yellow_brand_palette(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("body.dashboard-v2 .desktop-referral-icon", template)
        self.assertIn("body.dashboard-v2 .ref-desktop-share-row.is-recommended", template)
        self.assertIn("body.dashboard-v2 .ref-desktop-send-btn", template)
        self.assertIn("border: 1px solid #ffc700;", template)
        self.assertIn("background: rgba(255, 199, 0, 0.10);", template)
        self.assertNotIn("#a96dff", template)
        self.assertNotIn("#ad72ff", template)
        self.assertNotIn("rgba(155, 92, 255", template)
        self.assertNotIn("rgba(169, 109, 255", template)

    def test_referral_terms_support_accessible_close_telegram_back_and_mobile_swipe(self):
        template = template_source("engine/templates/dashboard.html")

        self.assertIn("function referralTermsIsOpen()", template)
        self.assertIn("modal && modal.classList.contains('is-open')", template)
        self.assertIn("event.key === 'Escape' && referralTermsIsOpen()", template)
        self.assertIn("const referralTermsClose = document.getElementById('referral-terms-close');", template)
        self.assertIn("if (referralTermsClose) referralTermsClose.focus();", template)
        self.assertIn("if (appContainer) appContainer.setAttribute('inert', '');", template)
        self.assertIn("if (appContainer) appContainer.removeAttribute('inert');", template)
        self.assertIn(
            "if (cabinetBack || referralTermsOpen || setupActive) "
            "back.show(); else back.hide();",
            template,
        )
        # Экраны и шторки мобильного кабинета закрываются нативной кнопкой первыми.
        self.assertIn("if (window.cmHandleBack && window.cmHandleBack()) return;", template)
        self.assertIn("closeReferralTerms();\n            } else if (newSetupStep", template)
        self.assertIn("function initReferralTermsSwipe()", template)
        self.assertIn("window.matchMedia('(max-width: 1024px)').matches", template)
        self.assertIn("if (dragY >= 76) closeReferralTerms();", template)
        self.assertIn("initReferralTermsSwipe();", template)


class ConfigPinsAdminApiTests(SimpleTestCase):
    def _user(self):
        return SimpleNamespace(
            id=1,
            username="alice",
            email="alice@example.com",
            telegram_id=None,
            expire_at=None,
            autopay_allow=True,
        )

    def _templates(self):
        return [
            SimpleNamespace(id=1, name="EU", is_active=True),
            SimpleNamespace(id=2, name="US", is_active=True),
            SimpleNamespace(id=3, name="Old", is_active=False),
        ]

    def _session(self, settings=None):
        from common.models.db import SystemSetting

        class FakeSession:
            def __init__(self, seed):
                self.settings = dict(seed or {})
                self.deleted = []

            def get(self, model, key):
                return self.settings.get(key)

            def add(self, obj):
                self.settings[obj.key] = obj

            def flush(self):
                pass

            def delete(self, obj):
                self.deleted.append(obj.key)
                self.settings.pop(obj.key, None)

            def commit(self):
                pass

            def close(self):
                pass

        seed = {}
        for key, value in (settings or {}).items():
            seed[key] = SystemSetting(key=key, value=value)
        return FakeSession(seed)

    def test_get_returns_templates_with_pinned_flags(self):
        session = self._session({"cfg_pin:alice": "2"})
        request = RequestFactory().get("/support-admin/api/config-pins/?q=alice")
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
            mock.patch("engine.views.admin_find_user", return_value=self._user()),
            mock.patch("engine.views.load_custom_config_templates", return_value=self._templates()),
        ):
            response = support_admin_api_config_pins(request)
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.content)["result"]
        self.assertEqual(result["pinned_ids"], [2])
        pinned = {t["id"]: t["pinned"] for t in result["templates"]}
        self.assertEqual(pinned, {1: False, 2: True, 3: False})

    def test_save_keeps_only_existing_ids_and_persists_csv(self):
        session = self._session()
        request = RequestFactory().post(
            "/support-admin/api/config-pins/",
            data={"q": "alice", "action": "save", "template_ids": "1,3,99"},
        )
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
            mock.patch("engine.views.admin_find_user", return_value=self._user()),
            mock.patch("engine.views.load_custom_config_templates", return_value=self._templates()),
        ):
            response = support_admin_api_config_pins(request)
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.content)["result"]
        # 99 не существует → отбрасывается; 1 и 3 существуют (в т.ч. неактивный 3).
        self.assertEqual(result["pinned_ids"], [1, 3])
        self.assertEqual(session.settings["cfg_pin:alice"].value, "1,3")

    def test_clear_deletes_setting(self):
        session = self._session({"cfg_pin:alice": "1,2"})
        request = RequestFactory().post(
            "/support-admin/api/config-pins/",
            data={"q": "alice", "action": "clear"},
        )
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
            mock.patch("engine.views.admin_find_user", return_value=self._user()),
            mock.patch("engine.views.load_custom_config_templates", return_value=self._templates()),
        ):
            response = support_admin_api_config_pins(request)
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.content)["result"]
        self.assertEqual(result["pinned_ids"], [])
        self.assertIn("cfg_pin:alice", session.deleted)

    def test_save_empty_selection_clears_existing(self):
        session = self._session({"cfg_pin:alice": "2"})
        request = RequestFactory().post(
            "/support-admin/api/config-pins/",
            data={"q": "alice", "action": "save", "template_ids": ""},
        )
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
            mock.patch("engine.views.admin_find_user", return_value=self._user()),
            mock.patch("engine.views.load_custom_config_templates", return_value=self._templates()),
        ):
            response = support_admin_api_config_pins(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["result"]["pinned_ids"], [])
        self.assertIn("cfg_pin:alice", session.deleted)

    def test_user_not_found_returns_404(self):
        session = self._session()
        request = RequestFactory().get("/support-admin/api/config-pins/?q=ghost")
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
            mock.patch("engine.views.admin_find_user", return_value=None),
        ):
            response = support_admin_api_config_pins(request)
        self.assertEqual(response.status_code, 404)


class ConfigPinsAdminTemplateTests(SimpleTestCase):
    def test_admin_dashboard_has_config_pins_ui(self):
        template = template_source("engine/templates/admin_dashboard.html")
        self.assertIn('data-config-pins-url', template)
        self.assertIn('id="config-pins-form"', template)
        self.assertIn("submitConfigPinsSearch", template)
        self.assertIn("saveConfigPins", template)
        self.assertIn("data-config-pins-save", template)
        self.assertIn("data-config-pins-clear", template)
        self.assertIn("Персональные конфиги", template)


class ConfigPinsCleanupTests(SimpleTestCase):
    def test_remove_config_template_id_prunes_and_drops_empty(self):
        from common.models.db import SystemSetting

        settings = {
            "cfg_pin:alice": SystemSetting(key="cfg_pin:alice", value="1,3"),
            "cfg_pin:bob": SystemSetting(key="cfg_pin:bob", value="3"),
            "cfg_pin:carol": SystemSetting(key="cfg_pin:carol", value="1,2"),
        }

        class FakeQuery:
            def __init__(self, rows):
                self.rows = rows

            def filter(self, *a, **k):
                return self

            def all(self):
                return list(self.rows)

        class FakeSession:
            def __init__(self, store):
                self.store = store

            def query(self, model):
                return FakeQuery(list(self.store.values()))

            def delete(self, obj):
                self.store.pop(obj.key, None)

        remove_config_template_id_from_pins(FakeSession(settings), 3)

        # У alice id=3 убран, осталась "1"; у bob id=3 был единственным — запись удалена;
        # carol не трогаем.
        self.assertEqual(settings["cfg_pin:alice"].value, "1")
        self.assertNotIn("cfg_pin:bob", settings)
        self.assertEqual(settings["cfg_pin:carol"].value, "1,2")

    def test_config_pins_payload_ignores_missing_template_ids(self):
        from common.models.db import SystemSetting

        class FakeSession:
            def __init__(self, setting):
                self.setting = setting

            def get(self, model, key):
                return self.setting if key == self.setting.key else None

        user = SimpleNamespace(
            id=1,
            username="alice",
            email="",
            telegram_id=None,
            expire_at=None,
            autopay_allow=True,
        )
        templates = [
            SimpleNamespace(id=1, name="A", is_active=True),
            SimpleNamespace(id=2, name="B", is_active=True),
        ]
        session = FakeSession(SystemSetting(key="cfg_pin:alice", value="2,999"))

        payload = config_pins_payload(session, user, templates)

        # 999 не существует → не попадает ни в pinned_ids, ни во флаги.
        self.assertEqual(payload["pinned_ids"], [2])
        self.assertEqual(
            {t["id"]: t["pinned"] for t in payload["templates"]},
            {1: False, 2: True},
        )


class ConfigModalScrollLockTests(SimpleTestCase):
    def test_config_modal_locks_background_scroll(self):
        template = template_source("engine/templates/admin_dashboard.html")
        # Фон под модалкой фиксируется (iOS-safe), а инерционный скролл не пробрасывается.
        self.assertIn("html.modal-open body { position: fixed", template)
        self.assertIn("function lockBodyScroll", template)
        self.assertIn("function unlockBodyScroll", template)
        self.assertIn("if (!wasOpen) lockBodyScroll();", template)
        self.assertIn("overscroll-behavior: contain", template)
        # Открытие/закрытие модалки конфига действительно вызывают лок/анлок.
        open_fn = template.split("function openConfigTemplateModal", 1)[1].split("function closeConfigTemplateModal", 1)[0]
        self.assertIn("lockBodyScroll()", open_fn)
        close_fn = template.split("function closeConfigTemplateModal", 1)[1].split("function setConfigSaving", 1)[0]
        self.assertIn("unlockBodyScroll()", close_fn)

    def test_config_modal_card_is_scroll_container_with_sticky_header(self):
        template = template_source("engine/templates/admin_dashboard.html")
        # Скроллится вся карточка (колесо/жест над шапкой тоже прокручивает к кнопке),
        # шапка закреплена сверху и непрозрачна.
        card_rule = template.split(".modal-card.config-template-modal-card {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-y: auto", card_rule)
        self.assertIn(".config-template-modal-card .modal-header { position: sticky", template)
        self.assertIn(".config-template-modal-card .modal-body { overflow: visible", template)


class AdminCssBraceBalanceTests(SimpleTestCase):
    def test_inline_styles_and_static_css_have_balanced_braces(self):
        # 10.09.2026 незакрытая `{` в инлайн-CSS уехала в прод и «съела» все
        # правила после себя (развалился топбар и полстраницы). CSS не падает
        # с ошибкой — единственная защита от такого выстрела — этот тест.
        import re

        template = template_source("engine/templates/admin_dashboard.html")
        for index, block in enumerate(re.findall(r"<style>(.*?)</style>", template, re.S)):
            with self.subTest(style_block=index):
                self.assertEqual(
                    block.count("{"), block.count("}"),
                    f"незакрытая скобка в <style> №{index} admin_dashboard.html",
                )
        for css_path in sorted(Path("engine/static/css").glob("*.css")):
            content = css_path.read_text()
            with self.subTest(css=css_path.name):
                self.assertEqual(
                    content.count("{"), content.count("}"),
                    f"незакрытая скобка в {css_path.name}",
                )


class ConfigTemplatesAdminUiTests(SimpleTestCase):
    def test_config_delivery_rules_share_one_responsive_workspace(self):
        template = template_source("engine/templates/admin_dashboard.html")

        # Утверждённый концепт (10.09.2026): пиннинг и UA-правила — два
        # постоянных равноправных инструмента рядом; аккордеона и выпадающих
        # «окон снизу под обеими плашками» больше нет.
        self.assertIn('class="config-delivery-section"', template)
        self.assertIn('<h2 id="config-delivery-title">Логика выдачи</h2>', template)
        delivery_markup = template.split('<section class="config-delivery-section"', 1)[1].split(
            '<div id="config-templates-result"', 1
        )[0]
        for gone in (
            "data-config-delivery-panel",
            "config-delivery-panels",
            "config-delivery-chevron",
            "config-delivery-intro",
            "aria-expanded",
        ):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, delivery_markup)
        self.assertNotIn("function toggleConfigDeliveryPanel(button)", template)
        self.assertIn('class="config-delivery-body config-delivery-pins"', delivery_markup)
        self.assertIn('class="config-delivery-body config-delivery-ua"', delivery_markup)
        self.assertNotIn("hidden>", delivery_markup)
        self.assertIn(
            ".config-delivery-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; align-items: start; }",
            template,
        )
        self.assertIn('id="config-pins-form"', delivery_markup)
        self.assertIn('id="ua-rule-form"', delivery_markup)
        self.assertIn('class="ua-rule-sentence"', delivery_markup)
        for field_name in ("match_substring", "variable_name", "value", "priority", "is_active"):
            self.assertIn(f'name="{field_name}"', delivery_markup)

    def test_ua_rules_render_as_readable_condition_flow(self):
        template = template_source("engine/templates/admin_dashboard.html")

        # Строка правила читается как «условие -> CLIENT=значение»; статус —
        # точка, действия появляются по hover, клик по строке — редактирование
        self.assertIn('class="ua-rule-item', template)
        self.assertIn('class="ua-rules-head"', template)
        self.assertIn('class="ua-rule-cond"', template)
        self.assertIn("${escapeHtml(rule.variable_name)}=<b>${escapeHtml(rule.value)}</b>", template)
        self.assertIn("data-ua-rule-edit=", template)
        self.assertIn("data-ua-rule-delete=", template)
        self.assertIn("!event.target.closest('[data-ua-rule-delete]')", template)
        self.assertNotIn("<small>Если UA содержит</small>", template)
        # При hover приоритет сменяется иконками 26px: ячейка держит эту
        # высоту всегда, иначе строка прыгала.
        self.assertIn(".ua-rule-prio-wrap { display: flex; align-items: center; justify-content: flex-end; min-height: 26px; }", template)

    def test_json_editor_is_tall(self):
        template = template_source("engine/templates/admin_dashboard.html")
        # codemirror.css грузится позже и ставит .CodeMirror { height: 300px };
        # наш селектор с оболочкой сильнее и растягивает редактор на экран.
        self.assertIn(".config-json-shell .CodeMirror { height: clamp(480px, 70vh, 960px); }", template)
        self.assertIn(".json-editor-textarea { min-height: clamp(480px, 70vh, 960px);", template)

    def test_json_editor_selection_is_visible(self):
        template = template_source("engine/templates/admin_dashboard.html")
        # Выделение текста в CodeMirror перекрывает блеклый фон темы material-darker,
        # иначе выделенный фрагмент почти не отличим от фона редактора.
        self.assertIn(".cm-s-material-darker div.CodeMirror-selected", template)
        self.assertIn(".cm-s-material-darker.CodeMirror-focused div.CodeMirror-selected", template)
        self.assertIn(".cm-s-material-darker .CodeMirror-line::selection", template)

    def test_config_modal_does_not_close_on_backdrop_or_escape(self):
        template = template_source("engine/templates/admin_dashboard.html")
        # Клик по фону и Escape не закрывают модалку конфига: это теряло
        # несохранённый JSON. Закрытие — только кнопками «Отмена»/крестик.
        self.assertNotIn("if (event.target.id === 'config-template-modal') closeConfigTemplateModal();", template)
        escape_handler = template.split("if (event.key === 'Escape')", 1)[1].split("});", 1)[0]
        self.assertNotIn("closeConfigTemplateModal", escape_handler)
        # Обычные кнопки закрытия остаются.
        self.assertIn("button.addEventListener('click', closeConfigTemplateModal);", template)
        # Просмотровая модалка пользователей источника по-прежнему закрывается фоном и Escape.
        self.assertIn("if (event.target.id === 'source-users-modal') closeSourceUsersModal();", template)
        self.assertIn("closeSourceUsersModal();", escape_handler)

    def test_config_pins_render_as_compact_chip_grid_with_filter(self):
        template = template_source("engine/templates/admin_dashboard.html")
        # Вместо вертикальной «колбасы» строк — сетка компактных чипов,
        # фильтр по названию, счётчик выбранных и свёрнутые неактивные конфиги.
        self.assertIn('class="config-pin-chip', template)
        self.assertNotIn("config-pin-row", template)
        self.assertIn("data-config-pins-filter", template)
        self.assertIn("data-config-pins-counter", template)
        self.assertIn('<details class="config-pins-inactive">', template)
        pins_list_rule = template.split(".config-pins-list {", 1)[1].split("}", 1)[0]
        self.assertIn("repeat(auto-fill", pins_list_rule)
        # Сохранение собирает чекбоксы из обоих списков (активные + неактивные).
        self.assertIn(".config-pins-list input[type=\"checkbox\"]:checked", template)

    def test_config_template_cards_have_no_json_previews(self):
        template = template_source("engine/templates/admin_dashboard.html")
        # Превью template_json и additional_headers из карточек убраны — карточки
        # компактные; наличие заголовков видно по бейджу.
        self.assertNotIn("config-template-preview", template)
        self.assertIn("+ заголовки", template)
        card_rule = template.split(".config-template-card {", 1)[1].split("}", 1)[0]
        self.assertNotIn("min-height: 440px", card_rule)

class NodeTrafficReportTests(SimpleTestCase):
    """Агрегация трафика нод для вкладки «Трафик нод» (engine/node_traffic.py)."""

    def _nodes(self):
        import proto.rwmanager_pb2 as rw_proto

        return [
            rw_proto.Node(uuid="n1", name="Германия", address="1.1.1.1", is_connected=True),
            rw_proto.Node(uuid="n2", name="Швеция", address="2.2.2.2", is_connected=True),
        ]

    def _fake_client(self, usage_by_node, users_by_uuid=None):
        import proto.rwmanager_pb2 as rw_proto

        class FakeRwms:
            def get_node_users_usage(self, request, *, timeout=None):
                rows = usage_by_node.get(request.node_uuid)
                if rows is None:
                    return None
                return rw_proto.GetNodeUsersUsageResponse(items=rows)

            def get_user_by_uuid(self, uuid):
                return (users_by_uuid or {}).get(uuid)

        return FakeRwms()

    def test_report_aggregates_users_across_nodes(self):
        import proto.rwmanager_pb2 as rw_proto
        from datetime import timedelta, timezone as tz
        from engine import node_traffic

        usage = {
            "n1": [
                rw_proto.NodeUserUsage(user_uuid="u1", username="111", total_bytes=100, date="2026-07-10"),
            ],
            "n2": [
                rw_proto.NodeUserUsage(user_uuid="u1", username="111", total_bytes=50, date="2026-07-10"),
                rw_proto.NodeUserUsage(user_uuid="u2", username="222", total_bytes=300, date="2026-07-10"),
            ],
        }
        client = self._fake_client(usage)
        end = datetime(2026, 7, 10, tzinfo=tz.utc)

        report = node_traffic.build_report(
            client, self._nodes(), end - timedelta(hours=1), end,
            top=50, min_gib=0, with_details=False,
        )

        self.assertEqual(report["total_bytes"], 450)
        self.assertEqual(report["users_with_traffic"], 2)
        self.assertEqual(report["failed_nodes"], [])
        first, second = report["users"]
        self.assertEqual(first["username"], "222")
        self.assertEqual(first["total_bytes"], 300)
        self.assertEqual(second["username"], "111")
        self.assertEqual(second["total_bytes"], 150)
        # у "111" больше всего трафика на "Германия" (100 из 150)
        self.assertEqual(second["top_node"], "Германия")

    def test_report_excludes_service_users_and_reports_failed_nodes(self):
        import proto.rwmanager_pb2 as rw_proto
        from datetime import timedelta, timezone as tz
        from engine import node_traffic

        usage = {
            "n1": [
                rw_proto.NodeUserUsage(user_uuid="sys", username="SYS-REROUTE", total_bytes=10**12, date="2026-07-10"),
                rw_proto.NodeUserUsage(user_uuid="u1", username="111", total_bytes=100, date="2026-07-10"),
            ],
            # ноды n2 нет в ответах — имитация HTTP 500 панели
        }
        client = self._fake_client(usage)
        end = datetime(2026, 7, 10, tzinfo=tz.utc)

        report = node_traffic.build_report(
            client, self._nodes(), end - timedelta(hours=1), end,
            top=50, min_gib=0, with_details=False,
        )

        # служебный пользователь не в выборке и не в totals
        self.assertEqual(report["total_bytes"], 100)
        self.assertEqual([u["username"] for u in report["users"]], ["111"])
        self.assertEqual(report["excluded_users"][0]["username"], "SYS-REROUTE")
        self.assertEqual(report["failed_nodes"], ["Швеция"])

    def test_report_enriches_details_for_shown_rows(self):
        import proto.rwmanager_pb2 as rw_proto
        from datetime import timedelta, timezone as tz
        from google.protobuf.timestamp_pb2 import Timestamp
        from engine import node_traffic

        expire = Timestamp()
        expire.FromDatetime(datetime(2026, 8, 3))
        details = rw_proto.UserResponse(
            uuid="u1", username="111", status=rw_proto.UserStatus.ACTIVE,
            expire_at=expire, telegram_id=111,
        )
        usage = {
            "n1": [rw_proto.NodeUserUsage(user_uuid="u1", username="111", total_bytes=100, date="2026-07-10")],
            "n2": [],
        }
        client = self._fake_client(usage, users_by_uuid={"u1": details})
        end = datetime(2026, 7, 10, tzinfo=tz.utc)

        report = node_traffic.build_report(
            client, self._nodes(), end - timedelta(hours=1), end,
            top=50, min_gib=0,
        )

        row = report["users"][0]
        self.assertEqual(row["status"], "ACTIVE")
        self.assertEqual(row["expire_at"], "2026-08-03")
        self.assertEqual(row["telegram_id"], 111)


class NodeTrafficAdminApiTests(SimpleTestCase):
    def test_node_traffic_rejects_invalid_period(self):
        from engine.views import support_admin_api_node_traffic

        request = RequestFactory().get("/support-admin/api/node-traffic/?hours=0")
        with mock.patch("engine.views.require_support_admin_role", return_value=None):
            response = support_admin_api_node_traffic(request)

        self.assertEqual(response.status_code, 400)

    def test_node_traffic_returns_502_when_rwms_unavailable(self):
        from engine.views import support_admin_api_node_traffic

        request = RequestFactory().get("/support-admin/api/node-traffic/")
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.node_traffic.list_nodes", return_value=None),
        ):
            response = support_admin_api_node_traffic(request)

        self.assertEqual(response.status_code, 502)

    def test_node_traffic_unknown_node_returns_404(self):
        import proto.rwmanager_pb2 as rw_proto
        from engine.views import support_admin_api_node_traffic

        nodes = [rw_proto.Node(uuid="n1", name="Германия")]
        request = RequestFactory().get("/support-admin/api/node-traffic/?node=missing")
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.node_traffic.list_nodes", return_value=nodes),
        ):
            response = support_admin_api_node_traffic(request)

        self.assertEqual(response.status_code, 404)

    def test_node_traffic_happy_path_filters_selected_node(self):
        import proto.rwmanager_pb2 as rw_proto
        from engine.views import support_admin_api_node_traffic

        nodes = [
            rw_proto.Node(uuid="n1", name="Германия"),
            rw_proto.Node(uuid="n2", name="Швеция"),
        ]
        request = RequestFactory().get(
            "/support-admin/api/node-traffic/?node=n1&hours=1&top=10"
        )
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.node_traffic.list_nodes", return_value=nodes),
            mock.patch(
                "engine.node_traffic.build_report", return_value={"users": []}
            ) as build_report,
        ):
            response = support_admin_api_node_traffic(request)

        self.assertEqual(response.status_code, 200)
        passed_nodes = build_report.call_args.args[1]
        self.assertEqual([n.uuid for n in passed_nodes], ["n1"])

    def test_traffic_nodes_endpoint_lists_nodes(self):
        import proto.rwmanager_pb2 as rw_proto
        from engine.views import support_admin_api_traffic_nodes

        nodes = [rw_proto.Node(uuid="n1", name="Германия", is_connected=True)]
        request = RequestFactory().get("/support-admin/api/traffic-nodes/")
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.node_traffic.list_nodes", return_value=nodes),
        ):
            response = support_admin_api_traffic_nodes(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["result"][0]["name"], "Германия")


class NodeTrafficTemplateTests(SimpleTestCase):
    def test_node_traffic_report_uses_current_admin_design_system(self):
        template = template_source("engine/templates/admin_dashboard.html")

        for marker in (
            'class="node-traffic-report"',
            'class="card node-traffic-summary-card"',
            'class="node-traffic-summary-grid"',
            'class="node-traffic-context-note"',
            'class="card node-traffic-table-card"',
            'class="node-traffic-table-scroll"',
            'class="node-traffic-share-track"',
            'class="node-traffic-status${statusKind}"',
            'class="node-traffic-top-node-copy"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, template)

        row_rule_start = template.index("#panel-node-traffic .node-traffic-row {")
        row_rule = template[row_rule_start:template.index("}", row_rule_start) + 1]
        self.assertIn("min-width: 1240px", row_rule)
        self.assertIn("font-size: 12.5px", row_rule)
        self.assertIn("const shareTone = share >= 10 ? ' is-hot'", template)
        self.assertIn("['ACTIVE', 'ENABLED'].includes(normalizedStatus)", template)
        self.assertNotIn('class="card info-card" style="margin-bottom:10px;"', template)

    def test_admin_dashboard_has_node_traffic_tab(self):
        # «Трафик нод» — самостоятельный раздел сайдбара под «Системой»:
        # это отчёт, за которым ходят напрямую, а не по пути к установке нод
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('data-tab="node-traffic"', template)
        # `.infra-legacy-panel { display:block }` beats `.tab-panel { display:none }`:
        # the panel must stay a plain tab-panel or it shows on every tab.
        self.assertIn('<section id="panel-node-traffic" class="tab-panel">', template)
        self.assertNotIn('id="panel-node-traffic" class="tab-panel infra-legacy-panel"', template)
        self.assertIn('.sidebar .tab-btn[data-tab="node-traffic"] i { --admin-nav-icon:', Path('engine/static/css/admin-concept.css').read_text())
        self.assertIn('id="panel-node-traffic" class="tab-panel', template)
        self.assertIn("if (tabId === 'node-traffic') initNodeTrafficTab();", template)
        # Из подвкладок инфраструктуры раздел убран без следов
        self.assertNotIn('data-subtab="inf-traffic"', template)
        self.assertNotIn('id="subpanel-inf-traffic"', template)
        self.assertNotIn("'inf-traffic'", template)
        self.assertIn('data-tab="infrastructure"', template)
        self.assertIn('id="node-traffic-form"', template)
        self.assertIn("data-traffic-nodes-url", template)
        self.assertIn("data-node-traffic-url", template)
        # раздел доступен только полному админу
        admin_only_block = template.split('{% if support_admin_is_full_admin %}')
        self.assertTrue(
            any('data-tab="infrastructure"' in part.split("{% endif %}")[0] for part in admin_only_block[1:])
        )

    def test_node_traffic_icons_exist_in_bundled_font_awesome(self):
        """Страница использует иконки FA 6.1+ (fa-magnifying-glass-chart,
        fa-ranking-star, глиф \\e522); с FA 6.0.0 они рендерились пустыми
        квадратами, поэтому шаблоны обязаны подключать FA >= 6.1."""
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("font-awesome/6.7.2/css/all.min.css", template)
        self.assertIn('content: "\\e522"', template)  # fa-magnifying-glass-chart
        self.assertNotIn('content: "\\e51d"', template)  # fa-laptop-file — чужой глиф
        self.assertIn('<i class="fas fa-magnifying-glass-chart"></i>', template)
        self.assertIn('<i class="fas fa-ranking-star"></i>', template)

        for path in sorted(Path("engine/templates").glob("*.html")):
            with self.subTest(template=path.name):
                self.assertNotIn("font-awesome/6.0.0", path.read_text())

    def test_node_traffic_summary_icons_are_readable(self):
        """Иконки сводки были 11px muted-серым на тёмном фоне — почти невидимы."""
        template = template_source("engine/templates/admin_dashboard.html")

        def css_rule(selector):
            start = template.index(f"{selector} {{")
            return template[start:template.index("}", start) + 1]

        section = css_rule("#panel-node-traffic .node-traffic-section-icon")
        self.assertIn("font-size: 14px", section)
        summary = css_rule("#panel-node-traffic .node-traffic-summary-item-icon")
        self.assertIn("font-size: 14px", summary)
        self.assertNotIn("color: var(--muted)", summary)
        # Общий селектор `.node-traffic-summary-item span` специфичнее правила
        # иконки: он превращал её плашку в block с 10px-шрифтом — глиф уезжал
        # в верхний левый угол. Подписи стилизуются только через -copy.
        self.assertNotIn(".node-traffic-summary-item span", template)
        self.assertNotIn(".node-traffic-summary-item strong", template)
        self.assertIn(".node-traffic-summary-item-copy span", template)
        self.assertIn(".node-traffic-summary-item-copy strong", template)
        # Светлая тема: у иконок свой контрастный цвет, а не белый из тёмной.
        self.assertIn(
            'html[data-admin-theme="light"] #panel-node-traffic .node-traffic-summary-item-icon',
            template,
        )

    def test_date_popover_is_kept_inside_viewport(self):
        # Календарь у правого края карточки не должен вылезать за экран:
        # после открытия/перелистывания/ресайза JS считает сдвиг и кладёт его в CSS-переменную.
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("function positionDatePopover(field)", template)
        self.assertIn("popover.style.setProperty('--date-popover-shift', `${Math.round(shift)}px`);", template)
        self.assertIn("transform: translateX(var(--date-popover-shift, 0px));", template)
        self.assertIn("transform: translateX(calc(-50% + var(--date-popover-shift, 0px)));", template)
        self.assertIn("positionDatePopover(field);\n            popover.querySelector('[data-date-prev]')", template)
        self.assertIn("document.querySelectorAll('[data-date-picker].open').forEach(positionDatePopover);", template)

    def test_node_traffic_loads_today_report_on_first_tab_open(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('<option value="1" selected>Сегодня (с 03:00 МСК)</option>', template)
        self.assertIn('<option value="24">Вчера + сегодня</option>', template)
        self.assertIn("let nodeTrafficInitialReportLoaded = false;", template)
        self.assertIn("if (!nodeTrafficInitialReportLoaded && form)", template)
        self.assertIn("nodeTrafficInitialReportLoaded = true;", template)
        self.assertIn("loadNodeTrafficForForm(form);", template)
        self.assertIn("function loadNodeTraffic(event)", template)


class NodeProvisionTemplateTests(SimpleTestCase):
    def setUp(self):
        self.template = template_source("engine/templates/admin_dashboard.html")

    def test_node_provision_separates_installations_and_scripts(self):
        for marker in (
            'data-node-provision-view="installations"',
            'data-node-provision-view="scripts"',
            'data-node-provision-panel="installations"',
            'data-node-provision-panel="scripts"',
            'class="node-provision-flow"',
            'id="node-provision-readiness"',
            'class="node-provision-form-grid"',
            'class="node-provision-request-row"',
            'class="node-provision-scripts-layout"',
            'class="node-provision-script-manager"',
            'id="node-provision-script-add"',
            'id="node-provision-script-rename"',
            'id="node-provision-script-delete"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        self.assertIn("function setNodeProvisionView(view)", self.template)
        self.assertIn("function submitNodeScriptNameForm(event)", self.template)
        self.assertIn("function deleteNodeScript(nodeType, label)", self.template)
        self.assertIn('name="script_name"', self.template)
        self.assertNotIn('class="node-provision-script-block"', self.template)
        self.assertNotIn('class="node-provision-table"', self.template)

    def test_node_script_editor_has_live_bash_highlighting(self):
        for marker in (
            'id="node-provision-editor-highlight"',
            'id="node-provision-editor-lines"',
            'data-node-script-editor=',
            'wrap="off"',
            "function nodeProvisionHighlightBashLine",
            "function renderNodeScriptEditor()",
            "NODE_PROVISION_BASH_KEYWORDS",
            "node-provision-bash-comment",
            "node-provision-bash-variable",
            "node-provision-bash-string",
            "editor.setRangeText('  '",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        # Подсветка живёт в отдельном слое, а исходный текст по-прежнему
        # читается из textarea и без преобразований отправляется в API.
        self.assertIn("body.append('content', editor.value);", self.template)
        self.assertIn("color: transparent", self.template)
        self.assertIn("pointer-events: none", self.template)

    def test_node_provision_layout_has_mobile_reflow(self):
        self.assertIn("@media (max-width: 760px)", self.template)
        self.assertIn(".node-provision-form-grid { grid-template-columns: 1fr; }", self.template)
        self.assertIn(".node-provision-request-row { grid-template-columns: 1fr; }", self.template)
        self.assertIn(".node-provision-script-nav { display: flex;", self.template)
        self.assertIn("overflow-x: auto", self.template)
        self.assertIn("min-height: 520px", self.template)
        self.assertIn(
            'html[data-admin-theme="light"] .node-provision-code-editor',
            self.template,
        )


class NodeTrafficDayGranularityTests(SimpleTestCase):
    """Панель хранит трафик посуточно (created_at = 00:00 дня, UTC); начало
    периода внутри дня отбрасывало весь этот день (инцидент 2026-07-10)."""

    def test_build_report_floors_start_to_day_boundary(self):
        import proto.rwmanager_pb2 as rw_proto
        from datetime import timezone as tz
        from engine import node_traffic

        captured = {}

        class FakeRwms:
            def get_node_users_usage(self, request, *, timeout=None):
                captured["start"] = request.start.ToDatetime()
                return rw_proto.GetNodeUsersUsageResponse(items=[])

        nodes = [rw_proto.Node(uuid="n1", name="Германия")]
        report = node_traffic.build_report(
            FakeRwms(),
            nodes,
            datetime(2026, 7, 9, 1, 36, tzinfo=tz.utc),
            datetime(2026, 7, 10, 1, 36, tzinfo=tz.utc),
            top=50,
            min_gib=0,
            with_details=False,
        )

        # запрос к rwms ушёл с началом в полночь UTC — суточный бакет 9 июля
        # (created_at = 09.07 00:00) не будет отброшен фильтром панели
        self.assertEqual(captured["start"], datetime(2026, 7, 9, 0, 0))
        # метки периода в отчёте показываются по МСК (UTC+3)
        self.assertEqual(report["start"], "2026-07-09 03:00")


class AcquisitionJourneyTests(SimpleTestCase):
    """Секции «Путь клиента»: тайминг первой оплаты, лестница продлений,
    переходы между тарифами."""

    @mock.patch("engine.views._acq_rows")
    def test_trial_timing_builds_buckets_with_shares(self, rows_mock):
        rows_mock.side_effect = [
            [{"trials": 1000, "converted": 100}],
            [{"bucket": 1, "users": 40}, {"bucket": 8, "users": 60}],
        ]

        result = _acq_trial_timing(object(), 365)

        self.assertEqual(result["trials"], 1000)
        self.assertEqual(result["converted"], 100)
        self.assertEqual(result["conversion_pct"], 10.0)
        self.assertEqual(len(result["buckets"]), len(ACQ_TIMING_LABELS))
        self.assertEqual(result["buckets"][0], {"label": "0–1", "users": 40, "pct": 40.0})
        self.assertEqual(result["buckets"][7], {"label": "7–8", "users": 60, "pct": 60.0})
        # Пустые бакеты присутствуют с нулями, а не пропущены.
        self.assertEqual(result["buckets"][1], {"label": "1–2", "users": 0, "pct": 0})

    @mock.patch("engine.views._acq_rows")
    def test_trial_timing_zero_division_safe(self, rows_mock):
        rows_mock.side_effect = [[{"trials": 0, "converted": 0}], []]

        result = _acq_trial_timing(object(), 30)

        self.assertEqual(result["conversion_pct"], 0)
        self.assertTrue(all(b["pct"] == 0 for b in result["buckets"]))

    @mock.patch("engine.views._acq_rows")
    def test_renewal_ladder_cohorts_and_totals(self, rows_mock):
        rows_mock.return_value = [
            {"cohort": "2026-05", "attracted": 100, "paid1": 20, "paid2": 10,
             "paid3": 5, "paid4": 2},
            {"cohort": "2026-06", "attracted": 200, "paid1": 30, "paid2": 12,
             "paid3": 6, "paid4": 3},
        ]

        result = _acq_renewal_ladder(object(), 12)

        self.assertEqual(len(result["cohorts"]), 2)
        self.assertEqual(result["cohorts"][0]["attracted"], 100)
        self.assertEqual(
            result["totals"],
            {"attracted": 300, "paid1": 50, "paid2": 22, "paid3": 11, "paid4": 5},
        )

    @mock.patch("engine.views._acq_rows")
    def test_tariff_paths_first_purchase_and_transitions(self, rows_mock):
        rows_mock.side_effect = [
            [
                {"tariff": "year", "prev_tariff": None, "users": 198},
                {"tariff": "year", "prev_tariff": "month", "users": 105},
                {"tariff": "year", "prev_tariff": "threemonths", "users": 54},
            ],
            [
                {"tariff": "year", "payments": 400, "buyers": 357,
                 "rub": Decimal("600000")},
            ],
        ]

        result = _acq_tariff_paths(object(), 12)

        self.assertEqual(len(result["tariffs"]), 1)
        year = result["tariffs"][0]
        self.assertEqual(year["adopters"], 357)
        self.assertEqual(year["first_purchase"], 198)
        self.assertEqual(year["first_purchase_pct"], 55.5)
        # Переходы отсортированы по убыванию людей.
        self.assertEqual(year["from"][0], {"tariff": "month", "users": 105})
        self.assertEqual(year["from"][1], {"tariff": "threemonths", "users": 54})

    @mock.patch("engine.views._acq_rows")
    def test_tariff_paths_transition_without_volume_row(self, rows_mock):
        # Тариф встречается в переходах, но не в объёмах окна (куплен на границе)
        # — не должен падать с KeyError.
        rows_mock.side_effect = [
            [{"tariff": "oneday", "prev_tariff": None, "users": 3}],
            [],
        ]

        result = _acq_tariff_paths(object(), 3)

        self.assertEqual(result["tariffs"][0]["tariff"], "oneday")
        self.assertEqual(result["tariffs"][0]["first_purchase_pct"], 100.0)

    def test_tariff_cte_covers_both_gateways(self):
        self.assertIn("wata_transactions", ACQ_PAYS_TARIFF_CTE)
        self.assertIn("yk_payments", ACQ_PAYS_TARIFF_CTE)
        self.assertIn("subscription_period", ACQ_PAYS_TARIFF_CTE)
        self.assertIn("tariff_id", ACQ_PAYS_TARIFF_CTE)

    def test_journey_subtab_present_in_template(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('data-subtab="acq-journey"', template)
        self.assertIn('id="subpanel-acq-journey"', template)
        self.assertIn('id="acq-timing-table"', template)
        self.assertIn('id="acq-ladder-table"', template)
        self.assertIn('id="acq-tariff-paths-table"', template)
        self.assertIn("'acq-journey': loadJourney", template)
        self.assertIn("acqFetch('trial_timing'", template)
        self.assertIn("acqFetch('renewal_ladder'", template)
        self.assertIn("acqFetch('tariff_paths'", template)


class AdminCensorBulkUpdateTests(SimpleTestCase):
    def test_bulk_update_changes_only_selected_fields_for_all_checks(self):
        request = RequestFactory().post(
            "/support-admin/api/censor-checks/",
            {
                "action": "bulk_update",
                "apply_mode": "1",
                "mode": "geo-light",
                "apply_api_key": "1",
                "api_key_id": "7",
                "apply_interval": "1",
                "interval_minutes": "360",
                "apply_alerts": "1",
                "alerts_enabled": "0",
                "apply_enabled": "1",
                "is_enabled": "1",
            },
        )
        session = mock.MagicMock()
        session.get.return_value = SimpleNamespace(id=7)
        session.query.return_value.update.return_value = 4

        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
        ):
            response = support_admin_api_censor_checks(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["updated"], 4)
        session.get.assert_called_once_with(RipeApiKey, 7)
        session.query.assert_called_once_with(CensorCheck)
        updates = session.query.return_value.update.call_args.args[0]
        self.assertIs(updates[CensorCheck.geo_mode], True)
        self.assertIs(updates[CensorCheck.light_mode], True)
        self.assertEqual(updates[CensorCheck.api_key_id], 7)
        self.assertEqual(updates[CensorCheck.interval_minutes], 360)
        self.assertIs(updates[CensorCheck.alerts_enabled], False)
        self.assertIs(updates[CensorCheck.is_enabled], True)
        self.assertIn(CensorCheck.updated_at, updates)
        self.assertEqual(
            session.query.return_value.update.call_args.kwargs,
            {"synchronize_session": False},
        )
        session.commit.assert_called_once_with()
        session.close.assert_called_once_with()

    def test_delete_many_removes_selected_checks_with_their_runs(self):
        request = RequestFactory().post(
            "/support-admin/api/censor-checks/",
            {"action": "delete_many", "check_ids": ["3", "5,7", "5", "мусор"]},
        )
        session = mock.MagicMock()
        session.query.return_value.filter.return_value.delete.return_value = 3

        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
            mock.patch("engine.views.admin_audit_write"),
        ):
            response = support_admin_api_censor_checks(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["deleted"], 3)
        # Сначала прогоны, потом сами проверки: иначе останутся сироты
        self.assertEqual(
            [call.args[0] for call in session.query.call_args_list],
            [CensorCheckRun, CensorCheck],
        )
        session.commit.assert_called_once_with()
        session.close.assert_called_once_with()

    def test_delete_many_without_selection_changes_nothing(self):
        request = RequestFactory().post(
            "/support-admin/api/censor-checks/",
            {"action": "delete_many", "check_ids": ["", "мусор"]},
        )
        session = mock.MagicMock()

        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
        ):
            response = support_admin_api_censor_checks(request)

        self.assertEqual(response.status_code, 400)
        session.query.assert_not_called()
        session.commit.assert_not_called()
        session.close.assert_called_once_with()

    def test_bulk_update_requires_an_explicit_field_selection(self):
        request = RequestFactory().post(
            "/support-admin/api/censor-checks/",
            {"action": "bulk_update", "mode": "geo-full"},
        )
        session = mock.MagicMock()

        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
        ):
            response = support_admin_api_censor_checks(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.content)["message"],
            "Выберите хотя бы один параметр",
        )
        session.query.assert_not_called()
        session.commit.assert_not_called()
        session.close.assert_called_once_with()

    @override_settings(RIPE_ATLAS_API_KEY="")
    def test_bulk_update_rejects_missing_default_key(self):
        request = RequestFactory().post(
            "/support-admin/api/censor-checks/",
            {
                "action": "bulk_update",
                "apply_api_key": "1",
                "api_key_id": "",
            },
        )
        session = mock.MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None

        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
        ):
            response = support_admin_api_censor_checks(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.content)["message"],
            "Ключ по умолчанию не настроен",
        )
        session.query.assert_called_once_with(RipeApiKey)
        session.commit.assert_not_called()
        session.close.assert_called_once_with()


class AdminChartPaletteTests(SimpleTestCase):
    """Расцветка серий на графиках админки."""

    def setUp(self):
        self.template = template_source("engine/templates/admin_dashboard.html")
        self.css = Path("engine/static/css/admin_dashboard.css").read_text()

    def test_dark_theme_keeps_historical_multi_hue_series_palette(self):
        # Тёмная тема должна использовать ровно ту палитру, что была до
        # редизайна: серо-жёлтые оттенки делали соседние серии неразличимыми.
        for token in (
            "indigo: {dark: 'rgba(120,140,255,.75)'",
            "amber: {dark: 'rgba(255,199,0,.9)'",
            "green: {dark: 'rgba(90,220,150,.95)'",
            "violet: {dark: '#9b6dff'",
            "orange: {dark: '#f5b85b'",
            "slate: {dark: '#d8dde8'",
            "sky: {dark: '#38bdf8'",
            "pink: {dark: '#f472b6'",
        ):
            self.assertIn(token, self.template)

        self.assertIn(
            "const tariffChartTones = Object.freeze(['indigo', 'amber', 'violet', 'orange', 'slate', 'sky', 'pink']);",
            self.template,
        )
        self.assertIn(
            "const tariffChartColors = tariffChartTones.map((tone) => CHART_TONES[tone].dark);",
            self.template,
        )
        self.assertIn(
            "const acquisitionChartTones = Object.freeze({repeat: 'indigo', new: 'amber', payers: 'green'});",
            self.template,
        )

    def test_greyscale_series_colors_are_gone_everywhere(self):
        # Ни одна серия не должна остаться на сером/белом/жёлтом варианте
        # редизайна — ни в определениях серий, ни в текстовых сводках.
        for leftover in (
            "'rgba(214,218,226,.58)'",
            "'rgba(214,218,226,.68)'",
            "'rgba(214,218,226,.80)'",
            "'rgba(214,218,226,.82)'",
            "'rgba(255,255,255,.92)'",
            "'rgba(255,255,255,.94)'",
            "'rgba(255,255,255,.96)'",
            "'rgba(255,226,122,.95)'",
            "'#fff2b3'",
            "'#d8a900'",
            "'#8b8f99'",
            "'#ffe27a'",
        ):
            self.assertNotIn(leftover, self.template)

        # Серии описываются токеном тона, а не готовой строкой цвета.
        self.assertNotIn("label: 'Подписки', color:", self.template)
        self.assertNotIn("label: 'Продажи', color:", self.template)
        self.assertIn("{label: 'Подписки', tone: 'indigo',", self.template)
        self.assertIn("{label: 'Продажи', tone: 'pink',", self.template)
        self.assertIn("{label: 'Расход, ₽', tone: 'amberSoft',", self.template)
        self.assertIn("{label: 'Медиана', tone: 'neutral',", self.template)

    def test_light_theme_series_palette_is_saturated_not_grey_yellow(self):
        # Светлой темы в старой версии не было: тона подобраны как затемнённые
        # аналоги тех же оттенков, а не как серо-жёлтая гамма.
        for token in (
            "light: 'rgba(67,80,207,.92)'",
            "light: 'rgba(169,116,0,.95)'",
            "light: 'rgba(9,120,76,.95)'",
            "light: '#8021d0'",
            "light: '#c8500a'",
            "light: '#4d5768'",
            "light: '#046f9f'",
            "light: '#b82a70'",
        ):
            self.assertIn(token, self.template)

        self.assertIn(
            "const tariffChartColorsLight = tariffChartTones.map((tone) => CHART_TONES[tone].light);",
            self.template,
        )
        # Прежние приглушённые светлые оттенки удалены.
        for leftover in ("'#8a6500'", "'#5f6876'", "'#a97900'", "'#7a8493'", "'#bd8a00'"):
            self.assertNotIn(leftover, self.template)

    def test_series_color_resolves_lazily_by_tone(self):
        # Цвет берётся из тона на каждой перерисовке, поэтому смена темы
        # перекрашивает уже нарисованные графики без перезагрузки данных.
        self.assertIn("if (series?.tone) return chartTone(series.tone);", self.template)
        self.assertIn("function chartTone(name)", self.template)
        # Хрупкое сопоставление по подписи и по подстроке цвета убрано.
        self.assertNotIn("label.startsWith('повторн')", self.template)
        self.assertNotIn("color.includes('214,218,226')", self.template)

    def test_summary_highlights_use_theme_aware_classes(self):
        # В сводках под графиками цвет задавался инлайном, из-за чего на
        # светлой теме белый текст был не виден.
        self.assertNotIn('<b style="color:#fff;">', self.template)
        self.assertNotIn('<b style="color:#ffc700">', self.template)
        for cls in ("chart-tone-amber", "chart-tone-indigo", "chart-tone-green", "chart-tone-pink", "chart-tone-strong"):
            self.assertIn(f'class="{cls}"', self.template)
            self.assertIn(f".{cls}", self.css)
        for cls in ("chart-tone-indigo", "chart-tone-amber", "chart-tone-green", "chart-tone-pink"):
            self.assertIn(f'html[data-admin-theme="light"] .{cls}', self.css)


class AdminChartReadabilityTests(SimpleTestCase):
    """Читаемость графиков: сетка, подписи осей, легенда, тултипы."""

    def setUp(self):
        self.template = template_source("engine/templates/admin_dashboard.html")
        self.css = Path("engine/static/css/admin_dashboard.css").read_text()

    def test_axis_labels_are_compact_and_carry_units(self):
        self.assertIn("function adminChartAxisLabel(value, fmt = 'raw')", self.template)
        self.assertIn("млн", self.template)
        self.assertIn("тыс", self.template)
        self.assertIn("if (fmt === 'pct') return `${Math.round(value)}%`;", self.template)
        # Обе оси обоих графиков используют общий форматтер; шестой вызов —
        # подписи сумм над столбиками (barLabels), считается один раз на бар.
        self.assertEqual(self.template.count("adminChartAxisLabel("), 6)
        self.assertIn("const barLabelTexts = barTotals.map((v) => adminChartAxisLabel(v, barFmt));", self.template)

    def test_tooltip_values_have_separators_and_units(self):
        self.assertIn("function acqFmtValue(series, value)", self.template)
        self.assertIn("if (series.fmt === 'rub') return `${fmtRub(value)} ₽`;", self.template)
        self.assertIn("if (series.fmt === 'pct') return `${Number(value).toFixed(1)}%`;", self.template)
        # Штучные метрики раньше печатались без разделителей тысяч.
        self.assertNotIn("sr.fmt === 'raw' ? sr.data[i]", self.template)
        self.assertIn(".acq-tooltip-value", self.css)
        self.assertIn(".acq-tooltip-name", self.css)

    def test_legend_wraps_on_narrow_canvas(self):
        self.assertIn("function acqLegendRows(ctx, legend, availableWidth)", self.template)
        self.assertIn("const legendRows = acqLegendRows(ctx", self.template)
        self.assertIn("padT = 6 + legendRows.length * legendRowH + 6", self.template)
        # Старая однострочная легенда уезжала за правый край канвы.
        self.assertNotIn("let lx = padL; ctx.textAlign = 'left';", self.template)

    def test_grid_and_axis_labels_gained_contrast(self):
        for token in ("gridStrong:", "barSeparator:", "label: 'rgba(206,215,228,.84)'", "label: 'rgba(44,51,62,.86)'"):
            self.assertIn(token, self.template)
        # Прежние блёклые значения убраны.
        self.assertNotIn("label: 'rgba(196,205,218,.62)'", self.template)
        self.assertNotIn("legend: 'rgba(255,255,255,.7)'", self.template)

    def test_x_axis_label_density_follows_measured_width(self):
        self.assertIn("ctx.measureText(String(lb)).width", self.template)
        self.assertNotIn("const every = Math.ceil(n / 14);", self.template)

    def test_stacked_segments_are_visually_separated(self):
        self.assertEqual(self.template.count("adminChartUiColor('barSeparator')"), 2)
        self.assertIn("adminChartUiColor(ratio === 0 ? 'gridStrong' : 'grid')", self.template)
        self.assertIn("adminChartUiColor(g === 0 ? 'gridStrong' : 'grid')", self.template)


class ReferralAntifraudPanelTests(SimpleTestCase):
    """Блок антифрода: одна панель вместо статус-карточек и отдельной формы."""

    def setUp(self):
        self.template = template_source("engine/templates/admin_dashboard.html")
        self.css = Path("engine/static/css/admin_dashboard.css").read_text()
        # Проверки «этого быть не должно» по тексту ограничиваем самой
        # карточкой: в шаблоне на 14 тысяч строк те же слова встречаются
        # в других разделах и дают ложные срабатывания
        card_start = self.template.index("referral-antifraud-card")
        self.antifraud_card = self.template[
            card_start:self.template.index("</section>", card_start)
        ]

    def test_single_panel_replaces_duplicated_status_cards(self):
        self.assertIn('<div class="antifraud-panel">', self.template)
        self.assertIn('class="antifraud-row antifraud-row-state"', self.template)
        self.assertIn('data-antifraud-current="limit"', self.template)
        self.assertIn('data-antifraud-current="window_minutes"', self.template)
        # Дублирующие карточки удалены и в разметке, и в рендере.
        self.assertNotIn("referral-antifraud-stat-icon", self.template)
        self.assertNotIn('"referral-antifraud-stat ', self.template)
        self.assertNotIn("referral-antifraud-fields", self.template)
        self.assertNotIn("referral-antifraud-actions", self.template)
        self.assertNotIn("Сохранить параметры", self.antifraud_card)

    def test_state_control_is_a_real_switch(self):
        self.assertIn('role="switch"', self.template)
        self.assertIn('aria-checked="false"', self.template)
        self.assertIn('class="antifraud-switch-track"', self.template)
        self.assertIn("toggle.setAttribute('aria-checked', String(isEnabled));", self.template)
        for selector in (
            ".antifraud-switch {",
            '.antifraud-switch[aria-checked="true"] .antifraud-switch-thumb',
            ".antifraud-switch:focus-visible",
        ):
            self.assertIn(selector, self.css)

    def test_business_contract_is_unchanged(self):
        # Имена полей, значения action и эндпоинт остаются прежними.
        self.assertIn('name="limit"', self.template)
        self.assertIn('name="window_minutes"', self.template)
        self.assertIn('name="action" value="set"', self.template)
        self.assertIn('name="action" value="enable"', self.template)
        self.assertIn("toggle.value = isEnabled ? 'disable' : 'enable';", self.template)
        self.assertIn("data-referral-antifraud-url=", self.template)
        self.assertIn("formData.set('action', event.submitter?.value || 'set');", self.template)

    def test_enter_in_a_field_still_saves_instead_of_toggling(self):
        # Переключатель стоит визуально первым, поэтому неявную отправку формы
        # держит скрытая кнопка action=set — иначе Enter включал бы автоблок.
        self.assertIn('class="antifraud-implicit-submit"', self.template)
        implicit = self.template.index('class="antifraud-implicit-submit"')
        toggle = self.template.index('id="referral-antifraud-toggle"')
        self.assertLess(implicit, toggle)
        self.assertIn(".antifraud-implicit-submit", self.css)

    def test_panel_matches_neighbouring_settings_table_styling(self):
        for selector in (".antifraud-panel {", ".antifraud-row {", ".antifraud-footer {"):
            self.assertIn(selector, self.css)
        self.assertIn('html[data-admin-theme="light"] .antifraud-panel,', self.css)
        self.assertIn('html[data-admin-theme="light"] .antifraud-row,', self.css)
        # Слот статуса остаётся только под загрузку/ошибку и скрыт, когда пуст.
        self.assertIn(".referral-antifraud-status:empty", self.css)
        self.assertIn("target.innerHTML = '';", self.template)


class AdminSettingsGroupsTests(SimpleTestCase):
    """Вкладки раздела «Система» строятся по захардкоженному в шаблоне списку
    SETTING_GROUPS. Любой ключ из реестра common обязан попасть в группу —
    иначе он молча уезжает в «Прочие» и остаётся без нормального UI."""

    def _grouped_keys(self):
        template = (
            Path(__file__).resolve().parent / "templates" / "admin_dashboard.html"
        ).read_text(encoding="utf-8")
        block = re.search(
            r"const SETTING_GROUPS = \[(.*?)\n        \];", template, re.S
        )
        self.assertIsNotNone(block, "не найден блок SETTING_GROUPS")
        slugs = {
            "tariffs",
            "winback",
            "payment",
            "referral",
            "referral-antifraud",
            "alerts",
            "antiabuse",
            "general",
        }
        return set(re.findall(r"'([a-z_0-9]+)'", block.group(1))) - slugs

    def test_every_runtime_setting_key_is_grouped(self):
        self.assertEqual(set(RUNTIME_SETTING_KEYS) - self._grouped_keys(), set())

    def test_no_unknown_keys_in_groups(self):
        self.assertEqual(self._grouped_keys() - set(RUNTIME_SETTING_KEYS), set())

    def test_mini_app_keys_are_grouped_into_general(self):
        # Ключи Mini App (`webapp_*`) — рабочие настройки бота острова; без
        # группы они уезжали в скрытую карточку «Прочие» и переставали
        # находиться глазами в подвкладке «Общие».
        grouped = self._grouped_keys()
        for key in ("webapp_enabled", "webapp_url", "webapp_button_text"):
            self.assertIn(key, grouped, key)


class AdSpendMultiAccountTests(SimpleTestCase):
    def test_model_has_account_with_composite_unique(self):
        from common.models.db import AdSpend

        assert "account" in AdSpend.__table__.columns
        column = AdSpend.__table__.columns["account"]
        self.assertFalse(column.nullable)
        self.assertEqual(column.server_default.arg, "default")

        unique = [
            c for c in AdSpend.__table__.constraints
            if c.name == "uq_ad_spends_day_channel_account"
        ]
        self.assertEqual(len(unique), 1)
        self.assertEqual(
            sorted(col.name for col in unique[0].columns),
            ["account", "channel", "day"],
        )

    def test_ad_spends_api_and_ui_are_account_aware(self):
        views_src = Path("engine/views.py").read_text()
        # Оба upsert-а конфликтуют по (day, channel, account) — данные разных
        # аккаунтов за один день сосуществуют и суммируются в аналитике.
        self.assertEqual(views_src.count("ON CONFLICT (day, channel, account)"), 2)
        self.assertNotIn("uq_ad_spends_day_channel\n", views_src)

        template = template_source("engine/templates/admin_dashboard.html")
        self.assertIn('id="acq-account-select"', template)
        self.assertIn('id="acq-account-add"', template)
        self.assertIn('id="acq-account-rename"', template)
        self.assertIn("currentAdAccount()", template)

    def test_rename_account_action_guards_against_overlap(self):
        views_src = Path("engine/views.py").read_text()
        # Переименование аккаунта прерывается, если у целевого имени уже есть
        # данные за пересекающиеся (day, channel) — ничего не затирается.
        self.assertIn('action == "rename_account"', views_src)
        self.assertIn("UPDATE ad_spends SET account", views_src)
        rename_block = views_src.split('action == "rename_account"', 1)[1]
        self.assertIn("EXISTS", rename_block.split("UPDATE", 1)[0])


class AdminAnalyticsStage1Tests(SimpleTestCase):
    """Этап 1 плана админки: MRR/churn, здоровье платежей, воронка 100МБ."""

    def test_acquisition_sections_include_mrr_and_payment_health(self):
        from engine.views import ACQ_SECTIONS

        self.assertIn("mrr", ACQ_SECTIONS)
        self.assertIn("payment_health", ACQ_SECTIONS)

    def test_mrr_tariff_months_exclude_short_tariffs(self):
        from engine.views import ACQ_MRR_TARIFF_MONTHS_SQL

        # Короткие тарифы не должны попадать в recognized-MRR.
        self.assertNotIn("oneday", ACQ_MRR_TARIFF_MONTHS_SQL)
        self.assertNotIn("threedays", ACQ_MRR_TARIFF_MONTHS_SQL)
        for tariff, months in (("month", 1), ("threemonths", 3), ("sixmonths", 6), ("year", 12)):
            self.assertIn(f"WHEN '{tariff}' THEN {months}", ACQ_MRR_TARIFF_MONTHS_SQL)

    def test_recurrent_mrr_factors_match_bot_formula(self):
        from engine.views import ACQ_MRR_RECURRENT_FACTORS

        self.assertEqual(ACQ_MRR_RECURRENT_FACTORS["month"], 1.0)
        self.assertEqual(ACQ_MRR_RECURRENT_FACTORS["oneday"], 30.0)
        self.assertAlmostEqual(ACQ_MRR_RECURRENT_FACTORS["year"], 1.0 / 12.0)

    def test_admin_template_has_new_subtabs(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('data-subtab="acq-mrr"', template)
        self.assertIn('data-subtab="acq-payhealth"', template)
        self.assertIn("'acq-mrr': loadMrr", template)
        self.assertIn("'acq-payhealth': loadPayHealth", template)
        # Топ клиентов по платежам удалён из вкладки «Платежи»: результат
        # поиска платежа показывается над сворачиваемой историей платежей.
        self.assertNotIn("data-top-payments-url", template)
        self.assertNotIn("loadTopPayments", template)
        self.assertNotIn("Топ клиентов по платежам", template)
        history_details = 'id="payments-history-details"'
        self.assertIn(history_details, template)
        # Результат поиска платежа стоит в разметке выше истории платежей.
        self.assertLess(
            template.index('id="payment-info-result"'),
            template.index(history_details),
        )
        # После успешного поиска история сворачивается, чтобы карточка была на виду.
        self.assertIn("historyDetails.open = false", template)

    def test_funnel_includes_traffic_thresholds(self):
        import inspect

        from engine.views import _acq_funnel

        src = inspect.getsource(_acq_funnel)
        self.assertIn("traffic_threshold_reached", src)
        self.assertIn("mb100", src)
        # Собственной вкладки у воронки больше нет («Путь когорты»), но
        # секция funnel живёт в API — её колонки использует «Реклама».
        self.assertNotIn('data-subtab="acq-funnel"', template_source("engine/templates/admin_dashboard.html"))


class SetupWizardTests(SimpleTestCase):
    """Мастер подключения в кабинете и Mini App."""

    def test_wizard_resets_to_start_after_finish(self):
        template = template_source("engine/templates/dashboard.html")

        # Повторный вход в мастер после «Завершить» (шаг done) начинается с
        # первого шага — без сброса клиент видел бы последний экран.
        self.assertIn("newSetupStep === 'done'", template)
        showtab_src = template.split("function showTab(tabId)", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("newSetupStep = 'start'", showtab_src)
        self.assertIn("renderNewSetupWizard()", showtab_src)

    def test_wizard_honors_apple_recommended_app(self):
        template = template_source("engine/templates/dashboard.html")

        # Настройка apple_recommended_app из админки доезжает до кабинета:
        # мастер и плоский флоу переключаются на Incy с шифрованной ссылкой.
        self.assertIn("const appleRecommendedApp = '{{ apple_recommended_app }}'", template)
        self.assertIn("const appleSubscriptionUrl = '{{ apple_subscription_url }}'", template)
        self.assertIn("appleRecommendedApp === 'incy'", template)
        self.assertIn("applyAppleRecommendedAppToInstallData()", template)
        self.assertIn("Скачать Incy из App Store", template)

    def test_dashboard_view_passes_apple_context(self):
        import inspect

        from engine.views import build_apple_subscription_link, dashboard

        src = inspect.getsource(dashboard)
        self.assertIn("build_apple_subscription_link", src)
        self.assertIn('"apple_recommended_app": apple_recommended_app', src)
        # INCY недоступен (нет node и т.п.) — молча откатываемся на Happ.
        self.assertIn(
            "IncyEncoderError",
            inspect.getsource(build_apple_subscription_link),
        )

    def test_install_links_use_base_subscription_url(self):
        """Ссылки установки шифруют базовый subscription_url без /custom-json.

        Основная подписка в панели теперь отдаёт тот же конфиг, что раньше
        отдавался по /custom-json, поэтому суффикс убран (как в боте).
        """
        import inspect

        from engine.views import build_apple_subscription_link, dashboard

        self.assertNotIn(
            '"/custom-json"', inspect.getsource(build_apple_subscription_link)
        )
        self.assertNotIn('"/custom-json"', inspect.getsource(dashboard))

    def test_build_apple_subscription_link_encrypts_plain_url(self):
        from unittest import mock

        from engine import views as _views

        with (
            mock.patch.object(
                _views, "apple_recommended_app_from_db", return_value="incy"
            ),
            mock.patch.object(
                _views, "encrypt_happ_url1", side_effect=lambda url: f"happ:{url}"
            ),
            mock.patch.object(
                _views, "encrypt_incy_url", side_effect=lambda url: f"incy:{url}"
            ),
        ):
            app, link = _views.build_apple_subscription_link(
                None, "https://sub.example/abc"
            )
        self.assertEqual(app, "incy")
        self.assertEqual(link, "incy:https://sub.example/abc")

        with (
            mock.patch.object(
                _views, "apple_recommended_app_from_db", return_value="happ"
            ),
            mock.patch.object(
                _views, "encrypt_happ_url1", side_effect=lambda url: f"happ:{url}"
            ),
        ):
            app, link = _views.build_apple_subscription_link(
                None, "https://sub.example/abc"
            )
        self.assertEqual(app, "happ")
        self.assertEqual(link, "happ:https://sub.example/abc")

    def test_apple_recommended_app_from_db_normalizes_values(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from engine.views import apple_recommended_app_from_db

        def session_with(value):
            session = MagicMock()
            session.get.return_value = (
                SimpleNamespace(value=value) if value is not None else None
            )
            return session

        self.assertEqual(apple_recommended_app_from_db(session_with(None)), "happ")
        self.assertEqual(apple_recommended_app_from_db(session_with("incy")), "incy")
        self.assertEqual(apple_recommended_app_from_db(session_with(" INCY ")), "incy")
        self.assertEqual(apple_recommended_app_from_db(session_with("happ")), "happ")
        self.assertEqual(apple_recommended_app_from_db(session_with("garbage")), "happ")


class AdminAdsCsvUxTests(SimpleTestCase):
    """UX импорта рекламного CSV: стилизованная кнопка файла и явный выбор
    аккаунта прямо в форме загрузки (раньше данные молча писались в аккаунт,
    выбранный в другой карточке, — легко было залить в default)."""

    def test_csv_file_input_is_styled_button(self):
        template = template_source("engine/templates/admin_dashboard.html")

        # Нативный инпут скрыт внутри стилизованной кнопки-label
        self.assertIn('id="acq-csv-file-label"', template)
        self.assertIn('id="acq-csv-filename"', template)
        self.assertIn('id="acq-csv-file-input"', template)
        # Имя выбранного файла показывается на кнопке
        self.assertIn("nameEl.textContent = file ? file.name", template)
        # Скрытый инпут больше не полагается на браузерный required
        self.assertIn("сначала выберите CSV-файл", template)

    def test_csv_form_has_explicit_account_selector(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('id="acq-csv-account"', template)
        # Селекторы синхронизированы в обе стороны
        self.assertIn("csvSel.value = currentAdAccount()", template)
        # Статус импорта называет аккаунт
        self.assertIn("импортировано в «${account}»", template)
        self.assertIn("загрузка в аккаунт «${account}»", template)


class AdminAcqDatePickerTests(SimpleTestCase):
    """Календарь периода в «Привлечении»: кастомный пикер как в «Аналитике»
    вместо нативных input[type=date], плюс запрет «конец раньше начала»."""

    def test_acquisition_periods_use_custom_date_picker(self):
        template = template_source("engine/templates/admin_dashboard.html")

        # Нативных date-инпутов в «Привлечении» нет — только hidden внутри
        # date-field («Выручка», «Окончания»).
        self.assertNotIn('type="date" id="acq-', template)
        self.assertIn('<input type="hidden" id="acq-revenue-start">', template)
        self.assertIn('<input type="hidden" id="acq-revenue-end">', template)
        self.assertIn('<input type="hidden" id="acq-expiry-start">', template)
        self.assertIn('<input type="hidden" id="acq-expiry-end">', template)
        # Программная установка дат идёт через setDateFieldValue (лейблы)
        self.assertIn("function setAcqDateField", template)
        self.assertIn("setAcqDateField('acq-revenue-start'", template)
        self.assertIn("setAcqDateField('acq-expiry-start'", template)

    def test_date_range_cannot_invert(self):
        template = template_source("engine/templates/admin_dashboard.html")

        # Дни вне диапазона выключены: у конца min — это начало, у начала
        # max — это конец. Подключено и в «Привлечении», и в «Аналитике».
        self.assertIn("function resolveRangeBound", template)
        self.assertIn("range-disabled", template)
        self.assertIn('data-range-min-from="#acq-revenue-start"', template)
        self.assertIn('data-range-max-from="#acq-revenue-end"', template)
        self.assertIn('data-range-min-from="#acq-expiry-start"', template)
        self.assertIn('data-range-max-from="#acq-expiry-end"', template)
        self.assertIn('data-range-min-from=\'[name="start"]\'', template)
        self.assertIn('data-range-min-from=\'[name="cohort_start"]\'', template)


class CabinetPaymentsHistoryTests(SimpleTestCase):
    """История платежей в кабинете: отмена автопродления убрана с видных мест
    и живёт маленькой ссылкой внутри листа истории."""

    def _build_session(self):
        from engine.views import YkPayment as YkModel

        yk_rows = [
            SimpleNamespace(
                created_at=datetime(2026, 8, 7, 12, 0),
                amount=299,
                currency="RUB",
                subscription_period="month",
                is_trial_promotion=False,
                status="succeeded",
            )
        ]
        wata_rows = [
            (
                SimpleNamespace(
                    payment_time=datetime(2026, 8, 8, 10, 0),
                    amount=1799,
                    currency="RUB",
                    transaction_status="Paid",
                    order_description="год",
                ),
                SimpleNamespace(tariff_id="year"),
            )
        ]

        class FakeQuery:
            def __init__(self, rows):
                self.rows = rows

            def filter(self, *args, **kwargs):
                return self

            def join(self, *args, **kwargs):
                return self

            def order_by(self, *args, **kwargs):
                return self

            def limit(self, *args, **kwargs):
                return self

            def all(self):
                return self.rows

        class FakeSession:
            def query(self, *models):
                return FakeQuery(yk_rows if models[0] is YkModel else wata_rows)

            def close(self):
                pass

        return FakeSession()

    def test_returns_merged_history_newest_first(self):
        from engine.views import cabinet_payments_history

        request = RequestFactory().get("/cabinet/api/payments/")
        request.user = SimpleNamespace(is_authenticated=True, id=42)

        with mock.patch(
            "engine.views.session_factory", return_value=self._build_session()
        ):
            response = cabinet_payments_history(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "ok")
        # WATA-год (08.08) свежее ЮКассы-месяца (07.08)
        self.assertEqual(
            [(p["tariff"], p["amount"]) for p in payload["payments"]],
            [("1 год", 1799), ("1 месяц", 299)],
        )
        self.assertNotIn("date_sort", payload["payments"][0])

    def test_requires_authentication_and_get(self):
        from engine.views import cabinet_payments_history

        anonymous = RequestFactory().get("/cabinet/api/payments/")
        anonymous.user = SimpleNamespace(is_authenticated=False, id=None)
        self.assertEqual(cabinet_payments_history(anonymous).status_code, 403)

        post = RequestFactory().post("/cabinet/api/payments/")
        post.user = SimpleNamespace(is_authenticated=True, id=42)
        self.assertEqual(cabinet_payments_history(post).status_code, 403)

    def test_cancel_autopay_is_buried_in_payments_sheet(self):
        template = template_source("engine/templates/dashboard.html")

        # Главный экран mini app: вместо «Автопродление» — «Платежи и подписка»
        # (редизайн b95ea64: строки-действия стали cabinet-action).
        self.assertNotIn('tg-mini-action-label">Автопродление', template)
        self.assertNotIn(">Автопродление</span>", template)
        # Мобильный кабинет: история платежей — строка на экране «Оплата».
        self.assertIn('class="cm-row" onclick="openPaymentsHistorySheet()"', template)
        self.assertIn("<span>История платежей</span>", template)
        # Профиль: заметной карточки отмены больше нет
        self.assertNotIn(
            '<div class="text-white font-black">Отключить автопродление</div>',
            template,
        )
        self.assertIn("Все ваши оплаты подписки", template)
        # Отмена доступна маленькой ссылкой внутри листа истории платежей
        self.assertIn('id="payments-history-sheet"', template)
        self.assertIn(
            "closePaymentsHistorySheet(); setTimeout(onAutopayButtonClick, 320);",
            template,
        )
        # FAQ указывает новый путь к отмене
        self.assertIn("Историю платежей", template)


class CabinetCancelAutopayEventTests(SimpleTestCase):
    """Отмена автоплатежа из кабинета обязана писать то же аналитическое
    событие, что и бот (confirm_cancel_autopay_clicked) — иначе отмены через
    сайт невидимы для графика churn, ежедневного отчёта и таймлайна."""

    def test_cabinet_cancel_writes_cancel_event(self):
        import inspect

        from engine.views import cancel_autopay

        src = inspect.getsource(cancel_autopay)
        self.assertIn("ConfirmCancelAutopayClicked", src)
        self.assertIn("add_event_log", src)
        # Событие пишется в той же транзакции, что удаление рекуррента
        self.assertLess(
            src.index("add_event_log"), src.index("session.commit()")
        )


class AdminRecurrentDynamicsTests(SimpleTestCase):
    """Активная рекуррентная база: фейл автосписания после последнего успеха
    выбивает пользователя из базы (мёртвая карта), отзыв разрешения в банке
    считается отменой. Раньше база завышалась: человек с умершей картой висел
    «активным» до 12 месяцев после последнего успешного списания."""

    @staticmethod
    def _dynamics(rows, months=3, today=date(2026, 8, 8)):
        from engine.views import _acq_recurrent_dynamics

        return _acq_recurrent_dynamics(rows, months, today=today)

    @staticmethod
    def _row(user_id, month, event_type, reason=None, cnt=1):
        return {
            "user_id": user_id,
            "m": month,
            "event_type": event_type,
            "reason": reason,
            "cnt": cnt,
        }

    def test_failure_after_last_success_removes_user_from_base(self):
        rows = [
            self._row(1, date(2026, 6, 1), "payment_regular_autopay_success"),
            self._row(1, date(2026, 7, 1), "payment_regular_autopay_failure"),
        ]

        dynamics = {d["month"]: d for d in self._dynamics(rows)}

        # В июне жив, с июля (фейл без последующего успеха) — выбыл.
        self.assertEqual(dynamics["2026-06-01"]["active_recurrents"], 1)
        self.assertEqual(dynamics["2026-07-01"]["active_recurrents"], 0)
        self.assertEqual(dynamics["2026-08-01"]["active_recurrents"], 0)
        self.assertEqual(dynamics["2026-07-01"]["autopay_failures"], 1)

    def test_failure_with_successful_retry_same_month_keeps_user_alive(self):
        # «Не хватило денег → пополнил → успешный ретрай» внутри месяца.
        rows = [
            self._row(1, date(2026, 7, 1), "payment_regular_autopay_failure"),
            self._row(1, date(2026, 7, 1), "payment_regular_autopay_success"),
        ]

        dynamics = {d["month"]: d for d in self._dynamics(rows)}

        self.assertEqual(dynamics["2026-07-01"]["active_recurrents"], 1)
        self.assertEqual(dynamics["2026-08-01"]["active_recurrents"], 1)

    def test_success_after_failure_revives_user(self):
        rows = [
            self._row(1, date(2026, 6, 1), "payment_regular_autopay_success"),
            self._row(1, date(2026, 7, 1), "payment_regular_autopay_failure"),
            self._row(1, date(2026, 8, 1), "payment_regular_autopay_success"),
        ]

        dynamics = {d["month"]: d for d in self._dynamics(rows)}

        self.assertEqual(dynamics["2026-07-01"]["active_recurrents"], 0)
        self.assertEqual(dynamics["2026-08-01"]["active_recurrents"], 1)

    def test_permission_revoked_counts_as_cancel_not_failure(self):
        from engine.views import ACQ_AUTOPAY_REVOKED_REASONS

        # Оба кода: из документации ЮКассы и реальный СБП-код из прода.
        self.assertIn("permission_revoked", ACQ_AUTOPAY_REVOKED_REASONS)
        self.assertIn("recurring_permission_revoked", ACQ_AUTOPAY_REVOKED_REASONS)

        rows = [
            self._row(1, date(2026, 6, 1), "payment_regular_autopay_success"),
            self._row(
                1,
                date(2026, 7, 1),
                "payment_regular_autopay_failure",
                reason="recurring_permission_revoked",
            ),
        ]

        dynamics = {d["month"]: d for d in self._dynamics(rows)}

        july = dynamics["2026-07-01"]
        self.assertEqual(july["active_recurrents"], 0)
        self.assertEqual(july["cancels"], 1)  # отмена, а не фейл
        self.assertEqual(july["autopay_failures"], 0)
        # Отзыв входит в churn: 1 отменившийся ÷ 1 активный на конец июня.
        self.assertEqual(july["churn_pct"], 100.0)

    def test_cancel_click_still_counts_as_cancel(self):
        # Старое поведение кнопки «отменить автоплатёж» сохранено.
        rows = [
            self._row(1, date(2026, 6, 1), "payment_regular_autopay_success"),
            self._row(1, date(2026, 7, 1), "confirm_cancel_autopay_clicked"),
        ]

        dynamics = {d["month"]: d for d in self._dynamics(rows)}

        self.assertEqual(dynamics["2026-07-01"]["active_recurrents"], 0)
        self.assertEqual(dynamics["2026-07-01"]["cancels"], 1)

    def test_events_query_reads_reason_from_payload(self):
        import inspect

        from engine.views import _acq_mrr

        src = inspect.getsource(_acq_mrr)
        self.assertIn("event_payload->>'reason'", src)

    def test_payment_card_exposes_autopay_type_and_cancellation_reason(self):
        import inspect

        from engine.views import admin_payment_info_payload

        src = inspect.getsource(admin_payment_info_payload)
        self.assertIn("Автосписание", src)
        self.assertIn("cancellation_reason", src)
        template = template_source("engine/templates/admin_dashboard.html")
        self.assertIn("Причина отмены", template)


class AdminClientWorkspaceTests(SimpleTestCase):
    """Компактная карточка клиента и фактический трафик из RWMS."""

    def test_rwms_traffic_payload_uses_real_panel_counters(self):
        from engine import views

        rwms = mock.Mock()
        rwms.get_user_by_username.return_value = SimpleNamespace(
            used_traffic_bytes=12_345_678,
            lifetime_used_traffic_bytes=98_765_432,
        )

        payload = views.admin_rwms_traffic_payload("594514115", client=rwms)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["used_traffic_bytes"], 12_345_678)
        self.assertEqual(payload["lifetime_used_traffic_bytes"], 98_765_432)
        rwms.get_user_by_username.assert_called_once_with("594514115")
        # SimpleNamespace без HasField — first_connected деградирует в None.
        self.assertIsNone(payload["first_connected"])

    def test_rwms_traffic_payload_exposes_first_connected_date(self):
        """Дата первого подключения тянется из RWMS (proto first_connected)
        и показывается в карточке клиента и быстрой карточке «Трафика нод»."""
        from engine import views

        class FakeRwmsUser:
            used_traffic_bytes = 1
            lifetime_used_traffic_bytes = 2
            first_connected = SimpleNamespace(
                ToDatetime=lambda: datetime(2026, 5, 1, 12, 30)
            )

            def HasField(self, name):
                return name == "first_connected"

        rwms = mock.Mock()
        rwms.get_user_by_username.return_value = FakeRwmsUser()

        payload = views.admin_rwms_traffic_payload("594514115", client=rwms)

        # Метка в МСК: 12:30 UTC → 15:30 (админка работает по Москве)
        self.assertEqual(payload["first_connected"], "01.05.2026 15:30")

    def test_node_traffic_user_popup_opens_quick_card(self):
        """Клик по пользователю в «Трафике нод» открывает модалку с данными
        подписки (user-payments + user-traffic) и кнопкой перехода в полную
        карточку клиента с управлением подпиской."""
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('data-node-traffic-user="${escapeHtml(user.username)}"', template)
        self.assertIn('id="node-traffic-user-modal"', template)
        self.assertIn("async function openNodeTrafficUserModal(username)", template)
        self.assertIn("function openClientCardFromNodeTraffic(username)", template)
        self.assertIn("data-nt-user-open-card", template)
        self.assertIn("showAdminTab('user-payments');", template)
        # Первое подключение — и в модалке, и в сводке карточки клиента.
        self.assertIn("data-client-first-connected-value", template)
        self.assertIn("traffic.first_connected || 'Не подключался'", template)

    def test_rwms_traffic_payload_degrades_without_breaking_client_card(self):
        from engine import views

        rwms = mock.Mock()
        rwms.get_user_by_username.side_effect = RuntimeError("rwms unavailable")

        with self.assertLogs(level="ERROR"):
            payload = views.admin_rwms_traffic_payload("594514115", client=rwms)

        self.assertEqual(
            payload,
            {
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
            },
        )

    def test_rwms_traffic_api_is_read_only_and_routed(self):
        import inspect
        from django.urls import reverse
        from engine import views

        self.assertEqual(
            reverse("support_admin_api_user_traffic"),
            "/support-admin/api/user-traffic/",
        )
        src = inspect.getsource(views.support_admin_api_user_traffic)
        helper_src = inspect.getsource(views.admin_rwms_traffic_payload)
        self.assertIn("get_user_by_username", helper_src)
        self.assertNotIn("update_user", helper_src)
        self.assertNotIn("delete", helper_src.lower())
        self.assertIn("require_support_admin", src)

    def test_client_template_matches_compact_overview_concept(self):
        template = template_source("engine/templates/admin_dashboard.html")
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn("data-user-traffic-url", template)
        self.assertIn('data-client-subtab="overview"', template)
        self.assertIn("client-summary-card", template)
        self.assertIn("data-client-traffic-value", template)
        self.assertIn("loadClientTraffic(clientCardState.q)", template)
        self.assertIn("Действия с клиентом", template)
        self.assertIn("Подписка и оплата", template)
        self.assertIn("Ограничения доступа", template)
        self.assertIn("автоплатёж отключится, рекуррент удалится", template)
        self.assertNotIn("function clientSubscriptionManageHtml", template)
        self.assertIn(".client-actions-grid", css)
        self.assertIn(".client-danger-panel", css)

    def test_client_overview_actions_use_registry_layout(self):
        """Действия с клиентом — один реестр: две группы, у строки справа две
        колонки одинаковой ширины, поле дней/часов прижато к своей кнопке."""
        template = template_source("engine/templates/admin_dashboard.html")
        css = Path("engine/static/css/admin-concept-customers.css").read_text()

        overview = template[template.index("function clientOverviewSectionHtml"):template.index("function formatClientTrafficBytes")]
        self.assertIn('class="card client-actions-grid client-action-registry"', overview)
        self.assertEqual(overview.count('class="client-action-pair"'), 2)
        self.assertEqual(overview.count("client-action-button is-primary"), 7)
        self.assertIn('data-client-sub-action="remove_traffic_limit">Снять лимит', overview)
        self.assertIn('data-client-sub-action="apply_trial_limit">Применить лимит', overview)
        self.assertIn('class="client-action-help"', overview)
        self.assertIn("ручной лимит владельца в панели не трогается", overview)
        for action in ("extend", "set_trial_hour", "stop_autopay", "apply_trial_limit", "remove_traffic_limit", "temp_ban", "block_account", "unblock_account"):
            self.assertIn(f'data-client-sub-action="{action}"', overview)
        for field in ('id="client-extend-days"', 'id="client-ban-hours"', 'data-client-section-link="referrals"'):
            self.assertIn(field, overview)
        self.assertIn("#panel-user-payments .client-action-registry .client-action-controls { display: grid; grid-template-columns: var(--client-action-btn) var(--client-action-btn);", css)
        self.assertIn(".client-action-button.is-primary { grid-column: 2; }", css)
        self.assertIn(".client-action-pair { grid-column: 1 / -1;", css)
        self.assertIn("@import url('./admin-concept-customers.css?v=11');", Path("engine/static/css/admin-concept.css").read_text())


class AdminMoscowTimeTests(SimpleTestCase):
    """Админка работает по московскому времени (UTC+3): метки времени
    сдвигаются при показе, бакеты аналитики режутся по московским суткам,
    границы периодов конвертируются в UTC-время БД вычитанием смещения."""

    def test_admin_labels_and_buckets_use_moscow_time(self):
        import inspect

        from engine import views

        from datetime import timedelta

        self.assertEqual(views.ADMIN_TZ_OFFSET, timedelta(hours=3))
        # 24.08 22:30 UTC — это уже 25.08 01:30 по Москве.
        self.assertEqual(
            views.admin_date_label(datetime(2026, 8, 24, 22, 30)),
            "25.08.2026 01:30",
        )
        self.assertEqual(
            views.admin_date_label(datetime(2026, 8, 24, 22, 30), with_time=False),
            "25.08.2026",
        )

        # SQL-бакеты: naive-UTC переводится в Europe/Moscow ДО date_trunc.
        sql = str(
            views.admin_stats_bucket_sql(views.EventLog.timestamp, "day").compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        self.assertIn("Europe/Moscow", sql)
        self.assertIn("date_trunc", sql)

        # Границы периодов аналитики — московские сутки (−3ч в UTC БД).
        for func_obj in (
            views.build_admin_interval_stats,
            views.build_admin_sales_series,
            views.support_admin_api_stats_source_users,
        ):
            self.assertIn("- ADMIN_TZ_OFFSET", inspect.getsource(func_obj))
        # «Сегодня» по умолчанию — московская дата.
        self.assertIn("admin_msk_today()", inspect.getsource(views.support_admin_api_stats))

        # Явных подписей UTC в админке не осталось (кроме комментариев кода).
        template = template_source("engine/templates/admin_dashboard.html")
        self.assertNotIn("Сегодня (UTC)", template)
        self.assertNotIn("} UTC<", template)


class AdminPaymentJournalTests(SimpleTestCase):
    """Единый современный журнал операций для двух списков платежей."""

    def test_client_and_global_payment_lists_use_expandable_journal(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("function paymentJournalRowsHtml", template)
        self.assertIn("function paymentStatusDescriptor", template)
        self.assertIn("function paymentAmountLabel", template)
        self.assertIn("data-payment-journal-toggle", template)
        self.assertIn("payment-journal-details", template)
        self.assertIn("paymentJournalRowsHtml(history, {context: 'client'})", template)
        self.assertIn(
            "paymentJournalRowsHtml(payments, {showUser: true, context: 'all'})",
            template,
        )
        self.assertIn("details.hidden = !shouldOpen", template)
        self.assertNotIn('<span class="payments-pages"', template)
        # Карточка клиента больше не держит собственную упрощённую разметку журнала.
        self.assertNotIn('class="payment-journal-details client-detail-notice"', template)
        self.assertNotIn('<b class="payment-journal-amount">', template)

    def test_admin_selects_are_enhanced_by_admin_select_script(self):
        """Нативные выпадающие списки в админке заменяет стилизованный
        компонент; сам <select> остаётся в DOM для форм, слушателей и тестов."""
        template = template_source("engine/templates/admin_dashboard.html")
        script = Path("engine/static/js/admin-select.js").read_text()
        # Прокси-селект кастомного пикера сегмента не должен оборачиваться
        # в ui-select — иначе рядом с «Выберите сегмент» появляется второй
        # «Сегмент…» со своим меню (баг 10.09.2026)
        self.assertIn('<select id="broadcast-segment" tabindex="-1" aria-hidden="true" data-native-select>', template)
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn("{% static 'js/admin-select.js' %}", template)
        self.assertIn("(pointer: coarse)", script)
        self.assertIn("select.dispatchEvent(new Event('change', {bubbles: true}))", script)
        self.assertIn("select.dispatchEvent(new Event('input', {bubbles: true}))", script)
        self.assertIn("new MutationObserver", script)
        self.assertIn("data-native-select", script)
        self.assertIn("[data-date-popover]", script)
        self.assertIn("window.adminSelect = {sync, enhance};", script)
        for selector in (".ui-select {", ".ui-select > select.ui-select-native {", ".ui-select-menu {", '.ui-select-option[aria-selected="true"]', 'html[data-admin-theme="light"] .ui-select-menu {'):
            self.assertIn(selector, css)

    def test_payment_journal_details_offer_copy_id_button(self):
        template = template_source("engine/templates/admin_dashboard.html")
        css = Path("engine/static/css/admin_dashboard.css").read_text()
        client_css = Path("engine/static/css/admin-concept-client-details.css").read_text()

        self.assertIn('data-payment-copy-id="${escapeHtml(payment.id)}"', template)
        self.assertIn('aria-label="Скопировать ID платежа"', template)
        self.assertIn("async function copyPaymentId(button)", template)
        self.assertIn("event.target.closest('[data-payment-copy-id]')", template)
        self.assertIn("showAdminToast('ID платежа скопирован', 'success')", template)
        self.assertIn("document.execCommand('copy')", template)
        for selector in (
            ".payment-journal-copy {",
            ".payment-journal-copy.is-copied",
            'html[data-admin-theme="light"] .payment-journal-copy {',
        ):
            self.assertIn(selector, css)
        self.assertIn("#panel-user-payments .client-payment-panel .payment-journal-columns {", client_css)
        self.assertIn("#panel-user-payments .client-payment-panel .payment-journal-chevron", client_css)
        self.assertNotIn(".client-payment-panel .payment-journal-row { grid-template-columns: 1fr 1fr; }", client_css)

    def test_payment_journal_has_desktop_mobile_and_light_styles(self):
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        for selector in (
            ".payment-journal-columns.has-user",
            ".payment-journal-status.is-success",
            ".payment-journal-status.is-pending",
            ".payment-journal-status.is-error",
            ".payment-journal-details:not([hidden])",
            'html[data-admin-theme="light"] .payment-journal',
        ):
            self.assertIn(selector, css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto 18px;", css)

    def test_global_payment_payload_exposes_provider_and_time(self):
        import inspect

        from engine import views

        source = inspect.getsource(views.support_admin_api_payments)
        self.assertIn('"system": "YooKassa"', source)
        self.assertIn('"system": "Wata"', source)
        self.assertNotIn("with_time=False", source)


class AdminStage3Tests(SimpleTestCase):
    """Этап 3 плана админки: аудит, таймлайн, diff RWMS, сообщения."""

    def test_new_endpoints_are_routed(self):
        from django.urls import reverse

        self.assertTrue(reverse("support_admin_api_audit_log"))
        self.assertTrue(reverse("support_admin_api_user_timeline"))
        self.assertTrue(reverse("support_admin_api_rwms_sync"))
        self.assertTrue(reverse("support_admin_api_direct_message"))

    def test_mutating_admin_endpoints_write_audit(self):
        import inspect

        from engine import views

        for func in (
            views.support_admin_api_subscription_manage,
            views.support_admin_api_referral_block,
            views.support_admin_api_runtime_settings,
        ):
            self.assertIn("admin_audit_write", inspect.getsource(func), func.__name__)

    def test_rwms_sync_never_deletes_or_recreates(self):
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_rwms_sync)
        # Только update: панель не пересоздаётся и не удаляется.
        self.assertIn("update_user", src)
        self.assertNotIn("add_user", src)
        self.assertNotIn("create_user", src)
        self.assertNotIn("delete", src.lower())

    def test_rwms_sync_does_not_shift_panel_time_by_local_timezone(self):
        """Регресс-гард ложного рассинхрона на 3 часа.

        rwms_expire_at возвращает naive UTC; вызов astimezone() на naive
        datetime трактует его как ЛОКАЛЬНОЕ время сервера (МСК) и сдвигает
        на -3 часа. Из-за этого вкладка «Синхронизация» показывала всем
        клиентам ложный рассинхрон, а «Панель → БД» портила users.expire_at.
        """
        import inspect

        from engine import views

        for func in (
            views.support_admin_api_rwms_sync,
            views.admin_rwms_diff,
        ):
            self.assertNotIn(
                "astimezone(timezone", inspect.getsource(func), func.__name__
            )
        # rwms_expire_at обязан отдавать naive datetime (UTC из protobuf).
        self.assertIn(
            "replace(tzinfo=None)", inspect.getsource(views.rwms_expire_at)
        )

    def test_client_card_has_new_subtabs(self):
        template = template_source("engine/templates/admin_dashboard.html")

        for subtab in ("timeline", "rwmssync", "message"):
            self.assertIn(f'data-client-subtab="{subtab}"', template)
        self.assertIn('data-subtab="sys-audit"', template)
        self.assertIn('data-subtab="sys-sync"', template)
        self.assertIn("data-audit-log-url", template)
        self.assertIn("data-direct-message-url", template)

    def test_audit_csv_protects_from_formula_injection(self):
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_audit_log)
        self.assertIn('("=", "+", "-", "@")', src)


class AdminStage4Tests(SimpleTestCase):
    """Этап 4: сегменты, рассылки, массовые операции."""

    def test_broadcast_title_and_segment_controls_align(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn(
            ".broadcast-primary-fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(250px, .88fr); gap: 12px; align-items: start; }",
            template,
        )
        self.assertIn("#broadcast-segment-hint:empty { display: none; }", template)

    def test_zero_broadcast_funnel_metrics_do_not_repeat_zero_percent(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("claims > 0 && claimRate !== null", template)
        self.assertIn("buyers > 0 && buyerRate !== null", template)
        self.assertIn("% от доставленных</small>", template)
        self.assertIn("% от забравших</small>", template)
        self.assertNotIn("<em>${claimRate}%</em>", template)
        self.assertNotIn("<em>${buyerRate}%</em>", template)

    def test_segments_registry_is_consistent(self):
        from common.models.segments import (
            ADMIN_SEGMENTS,
            segment_count_sql,
            segment_user_ids_sql,
            segment_where_sql,
        )

        for key in ADMIN_SEGMENTS:
            where = segment_where_sql(key)
            # Рассылки идут ботами и не должны трогать заблокированных.
            self.assertIn("telegram_id IS NOT NULL", where)
            self.assertIn("user_blocks", where)
            self.assertIn("FROM users u", segment_count_sql(key))
            self.assertIn("u.telegram_id", segment_user_ids_sql(key))

        with self.assertRaises(ValueError):
            segment_where_sql("nope")

    def test_segments_endpoint_survives_single_segment_failure(self):
        """Один медленный/сломанный сегмент не должен ронять весь пикер
        получателей: у него count=null, остальные сегменты живут."""
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_segments)
        self.assertIn("SET LOCAL statement_timeout", src)
        self.assertIn("SEGMENT_COUNT_TIMEOUT_MS", src)
        self.assertIn("db_session.rollback()", src)
        self.assertIn("count = None", src)
        self.assertIn("logging.exception", src)
        # Быстрый путь: один проход вместо 18 запросов, с откатом на
        # пер-сегментный подсчёт при любой ошибке.
        self.assertIn("segments_counts_sql", src)
        self.assertIn("SEGMENTS_FAST_COUNT_TIMEOUT_MS", src)

        template = template_source("engine/templates/admin_dashboard.html")
        # Фронт показывает «—» вместо нуля, если охват сегмента не посчитался.
        self.assertIn("function broadcastSegmentCountLabel", template)
        self.assertIn("broadcastSegmentCountLabel(segment)", template)

    def test_broadcasts_can_be_archived(self):
        """Архив рассылок: тестовые прогоны убираются из основного списка.
        Архивировать running нельзя; боты архив не видят и не учитывают."""
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_broadcasts)
        self.assertIn('request.GET.get("archived") == "1"', src)
        self.assertIn("Broadcast.archived_at.is_(None)", src)
        self.assertIn("Broadcast.archived_at.isnot(None)", src)
        self.assertIn('action in ("archive", "unarchive")', src)
        self.assertIn("Сначала остановите рассылку", src)
        self.assertIn('f"broadcast_{action}"', src)

        from common.models.db import Broadcast

        self.assertTrue(hasattr(Broadcast, "archived_at"))

        payload_src = inspect.getsource(views.admin_broadcast_payload)
        self.assertIn('"archived": bool(broadcast.archived_at)', payload_src)

        template = template_source("engine/templates/admin_dashboard.html")
        self.assertIn('id="broadcasts-archive-toggle"', template)
        self.assertIn("data-broadcast-archive=", template)
        self.assertIn("data-broadcast-unarchive=", template)
        self.assertIn("broadcastsArchiveView", template)
        self.assertIn("?archived=1", template)
        self.assertIn("Архив пуст.", template)

    def test_broadcast_history_polls_without_flicker(self):
        """Автообновление истории рассылок (раз в 5с при running) не должно
        подменять список спиннером — страница «моргала» на каждом тике."""
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("async function loadBroadcastsList(background = false)", template)
        self.assertIn("setTimeout(() => loadBroadcastsList(true), 5000)", template)
        self.assertIn("if (!background) {", template)
        # Ручное обновление — через стрелку, иначе event станет background.
        self.assertIn(
            "addEventListener('click', () => loadBroadcastsList())", template
        )
        self.assertNotIn("setTimeout(loadBroadcastsList, 5000)", template)

    def test_disable_access_is_full_account_block_like_bot_block_user(self):
        """«Отключить доступ» = полная блокировка, как /block-user в боте:
        user_blocks (бот отвечает только «аккаунт заблокирован»), блок
        рефералки, автоплатёж выключен и рекуррент удалён, RWMS → DISABLED.
        Обратное действие — как /unblock-user (RWMS ACTIVE только при живом
        сроке, автоплатёж остаётся выключенным)."""
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_subscription_manage)
        self.assertIn('if action in ("block_account", "disable_subscription"):', src)
        self.assertIn('if action == "unblock_account":', src)
        self.assertIn("admin_block_account(", src)
        self.assertIn("admin_unblock_account(", src)

        block_src = inspect.getsource(views.admin_block_account)
        for needle in (
            "UserBlock(user_id=user.id, reason=reason)",
            "ReferralProgramBlock(user_id=user.id, reason=reason)",
            "user.autopay_allow = False",
            "YkRecurrentPayment.user_id == user.id",
            "proto.UserStatus.DISABLED",
            '"account_block"',
        ):
            self.assertIn(needle, block_src)
        unblock_src = inspect.getsource(views.admin_unblock_account)
        self.assertIn("UserBlock.user_id == user.id", unblock_src)
        self.assertIn("sa_delete(ReferralProgramBlock)", unblock_src)
        self.assertIn("proto.UserStatus.ACTIVE", unblock_src)
        self.assertNotIn("autopay_allow = True", unblock_src)

        payload_src = inspect.getsource(views.admin_referral_payload)
        self.assertIn('"account_block": admin_account_block_payload(db_session, user)', payload_src)

        template = template_source("engine/templates/admin_dashboard.html")
        self.assertIn('data-client-sub-action="block_account"', template)
        self.assertIn('data-client-sub-action="unblock_account"', template)
        self.assertNotIn('data-client-sub-action="disable_subscription"', template)
        self.assertIn("const accountBlocked = Boolean(accountBlock?.blocked);", template)
        self.assertIn("Полностью заблокировать аккаунт (как /block-user)?", template)

    def test_temp_ban_pushes_notice_to_bot_queues(self):
        """Временный бан из админки уведомляет пользователя тем же текстом, что
        бот (NOTIFY_TEMPORARY_BAN): служебный пуш admin-temporary-ban в
        Redis-очереди всех ботов (как user-notify), шлёт сам бот."""
        import inspect
        from unittest import mock

        from django.test import override_settings

        from engine import bot_push, views

        src = inspect.getsource(views.support_admin_api_subscription_manage)
        self.assertIn("push_admin_temporary_ban(", src)
        self.assertIn('"user_notified": user_notified', src)

        with override_settings(BOT_REDIS_HOST="", BOT_REDIS_QUEUES="a,b"):
            self.assertFalse(bot_push.is_enabled())
            self.assertFalse(bot_push.push_admin_temporary_ban(42, 120))

        fake = mock.Mock()
        with override_settings(
            BOT_REDIS_HOST="redis", BOT_REDIS_QUEUES="brand-vpn-bot, brand-vps-bot"
        ), mock.patch.object(bot_push, "_redis_client", return_value=fake):
            self.assertTrue(bot_push.push_admin_temporary_ban("42", 120))
            # Без telegram_id уведомлять некого
            self.assertFalse(bot_push.push_admin_temporary_ban(None, 120))

        self.assertEqual(fake.rpush.call_count, 2)
        queues = [call.args[0] for call in fake.rpush.call_args_list]
        self.assertEqual(queues, ["brand-vpn-bot", "brand-vps-bot"])
        payload = json.loads(fake.rpush.call_args_list[0].args[1])
        self.assertEqual(payload["type"], "admin-temporary-ban")
        self.assertEqual(payload["notification_type"], "admin_temporary_ban")
        self.assertEqual(payload["telegram_id"], 42)
        self.assertEqual(payload["ban_minutes"], 120)

        # Redis недоступен — действие не падает, пуш просто не уходит (False).
        fake_err = mock.Mock()
        fake_err.rpush.side_effect = RuntimeError("down")
        with override_settings(BOT_REDIS_HOST="redis", BOT_REDIS_QUEUES="q"), mock.patch.object(
            bot_push, "_redis_client", return_value=fake_err
        ), self.assertLogs(level="ERROR"):
            self.assertFalse(bot_push.push_admin_temporary_ban(42, 60))

        template = template_source("engine/templates/admin_dashboard.html")
        self.assertIn("result.user_notified", template)

    def test_prepare_refund_removes_recurrent_payments(self):
        """«Подготовить возврат» (set_trial_hour) обязан снимать автоплатёж:
        срок 1 час сразу попадает в окно автосписания yk-recurrent
        [expire-4ч; expire+overdue], и без удаления рекуррента клиенту,
        которому возвращают деньги, спишут их снова. Поведение — как у
        stop_autopay и бот-команды /set-trial-hour."""
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_subscription_manage)
        trial = src[src.index('if action == "set_trial_hour":'):src.index('elif action == "extend":')]
        self.assertIn("user.autopay_allow = False", trial)
        self.assertIn("YkRecurrentPayment.user_id == user.id", trial)
        self.assertIn(".delete(synchronize_session=False)", trial)
        self.assertIn("removed_recurrents=removed_recurrents", src)
        self.assertIn('"removed_recurrents": removed_recurrents', src)
        self.assertIn("Возврат подготовлен: срок 1 час, автоплатёж отключён", src)

        template = template_source("engine/templates/admin_dashboard.html")
        self.assertIn("Срок станет 1 час · автоплатёж отключится, рекуррент удалится", template)
        self.assertNotIn("автоплатёж не изменится", template)
        self.assertIn("автоплатёж будет отключён (рекуррент удалён)?", template)

    def test_broadcast_can_exclude_users_who_activated_promo(self):
        """Переключатель «Кому отправлять промокод из кнопки»: режим
        promo_recipients=exclude_activated привязан к кнопке claim_promo, promo_id
        кнопки пишется в broadcasts.exclude_promo_id; total считается с тем же
        фильтром (EXCLUDE_PROMO_USED_SQL), что применяют боты при выборке."""
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_broadcasts)
        self.assertIn('request.POST.get("promo_recipients") or "all"', src)
        self.assertIn("promo_recipients not in BROADCAST_PROMO_RECIPIENTS", src)
        self.assertIn('promo_recipients == "exclude_activated"', src)
        self.assertEqual(views.BROADCAST_PROMO_RECIPIENTS, {"all", "exclude_activated"})
        self.assertIn("Исключение активировавших требует кнопку промокода", src)
        self.assertIn("segment_count_sql(segment, exclude_promo=True)", src)
        self.assertIn('{"exclude_promo_id": exclude_promo_id}', src)
        self.assertIn("exclude_promo_id=exclude_promo_id,", src)
        payload_src = inspect.getsource(views.admin_broadcast_payload)
        self.assertIn('"exclude_promo_id"', payload_src)
        self.assertIn('"exclude_promo_code"', payload_src)

        from types import SimpleNamespace

        buttons = [{"type": "claim_promo", "promo_id": 7, "code": "SALE20"}]
        self.assertEqual(
            views.admin_broadcast_exclude_promo_code(
                SimpleNamespace(exclude_promo_id=7, buttons=buttons)
            ),
            "SALE20",
        )
        self.assertIsNone(
            views.admin_broadcast_exclude_promo_code(
                SimpleNamespace(exclude_promo_id=None, buttons=buttons)
            )
        )

        from common.models.db import Broadcast
        from common.models.segments import EXCLUDE_PROMO_USED_SQL, segment_count_sql

        self.assertTrue(hasattr(Broadcast, "exclude_promo_id"))
        self.assertIn("promo_code_uses", EXCLUDE_PROMO_USED_SQL)
        self.assertIn(":exclude_promo_id", EXCLUDE_PROMO_USED_SQL)
        self.assertNotIn("promo_code_uses", segment_count_sql("all"))
        self.assertIn("promo_code_uses", segment_count_sql("all", exclude_promo=True))

        template = template_source("engine/templates/admin_dashboard.html")
        # Явный переключатель режима (всем / исключить активировавших),
        # по умолчанию — всем; без кнопки промокода радио неактивны.
        self.assertIn('id="broadcast-promo-audience" data-has-promo="false"', template)
        self.assertIn('name="broadcast-promo-recipients" value="all" checked disabled', template)
        self.assertIn('name="broadcast-promo-recipients" value="exclude_activated" disabled', template)
        self.assertIn("function syncBroadcastPromoAudience()", template)
        self.assertIn("function broadcastPromoRecipients()", template)
        self.assertIn("body.append('promo_recipients', promoRecipients)", template)
        self.assertIn("row.exclude_promo_id ?", template)

    def test_broadcast_tariff_button_has_no_price_override(self):
        """Промо-цена на кнопках тарифов убрана: в callback_data бота цена ехала
        из рассылки и принималась на веру (payment сумму не сверял), поэтому
        механизм вырезан целиком — скидки делаются только промокодом. Кнопка
        type=tariffs остаётся и означает «тарифы по актуальным ценам»,
        tariff_ids сохраняется. Легаси-ключ price_overrides во входящем JSON
        игнорируется, а не отвергается: старые рассылки с ним в buttons должны
        открываться и пересохраняться без ошибок (данные не мигрируем)."""
        import inspect

        from engine import views

        parse = views.admin_broadcast_parse_buttons

        # Обычная кнопка тарифов: сохраняются только идентификаторы.
        self.assertEqual(
            parse(None, json.dumps([{"type": "tariffs", "tariff_ids": ["month", "year"]}])),
            [{"type": "tariffs", "tariff_ids": ["month", "year"]}],
        )
        # Пустой список — вся витрина тарифов.
        self.assertEqual(
            parse(None, json.dumps([{"type": "tariffs"}])),
            [{"type": "tariffs", "tariff_ids": None}],
        )
        # Присланная промо-цена не сохраняется и не добавляет свой тариф в список.
        cleaned = parse(
            None,
            json.dumps(
                [
                    {
                        "type": "tariffs",
                        "tariff_ids": ["month"],
                        "price_overrides": {"year": 1},
                    }
                ]
            ),
        )
        self.assertEqual(cleaned, [{"type": "tariffs", "tariff_ids": ["month"]}])
        self.assertNotIn("price_overrides", cleaned[0])
        # Существующая рассылка: её buttons из БД проходят валидацию заново
        # (открыть и сохранить) без исключения, ключ просто отбрасывается.
        legacy = [
            {"type": "url", "text": "Сайт", "url": "https://example.com", "style": None},
            {
                "type": "tariffs",
                "tariff_ids": ["month", "year"],
                "price_overrides": {"month": 1, "year": 1},
            },
        ]
        self.assertEqual(
            parse(None, json.dumps(legacy)),
            [
                {"type": "url", "text": "Сайт", "url": "https://example.com", "style": None},
                {"type": "tariffs", "tariff_ids": ["month", "year"]},
            ],
        )
        # Неизвестный тариф по-прежнему отвергается.
        with self.assertRaises(ValueError):
            parse(None, json.dumps([{"type": "tariffs", "tariff_ids": ["decade"]}]))

        src = inspect.getsource(views.admin_broadcast_parse_buttons)
        self.assertNotIn('"price_overrides": overrides', src)
        self.assertNotIn("Некорректная цена тарифа", src)

        template = template_source("engine/templates/admin_dashboard.html")
        # В форме не осталось ни поля с ценой, ни разбора "month=199".
        self.assertNotIn("price_overrides", template)
        self.assertNotIn("month=199", template)
        self.assertNotIn("Некорректная цена: ${item}", template)
        self.assertIn(
            'placeholder="month, year (пусто — все тарифы; цены всегда актуальные)"',
            template,
        )
        self.assertIn("return {tariff_ids: tariff_ids.length ? tariff_ids : null};", template)

    def test_broadcast_link_preview_can_be_disabled(self):
        """Чекбокс «Отключить превью ссылок»: флаг сохраняется в broadcasts и
        уходит ботам (боты шлют с disable_web_page_preview, как /sendmsg)."""
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_broadcasts)
        self.assertIn('request.POST.get("disable_preview") == "1"', src)
        payload_src = inspect.getsource(views.admin_broadcast_payload)
        self.assertIn("disable_link_preview", payload_src)

        from common.models.db import Broadcast

        self.assertTrue(hasattr(Broadcast, "disable_link_preview"))

        template = template_source("engine/templates/admin_dashboard.html")
        self.assertIn('id="broadcast-disable-preview"', template)
        self.assertIn("checked", template)
        self.assertIn("body.append('disable_preview', '1')", template)

    def test_broadcast_schedule_time_is_msk_and_bounded(self):
        """Время из datetime-local — МСК без зоны; хранится naive UTC (−3 ч).
        Прошлое и «дальше 90 дней» отклоняются с текстом для админа, пустое —
        «сразу»."""
        from datetime import datetime, timedelta

        from engine import views

        now = datetime(2026, 9, 14, 9, 0)  # UTC
        self.assertIsNone(views.admin_broadcast_parse_schedule("", now=now))
        self.assertIsNone(views.admin_broadcast_parse_schedule(None, now=now))
        self.assertEqual(
            views.admin_broadcast_parse_schedule("2026-09-14T18:30", now=now),
            datetime(2026, 9, 14, 15, 30),
        )
        with self.assertRaisesRegex(ValueError, "уже прошло"):
            views.admin_broadcast_parse_schedule("2026-09-14T12:00", now=now)
        with self.assertRaisesRegex(ValueError, "уже прошло"):
            # ровно «сейчас» по МСК — меньше минуты вперёд
            views.admin_broadcast_parse_schedule("2026-09-14T12:00", now=now)
        with self.assertRaisesRegex(ValueError, "90 дней"):
            views.admin_broadcast_parse_schedule(
                (now + timedelta(days=91, hours=3)).strftime("%Y-%m-%dT%H:%M"),
                now=now,
            )
        with self.assertRaisesRegex(ValueError, "дату и время"):
            views.admin_broadcast_parse_schedule("завтра", now=now)
        with self.assertRaisesRegex(ValueError, "дату и время"):
            views.admin_broadcast_parse_schedule("2026-09-14 18:30", now=now)

    def test_broadcasts_can_be_scheduled(self):
        """Отложенные рассылки: status=scheduled + scheduled_at (naive UTC);
        стартуют боты (первый атомарно переводит в running и пересчитывает
        total) или админ кнопкой «Отправить сейчас». До старта — перенос и
        отмена; архивировать нельзя, тест «себе» всегда сразу."""
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_broadcasts)
        self.assertIn('request.POST.get("scheduled_at")', src)
        self.assertIn('status="scheduled" if scheduled_at else "running"', src)
        self.assertIn("scheduled_at=scheduled_at,", src)
        # тест — всегда сразу
        self.assertIn("if not is_test:\n                try:\n                    scheduled_at = admin_broadcast_parse_schedule(", src)
        # stop отменяет отложенную, архив запрещён до старта
        self.assertIn('broadcast.status in ("running", "scheduled")', src)
        self.assertIn('"broadcast_cancel" if was_scheduled else "broadcast_stop"', src)
        # перенос и «отправить сейчас» — только для scheduled
        self.assertIn('action in ("reschedule", "send_now")', src)
        self.assertIn('broadcast.status != "scheduled"', src)
        self.assertIn("Рассылка уже не запланирована", src)
        self.assertIn('"broadcast_reschedule"', src)
        self.assertIn('"broadcast_send_now"', src)
        self.assertIn("broadcast.total = admin_broadcast_count_total(db_session, broadcast)", src)
        self.assertIn('"broadcast_schedule" if scheduled_at else "broadcast_create"', src)

        count_src = inspect.getsource(views.admin_broadcast_count_total)
        self.assertIn("segment_count_sql(broadcast.segment, exclude_promo=True)", count_src)

        payload_src = inspect.getsource(views.admin_broadcast_payload)
        self.assertIn('"scheduled_at": admin_date_label(scheduled_at)', payload_src)
        self.assertIn('"scheduled_at_input"', payload_src)
        self.assertIn("BROADCAST_SCHEDULE_FORMAT", payload_src)

        from common.models.db import Broadcast

        self.assertTrue(hasattr(Broadcast, "scheduled_at"))

        template = template_source("engine/templates/admin_dashboard.html")
        for needle in (
            'id="broadcast-schedule" data-mode="now"',
            'name="broadcast-schedule-mode" value="now" checked',
            'name="broadcast-schedule-mode" value="later"',
            'id="broadcast-schedule-at"',
            "function broadcastScheduleMode()",
            "function setBroadcastScheduleMode(mode)",
            "if (scheduleLater) body.append('scheduled_at', scheduledAt);",
            "Запланировать рассылку",
            "scheduled: {label: 'Запланирована', icon: 'fa-clock'}",
            "data-broadcast-reschedule=",
            "data-broadcast-send-now=",
            'data-broadcast-cancel="1"',
            "broadcastSchedulePost('reschedule'",
            "broadcastSchedulePost('send_now'",
            "row.status === 'scheduled'",
        ):
            self.assertIn(needle, template, needle)
        css = template_source("engine/static/css/admin-concept-customers.css")
        self.assertIn('.broadcast-record[data-status="scheduled"]', css)

    def test_fast_segment_counts_cover_every_segment(self):
        """У каждого сегмента из ADMIN_SEGMENTS должно быть условие быстрого
        подсчёта — иначе новый сегмент молча уйдёт на медленный fallback."""
        from common.models.segments import (
            ADMIN_SEGMENTS,
            SEGMENT_COUNT_CONDITIONS,
            segments_counts_sql,
        )

        self.assertEqual(set(ADMIN_SEGMENTS), set(SEGMENT_COUNT_CONDITIONS))
        sql = segments_counts_sql()
        for key in ADMIN_SEGMENTS:
            self.assertIn(f'AS "{key}"', sql)
        # Базовая выборка повторяет segment_where_sql: без заблокированных,
        # только с Telegram.
        self.assertIn("user_blocks", sql)
        self.assertIn("u.telegram_id IS NOT NULL", sql)

    def test_stage4_endpoints_are_routed(self):
        from django.urls import reverse

        for name in (
            "support_admin_api_segments",
            "support_admin_api_broadcasts",
            "support_admin_api_bulk",
        ):
            self.assertTrue(reverse(name))

    def test_bulk_endpoint_audits_and_has_dry_run(self):
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_bulk)
        self.assertIn("dry_run", src)
        self.assertIn("admin_audit_write", src)
        self.assertIn("BULK_MAX_IDS", src)

    def test_bulk_never_deletes_users_or_subscriptions(self):
        import inspect

        from engine import views

        for func in (views.admin_bulk_extend, views.admin_bulk_block, views.admin_bulk_unblock):
            src = inspect.getsource(func)
            self.assertNotIn("add_user", src)
            # Единственный delete — снятие рекуррентов/блокировок, не пользователей.
            self.assertNotIn("query(User).", src)

    def test_template_has_broadcasts_and_bulk_panels(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('data-tab="broadcasts"', template)
        self.assertIn('data-tab="bulk-actions"', template)
        self.assertIn('id="bulk-preview"', template)
        self.assertIn("dry-run", template)
        self.assertIn('data-broadcast-stop=', template)


class AdminSubscriptionManageRwmsStrictTests(SimpleTestCase):
    """Админ-действия по сроку (extend / set_trial_hour) читают панель строго.

    Блип RWMS/панели (RwmsUnavailableError) — НЕ «подписки нет»: ответ 503,
    rwms_updated=false и никакого AddUser/пересоздания; срок в БД при этом
    уже сохранён (БД — истина по времени). Пересоздание через
    create_user_until — только по достоверному NOT_FOUND (None), как раньше;
    найденная запись обновляется update_user in-place."""

    class _FakeSession:
        def __init__(self):
            self.added = []
            self.commits = 0
            self.closed = False
            self.recurrent_deletes = 0

        def get(self, model, key):
            # system_settings пусты: антиабьюз выключен (дефолты common)
            return None

        def add(self, obj):
            self.added.append(obj)

        def query(self, *models):
            session = self

            class _Query:
                def filter(self, *args, **kwargs):
                    return self

                def delete(self, synchronize_session=False):
                    session.recurrent_deletes += 1
                    return 1

            return _Query()

        def commit(self):
            self.commits += 1

        def close(self):
            self.closed = True

    def _user(self):
        return SimpleNamespace(
            id=7,
            username="42",
            email="user@example.com",
            telegram_id=42,
            # Срок в будущем: база продления — expire_at из БД, а не utcnow.
            expire_at=datetime(2030, 1, 1, 12, 0, 0),
            autopay_allow=True,
        )

    def _call(self, action, client, days=None):
        from engine.views import support_admin_api_subscription_manage

        session = self._FakeSession()
        user = self._user()
        data = {"q": user.username, "action": action}
        if days is not None:
            data["days"] = str(days)
        request = RequestFactory().post(
            "/support-admin/api/subscription-manage/", data=data
        )
        request.session = {}
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
            mock.patch("engine.views.admin_find_user", return_value=user),
            mock.patch("engine.views.rwms_client", client),
        ):
            response = support_admin_api_subscription_manage(request)
        return response, session, user

    def test_rwms_unavailable_returns_503_and_never_recreates(self):
        from datetime import timedelta

        original_expire = self._user().expire_at
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = RwmsUnavailableError(
            "42", None, "panel down"
        )

        with self.assertLogs(level="WARNING") as logs:
            response, session, user = self._call("extend", client, days=7)

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "error")
        self.assertIn("временно недоступны", payload["message"])
        self.assertIs(payload["result"]["rwms_updated"], False)
        self.assertEqual(payload["result"]["user"]["username"], "42")
        # Панель не тронута: ни update, ни AddUser/пересоздания.
        client.add_user.assert_not_called()
        client.update_user.assert_not_called()
        client.get_user_by_username.assert_not_called()
        client.get_user_by_username_strict.assert_called_once_with("42")
        # Срок в БД уже сохранён (БД — истина по времени), сессия закрыта.
        self.assertEqual(session.commits, 1)
        self.assertEqual(user.expire_at, original_expire + timedelta(days=7))
        self.assertTrue(session.closed)
        self.assertTrue(any("panel left untouched" in line for line in logs.output))

    def test_rwms_unavailable_on_refund_prep_keeps_autopay_removed(self):
        """«Подготовить возврат» при блипе панели: автоплатёж всё равно снят и
        рекуррент удалён в БД (иначе клиенту спишут повторно), панель — 503
        без пересоздания."""
        from datetime import timedelta

        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = RwmsUnavailableError(
            "42", None, "panel down"
        )

        with self.assertLogs(level="WARNING"):
            response, session, user = self._call("set_trial_hour", client)

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content)
        self.assertIs(payload["result"]["rwms_updated"], False)
        self.assertEqual(payload["result"]["removed_recurrents"], 1)
        self.assertFalse(user.autopay_allow)
        self.assertEqual(session.recurrent_deletes, 1)
        self.assertEqual(session.commits, 1)
        self.assertLess(user.expire_at, datetime.utcnow() + timedelta(hours=1, minutes=1))
        client.add_user.assert_not_called()
        client.update_user.assert_not_called()

    def test_confirmed_not_found_recreates_subscription_as_before(self):
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = None  # достоверный NOT_FOUND
        client.add_user.return_value = SimpleNamespace(uuid="recreated")

        with self.assertLogs(level="WARNING") as logs:
            response, session, user = self._call("extend", client, days=3)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "ok")
        self.assertIs(payload["result"]["rwms_updated"], True)
        client.add_user.assert_called_once()
        add_request = client.add_user.call_args.args[0]
        self.assertEqual(add_request.username, "42")
        self.assertEqual(add_request.telegram_id, 42)
        client.update_user.assert_not_called()
        client.get_user_by_username.assert_not_called()
        self.assertEqual(session.commits, 1)
        self.assertTrue(any("recreating" in line for line in logs.output))

    def test_found_subscription_is_updated_in_place(self):
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            uuid="panel-uuid",
            email="user@example.com",
            telegram_id=42,
            active_internal_squads=[],
        )
        client.update_user.return_value = SimpleNamespace(uuid="panel-uuid")

        response, session, user = self._call("extend", client, days=3)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertIs(payload["result"]["rwms_updated"], True)
        client.update_user.assert_called_once()
        self.assertEqual(client.update_user.call_args.args[0].uuid, "panel-uuid")
        client.add_user.assert_not_called()
        client.get_user_by_username.assert_not_called()

    def test_extend_and_refund_prep_keep_panel_traffic_limit_untouched(self):
        """Антиабьюз: продление / «Подготовить возврат» не трогают лимит
        трафика и стратегию сброса ограниченного триала. Раньше в UpdateUser
        уезжал явный traffic_limit_strategy=NO_RESET: после HasField-фикса RWMS
        он затирал стратегию day/week/... при сохранённом traffic_limit_bytes —
        лимит становился одноразовым на весь срок."""
        import proto.rwmanager_pb2 as rw_proto

        for action, days in (("extend", 3), ("set_trial_hour", None)):
            client = mock.Mock()
            client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
                uuid="panel-uuid",
                email="user@example.com",
                telegram_id=42,
                active_internal_squads=[SimpleNamespace(uuid="squad-1")],
                traffic_limit_bytes=5 * 1024**3,
                traffic_limit_strategy=rw_proto.TrafficLimitStrategy.DAY,
                status=rw_proto.UserStatus.ACTIVE,
            )
            client.update_user.return_value = SimpleNamespace(uuid="panel-uuid")

            response, session, user = self._call(action, client, days=days)

            self.assertEqual(response.status_code, 200, action)
            self.assertIs(json.loads(response.content)["result"]["rwms_updated"], True)
            client.update_user.assert_called_once()
            request = client.update_user.call_args.args[0]
            self.assertEqual(request.uuid, "panel-uuid")
            self.assertFalse(request.HasField("traffic_limit_strategy"), action)
            self.assertFalse(request.HasField("traffic_limit_bytes"), action)
            self.assertTrue(request.HasField("expire_at"), action)
            self.assertEqual(request.status, rw_proto.UserStatus.ACTIVE)
            self.assertEqual(list(request.active_internal_squads), ["squad-1"])
            client.add_user.assert_not_called()

    def test_admin_extensions_never_send_traffic_limit_fields(self):
        """Регресс-гард: явный NO_RESET не должен вернуться в продления сайта
        (карточка клиента, «БД → панель», bulk extend_days). Снятие лимита —
        только admin_rwms_remove_traffic_limit и оплата."""
        import inspect

        from engine import views

        manage = inspect.getsource(views.support_admin_api_subscription_manage)
        expire_branch = manage[manage.index("old_expire = user.expire_at"):]
        for name, src in (
            ("subscription_manage/expire", expire_branch),
            ("rwms_sync", inspect.getsource(views.support_admin_api_rwms_sync)),
            ("admin_bulk_extend", inspect.getsource(views.admin_bulk_extend)),
        ):
            self.assertNotIn("traffic_limit_strategy=", src, name)
            self.assertNotIn("traffic_limit_bytes=", src, name)
        # Намеренное снятие лимита остаётся явным.
        self.assertIn(
            "traffic_limit_strategy=proto.TrafficLimitStrategy.NO_RESET",
            inspect.getsource(views.admin_rwms_remove_traffic_limit),
        )

    def test_expire_branch_source_uses_strict_read_only(self):
        """Регресс-гард: ветка продления/возврата не должна вернуться на
        deprecated get_user_by_username (любая ошибка → None → AddUser)."""
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_subscription_manage)
        branch = src[src.index("old_expire = user.expire_at"):]
        self.assertIn("get_user_by_username_strict(user.username)", branch)
        self.assertIn("except RwmsUnavailableError", branch)
        self.assertNotIn("rwms_client.get_user_by_username(", branch)
        # create_user_until — только после strict-чтения (достоверный NOT_FOUND).
        self.assertLess(
            branch.index("except RwmsUnavailableError"),
            branch.index("create_user_until("),
        )

        template = template_source("engine/templates/admin_dashboard.html")
        # UI показывает серверное сообщение 503, а не общее «Не удалось».
        self.assertIn("const errorPayload = await response.json();", template)


class AdminStage5PromoTests(SimpleTestCase):
    """Этап 5: промокоды, купоны, скидка на первую покупку."""

    def test_promocodes_endpoint_routed_and_audited(self):
        import inspect

        from django.urls import reverse

        from engine import views

        self.assertTrue(reverse("support_admin_api_promocodes"))
        src = inspect.getsource(views.support_admin_api_promocodes)
        self.assertIn("admin_audit_write", src)
        # Купоны партий всегда одноразовые.
        self.assertIn("max_uses=1", src)

    def test_site_checkout_applies_discount_only_before_first_payment(self):
        import inspect

        from engine import views

        # Правила действия скидки вынесены в active_first_purchase_discount
        # (её же использует экран покупки мобильного кабинета).
        src = inspect.getsource(views.active_first_purchase_discount)
        self.assertIn("has_paid", src)
        self.assertIn("valid_until", src)
        src = inspect.getsource(views.site_apply_first_purchase_discount)
        self.assertIn("active_first_purchase_discount(db_session, user)", src)
        # Цена не может уйти ниже 1 ₽ и скидка fail-open при ошибках.
        self.assertIn("max(1,", src)
        self.assertIn("except Exception", src)

    def test_yk_payment_carries_promo_metadata(self):
        import inspect

        from engine import payments

        src = inspect.getsource(payments.create_yk_payment_sync)
        self.assertIn('"promo": promo', src)

    def test_template_has_promocodes_tab(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('data-tab="promocodes"', template)
        self.assertIn('id="promo-create"', template)
        # Один конструктор с режимом (концепт 10.09.2026) — отдельной
        # кнопки партии больше нет
        self.assertNotIn('id="batch-create"', template)
        self.assertIn('data-promo-mode="batch"', template)
        self.assertIn("function setPromoBuilderMode(mode)", template)
        self.assertIn("start=promo_", Path("engine/views.py").read_text())

    def test_promocode_editor_explains_effects_and_audiences(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn("Один код — одна активация на человека", template)
        self.assertIn("Скидка на следующую оплату", template)
        self.assertIn("Без даты она доступна 72 часа", template)
        self.assertIn('data-promo-type-picker="promo"', template)
        # Пикер один: партия использует общие эффект/срок/аудиторию
        self.assertNotIn('data-promo-type-picker="batch"', template)
        self.assertIn('id="promo-first-only"', template)
        self.assertNotIn('id="batch-first-only"', template)
        self.assertIn('data-promo-mode-only="batch"', template)
        self.assertIn('id="batch-name"', template)
        self.assertIn('id="batch-count"', template)
        self.assertIn("renderPromoCodes", template)
        self.assertIn("renderPromoBatches", template)
        self.assertNotIn('<select id="promo-type"', template)

    def test_promocode_icons_and_supporting_copy_are_readable(self):
        template = template_source("engine/templates/admin_dashboard.html")

        def css_rule(selector):
            start = template.index(f"{selector} {{")
            return template[start:template.index("}", start) + 1]

        self.assertIn("display: grid", css_rule(".promo-guide-intro-icon"))
        self.assertIn("font-size: 15px", css_rule(".promo-guide-intro-icon"))
        self.assertIn("display: grid", css_rule(".promo-rule-icon"))
        self.assertIn("font-size: 12px", css_rule(".promo-rule-icon"))
        self.assertIn("font-size: 10.5px", css_rule(".promo-field-label"))
        self.assertIn("font-size: 10px", css_rule(".promo-guide-tags span"))
        self.assertIn(
            ".promo-rule > div > span { color: var(--muted); font-size: 10.5px;",
            template,
        )
        self.assertIn("font-size: inherit", css_rule(".promo-guide-intro-icon i, .promo-guide-card-icon i, .promo-card-icon i, .promo-type-option-icon i, .promo-rule-icon i"))
        self.assertIn('<i class="fas fa-key"></i>', template)
        self.assertNotIn(".promo-guide-intro b, .promo-guide-intro span", template)
        self.assertNotIn(".promo-rule b, .promo-rule span", template)

    def test_batch_payload_exposes_effect_and_audience(self):
        from types import SimpleNamespace

        from engine.views import admin_promo_batch_payload

        batch = SimpleNamespace(
            id=7,
            name="Blogger August",
            comment="Партнёру",
            created_at=datetime(2026, 8, 11, 12, 30),
        )
        sample = SimpleNamespace(
            promo_type="discount",
            value=25,
            first_purchase_only=True,
            valid_until=datetime(2026, 8, 20),
        )

        payload = admin_promo_batch_payload(batch, sample, 50, 12)

        self.assertEqual(payload["promo_type"], "discount")
        self.assertEqual(payload["value"], 25)
        self.assertTrue(payload["first_purchase_only"])
        self.assertEqual(payload["codes"], 50)
        self.assertEqual(payload["used"], 12)
        self.assertTrue(payload["valid_until"])


class AdminStage6RolesTests(SimpleTestCase):
    """Этап 6: персональные аккаунты и роль marketer."""

    def test_accounts_endpoint_routed_full_only(self):
        import inspect

        from django.urls import reverse

        from engine import views

        self.assertTrue(reverse("support_admin_api_accounts"))
        src = inspect.getsource(views.support_admin_api_accounts)
        self.assertIn("SUPPORT_ADMIN_ROLE_ADMIN", src)
        self.assertIn("make_password", src)
        self.assertIn("admin_audit_write", src)

    def test_marketer_has_analytics_but_not_system(self):
        import inspect

        from engine import views

        for func in (
            views.support_admin_api_stats,
            views.support_admin_api_acquisition,
            views.support_admin_api_broadcasts,
            views.support_admin_api_promocodes,
        ):
            self.assertIn(
                "ANALYTICS_ROLES", inspect.getsource(func), func.__name__
            )
        # Мутации клиентов/системы остаются только для full.
        for func in (
            views.support_admin_api_subscription_manage,
            views.support_admin_api_bulk,
            views.support_admin_api_runtime_settings,
            views.support_admin_api_audit_log,
        ):
            self.assertIn(
                "require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)",
                inspect.getsource(func),
                func.__name__,
            )

    def test_login_supports_personal_accounts_with_fallback(self):
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_login)
        self.assertIn("AdminAccount", src)
        self.assertIn("check_password", src)
        # Общие пароли остаются как запасной вход.
        self.assertIn("SUPPORT_ADMIN_PASSWORD", src)
        # Логин пишется в сессию для аудита.
        self.assertIn("SUPPORT_ADMIN_ACCOUNT_SESSION_KEY", src)

    def test_template_gates_marketer_sections(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn(
            "{% if support_admin_is_full_admin or support_admin_is_marketer %}",
            template,
        )
        self.assertIn('data-subtab="sys-staff"', template)
        self.assertIn('id="staff-create"', template)
        login_template = Path(
            "engine/templates/support_admin_login.html"
        ).read_text()
        self.assertIn('name="login"', login_template)


class DormantSegmentsTests(SimpleTestCase):
    """Win-back-сегменты «спящих» (истекли >30 дней назад) для рассылок."""

    def test_dormant_segments_registered(self):
        from common.models.segments import ADMIN_SEGMENTS

        self.assertIn("dormant_30d_paid", ADMIN_SEGMENTS)
        self.assertIn("dormant_30d_used_free", ADMIN_SEGMENTS)

        label, condition = ADMIN_SEGMENTS["dormant_30d_paid"]
        self.assertIn("Неактивны >30 дней, платили", label)
        self.assertIn("interval '30 days'", condition)
        self.assertIn("u.expire_at <=", condition)
        self.assertIn("yk_payments", condition)

        label, condition = ADMIN_SEGMENTS["dormant_30d_used_free"]
        self.assertIn("не платили", label)
        self.assertIn("NOT (", condition)
        self.assertIn("user_traffic_progress", condition)
        self.assertIn("tp.passed_0", condition)

    def test_dormant_segments_do_not_overlap_by_payments(self):
        # Один и тот же пользователь не должен попадать в оба сегмента:
        # первый требует наличие оплат, второй — их отсутствие.
        from common.models.segments import ADMIN_SEGMENTS, PAYS_EXISTS_SQL

        paid = ADMIN_SEGMENTS["dormant_30d_paid"][1]
        free = ADMIN_SEGMENTS["dormant_30d_used_free"][1]
        self.assertIn(f"({PAYS_EXISTS_SQL})", paid)
        self.assertIn(f"NOT ({PAYS_EXISTS_SQL})", free)

    def test_segments_synced_with_bot_repo(self):
        # Сегменты читает и бот (выборка получателей рассылки); файлы обязаны
        # быть идентичны, иначе рассылка по новому сегменту упадёт в боте.
        bot_copy = Path(
            "../monkey-island-vpn-bot/common/models/segments.py"
        )
        if not bot_copy.exists():
            self.skipTest("репозиторий бота недоступен")
        self.assertEqual(
            Path("common/models/segments.py").read_text(), bot_copy.read_text()
        )


class PromoCohortTests(SimpleTestCase):
    """Когорта по промокоду: судьба активировавших (покупки, статус, автоплатёж)."""

    def test_endpoint_is_routed(self):
        from django.urls import reverse

        self.assertTrue(reverse("support_admin_api_promo_cohort"))

    def test_view_is_read_only_and_counts_payments_after_activation(self):
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_promo_cohort)
        # Только чтение: никаких мутаций и коммитов.
        self.assertNotIn("db_session.add", src)
        self.assertNotIn("db_session.delete", src)
        self.assertNotIn("commit", src)
        # Платежи считаются строго ПОСЛЕ активации промокода.
        self.assertIn("p.created_at > uses.created_at", src)
        # timestamptz приводится к naive UTC прямо в SQL: иначе граница
        # зависела бы от TimeZone сессии БД, а min() в питоне падал бы на
        # смеси naive и aware
        self.assertIn("t.payment_time AT TIME ZONE 'UTC' > uses.created_at", src)
        # Оба платёжных провайдера и судьба: статус, автоплатёж, блокировка.
        self.assertIn("yk_payments", src)
        self.assertIn("wata_transactions", src)
        self.assertIn("yk_recurrent_payments", src)
        self.assertIn("user_blocks", src)
        # Конверсия рассылок: только реальные (не тестовые) с кнопкой промо.
        self.assertIn("claim_promo", src)
        self.assertIn("test_telegram_id IS NULL", src)
        self.assertIn("PROMO_COHORT_USERS_LIMIT", src)

    def test_requires_promo_or_batch_id(self):
        from engine.views import support_admin_api_promo_cohort

        with mock.patch(
            "engine.views.require_support_admin_any", return_value=None
        ):
            request = RequestFactory().get("/support-admin/api/promo-cohort/")
            self.assertEqual(support_admin_api_promo_cohort(request).status_code, 400)
            request = RequestFactory().get(
                "/support-admin/api/promo-cohort/?promo_id=1&batch_id=2"
            )
            self.assertEqual(support_admin_api_promo_cohort(request).status_code, 400)

    def test_endpoint_returns_funnel_states_and_first_payment_timeline(self):
        from engine.views import support_admin_api_promo_cohort

        def row(
            user_id,
            activated_at,
            expire_at,
            *,
            yk_count=0,
            yk_total=0,
            yk_first=None,
            wata_count=0,
            wata_total=0,
            wata_first=None,
            has_autopay=False,
            is_blocked=False,
        ):
            return SimpleNamespace(
                user_id=user_id,
                telegram_id=100000 + user_id,
                activated_at=activated_at,
                expire_at=expire_at,
                has_autopay=has_autopay,
                is_blocked=is_blocked,
                yk_cnt=yk_count,
                yk_total=yk_total,
                yk_first=yk_first,
                wt_cnt=wata_count,
                wt_total=wata_total,
                wt_first=wata_first,
            )

        future = datetime(2099, 1, 1)
        expired = datetime(2020, 1, 1)
        rows = [
            row(
                1,
                datetime(2026, 8, 1, 10),
                future,
                yk_count=2,
                yk_total=2000,
                yk_first=datetime(2026, 8, 3, 10),
                has_autopay=True,
            ),
            row(2, datetime(2026, 8, 1, 12), future),
            row(
                3,
                datetime(2026, 8, 2, 8),
                expired,
                wata_count=1,
                wata_total=900,
                wata_first=datetime(2026, 8, 4, 8, tzinfo=timezone.utc),
            ),
            row(4, datetime(2026, 8, 2, 14), expired),
            row(5, datetime(2026, 8, 2, 16), future, is_blocked=True),
        ]

        cohort_query = mock.Mock()
        cohort_query.all.return_value = rows
        discounts_query = mock.Mock()
        discounts_query.scalar.return_value = 2
        broadcasts_query = mock.Mock()
        broadcasts_query.all.return_value = []
        yk_tariff_query = mock.MagicMock()
        wata_tariff_query = mock.MagicMock()
        for tariff_query in (yk_tariff_query, wata_tariff_query):
            tariff_query.select_from.return_value = tariff_query
            tariff_query.join.return_value = tariff_query
            tariff_query.filter.return_value = tariff_query
            tariff_query.group_by.return_value = tariff_query
        yk_tariff_query.all.return_value = [("month", 1, 1000)]
        wata_tariff_query.all.return_value = [
            ("month", 1, 1000),
            ("year", 1, 900),
        ]
        db_session = mock.Mock()
        db_session.get.return_value = SimpleNamespace(
            id=7,
            code="TEST30",
            promo_type="days",
            value=30,
        )
        db_session.execute.side_effect = [
            cohort_query,
            discounts_query,
            broadcasts_query,
        ]
        db_session.query.side_effect = [yk_tariff_query, wata_tariff_query]

        with (
            mock.patch("engine.views.require_support_admin_any", return_value=None),
            mock.patch("engine.views.session_factory", return_value=db_session),
        ):
            request = RequestFactory().get(
                "/support-admin/api/promo-cohort/?promo_id=7"
            )
            response = support_admin_api_promo_cohort(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        result = payload["result"]
        cohort = result["cohort"]
        self.assertEqual(cohort["activations"], 5)
        self.assertEqual(cohort["buyers"], 2)
        self.assertEqual(cohort["repeat_buyers"], 1)
        self.assertEqual(cohort["second_purchase_pct"], 50.0)
        self.assertEqual(cohort["payments"], 3)
        self.assertEqual(cohort["revenue"], 2900)
        self.assertEqual(cohort["revenue_per_activation"], 580)
        self.assertEqual(cohort["active_paid"], 1)
        self.assertEqual(cohort["active_without_purchase"], 1)
        self.assertEqual(cohort["expired_without_purchase"], 1)
        self.assertEqual(cohort["returned_then_churned"], 1)
        self.assertEqual(cohort["blocked"], 1)
        self.assertEqual(
            cohort["active_paid"]
            + cohort["active_without_purchase"]
            + cohort["expired_without_purchase"]
            + cohort["returned_then_churned"]
            + cohort["blocked"],
            cohort["activations"],
        )
        self.assertEqual(
            result["first_payments_by_day"],
            [["2026-08-03", 1], ["2026-08-04", 1]],
        )
        self.assertEqual(
            result["activations_by_day"],
            [["2026-08-01", 2], ["2026-08-02", 3]],
        )
        self.assertEqual(
            result["tariffs"],
            [
                {
                    "id": "month",
                    "name": "1 месяц",
                    "payments": 2,
                    "revenue": 2000,
                    "share_pct": 66.7,
                },
                {
                    "id": "year",
                    "name": "1 год",
                    "payments": 1,
                    "revenue": 900,
                    "share_pct": 33.3,
                },
            ],
        )
        db_session.close.assert_called_once_with()

    def test_template_has_cohort_ui_in_analytics(self):
        """Когорты живут вкладкой в «Аналитике»: обзор, динамика и люди;
        из раздела «Промокоды» кнопки убраны."""
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('data-subtab="promo-cohorts"', template)
        self.assertIn('id="subpanel-promo-cohorts"', template)
        self.assertIn('id="promo-cohort-form"', template)
        self.assertIn('id="promo-cohort-select"', template)
        self.assertIn('id="promo-cohort-chart"', template)
        self.assertIn('id="promo-cohort-chart-empty"', template)
        self.assertIn('id="promo-cohort-card"', template)
        self.assertIn('id="promo-cohort-body"', template)
        self.assertIn("data-promo-cohort-url", template)
        self.assertIn('data-promo-cohort-view="overview"', template)
        self.assertIn('data-promo-cohort-view="dynamics"', template)
        self.assertIn('data-promo-cohort-view="users"', template)
        self.assertIn("Воронка монетизации", template)
        self.assertIn("Состояние когорты сейчас", template)
        self.assertIn("Какие тарифы оплатила когорта", template)
        self.assertIn("Оплатили второй раз", template)
        self.assertIn("second_purchase_pct", template)
        self.assertIn("promo-cohort-tariff-list", template)
        self.assertIn("first_payments_by_day", template)
        self.assertIn("function renderPromoCohort", template)
        self.assertIn("function renderPromoCohortChart", template)
        self.assertIn("function setupPromoCohortViews", template)
        self.assertIn("function loadPromoCohort", template)
        self.assertIn("function loadPromoCohortOptions", template)
        self.assertIn("function openUserCard", template)
        # Из когорты можно провалиться в карточку клиента.
        self.assertIn("data-open-user", template)
        # График рисует канвас-рендер вкладки «Привлечение» через window
        # (прямой вызов из чужого scope — регресс AdminScriptScopeTests).
        self.assertIn("window.acqDraw = acqDraw;", template)
        self.assertIn("window.acqDraw(", template)
        # Кнопок когорты в реестре промокодов больше нет.
        self.assertNotIn('data-promo-cohort="', template)
        self.assertNotIn('data-batch-cohort="', template)
        self.assertNotIn("promo-cohort-close", template)


class AdminScriptScopeTests(SimpleTestCase):
    """Регресс прода: вкладки «Промокоды»/«Сотрудники» падали с «Не удалось
    загрузить», потому что рендер в основном script-блоке звал acqTable из
    другого script-scope (блок аналитики). Основной блок обязан использовать
    только собственные render-хелперы."""

    def _main_script_block(self):
        import re

        template = template_source("engine/templates/admin_dashboard.html")
        blocks = re.findall(r"<script>(.*?)</script>", template, re.S)
        return max(blocks, key=len)

    def test_main_admin_block_does_not_use_foreign_acq_table(self):
        block = self._main_script_block()

        self.assertNotIn("acqTable(", block)
        self.assertIn("function adminTable(", block)

    def test_admin_loaders_render_via_local_helper(self):
        block = self._main_script_block()

        for loader in ("loadPromocodes", "loadStaffAccounts", "loadSysAuditLog",
                       "loadSysSyncMismatches"):
            self.assertIn(loader, block)
        self.assertGreaterEqual(block.count("adminTable("), 5)
        self.assertIn("function renderPromoCodes(", block)
        self.assertIn("function renderPromoBatches(", block)


from django.test import TestCase as _CabTestCase
from engine import views as _cab_views


class CabinetDevicesApiTests(_CabTestCase):
    """HWID-устройства подписки: список и удаление из личного кабинета."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.user = User.objects.create_user(
            username="hwid_tester", password="x"
        )
        self.client.force_login(self.user)
        # Кеш панельного fallback-лимита живёт на модуле — сбрасываем,
        # чтобы тесты не влияли друг на друга.
        _cab_views._hwid_settings_cache.update({"value": None, "expires_at": 0.0})

    def _proto_device(self, hwid="dev-1", model="iPhone 15 Pro"):
        import proto.rwmanager_pb2 as proto

        d = proto.HwidDevice(hwid=hwid, platform="ios", device_model=model)
        d.updated_at.GetCurrentTime()
        return d

    def _subscription(self, uuid="rw-uuid-1", limit=15):
        import proto.rwmanager_pb2 as proto

        sub = proto.UserResponse(uuid=uuid, username="hwid_tester")
        sub.hwid_device_limit = limit
        return sub

    def test_devices_list_ok(self):
        import proto.rwmanager_pb2 as proto
        from unittest.mock import patch

        resp_proto = proto.GetUserHwidDevicesResponse(
            total=1, devices=[self._proto_device()]
        )
        with patch.object(
            _cab_views.rwms_client, "get_user_by_username_strict",
            return_value=self._subscription(),
        ), patch.object(
            _cab_views.rwms_client, "get_user_hwid_devices",
            return_value=resp_proto,
        ) as mocked:
            response = self.client.get("/api/cabinet/devices/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["limit"], 15)
        self.assertEqual(payload["devices"][0]["hwid"], "dev-1")
        self.assertEqual(payload["devices"][0]["device_model"], "iPhone 15 Pro")
        mocked.assert_called_once_with("rw-uuid-1")

    def test_devices_list_limit_panel_fallback(self):
        """Без личного hwid_device_limit кабинет показывает глобальный
        лимит панели (hwidSettings.fallbackDeviceLimit)."""
        import proto.rwmanager_pb2 as proto
        from unittest.mock import patch

        sub = proto.UserResponse(uuid="rw-uuid-1", username="hwid_tester")
        resp_proto = proto.GetUserHwidDevicesResponse(total=0, devices=[])
        hwid_settings = proto.GetHwidSettingsResponse(
            enabled=True, fallback_device_limit=25
        )
        with patch.object(
            _cab_views.rwms_client, "get_user_by_username_strict", return_value=sub
        ), patch.object(
            _cab_views.rwms_client, "get_user_hwid_devices",
            return_value=resp_proto,
        ), patch.object(
            _cab_views.rwms_client, "get_hwid_settings",
            return_value=hwid_settings,
        ):
            response = self.client.get("/api/cabinet/devices/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limit"], 25)

    def test_devices_list_limit_product_fallback(self):
        """Панельные настройки недоступны — остаётся продуктовый лимит."""
        import proto.rwmanager_pb2 as proto
        from unittest.mock import patch

        sub = proto.UserResponse(uuid="rw-uuid-1", username="hwid_tester")
        resp_proto = proto.GetUserHwidDevicesResponse(total=0, devices=[])
        with patch.object(
            _cab_views.rwms_client, "get_user_by_username_strict", return_value=sub
        ), patch.object(
            _cab_views.rwms_client, "get_user_hwid_devices",
            return_value=resp_proto,
        ), patch.object(
            _cab_views.rwms_client, "get_hwid_settings", return_value=None
        ):
            response = self.client.get("/api/cabinet/devices/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limit"], 15)

    def test_devices_list_personal_limit_wins(self):
        """Личный hwid_device_limit подписки важнее панельного fallback."""
        import proto.rwmanager_pb2 as proto
        from unittest.mock import patch

        resp_proto = proto.GetUserHwidDevicesResponse(total=0, devices=[])
        with patch.object(
            _cab_views.rwms_client, "get_user_by_username_strict",
            return_value=self._subscription(limit=5),
        ), patch.object(
            _cab_views.rwms_client, "get_user_hwid_devices",
            return_value=resp_proto,
        ), patch.object(
            _cab_views.rwms_client, "get_hwid_settings",
        ) as settings_mock:
            response = self.client.get("/api/cabinet/devices/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limit"], 5)
        settings_mock.assert_not_called()

    def test_panel_fallback_limit_cached(self):
        """Fallback-лимит панели кешируется — RWMS дёргается один раз."""
        import proto.rwmanager_pb2 as proto
        from unittest.mock import patch

        sub = proto.UserResponse(uuid="rw-uuid-1", username="hwid_tester")
        resp_proto = proto.GetUserHwidDevicesResponse(total=0, devices=[])
        hwid_settings = proto.GetHwidSettingsResponse(
            enabled=True, fallback_device_limit=25
        )
        with patch.object(
            _cab_views.rwms_client, "get_user_by_username_strict", return_value=sub
        ), patch.object(
            _cab_views.rwms_client, "get_user_hwid_devices",
            return_value=resp_proto,
        ), patch.object(
            _cab_views.rwms_client, "get_hwid_settings",
            return_value=hwid_settings,
        ) as settings_mock:
            first = self.client.get("/api/cabinet/devices/")
            second = self.client.get("/api/cabinet/devices/")

        self.assertEqual(first.json()["limit"], 25)
        self.assertEqual(second.json()["limit"], 25)
        settings_mock.assert_called_once()

    def test_devices_list_no_subscription(self):
        from unittest.mock import patch

        with patch.object(
            _cab_views.rwms_client, "get_user_by_username_strict", return_value=None
        ):
            response = self.client.get("/api/cabinet/devices/")
        self.assertEqual(response.status_code, 404)

    def test_devices_list_requires_auth(self):
        self.client.logout()
        response = self.client.get("/api/cabinet/devices/")
        self.assertEqual(response.status_code, 302)

    def test_device_delete_ok(self):
        import proto.rwmanager_pb2 as proto
        from unittest.mock import patch

        resp_proto = proto.DeleteUserHwidDeviceResponse(total=0, devices=[])
        with patch.object(
            _cab_views.rwms_client, "get_user_by_username_strict",
            return_value=self._subscription(),
        ), patch.object(
            _cab_views.rwms_client, "delete_user_hwid_device",
            return_value=resp_proto,
        ) as mocked:
            response = self.client.post(
                "/api/cabinet/devices/delete/", {"hwid": "dev-1"}
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["total"], 0)
        mocked.assert_called_once_with("rw-uuid-1", "dev-1")

    def test_device_delete_requires_hwid(self):
        from unittest.mock import patch

        with patch.object(
            _cab_views.rwms_client, "get_user_by_username_strict",
            return_value=self._subscription(),
        ):
            response = self.client.post("/api/cabinet/devices/delete/", {})
        self.assertEqual(response.status_code, 400)

    def test_device_delete_get_not_allowed(self):
        response = self.client.get("/api/cabinet/devices/delete/")
        self.assertEqual(response.status_code, 405)


class CabinetDevicesRwmsUnavailableTests(_CabTestCase):
    """Недоступность RWMS/панели в HWID-эндпоинтах кабинета: «данные временно
    недоступны» (503), а НЕ «subscription not found» (404) — блип панели
    нельзя показывать как отсутствие подписки."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.user = User.objects.create_user(
            username="hwid_tester_down", password="x"
        )
        self.client.force_login(self.user)
        _cab_views._hwid_settings_cache.update({"value": None, "expires_at": 0.0})

    def _unavailable_patch(self):
        from unittest.mock import patch

        from common.rwms_client import RwmsUnavailableError

        return patch.object(
            _cab_views.rwms_client,
            "get_user_by_username_strict",
            side_effect=RwmsUnavailableError("hwid_tester_down", None, "panel down"),
        )

    def test_devices_list_rwms_unavailable_is_503_not_404(self):
        with self._unavailable_patch():
            response = self.client.get("/api/cabinet/devices/")
        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("временно недоступны", payload["message"])

    def test_device_delete_rwms_unavailable_is_503_not_404(self):
        with self._unavailable_patch():
            response = self.client.post(
                "/api/cabinet/devices/delete/", {"hwid": "dev-1"}
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")


class DashboardRwmsDegradationTests(SimpleTestCase):
    """Оплаченный клиент не должен видеть «истекла» из-за блипа RWMS/панели.

    Политика «БД — истина по времени, панель — истина по существованию ключа»:
    - RwmsUnavailableError → деградация к остатку из user.time_until_expiration
      (кабинет рендерится штатно, has_subscription_access по БД);
    - достоверный NOT_FOUND (strict → None) → прежнее поведение: «истекла».
    """

    def _dashboard_context(
        self, rwms_patch_kwargs, time_left=None, telegram_id=100500, events=None
    ):
        from django.http import HttpResponse

        from engine import views as _views

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def order_by(self, *args, **kwargs):
                return self

            def first(self):
                return None

            def scalar(self):
                return 0

            def all(self):
                return []

        class FakeSession:
            def query(self, *args, **kwargs):
                return FakeQuery()

            def get(self, *args, **kwargs):
                return None

            def commit(self):
                if events is not None:
                    events.append("commit")
                return None

            def rollback(self):
                if events is not None:
                    events.append("rollback")
                return None

            def close(self):
                if events is not None:
                    events.append("close")
                return None

        class SessionDict(dict):
            modified = False

        request = RequestFactory().get("/dashboard/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            id=42,
            username="user-42",
            telegram_id=telegram_id,
            email="user@example.com",
            time_until_expiration=time_left,
        )
        request.session = SessionDict()

        with (
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch.object(
                _views.rwms_client, "get_user_by_username_strict", **rwms_patch_kwargs
            ),
            mock.patch("engine.views.get_runtime_actual_tariffs", return_value=[]),
            mock.patch(
                "engine.views.runtime_int_from_db",
                side_effect=lambda _s, _k, default_value, **kw: default_value,
            ),
            mock.patch(
                "engine.views.build_apple_subscription_link",
                return_value=("happ", "happ://sub"),
            ),
            mock.patch(
                "engine.views.encrypt_happ_url1", return_value="happ://encrypted"
            ),
            mock.patch(
                "engine.views.render", return_value=HttpResponse("ok")
            ) as render_mock,
        ):
            response = _views.dashboard(request)

        self.assertEqual(response.status_code, 200)
        return render_mock.call_args.args[2]

    def test_rwms_unavailable_degrades_to_db_expiration(self):
        from datetime import timedelta

        from common.rwms_client import RwmsUnavailableError

        context = self._dashboard_context(
            {"side_effect": RwmsUnavailableError("user-42", None, "panel down")},
            time_left=timedelta(days=5),
        )
        # Активный по БД юзер НЕ видит «истекла» при недоступности панели.
        self.assertTrue(context["has_subscription_access"])
        self.assertGreater(context["seconds_left"], 0)
        self.assertEqual(context["days_left"], 5)
        self.assertEqual(context["time_left_value"], 5)
        # Панельных данных нет — ссылки установки пустые (для них нужна панель).
        self.assertEqual(context["plain_subscription_url"], "")
        self.assertEqual(context["happ_subscription_url"], "")
        self.assertEqual(context["apple_subscription_url"], "")

    def test_confirmed_not_found_still_shows_expired(self):
        from datetime import timedelta

        context = self._dashboard_context(
            {"return_value": None}, time_left=timedelta(days=5)
        )
        # Достоверный NOT_FOUND: подписки в панели нет — «истекла», как раньше.
        self.assertFalse(context["has_subscription_access"])
        self.assertEqual(context["seconds_left"], -1)
        self.assertEqual(context["days_left"], 0)
        self.assertEqual(context["time_left_value"], 0)

    def test_bind_link_is_committed_only_after_main_session_is_closed(self):
        # commit в основной сессии истекал бы traffic_progress/support_ticket,
        # и обращение к ним после close падало бы DetachedInstanceError.
        from datetime import timedelta

        events = []
        with mock.patch(
            "engine.views.get_or_create_telegram_bind_link",
            return_value="https://t.me/test_bot?start=bind_42_token",
        ) as issue:
            context = self._dashboard_context(
                {"return_value": None},
                time_left=timedelta(days=5),
                telegram_id=None,
                events=events,
            )

        issue.assert_called_once()
        self.assertEqual(
            context["tg_bind_link"], "https://t.me/test_bot?start=bind_42_token"
        )
        self.assertTrue(context["show_telegram_bind_banner"])
        self.assertEqual(events.count("commit"), 1)
        self.assertLess(events.index("close"), events.index("commit"))

    def test_bind_link_write_failure_renders_dashboard_without_banner(self):
        from datetime import timedelta

        from sqlalchemy.exc import OperationalError

        events = []
        with mock.patch(
            "engine.views.get_or_create_telegram_bind_link",
            side_effect=OperationalError("INSERT", {}, Exception("read-only")),
        ), self.assertLogs(level="ERROR"):
            context = self._dashboard_context(
                {"return_value": None},
                time_left=timedelta(days=5),
                telegram_id=None,
                events=events,
            )

        self.assertIsNone(context["tg_bind_link"])
        self.assertFalse(context["show_telegram_bind_banner"])
        self.assertNotIn("commit", events)
        self.assertIn("rollback", events)

    def test_rwms_unavailable_does_not_grant_access_to_expired_db_user(self):
        from datetime import timedelta

        from common.rwms_client import RwmsUnavailableError

        context = self._dashboard_context(
            {"side_effect": RwmsUnavailableError("user-42", None, "down")},
            time_left=timedelta(seconds=-100),
        )
        # Деградация не «дарит» доступ: истёкший по БД остаётся истёкшим.
        self.assertFalse(context["has_subscription_access"])
        self.assertEqual(context["days_left"], 0)

    def test_normal_flow_uses_panel_subscription_url(self):
        from datetime import timedelta

        sub = SimpleNamespace(
            subscription_url="https://sub.example/u",
            HasField=lambda _field_name: False,
        )
        context = self._dashboard_context(
            {"return_value": sub}, time_left=timedelta(days=3)
        )
        self.assertTrue(context["has_subscription_access"])
        self.assertEqual(
            context["plain_subscription_url"], "https://sub.example/u"
        )
        self.assertEqual(context["days_left"], 3)


class PurchasePaymentStatusFixtureMixin:
    """Общая фикстура для тестов ``get_purchase_payment_status``."""

    ORDER_ID = "order-1"

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(
            self.engine,
            tables=[
                WataInvoice.__table__,
                WataTransaction.__table__,
                YkPayment.__table__,
            ],
        )
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        self.addCleanup(self.session.close)

    def _login_token(self, payment_reference=ORDER_ID):
        return SimpleNamespace(
            user_id=42,
            created_at=datetime(2026, 8, 26, 10, 0, 0),
            payment_gateway="wata",
            payment_reference=payment_reference,
        )

    def _add_invoice(self, order_id=ORDER_ID, expires_at=datetime(2030, 1, 1)):
        self.session.add(
            WataInvoice(
                id=1,
                user_id=42,
                invoice_id=f"inv-{order_id}",
                amount=299,
                currency="RUB",
                status="Opened",
                url="https://wata.example/pay",
                terminal_name="t",
                terminal_public_id="tp",
                creation_time=datetime(2026, 8, 26, 10, 1, 0),
                order_id=order_id,
                expiration_datetime=expires_at,
                tariff_id="month",
            )
        )
        self.session.flush()

    def _add_transaction(self, row_id, status, payment_time, order_id=ORDER_ID):
        self.session.add(
            WataTransaction(
                id=row_id,
                transaction_id=f"tx-{row_id}",
                transaction_type="Payment",
                terminal_public_id="tp",
                transaction_status=status,
                terminal_name="t",
                amount=299,
                currency="RUB",
                order_id=order_id,
                order_description="месяц",
                commission=0,
                payment_time=payment_time,
            )
        )
        self.session.flush()


class PurchasePaymentOutcomeTests(PurchasePaymentStatusFixtureMixin, SimpleTestCase):
    """P1: исход заказа — ФАКТ оплаты, а не «последняя по времени» транзакция.

    Declined-строку платёжный сервис добирает из вебхука с
    ``payment_time`` = ВРЕМЕНЕМ ОТКАЗА (monkey-island-payment,
    ``_add_wata_transaction``). Поэтому у заказа с несколькими попытками
    отклонённая попытка легко оказывается «последней» уже ПОСЛЕ успешной
    оплаты, и оплатившему показывалось «платёж не прошёл» — вместо ссылки в
    кабинет. Оплаченный заказ обязан быть успешным независимо от времени
    прочих попыток."""

    def test_paid_attempt_wins_over_later_declined_attempt(self):
        """Попытка B оплачена в T3, попытка A отклонена в T4 > T3."""
        self._add_invoice()
        self._add_transaction(1, "Paid", datetime(2026, 8, 26, 10, 3, 0))
        self._add_transaction(2, "Declined", datetime(2026, 8, 26, 10, 4, 0))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "succeeded")
        self.assertEqual(message, "Платеж прошел успешно")

    def test_paid_attempt_wins_without_invoice_row(self):
        """Зеркальный путь без строки инвойса: заказ известен только из токена."""
        self._add_transaction(1, "Paid", datetime(2026, 8, 26, 10, 2, 0))
        self._add_transaction(2, "Declined", datetime(2026, 8, 26, 10, 9, 0))

        status, _message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "succeeded")

    def test_only_declined_transactions_still_fail(self):
        self._add_invoice()
        self._add_transaction(1, "Declined", datetime(2026, 8, 26, 10, 3, 0))
        self._add_transaction(2, "Declined", datetime(2026, 8, 26, 10, 4, 0))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "failed")
        self.assertEqual(message, "Платеж не прошел")

    def test_no_transactions_keeps_previous_behaviour(self):
        self._add_invoice()

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "pending")
        self.assertEqual(message, "Ждем подтверждения платежа")

    def test_expired_invoice_without_transactions_still_fails(self):
        self._add_invoice(expires_at=datetime(2020, 1, 1))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "failed")
        self.assertEqual(message, "Время оплаты истекло")

    def test_paid_transaction_of_another_order_is_not_counted(self):
        """Чужой заказ не делает эту покупку успешной."""
        self._add_invoice()
        self._add_transaction(
            1, "Paid", datetime(2026, 8, 26, 10, 3, 0), order_id="other-order"
        )
        self._add_transaction(2, "Declined", datetime(2026, 8, 26, 10, 4, 0))

        status, _message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "failed")

    def test_yookassa_token_does_not_use_another_payment_of_same_user(self):
        self.session.add(
            YkPayment(
                id=1,
                user_id=42,
                amount=299,
                currency="RUB",
                status="succeeded",
                created_at=datetime(2026, 8, 26, 10, 1, 0),
                payment_id="other-payment",
                subscription_period="month",
            )
        )
        self.session.flush()
        token = SimpleNamespace(
            user_id=42,
            created_at=datetime(2026, 8, 26, 10, 0, 0),
            payment_gateway="yookassa",
            payment_reference="expected-payment",
        )

        status, _message = get_purchase_payment_status(self.session, token)

        self.assertEqual(status, "pending")

    def test_wata_token_does_not_use_recent_invoice_of_same_user(self):
        self._add_invoice(order_id="other-order")
        self._add_transaction(
            1,
            "Paid",
            datetime(2026, 8, 26, 10, 3, 0),
            order_id="other-order",
        )

        status, _message = get_purchase_payment_status(
            self.session, self._login_token(payment_reference="expected-order")
        )

        self.assertEqual(status, "pending")


class PurchasePaymentNonTerminalStatusTests(
    PurchasePaymentStatusFixtureMixin, SimpleTestCase
):
    """P1: НЕтерминальный статус транзакции — это ожидание, а не отказ.

    У Wata терминальны только ``Paid`` и ``Declined``. Промежуточные
    ``Created``/``Pending`` (СБП, 3DS) платёжный сервис сохраняет отдельной
    строкой (monkey-island-payment/wata_webhook_handler.py, ветка «прочие
    статусы» -> ``_add_wata_transaction``) и лишь потом переводит их в ``Paid``
    вторым вебхуком.

    Раньше обе fallback-ветки возвращали ``failed`` для ЛЮБОГО статуса != Paid,
    и человек, который в этот момент открыл /payment/status/<token>/, видел
    «Платеж не прошел». Шаблон payment_status.html на ``failed`` НАВСЕГДА
    останавливает опрос, шлёт цель ``payment_failed`` и прячет кнопку входа —
    пришедший через 10-30 секунд ``Paid`` пользователь уже не увидел бы."""

    def test_pending_transaction_is_not_a_failure(self):
        self._add_invoice()
        self._add_transaction(1, "Pending", datetime(2026, 8, 26, 10, 3, 0))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "pending")
        self.assertEqual(message, "Ждем подтверждения платежа")

    def test_created_transaction_is_not_a_failure(self):
        self._add_invoice()
        self._add_transaction(1, "Created", datetime(2026, 8, 26, 10, 3, 0))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "pending")
        self.assertEqual(message, "Ждем подтверждения платежа")

    def test_pending_transaction_without_invoice_row_is_not_a_failure(self):
        """Зеркальная ветка: заказ известен только из токена, строки инвойса нет."""
        self._add_transaction(1, "Pending", datetime(2026, 8, 26, 10, 3, 0))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "pending")
        self.assertEqual(message, "Ждем подтверждения платежа")

    def test_pending_then_paid_same_row_is_succeeded(self):
        """Реальный путь: ``_mark_wata_transaction_paid`` обновляет ту же строку."""
        self._add_invoice()
        self._add_transaction(1, "Pending", datetime(2026, 8, 26, 10, 3, 0))

        status, _message = get_purchase_payment_status(
            self.session, self._login_token()
        )
        self.assertEqual(status, "pending")

        transaction = (
            self.session.query(WataTransaction)
            .filter(WataTransaction.id == 1)
            .one()
        )
        transaction.transaction_status = "Paid"
        transaction.payment_time = datetime(2026, 8, 26, 10, 3, 30)
        self.session.flush()

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "succeeded")
        self.assertEqual(message, "Платеж прошел успешно")

    def test_pending_then_paid_separate_row_is_succeeded(self):
        self._add_invoice()
        self._add_transaction(1, "Pending", datetime(2026, 8, 26, 10, 3, 0))
        self._add_transaction(2, "Paid", datetime(2026, 8, 26, 10, 3, 30))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "succeeded")
        self.assertEqual(message, "Платеж прошел успешно")

    def test_declined_transaction_still_fails(self):
        """Явный отказ остаётся отказом — поведение не ослаблено."""
        self._add_invoice()
        self._add_transaction(1, "Declined", datetime(2026, 8, 26, 10, 3, 0))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "failed")
        self.assertEqual(message, "Платеж не прошел")

    def test_declined_without_invoice_row_still_fails(self):
        self._add_transaction(1, "Declined", datetime(2026, 8, 26, 10, 3, 0))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "failed")
        self.assertEqual(message, "Платеж не прошел")

    def test_expired_invoice_without_transactions_keeps_failing(self):
        """Ветка истёкшего инвойса не тронута: отказ там обоснован."""
        self._add_invoice(expires_at=datetime(2020, 1, 1))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "failed")
        self.assertEqual(message, "Время оплаты истекло")

    def test_just_expired_invoice_with_pending_transaction_waits(self):
        """Начатый платёж важнее срока инвойса — но только в пределах grace.

        ``Paid`` по СБП приходит вторым вебхуком через десятки секунд и вполне
        может прийти уже после ``expiration_datetime``. Закрывать экран сразу
        значило бы воспроизвести ту же ошибку: опрос встал бы навсегда и
        итоговый ``Paid`` пользователь бы не увидел."""
        from datetime import timedelta as _timedelta

        self._add_invoice(expires_at=datetime.utcnow() - _timedelta(minutes=1))
        self._add_transaction(1, "Pending", datetime(2026, 8, 26, 10, 3, 0))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "pending")
        self.assertEqual(message, "Ждем подтверждения платежа")

    def test_long_expired_invoice_with_stuck_pending_transaction_stops_waiting(self):
        """Обратная сторона: застрявшая нетерминальная строка (терминальный
        вебхук так и не пришёл) не должна крутить опрос вечно — после grace
        экран закрывается понятным «время истекло»."""
        self._add_invoice(expires_at=datetime(2020, 1, 1))
        self._add_transaction(1, "Pending", datetime(2026, 8, 26, 10, 3, 0))

        status, message = get_purchase_payment_status(
            self.session, self._login_token()
        )

        self.assertEqual(status, "failed")
        self.assertEqual(message, "Время оплаты истекло")

    def test_paid_wins_even_for_long_expired_invoice(self):
        """Оплата важнее любого срока: факт Paid проверяется до всех веток."""
        self._add_invoice(expires_at=datetime(2020, 1, 1))
        self._add_transaction(1, "Paid", datetime(2026, 8, 26, 10, 3, 0))

        status, _ = get_purchase_payment_status(self.session, self._login_token())

        self.assertEqual(status, "succeeded")


@override_settings(SITE_TRIAL_REGISTRATION_ENABLED=True, SITE_TRIAL_PERIOD_DAYS=7)
class SiteRegistrationLocalRowOwnershipTests(SimpleTestCase):
    """P1: guard владельца ЛОКАЛЬНОЙ строки живёт в ``sync_local_user_from_rwms``
    и потому работает на ВСЕХ путях, которые туда ведут.

    Сценарий один и тот же: жертва зарегистрировалась на адрес A, позже сменила
    почту на B; панель осталась с A (её обновление best-effort). Новый владелец
    адреса A вводит его в форму — имя детерминировано от A, панельный guard
    проходит, но строка users под этим именем принадлежит уже адресу B.

    Путей в ``sync_local_user_from_rwms`` четыре: adoption по email, adoption по
    telegram_id, ветка «триал выключен» и восстановление после провала
    ``create_user``. Проверка обязана оставаться в самой функции: перенос её в
    одну лишь ветку adoption оставит три других пути незакрытыми."""

    EMAIL = "reused@example.com"
    CONTEXT = {"referrer": None, "traffic_source": 42, "ymid": None}
    VICTIM_EMAIL = "victim-new@example.com"

    def _victim(self, telegram_id=None, email=None):
        victim = User(
            email=self.VICTIM_EMAIL if email is None else email,
            username=deterministic_username(self.EMAIL),
            telegram_id=telegram_id,
        )
        victim.id = 777
        return victim

    def _panel_record(self, username=None):
        return _FakeSiteRwUser(
            username=username or deterministic_username(self.EMAIL),
            email=self.EMAIL,  # панель осталась со СТАРЫМ адресом жертвы
        )

    def _assert_nothing_touched(self, session, victim, client=None):
        self.assertEqual(session.added, [])
        self.assertEqual(victim.email, self.VICTIM_EMAIL)
        if client is not None:
            client.add_user.assert_not_called()
            client.update_user.assert_not_called()

    def test_email_adoption_refuses_foreign_row_even_when_it_has_telegram_id(self):
        """Мутация «...and user.telegram_id is None» в условии обязана падать:
        привязанный телеграм жертвы НЕ делает захват аккаунта допустимым."""
        victim = self._victim(telegram_id=555000)
        session = _SiteRegistrationSessionWithExistingUser(victim)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = self._panel_record()

        with self.assertLogs(level="CRITICAL") as captured_logs:
            with self.assertRaises(SiteRegistrationOwnershipConflict):
                with mock.patch(
                    "engine.views.get_registration_context",
                    return_value=dict(self.CONTEXT),
                ), mock.patch("engine.views.rwms_client", client), mock.patch(
                    "engine.views.create_user"
                ) as create_rwms_user:
                    create_site_user(
                        session,
                        self.EMAIL,
                        SimpleNamespace(),
                        creation_channel="site_magic_link",
                    )

        self.assertTrue(any("ALERT:" in line for line in captured_logs.output))
        create_rwms_user.assert_not_called()
        self._assert_nothing_touched(session, victim, client)
        self.assertEqual(victim.telegram_id, 555000)

    @override_settings(SITE_TRIAL_REGISTRATION_ENABLED=False)
    def test_disabled_trial_path_refuses_foreign_local_row(self):
        """Ветка «триал выключен»: подписка ищется по identity, но чужую строку
        users принимать всё так же нельзя."""
        victim = self._victim()
        session = _SiteRegistrationSessionWithExistingUser(victim)

        with self.assertLogs(level="CRITICAL") as captured_logs:
            with self.assertRaises(SiteRegistrationOwnershipConflict):
                with mock.patch(
                    "engine.views.get_registration_context",
                    return_value=dict(self.CONTEXT),
                ), mock.patch(
                    "engine.views.find_rwms_user_by_identity",
                    return_value=self._panel_record(),
                ), mock.patch(
                    "engine.views.create_user"
                ) as create_rwms_user:
                    create_site_user(
                        session,
                        self.EMAIL,
                        SimpleNamespace(),
                        creation_channel="site_magic_link",
                    )

        self.assertTrue(any("ALERT:" in line for line in captured_logs.output))
        create_rwms_user.assert_not_called()
        self._assert_nothing_touched(session, victim)

    def test_recovery_after_failed_create_refuses_foreign_local_row(self):
        """Восстановление после провала ``create_user``: найденная по identity
        подписка тоже не даёт права занять чужую строку users."""
        victim = self._victim()
        session = _SiteRegistrationSessionWithExistingUser(victim)

        with self.assertLogs(level="CRITICAL") as captured_logs:
            with self.assertRaises(SiteRegistrationOwnershipConflict):
                with mock.patch(
                    "engine.views.get_registration_context",
                    return_value=dict(self.CONTEXT),
                ), mock.patch(
                    "engine.views.resolve_existing_site_subscription",
                    side_effect=[None, self._panel_record()],
                ), mock.patch(
                    "engine.views.create_user", return_value=None
                ):
                    create_site_user(
                        session,
                        self.EMAIL,
                        SimpleNamespace(),
                        creation_channel="site_magic_link",
                    )

        self.assertTrue(any("ALERT:" in line for line in captured_logs.output))
        self._assert_nothing_touched(session, victim)

    def test_telegram_adoption_refuses_row_of_another_telegram_account(self):
        """Тот же guard на telegram-пути: строка users под именем ``42``,
        привязанная к другому telegram_id, не отдаётся текущему входу."""
        foreign_row = User(email=None, username="42", telegram_id=999)
        foreign_row.id = 778
        session = _SiteRegistrationSessionWithExistingUser(foreign_row)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            username="42", telegram_id=42
        )

        with self.assertLogs(level="CRITICAL") as captured_logs:
            with self.assertRaises(SiteRegistrationOwnershipConflict):
                with mock.patch(
                    "engine.views.get_registration_context",
                    return_value=dict(self.CONTEXT),
                ), mock.patch("engine.views.rwms_client", client), mock.patch(
                    "engine.views.create_user"
                ) as create_rwms_user:
                    create_site_user(
                        session,
                        None,
                        SimpleNamespace(),
                        telegram_id=42,
                        creation_channel="site_telegram_widget",
                    )

        self.assertTrue(any("ALERT:" in line for line in captured_logs.output))
        create_rwms_user.assert_not_called()
        client.add_user.assert_not_called()
        self.assertEqual(session.added, [])
        self.assertEqual(foreign_row.telegram_id, 999)

    def test_same_owner_row_is_still_adopted_on_every_path(self):
        """Обратная сторона guard'а: своя строка принимается на всех путях —
        crash-window recovery не сломан ни на одном из них."""
        for path in ("adoption", "trial_disabled", "recovery"):
            with self.subTest(path=path):
                own_row = self._victim(email=self.EMAIL)
                session = _SiteRegistrationSessionWithExistingUser(own_row)
                client = mock.Mock()
                client.get_user_by_username_strict.return_value = self._panel_record()

                patches = [
                    mock.patch(
                        "engine.views.get_registration_context",
                        return_value=dict(self.CONTEXT),
                    ),
                    mock.patch("engine.views.rwms_client", client),
                    mock.patch("engine.views.add_user_to_traffic_progress"),
                    mock.patch("engine.views.add_event_log"),
                    mock.patch("engine.views.create_user", return_value=None),
                ]
                if path != "adoption":
                    patches.append(
                        mock.patch(
                            "engine.views.find_rwms_user_by_identity",
                            return_value=self._panel_record(),
                        )
                    )
                if path == "recovery":
                    patches.append(
                        mock.patch(
                            "engine.views.resolve_existing_site_subscription",
                            return_value=None,
                        )
                    )

                with ExitStack() as stack:
                    stack.enter_context(
                        override_settings(
                            SITE_TRIAL_REGISTRATION_ENABLED=(path != "trial_disabled")
                        )
                    )
                    for patcher in patches:
                        stack.enter_context(patcher)
                    user = create_site_user(
                        session,
                        self.EMAIL,
                        SimpleNamespace(),
                        creation_channel="site_magic_link",
                    )

                self.assertIs(user, own_row)
                self.assertEqual(session.added, [])


class SiteRegistrationOwnershipMessageTests(SimpleTestCase):
    """P2: конфликт владельца — не «сервис временно недоступен».

    Повтор выведет ТО ЖЕ детерминированное имя и упрётся в тот же guard,
    поэтому «повторите через пару минут» отправляло пользователя в бесконечный
    цикл. Такому пользователю нужен оператор."""

    def test_ownership_conflict_is_a_registration_failure_subtype(self):
        # Обратная совместимость: любой существующий обработчик
        # SiteRegistrationUnavailable продолжает ловить конфликт владельца
        # (регистрация останавливается, подписка не создаётся).
        self.assertTrue(
            issubclass(SiteRegistrationOwnershipConflict, SiteRegistrationUnavailable)
        )
        self.assertNotEqual(
            SITE_REGISTRATION_SUPPORT_MESSAGE, SITE_REGISTRATION_RETRY_MESSAGE
        )
        self.assertIn("поддержку", SITE_REGISTRATION_SUPPORT_MESSAGE)
        self.assertNotIn("через пару минут", SITE_REGISTRATION_SUPPORT_MESSAGE)

    def test_unavailable_docstring_no_longer_promises_a_successful_retry(self):
        # Докстринг обязан отражать, что «следующая попытка продолжит с того же
        # места» — это про временный отказ, а не про конфликт владельца.
        docstring = SiteRegistrationUnavailable.__doc__ or ""
        self.assertIn("SiteRegistrationOwnershipConflict", docstring)
        self.assertIn("ВРЕМЕННЫЙ", docstring)

    def _magic_link_session(self):
        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return None

        class FakeBegin:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeSession:
            def begin(self):
                return FakeBegin()

            def query(self, *args, **kwargs):
                return FakeQuery()

            def add(self, obj):
                return None

            def close(self):
                return None

        return FakeSession()

    def test_confirmed_registration_sends_ownership_conflict_to_support(self):
        request = RequestFactory().get("/login/register/token/")
        request.session = {}
        request.user = SimpleNamespace(is_authenticated=False)
        token = views.build_site_registration_token("reused@example.com", {})

        with mock.patch(
            "engine.views.session_factory", return_value=self._magic_link_session()
        ), mock.patch(
            "engine.views.create_site_user",
            side_effect=SiteRegistrationOwnershipConflict("row belongs to other"),
        ):
            response = views.auth_by_registration_link(request, token)

        self.assertEqual(response.status_code, 409)
        self.assertIn(SITE_REGISTRATION_SUPPORT_MESSAGE, response.content.decode())

    @override_settings(PAYMENT_GATEWAY="wata")
    def test_pay_sends_ownership_conflict_to_support(self):
        tariff = SimpleNamespace(price=100, db_tariff_id="month", description="1 месяц")

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def with_for_update(self, **kwargs):
                # FINAL-PAY-03: анонимная ветка читает users FOR UPDATE.
                return self

            def first(self):
                return None

        class FakeSession:
            def query(self, model):
                return FakeQuery()

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        request = RequestFactory().post(
            "/pay/",
            {"email": "reused@example.com", "tariff_id": "month"},
            HTTP_HOST="example.com",
            HTTP_X_PAYMENT_LAUNCH="new-tab",
        )
        request.user = SimpleNamespace(is_authenticated=False, id=None)
        request.session = SessionDict()

        with (
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.get_runtime_actual_tariffs", return_value=[tariff]),
            mock.patch(
                "engine.views.create_site_user",
                side_effect=SiteRegistrationOwnershipConflict("row belongs to other"),
            ),
            mock.patch("engine.views.create_wata_payment_sync") as create_invoice,
        ):
            response = pay(request)

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.content)
        self.assertEqual(payload["message"], SITE_REGISTRATION_SUPPORT_MESSAGE)
        create_invoice.assert_not_called()


from engine import views  # noqa: E402  (локальный импорт для тестов ниже)
from common.models.db import PurchaseLoginToken  # noqa: E402


class PurchaseLinkAuthRequiresPaymentTests(SimpleTestCase):
    """CRITICAL: only the separate email-delivered token can authenticate."""

    LOGIN_TOKEN = "plogin_test-token"

    def _run_auth(self, payment_status):
        token_row = SimpleNamespace(
            user_id=42,
            token_hash=views.hash_purchase_login_token(self.LOGIN_TOKEN),
            revoked_at=None,
            last_used_at=None,
            payment_gateway="wata",
            payment_reference="order-1",
            created_at=datetime(2026, 8, 26, 10, 0, 0),
        )
        victim = SimpleNamespace(id=42, email="victim@example.com")

        class _Q:
            def __init__(self, result):
                self.__result = result

            def filter(self, *a, **kw):
                return self

            def first(self):
                return self.__result

        class _Session:
            def __init__(self):
                self.committed = False

            def query(self, model):
                return _Q(token_row if model is PurchaseLoginToken else victim)

            def commit(self):
                self.committed = True

            def close(self):
                pass

        session = _Session()
        authorized = []

        with mock.patch.object(views, "session_factory", return_value=session), \
                mock.patch.object(
                    views,
                    "get_purchase_payment_status",
                    return_value=(payment_status, "msg"),
                ), \
                mock.patch.object(
                    views,
                    "authorize_user_session",
                    side_effect=lambda r, u: authorized.append(u),
                ), \
                mock.patch.object(views, "add_event_log_once"), \
                mock.patch.object(
                    views, "render_login", side_effect=lambda r, ctx: ("login", ctx)
                ), \
                mock.patch.object(views, "redirect", side_effect=lambda name: ("redirect", name)):
            result = views.auth_by_purchase_link(mock.Mock(), self.LOGIN_TOKEN)

        return result, authorized, session

    def test_unpaid_token_does_not_authorize(self):
        for status in ("pending", "failed"):
            with self.subTest(status=status):
                result, authorized, session = self._run_auth(status)
                self.assertEqual(result[0], "login")
                self.assertEqual(authorized, [], "вход по неоплаченному токену")
                self.assertFalse(session.committed)

    def test_paid_token_authorizes_as_before(self):
        result, authorized, _ = self._run_auth("succeeded")

        self.assertEqual(result, ("redirect", "dashboard"))
        self.assertEqual([u.id for u in authorized], [42])

    def test_browser_visible_status_token_never_authorizes(self):
        request = mock.Mock()
        with mock.patch.object(views, "session_factory") as session_factory, \
                mock.patch.object(
                    views, "render_login", side_effect=lambda r, ctx: ("login", ctx)
                ):
            session_factory.return_value.close.return_value = None
            result = views.auth_by_purchase_link(request, "pstatus_test-token")

        self.assertEqual(result[0], "login")
        session_factory.return_value.query.assert_not_called()

    def test_legacy_unprefixed_token_fails_closed(self):
        request = mock.Mock()
        with mock.patch.object(views, "session_factory") as session_factory, \
                mock.patch.object(
                    views, "render_login", side_effect=lambda r, ctx: ("login", ctx)
                ):
            session_factory.return_value.close.return_value = None
            result = views.auth_by_purchase_link(request, "legacy-token")

        self.assertEqual(result[0], "login")
        # Старая ссылка никогда не авторизует и не трогает БД, но объясняет,
        # как войти (PAY-05/AUTH-04/TR-02).
        self.assertEqual(result[1]["error"], views.LEGACY_PURCHASE_LOGIN_MESSAGE)
        self.assertIn("Подписка продолжает работать", result[1]["error"])
        session_factory.assert_not_called()
        session_factory.return_value.query.assert_not_called()


class InfraServersDashboardTemplateTests(SimpleTestCase):
    """Регрессии нового UX «Инфраструктура → Серверы»."""

    def setUp(self):
        self.template = template_source("engine/templates/admin_dashboard.html")

    def test_servers_overview_has_filters_table_and_preview(self):
        for marker in (
            'class="infra-page-head"',
            'id="infra-search"',
            'id="infra-status-filter"',
            'id="infra-summary"',
            'class="infra-workspace"',
            'id="infra-servers-table"',
            "data-infra-open-detail=",
            "data-infra-preview-open",
            "function selectInfraServer(serverId)",
            "function renderInfraPreview(server, detail = null, loadFailed = false)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        # Серверы регистрирует node-agent: UI не должен обещать ручное создание.
        self.assertNotIn("+ Добавить сервер", self.template)

    def test_server_tiles_are_the_only_view(self):
        # Утверждённый компактный концепт (10.09.2026): один вид — плитки;
        # табличный рендер и переключатель видов удалены
        for marker in (
            'class="infra-server-grid"',
            'class="infra-server-card${cardTone}"',
            'class="infra-server-card-dot${dotTone}"',
            'class="infra-server-card-traffic"',
            'class="infra-server-card-meta"',
            "function infraServerPresentation(server)",
            "function renderInfraServerCards(servers)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        # Карточки не подменяют реальные метрики декоративными данными.
        for metric in (
            "infraFmtBps(server.rx_bps)",
            "infraFmtBps(server.tx_bps)",
            "server.tcp_connections.toLocaleString('ru-RU')",
            "server.effective_limit_mbps",
            "infraFmtAge(server.last_seen_age)",
        ):
            with self.subTest(metric=metric):
                self.assertIn(metric, self.template)

        for gone in (
            "renderInfraServerTable",
            "data-infra-view",
            "infraServersView",
            "INFRA_SERVERS_VIEW_STORAGE_KEY",
            "infra-view-switch",
        ):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.template)

    def test_server_view_preference_and_card_interactions_are_preserved(self):
        for marker in (
            "event.target !== serverElement",
            "focus({preventScroll: true})",
            'data-infra-menu="${server.id}"',
            'data-infra-open-detail="${server.id}"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        for css_marker in (
            ".infra-server-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));",
            ".infra-server-card:hover",
            'html[data-admin-theme="light"] .infra-server-card',
        ):
            with self.subTest(css_marker=css_marker):
                self.assertIn(css_marker, self.template)

    def test_server_opens_as_nested_screen_without_covering_admin_navigation(self):
        for marker in (
            'id="infra-servers-overview"',
            'class="infra-server-screen" id="infra-server-screen"',
            'id="infra-server-detail"',
            'class="infra-server-screen-head"',
            '<i class="fas fa-arrow-left"></i>Назад',
            "document.getElementById('infra-detail-close')?.addEventListener('click', closeInfraServer);",
            'id="infra-traffic-chart"',
            'id="infra-conn-chart"',
            "['3h', '24h', '7d', '30d']",
            "overview.hidden = true;",
            "screen.hidden = false;",
            "screen.setAttribute('aria-hidden', 'false');",
            "window.scrollTo({top: infraOverviewScrollTop, behavior: 'auto'});",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        open_start = self.template.index("async function openInfraServer(serverId)")
        open_end = self.template.index("function closeInfraServer()", open_start)
        open_block = self.template[open_start:open_end]
        self.assertNotIn("scrollIntoView", open_block)
        self.assertIn("infra-detail-loading", open_block)
        self.assertNotIn("lockBodyScroll", open_block)
        self.assertNotIn('id="infra-detail-modal"', self.template)
        screen_markup = self.template[
            self.template.index('<section class="infra-server-screen"') :
            self.template.index("</section>", self.template.index('<section class="infra-server-screen"'))
        ]
        self.assertLess(screen_markup.index('id="infra-detail-close"'), screen_markup.index('id="infra-server-detail"'))
        self.assertLess(
            self.template.index('id="infra-server-screen"'),
            self.template.index("</main>"),
        )

    def test_servers_use_centered_canvas_and_readable_card_spacing(self):
        for marker in (
            "#subpanel-inf-servers { width: 100%; max-width: 1520px; margin-inline: auto; }",
            ".infra-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-bottom: 18px; }",
            ".infra-server-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; padding: 14px; }",
            ".infra-detail-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }",
            ".infra-detail-stat-sub { margin-top: 8px; color: var(--muted); font-size: 10.5px; line-height: 1.35; font-weight: 700; overflow-wrap: anywhere; }",
            ".infra-detail-chart-card { margin-bottom: 20px;",
            ".infra-detail-who { margin-bottom: 20px;",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        stat_rule_start = self.template.index(".infra-detail-stat-value {")
        stat_rule_end = self.template.index("}", stat_rule_start)
        stat_rule = self.template[stat_rule_start:stat_rule_end]
        self.assertNotIn("text-overflow: ellipsis", stat_rule)
        self.assertNotIn("overflow: hidden", stat_rule)

    def test_background_refresh_preserves_nested_screen_scroll_position(self):
        self.assertIn("const previousScrollTop = window.scrollY;", self.template)
        self.assertIn(
            "window.scrollTo({top: previousScrollTop, behavior: 'auto'});",
            self.template,
        )
        self.assertIn(
            "if (!full && container.contains(document.activeElement)", self.template
        )

    def test_server_ips_are_grouped_by_interface_without_losing_actions(self):
        for marker in (
            "function infraGroupIpsByInterface(ips, wanInterface)",
            'class="infra-ip-groups"',
            'class="infra-ip-group${group.ips.length > 2',
            "data-infra-interface=",
            "Резерв / не назначены",
            "left.isReserve ? 1 : -1",
            "leftIsWan ? -1 : 1",
            "data-infra-ensure-ip=",
            "data-infra-replace-ip=",
            "data-infra-unblock-ip=",
            "data-infra-block-ip=",
            "data-infra-delete-ip=",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_original_server_controls_remain_visible_and_discoverable(self):
        for marker in (
            'id="infra-force-check"',
            'id="infra-snooze-toggle"',
            'id="infra-archive-toggle"',
            'id="infra-server-edit-form"',
            'id="infra-add-ip-form"',
            'id="infra-add-domain-form"',
            'id="infra-traffic-chart"',
            'id="infra-conn-chart"',
            "data-infra-period=",
            'data-infra-detail-target="infra-detail-parameters"',
            'data-infra-detail-target="infra-detail-ips"',
            'data-infra-detail-target="infra-detail-domains"',
            'data-infra-detail-target="infra-detail-journal"',
            "window.scrollTo({top: Math.max(0, Math.round(nextTop)), behavior: 'auto'});",
            "Управление и графики",
            "ТСПУ и детектор",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_server_header_actions_keep_stable_geometry_while_tspu_action_runs(self):
        # Длина pending-подписи не должна переносить период и
        # перестраивать всю hero-шапку во время POST-запроса.
        for marker in (
            ".infra-detail-hero-actions { display: grid; grid-template-columns: repeat(4, max-content);",
            ".infra-period-switch { grid-column: 1 / -1; justify-self: end;",
            ".infra-detail-hero-actions { width: 100%; grid-template-columns: repeat(2, minmax(0, 1fr));",
            ".infra-period-switch { grid-column: 1 / -1; grid-row: 1; width: 100%;",
            "button.classList.contains('infra-detail-action')",
            "button.style.inlineSize = `${stableWidth}px`;",
            "button.style.minInlineSize = `${stableWidth}px`;",
            "button.style.inlineSize = button.dataset.infraIdleInlineSize;",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_server_forms_use_styled_controls_and_keep_field_contracts(self):
        for marker in (
            'class="infra-form"',
            'class="infra-form-field"',
            'class="infra-control"',
            'class="infra-control infra-prefix-control"',
            'class="btn infra-form-submit"',
            'name="display_name"',
            'name="bandwidth_limit_mbps"',
            'name="notes"',
            'name="ip"',
            'name="prefix"',
            'name="comment"',
            'name="domain"',
            "form.display_name.value",
            "form.bandwidth_limit_mbps.value",
            "form.notes.value",
            "const input = form.ip;",
            "form.prefix.value",
            "form.comment.value",
            "const input = form.domain;",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_reserve_ip_form_uses_compact_fields_and_content_width_button(self):
        for marker in (
            ".infra-address-fields { display: grid; grid-template-columns: minmax(220px, 330px) 76px minmax(220px, 360px) auto;",
            "#infra-add-ip-form { max-width: 1040px; }",
            "#infra-add-ip-form .infra-form-submit { width: auto; min-width: 190px;",
            ".infra-address-fields { grid-template-columns: minmax(0, 1fr) 68px; }",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        form_start = self.template.index('id="infra-add-ip-form"')
        form_end = self.template.index("</form>", form_start)
        form = self.template[form_start:form_end]
        self.assertLess(form.index('name="ip"'), form.index('name="prefix"'))
        self.assertLess(form.index('name="prefix"'), form.index('name="comment"'))
        self.assertLess(form.index('name="comment"'), form.index("Добавить в резерв"))

    def test_monitoring_presets_are_available_from_toolbar_modal(self):
        for marker in (
            'id="infra-settings-open"',
            'aria-controls="infra-settings-modal"',
            'id="infra-settings-modal"',
            'id="infra-settings-close"',
            'id="infra-settings"',
            "function openInfraSettings()",
            "function closeInfraSettings()",
            "loadInfraSettings();",
            'class="infra-setting-control"',
            "data-infra-setting=",
            "data-infra-setting-save=",
            "infraPost(main.dataset.infraSettingsUrl",
            "OFFLINE-пороги, нагрузка, аномалии, кулдауны",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        self.assertGreater(
            self.template.index('id="infra-settings-modal"'),
            self.template.index("</main>"),
        )

    def test_open_server_uses_observability_page_and_tabbed_management(self):
        for marker in (
            'class="infra-detail-hero"',
            'class="infra-detail-stats"',
            "'is-purple', 'fa-wave-square'",
            "'is-blue', 'fa-arrow-down'",
            "'is-copper', 'fa-bolt'",
            "'is-olive', 'fa-gauge-high'",
            'class="infra-detail-route"',
            'class="card infra-detail-chart-card"',
            'class="card infra-detail-who"',
            'class="infra-detail-who-bar"',
            ".infra-detail-management-workspace { overflow: hidden;",
            ".infra-detail-management-tabs { display: flex;",
            ".infra-detail-management-panel[hidden] { display: none; }",
            'class="infra-detail-management-workspace"',
            'class="infra-detail-management-tabs" role="tablist"',
            'role="tab" aria-controls="infra-detail-server-info"',
            'data-infra-management-tab="server"',
            'data-infra-management-tab="parameters"',
            'data-infra-management-tab="ips"',
            'data-infra-management-tab="domains"',
            'role="tabpanel" aria-labelledby="infra-management-tab-ips"',
            'data-infra-management-panel="ips"',
            "function infraSetManagementTab(container, tab, options = {})",
            "infraDetailManagementTab = tab;",
            "button.setAttribute('aria-selected', selected ? 'true' : 'false');",
            "panel.hidden = panel.dataset.infraManagementPanel !== tab;",
            "if (event.key === 'ArrowRight')",
            'class="card infra-detail-card infra-detail-management-panel" id="infra-detail-ips"',
            'class="card infra-detail-card infra-detail-journal-wide"',
            "const activeWanIp = (detail.ips || []).find",
            "const entry = detail.entry || null;",
            "const legacyEntry = (detail.domains || [])[0]",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        management_start = self.template.index(
            '<div class="infra-detail-management-workspace"'
        )
        management_end = self.template.index(
            'class="card infra-detail-card infra-detail-journal-wide"', management_start
        )
        management = self.template[management_start:management_end]
        expected_ids = (
            "infra-detail-server-info",
            "infra-detail-parameters",
            "infra-detail-ips",
            "infra-detail-domains",
        )
        for section_id in expected_ids:
            with self.subTest(section_id=section_id):
                self.assertIn(f'id="{section_id}"', management)

        self.assertNotIn(".infra-detail-management-grid", self.template)

        # География приходит с backend и дополняет, а не заменяет реальные IP.
        for marker in (
            "const whoRows = (who.top_addresses || []).slice(0, 20);",
            # Адреса самих нод отброшены на backend; пилюля только при N > 0
            "${Number(who.excluded_node_ips || 0) > 0 ? `<span class=\"infra-detail-who-pill\"",
            "исключено адресов нод: ${Number(who.excluded_node_ips || 0).toLocaleString('ru-RU')}</span>` : ''}",
            "const geo = who.geo || {};",
            "const whoTotalHits = Math.max(",
            "const barWidth = Math.max(2, Math.round(hits / whoMaxHits * 100));",
            'class="infra-detail-who-share">${share.toFixed(1)}%</div>',
            "geoPanelHtml('Регионы России'",
            "geoPanelHtml('Страны'",
            "items.slice(0, 8)",
            "items.slice(8)",
            'class="infra-geo-grid"',
            'class="infra-detail-who-analytics"',
            'class="infra-detail-who-columns"',
            'class="infra-detail-who-list">${whoTop}',
            "IP Geolocation by DB-IP",
            "География появится после первой автоматической загрузки DB-IP City Lite",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        self.assertIn(
            ".infra-geo-grid { grid-template-columns: 1fr;",
            self.template,
        )
        self.assertIn(
            ".infra-geo-label { grid-column: 1 / -1; grid-row: 1; }",
            self.template,
        )
        self.assertIn(
            ".infra-geo-bar { grid-column: 1; grid-row: 2; }",
            self.template,
        )
        for marker in (
            ".infra-detail-who { margin-bottom: 20px; padding: 19px 21px 17px; }",
            ".infra-geo-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start;",
            "column-gap: clamp(28px, 4vw, 62px); row-gap: 0;",
            "min-height: 27px;",
            ".infra-detail-who-row { display: grid; align-items: center; gap: 13px; min-height: 29px;",
            ".infra-detail-who-columns { display: none; }",
        ):
            with self.subTest(compact_marker=marker):
                self.assertIn(marker, self.template)

        self.assertNotIn(
            ".infra-detail-who-row { display: grid; grid-template-columns: minmax(150px, 1.2fr)",
            self.template,
        )

    def test_server_actions_are_responsive_and_preserve_field_focus(self):
        for marker in (
            "function infraSetActionBusy(target, busy, label = 'Выполняем…')",
            'class="infra-action-spinner"',
            "status.className = 'infra-pending-operation';",
            "function infraSnapshotInteraction(container)",
            "function infraRestoreInteraction(container, interaction)",
            "infraRestoreInteraction(container, interaction);",
            "await Promise.all(refreshes);",
            "busyTarget: form",
            "pendingLabel: 'Добавляем…'",
            "input.focus({preventScroll: true});",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_domain_changes_update_in_place_without_redrawing_charts(self):
        for marker in (
            'id="infra-domains-list"',
            "function infraAppendPendingDomain(container, domain)",
            "function infraConfirmPendingDomain(container, pendingRow, domain, server, clientSnis = [])",
            "function infraWireDomainDeleteButton(button, server)",
            "function infraServerSnisPanelHtml(detail, blockedSnis, diagAt)",
            "action: 'set_server_snis'",
            'id="infra-server-snis-form"',
            'data-infra-management-tab="snis"',
            "snis: 'infra-detail-snis',",
            "function infraAgeLabel(minutes)",
            "class=\"infra-diag-stale-note\"",
            ".infra-diag.is-stale .infra-diag-card { --tone: var(--panel-border); opacity: .62; }",
            "pendingLabel: 'Привязываем…'",
            "refreshDetail: false",
            "infraSyncDomainEmptyState(list);",
            'data-infra-management-count="domains"',
            "count.textContent = String(domainCount);",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_domain_management_uses_compact_tiles_and_inline_form(self):
        for marker in (
            ".infra-domain-manage-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 440px)); align-content: start; align-items: start; gap: 10px; }",
            ".infra-domain-wire { color: var(--muted); font-size: 10px;",
            'placeholder="как домены сервера"',
            ".infra-domain-manage-item { display: flex; flex-direction: column; align-items: stretch; gap: 9px; min-height: 50px;",
            ".infra-domain-manage-head { display: flex; align-items: center; justify-content: space-between;",
            ".infra-snis-form { grid-template-columns: minmax(260px, 620px) auto;",
            ".infra-detail-management-panel#infra-detail-domains { display: block; min-height: 0; }",
            'id="infra-add-domain-form" class="infra-form infra-domain-add-form"',
            ".infra-domain-add-form { grid-template-columns: minmax(220px, 420px) auto;",
            'name="client_snis"',
            ".infra-domain-add-form .infra-form-submit { width: auto; min-width: 180px;",
            ".infra-domain-manage-list { grid-template-columns: 1fr; }",
            ".infra-domain-add-form, .infra-snis-form { grid-template-columns: 1fr; max-width: none; }",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        domains_panel_start = self.template.index(
            '<section class="card infra-detail-card infra-detail-management-panel" id="infra-detail-domains"'
        )
        domains_panel_end = self.template.index("</section>", domains_panel_start)
        domains_panel = self.template[domains_panel_start:domains_panel_end]
        self.assertLess(domains_panel.index('id="infra-domains-list"'), domains_panel.index('id="infra-add-domain-form"'))
        self.assertNotIn('style="margin-top:14px;"', domains_panel)

    def test_server_journal_is_a_readable_timeline_with_collapsible_details(self):
        for marker in (
            'class="infra-journal-head"',
            'class="infra-log-rail"',
            'class="infra-log-card"',
            ".infra-log-row { display: grid; grid-template-columns: 108px 30px minmax(0, 1fr);",
            ".infra-log-rail i { position: relative; z-index: 1; width: 28px; min-width: 28px; height: 28px; flex: 0 0 28px; aspect-ratio: 1 / 1;",
            ".infra-log-row { grid-template-columns: 30px minmax(0, 1fr); gap: 4px 10px; }",
            'class="infra-log-details"',
            '<summary>Технические детали</summary>',
            "const replacementStatusLabels = {",
            "const anomalyStatusLabels = {",
            "const commandStatusLabels = {",
            "const commandKindLabels = {ensure_ip: 'Установить IP на интерфейс'};",
            "journal.sort((left, right)",
            "const journalCountLabel = (count) => {",
            "journal.map((entry) => entry.html).join('')",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        self.assertNotIn("journal.join('')", self.template)

    def test_open_journal_details_survive_five_second_auto_refresh(self):
        for marker in (
            "function infraSnapshotOpenDetails(container)",
            "details[data-infra-persistent-details][open]",
            "function infraRestoreOpenDetails(container, openDetails)",
            "const openDetails = infraSnapshotOpenDetails(container);",
            "infraRestoreOpenDetails(container, openDetails);",
            'data-infra-persistent-details="${escapeHtml(detailsKey)}"',
            "detailsKey: `journal-replacement-${r.id}`",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        snapshot_index = self.template.index(
            "const openDetails = infraSnapshotOpenDetails(container);"
        )
        render_index = self.template.index(
            "renderInfraDetail(container, detailPayload.result, telemetryPayload.result, full);",
            snapshot_index,
        )
        restore_index = self.template.index(
            "infraRestoreOpenDetails(container, openDetails);",
            render_index,
        )
        self.assertLess(snapshot_index, render_index)
        self.assertLess(render_index, restore_index)

    def test_open_geo_rankings_survive_five_second_auto_refresh(self):
        for marker in (
            "const geoPanelHtml = (title, rows, isCountry, emptyText, detailsKey) => {",
            '<details class="infra-geo-more" data-infra-persistent-details="${escapeHtml(detailsKey)}">',
            "'infra-geo-regions-' + server.id",
            "'infra-geo-countries-' + server.id",
            "const openDetails = infraSnapshotOpenDetails(container);",
            "infraRestoreOpenDetails(container, openDetails);",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_expanded_geo_rankings_put_collapse_control_after_extra_rows(self):
        for marker in (
            ".infra-geo-more[open] { display: flex; flex-direction: column; }",
            ".infra-geo-more[open] > .infra-geo-list { order: 1; }",
            ".infra-geo-more[open] > summary { order: 2; margin-top: 5px; }",
            '<span class="infra-geo-more-collapsed">ещё ${items.length - 8}</span>',
            '<span class="infra-geo-more-expanded">свернуть</span>',
            ".infra-geo-more[open] .infra-geo-more-collapsed { display: none; }",
            ".infra-geo-more[open] .infra-geo-more-expanded { display: inline; }",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_node_capacity_uses_structured_metrics_and_action_cards(self):
        for marker in (
            ".infra-capacity { --capacity-tone: var(--green); margin: 16px 0 20px;",
            'class="card infra-detail-card infra-capacity is-${escapeHtml(capacity.level || \'ok\')}"',
            'class="infra-capacity-metrics"',
            'class="infra-capacity-issues"',
            "const capacityProblemView = (message) => {",
            "title: 'Слишком низкий worker_connections'",
            "title: 'Workers не применили новый лимит файлов'",
            'class="infra-capacity-worker"',
            'data-infra-persistent-details="capacity-technical-${server.id}"',
            ".infra-capacity-metrics, .infra-capacity-issues { grid-template-columns: 1fr; }",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)
        self.assertNotIn("infra-cap-problem", self.template)

    def test_ip_groups_use_compact_responsive_grid(self):
        for marker in (
            ".infra-ip-groups { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));",
            ".infra-ip-group.is-wide { grid-column: 1 / -1; }",
            ".infra-ip-groups { grid-template-columns: repeat(2, minmax(0, 1fr)); }",
            ".infra-ip-groups { grid-template-columns: 1fr; }",
            "group.ips.length > 2 ? ' is-wide' : ''",
            "const collapsibleReserve = group.isReserve && group.ips.length > 6;",
            'class="infra-ip-reserve-details"',
            'aria-label="Показать или скрыть резервные IP-адреса"',
            "infraReserveExpanded = event.currentTarget.open;",
            ".infra-ip-reserve-details[open] .infra-ip-reserve-chevron",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_compact_server_buttons_keep_contrast_in_light_theme(self):
        for marker in (
            'html[data-admin-theme="light"] .infra-mini-btn:hover',
            "background: rgba(181,139,0,.07); color: #242a34;",
            'html[data-admin-theme="light"] .infra-mini-btn.danger:hover',
            "color: #b52f3e;",
            'html[data-admin-theme="light"] .infra-mini-btn:focus-visible',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_server_rows_keep_visible_hover_in_light_theme(self):
        # Табличный вид удалён (10.09.2026); hover живёт на плитке
        for marker in (
            ".infra-server-card:hover { border-color: rgba(255,199,0,.35); }",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_server_list_uses_readable_typography(self):
        for marker in (
            "#subpanel-inf-servers { font-family: 'Manrope'",
            "#infra-server-detail, .infra-server-screen-head { font-family: 'Inter'",
            "font-optical-sizing: auto; font-synthesis: none;",
            "#subpanel-inf-servers .infra-server-card-name { font-size: 15px; font-weight: 800;",
            "#subpanel-inf-servers .infra-server-card-host { font-size: 11.5px; font-weight: 600; }",
            ".infra-detail-stat-label { font-size: 12px; font-weight: 700; }",
            ".infra-detail-stat-value { font-size: clamp(25px, 1.7vw, 34px); font-weight: 800; }",
            "#infra-server-detail .infra-detail-card h4 { font-size: 17px; line-height: 1.2; font-weight: 700;",
            "#infra-server-detail .infra-kv { font-size: 13.5px; line-height: 1.4; }",
            '#infra-server-detail .infra-kv code { font-family: "SFMono-Regular"',
            'class="infra-machine-id"',
            "#infra-server-detail .infra-control { font-size: 12.5px; font-weight: 600; }",
            ".infra-table-heading h3 { margin: 0; color: var(--text-main); font-size: 16px;",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_management_tabs_and_panel_share_one_seamless_surface(self):
        for marker in (
            ".infra-detail-management-workspace { overflow: hidden; border: 1px solid var(--panel-border); border-radius: 12px; background: var(--card-bg); }",
            ".infra-detail-management-panels { min-width: 0; background: var(--card-bg); }",
            "#infra-server-detail .infra-detail-management-panels > .infra-detail-management-panel { min-width: 0; min-height: 220px; margin: 0;",
            "border: 0; border-radius: 0; background: var(--card-bg); box-shadow: none; backdrop-filter: none;",
            'html[data-admin-theme="light"] #infra-server-detail .infra-detail-management-workspace,',
            'html[data-admin-theme="light"] #infra-server-detail .infra-detail-management-panels > .infra-detail-management-panel { background: #fff; }',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)


# ============================================================================
# Антиабьюз: лимит трафика пробных подписок и алерты ip-guard
# ============================================================================


class _AntiabuseSqliteMixin:
    """SQLite-фикстура антиабьюза: users + платежи (семантика PAYS_EXISTS_SQL)
    + system_settings + user_blocks + таблицы ip-guard. JSONB-таблицы (журнал
    админов) на SQLite не создаются — в тестах эндпоинтов admin_audit_write
    мокается и проверяется по вызовам."""

    def setUp(self):
        from common.models.db import IpAlert
        from common.models.db import ManagedTrafficLimit
        from common.models.db import SystemSetting
        from common.models.db import UserBlock
        from common.models.db import UserIpObservation

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(
            self.engine,
            tables=[
                User.__table__,
                SystemSetting.__table__,
                YkPayment.__table__,
                WataInvoice.__table__,
                WataTransaction.__table__,
                UserBlock.__table__,
                IpAlert.__table__,
                UserIpObservation.__table__,
                ManagedTrafficLimit.__table__,
            ],
        )
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        self.addCleanup(self.session.close)
        self._next_row_id = 100
        # На SQLite users.id (BIGINT PK) не rowid-алиас и не автоинкрементится,
        # а Sequence("users_id_seq") игнорируется — INSERT прод-кода
        # (create_site_user) без явного id падает по NOT NULL. Подставляем id
        # на flush; счётчик далеко от явных id тестов, чтобы не пересекаться.
        self._next_auto_user_id = 900_000

        def _assign_sqlite_user_ids(session, flush_context, instances):
            for obj in session.new:
                if isinstance(obj, User) and obj.id is None:
                    self._next_auto_user_id += 1
                    obj.id = self._next_auto_user_id

        sa_event.listen(self.session, "before_flush", _assign_sqlite_user_ids)
        self.addCleanup(
            sa_event.remove, self.session, "before_flush", _assign_sqlite_user_ids
        )

    # --- маркеры managed_traffic_limits (антиабьюз v2) ---------------------

    def _marker(
        self,
        user,
        limit_bytes=5 * 1024**3,
        strategy="DAY",
        reason="trial",
        release_on="payment",
        applied_by="bot:start",
    ):
        from common.models.db import ManagedTrafficLimit

        marker = ManagedTrafficLimit(
            user_id=user.id,
            limit_bytes=limit_bytes,
            strategy=strategy,
            reason=reason,
            release_on=release_on,
            applied_by=applied_by,
            applied_at=datetime(2026, 9, 5, 10, 50, 0),
        )
        self.session.add(marker)
        self.session.flush()
        return marker

    def _marker_of(self, user_id):
        from common.models.db import ManagedTrafficLimit

        self.session.expire_all()
        return self.session.get(ManagedTrafficLimit, user_id)

    def _drop_marker_table(self):
        """«Миграция не накачена»: таблицы маркеров нет."""
        from common.models.db import ManagedTrafficLimit

        self.session.close()
        ManagedTrafficLimit.__table__.drop(self.engine)

    def _row_id(self):
        self._next_row_id += 1
        return self._next_row_id

    def _user(self, user_id, username, **kwargs):
        user = User(id=user_id, username=username, **kwargs)
        self.session.add(user)
        self.session.flush()
        return user

    def _set(self, key, value):
        from common.models.db import SystemSetting

        setting = self.session.get(SystemSetting, key)
        if setting is None:
            self.session.add(SystemSetting(key=key, value=value))
        else:
            setting.value = value
        self.session.flush()

    def _yk_payment(self, user, status="succeeded"):
        self.session.add(
            YkPayment(
                id=self._row_id(),
                user_id=user.id,
                amount=249,
                currency="RUB",
                status=status,
                created_at=datetime(2026, 8, 1, 12, 0, 0),
                payment_id=f"yk-{user.id}-{status}",
                subscription_period="month",
            )
        )
        self.session.flush()

    def _wata_payment(self, user, status="Paid"):
        order_id = f"order-{user.id}-{status}"
        self.session.add(
            WataInvoice(
                id=self._row_id(),
                user_id=user.id,
                invoice_id=f"inv-{order_id}",
                amount=249,
                currency="RUB",
                status="Opened",
                terminal_name="t",
                terminal_public_id="tp",
                creation_time=datetime(2026, 8, 1, 12, 0, 0),
                order_id=order_id,
                expiration_datetime=datetime(2030, 1, 1),
                tariff_id="month",
            )
        )
        self.session.add(
            WataTransaction(
                id=self._row_id(),
                transaction_id=f"tx-{order_id}",
                transaction_type="Payment",
                terminal_public_id="tp",
                transaction_status=status,
                terminal_name="t",
                amount=249,
                currency="RUB",
                order_id=order_id,
                order_description="d",
                commission=0,
                payment_time=datetime(2026, 8, 1, 12, 5, 0),
            )
        )
        self.session.flush()


class AntiabuseNeverPaidHelperTests(_AntiabuseSqliteMixin, SimpleTestCase):
    def test_user_never_paid_matches_pays_exists_semantics(self):
        """«Пробная без платежа» = нет yk succeeded и нет wata Paid (JOIN
        wata_invoices по order_id); pending/Declined платежом не считаются;
        отсутствующий пользователь — None, а не «не платил»."""
        from engine.sql_helpers import user_never_paid

        trial = self._user(1, "trial")
        yk = self._user(2, "yk")
        self._yk_payment(yk)
        pending = self._user(3, "pending")
        self._yk_payment(pending, status="pending")
        wata = self._user(4, "wata")
        self._wata_payment(wata)
        declined = self._user(5, "declined")
        self._wata_payment(declined, status="Declined")

        self.assertIs(user_never_paid(self.session, trial.id), True)
        self.assertIs(user_never_paid(self.session, yk.id), False)
        self.assertIs(user_never_paid(self.session, pending.id), True)
        self.assertIs(user_never_paid(self.session, wata.id), False)
        self.assertIs(user_never_paid(self.session, declined.id), True)
        self.assertIsNone(user_never_paid(self.session, 999))
        self.assertIsNone(user_never_paid(self.session, None))


class AntiabuseTrialLimitHelpersTests(_AntiabuseSqliteMixin, SimpleTestCase):
    def test_disabled_by_default_means_no_limit(self):
        import proto.rwmanager_pb2 as rw_proto

        from engine import rwms_helpers

        self.assertFalse(rwms_helpers.trial_traffic_limit_enabled(self.session))
        self.assertIsNone(rwms_helpers.trial_traffic_limit_for_new_trial(self.session))
        user = self._user(1, "u")
        self.assertIsNone(rwms_helpers.trial_traffic_limit_for_user(self.session, user))
        # Сконфигурированный лимит (для кнопок «применить») — дефолты common.
        limit = rwms_helpers.trial_traffic_limit_configured(self.session)
        self.assertEqual(limit.limit_gb, 5.0)
        self.assertEqual(limit.limit_bytes, 5 * 1024**3)
        self.assertEqual(limit.strategy_key, "day")
        self.assertEqual(limit.strategy_name, "DAY")
        self.assertEqual(limit.strategy, rw_proto.TrafficLimitStrategy.DAY)

    def test_enabled_limit_from_settings_with_month_rolling(self):
        import proto.rwmanager_pb2 as rw_proto

        from engine import rwms_helpers

        self._set("trial_traffic_limit_enabled", "1")
        self._set("trial_traffic_limit_gb", "0.5")
        self._set("trial_traffic_limit_strategy", "month_rolling")

        limit = rwms_helpers.trial_traffic_limit_for_new_trial(self.session)

        self.assertEqual(limit.limit_bytes, 536870912)
        self.assertEqual(limit.strategy_name, "MONTH_ROLLING")
        self.assertEqual(limit.strategy, rw_proto.TrafficLimitStrategy.MONTH_ROLLING)
        self.assertEqual(rw_proto.TrafficLimitStrategy.MONTH_ROLLING, 4)

    def test_limit_for_existing_user_requires_never_paid(self):
        from engine import rwms_helpers

        self._set("trial_traffic_limit_enabled", "1")
        trial = self._user(1, "trial")
        paid = self._user(2, "paid")
        self._yk_payment(paid)

        self.assertIsNotNone(rwms_helpers.trial_traffic_limit_for_user(self.session, trial))
        self.assertIsNone(rwms_helpers.trial_traffic_limit_for_user(self.session, paid))
        self.assertIsNone(rwms_helpers.trial_traffic_limit_for_user(self.session, None))
        # Нет строки users — лимит не ставится (None ≠ «не платил»).
        self.assertIsNone(
            rwms_helpers.trial_traffic_limit_for_user(self.session, SimpleNamespace(id=999))
        )

    def test_broken_settings_fall_back_to_common_defaults(self):
        from engine import rwms_helpers

        self._set("trial_traffic_limit_enabled", "1")
        self._set("trial_traffic_limit_gb", "abc")
        self._set("trial_traffic_limit_strategy", "quarter")

        limit = rwms_helpers.trial_traffic_limit_for_new_trial(self.session)

        self.assertEqual(limit.limit_gb, 5.0)
        self.assertEqual(limit.strategy_key, "day")


class AntiabuseCreateUserRequestTests(SimpleTestCase):
    def test_create_user_until_without_limit_is_unchanged(self):
        import proto.rwmanager_pb2 as rw_proto

        from engine import rwms_helpers

        client = mock.Mock()
        client.add_user.return_value = SimpleNamespace(uuid="u")

        rwms_helpers.create_user_until(
            client, "42", datetime(2030, 1, 1), email="a@b.c", telegram_id=42
        )

        request = client.add_user.call_args.args[0]
        self.assertFalse(request.HasField("traffic_limit_bytes"))
        self.assertEqual(request.traffic_limit_strategy, rw_proto.TrafficLimitStrategy.NO_RESET)
        self.assertEqual(request.status, rw_proto.UserStatus.ACTIVE)
        self.assertEqual(request.username, "42")
        self.assertEqual(request.telegram_id, 42)

    def test_create_user_with_limit_sets_field_13_and_strategy(self):
        import proto.rwmanager_pb2 as rw_proto

        from engine import rwms_helpers

        client = mock.Mock()
        client.add_user.return_value = SimpleNamespace(uuid="u")
        limit = rwms_helpers.TrialTrafficLimit(
            limit_gb=0.5,
            limit_bytes=536870912,
            strategy_key="month_rolling",
            strategy_name="MONTH_ROLLING",
        )

        with self.assertLogs(level="INFO") as logs:
            rwms_helpers.create_user(client, "42", 7, traffic_limit=limit)

        request = client.add_user.call_args.args[0]
        self.assertTrue(request.HasField("traffic_limit_bytes"))
        self.assertEqual(request.traffic_limit_bytes, 536870912)
        self.assertEqual(
            request.traffic_limit_strategy, rw_proto.TrafficLimitStrategy.MONTH_ROLLING
        )
        # Поле добавлено аддитивно: номер 13, существующие не тронуты.
        self.assertEqual(
            rw_proto.AddUserRequest.DESCRIPTOR.fields_by_name["traffic_limit_bytes"].number,
            13,
        )
        self.assertTrue(any("trial traffic limit" in line for line in logs.output))

    def test_trial_creation_paths_pass_limit_from_settings(self):
        import inspect

        from engine import views
        from mobile_api import provisioning

        src = inspect.getsource(views.create_site_user)
        self.assertIn("trial_limit = trial_traffic_limit_for_new_trial(db_session)", src)
        self.assertIn("traffic_limit=trial_limit", src)
        # Маркер — в той же сессии, что и строка users.
        self.assertIn("record_trial_limit_marker(db_session, user, trial_limit)", src)
        mobile_src = inspect.getsource(provisioning)
        self.assertIn("trial_limit = trial_traffic_limit_for_new_trial(db_session)", mobile_src)
        self.assertIn("traffic_limit=trial_limit", mobile_src)
        self.assertIn("record_trial_limit_marker(db_session, user, trial_limit)", mobile_src)
        # Пересоздание при достоверном NOT_FOUND — только never_paid и включено;
        # маркер site:admin:<login> в той же сессии.
        manage_src = inspect.getsource(views.support_admin_api_subscription_manage)
        self.assertIn("recreate_limit = trial_traffic_limit_for_user(db_session, user)", manage_src)
        self.assertIn("traffic_limit=recreate_limit", manage_src)
        self.assertIn("record_trial_limit_marker(", manage_src)

    def test_site_registration_forwards_limit_to_create_user(self):
        from engine.views import create_site_user

        session = _SiteRegistrationFakeSession()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = None
        sentinel = object()
        email = "limit@example.com"
        context = {
            "referrer": None,
            "ymid": None,
            "traffic_source": None,
        }

        with mock.patch(
            "engine.views.get_registration_context", return_value=context
        ), mock.patch("engine.views.rwms_client", client), mock.patch(
            "engine.views.trial_traffic_limit_for_new_trial", return_value=sentinel
        ), mock.patch(
            "engine.views.create_user",
            side_effect=lambda **kwargs: _FakeSiteRwUser(username=kwargs["username"]),
        ) as create_rwms_user, mock.patch(
            "engine.views.add_user_to_traffic_progress"
        ), mock.patch("engine.views.add_event_log"), mock.patch(
            "engine.views.should_create_trial_for_channel", return_value=True
        ), mock.patch(
            "engine.views.record_trial_limit_marker"
        ) as record_marker, mock.patch("engine.views.add_traffic_limit_event"):
            user = create_site_user(session, email, SimpleNamespace())

        self.assertIs(create_rwms_user.call_args.kwargs["traffic_limit"], sentinel)
        # Тот же лимит уходит в маркер для только что созданной строки users.
        self.assertIs(record_marker.call_args.args[2], sentinel)
        self.assertIs(record_marker.call_args.args[1], user)


class AntiabuseClientActionsTests(_AntiabuseSqliteMixin, SimpleTestCase):
    """Действия карточки клиента apply_trial_limit / remove_traffic_limit.

    v2: панель — mock RWMS, маркеры managed_traffic_limits — реальная SQLite-
    сессия; аудит и event_logs (JSONB) мокаются и проверяются по вызовам.
    Ручные лимиты владельца (без маркера) не снимаются и без подтверждения не
    заменяются."""

    class _Session:
        """Фейковая сессия для продлений (push_to_panel / bulk extend), которые
        маркеры не трогают."""

        def __init__(self, paid=False, settings=None):
            self.paid = paid
            self.settings = settings or {}
            self.added = []
            self.commits = 0
            self.closed = False

        def get(self, model, key):
            value = self.settings.get(key)
            return SimpleNamespace(key=key, value=value) if value is not None else None

        def execute(self, statement, params=None):
            return SimpleNamespace(first=lambda: (self.paid,))

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            self.commits += 1

        def rollback(self):
            return None

        def close(self):
            self.closed = True

    def setUp(self):
        super().setUp()
        self.audit = mock.Mock()
        self.events = mock.Mock()
        self.user = self._user(
            7,
            "42",
            telegram_id=42,
            expire_at=datetime(2030, 1, 1, 12, 0, 0),
            autopay_allow=True,
        )
        self.session.commit()

    def _panel(self, limit_bytes=0, strategy=0, status=0, uuid="panel-uuid"):
        return SimpleNamespace(
            uuid=uuid,
            traffic_limit_bytes=limit_bytes,
            traffic_limit_strategy=strategy,
            status=status,
        )

    def _client(self, panel=None, update_result=SimpleNamespace(uuid="panel-uuid")):
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = panel
        client.update_user.return_value = update_result
        return client

    def _call(self, action, client, **data):
        from engine.views import support_admin_api_subscription_manage

        request = RequestFactory().post(
            "/support-admin/api/subscription-manage/",
            data={"q": "42", "action": action, **data},
        )
        request.session = {}
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=self.session),
            mock.patch("engine.views.rwms_client", client),
            mock.patch("engine.views.admin_audit_write", self.audit),
            mock.patch("engine.views.add_traffic_limit_event", self.events),
        ):
            response = support_admin_api_subscription_manage(request)
        return response, json.loads(response.content)

    def _audit_actions(self):
        return [(call.args[2], call.kwargs) for call in self.audit.call_args_list]

    @staticmethod
    def _fake_session_audit_actions(session):
        from common.models.db import AdminAuditLog

        return [(o.action, o.details) for o in session.added if isinstance(o, AdminAuditLog)]

    def _event_types(self):
        return [call.args[2] for call in self.events.call_args_list]

    def test_apply_trial_limit_updates_panel_without_squads_or_status(self):
        import proto.rwmanager_pb2 as rw_proto

        self._set("trial_traffic_limit_gb", "1.5")
        self._set("trial_traffic_limit_strategy", "week")
        self.session.commit()
        client = self._client(self._panel())

        response, payload = self._call("apply_trial_limit", client)

        self.assertEqual(response.status_code, 200)
        self.assertIs(payload["result"]["rwms_updated"], True)
        self.assertIs(payload["result"]["never_paid"], True)
        self.assertEqual(payload["result"]["outcome"], "applied")
        self.assertIn("1.5 ГиБ", payload["result"]["action_label"])
        self.assertIn("управляемым", payload["result"]["action_label"])
        request = client.update_user.call_args.args[0]
        self.assertEqual(request.uuid, "panel-uuid")
        self.assertEqual(request.traffic_limit_bytes, int(1.5 * 1024**3))
        self.assertEqual(request.traffic_limit_strategy, rw_proto.TrafficLimitStrategy.WEEK)
        self.assertFalse(request.HasField("status"))
        self.assertEqual(list(request.active_internal_squads), [])
        client.get_user_by_username.assert_not_called()
        client.add_user.assert_not_called()
        # Маркер в той же сессии: trial / payment / site:admin:<login>.
        marker = self._marker_of(7)
        self.assertIsNotNone(marker)
        self.assertEqual(marker.limit_bytes, int(1.5 * 1024**3))
        self.assertEqual(marker.strategy, "WEEK")
        self.assertEqual(marker.reason, "trial")
        self.assertEqual(marker.release_on, "payment")
        self.assertTrue(marker.applied_by.startswith("site:admin:"))
        actions = self._audit_actions()
        self.assertEqual(actions[0][0], "apply_trial_limit")
        self.assertEqual(actions[0][1]["strategy"], "week")
        self.assertEqual(actions[0][1]["outcome"], "applied")
        self.assertEqual(self._event_types(), ["traffic_limit_applied"])

    def test_apply_trial_limit_refuses_paid_client_without_force(self):
        self._yk_payment(self.user)
        self.session.commit()
        client = self._client(self._panel())

        response, payload = self._call("apply_trial_limit", client)

        self.assertEqual(response.status_code, 409)
        self.assertIs(payload["paid"], True)
        client.update_user.assert_not_called()
        self.assertIsNone(self._marker_of(7))

        response, payload = self._call("apply_trial_limit", client, force="1")

        self.assertEqual(response.status_code, 200)
        client.update_user.assert_called_once()
        actions = self._audit_actions()
        self.assertIs(actions[0][1]["forced"], True)
        self.assertIs(actions[0][1]["never_paid"], False)
        self.assertIsNotNone(self._marker_of(7))

    def test_apply_trial_limit_marks_equal_panel_limit_as_managed(self):
        """Панель уже несёт ровно лимит пробных, маркера нет (лимит поставлен
        до появления таблицы): панель не трогаем, маркер пишем — с этого
        момента лимит снимет оплата. Повтор — без изменений и без события."""
        client = self._client(self._panel(5 * 1024**3, 1))  # DAY — дефолт

        response, payload = self._call("apply_trial_limit", client)

        self.assertEqual(response.status_code, 200)
        self.assertIs(payload["result"]["rwms_updated"], False)
        self.assertEqual(payload["result"]["outcome"], "marked")
        self.assertTrue(payload["result"]["action_label"].startswith("Без изменений в панели"))
        self.assertIn("помечен управляемым", payload["result"]["action_label"])
        client.update_user.assert_not_called()
        marker = self._marker_of(7)
        self.assertEqual((marker.limit_bytes, marker.strategy), (5 * 1024**3, "DAY"))
        self.assertEqual(self._event_types(), ["traffic_limit_applied"])

        response, payload = self._call("apply_trial_limit", client)

        self.assertEqual(payload["result"]["outcome"], "unchanged")
        self.assertIn("управляемый", payload["result"]["action_label"])
        client.update_user.assert_not_called()
        self.assertEqual(self._event_types(), ["traffic_limit_applied"])

    def test_apply_trial_limit_refuses_manual_panel_limit_without_override_manual(self):
        """Ручной кап владельца (лимит без маркера, не равный лимиту пробных)
        неприкосновенен: 409 {"manual": true}; force=1 (подтверждение «клиент
        платил») его НЕ заменяет; заменить — только с override_manual=1 (явное
        решение админа именно о ручном лимите), после чего лимит становится
        управляемым."""
        client = self._client(self._panel(1024**3, 1))

        response, payload = self._call("apply_trial_limit", client)

        self.assertEqual(response.status_code, 409)
        self.assertIs(payload["manual"], True)
        self.assertIs(payload["paid"], False)
        self.assertIs(payload["admin_limit"], False)
        self.assertIn("1 ГиБ · ежедневно", payload["message"])
        self.assertEqual(payload["manual_message"], payload["message"])
        client.update_user.assert_not_called()
        self.assertIsNone(self._marker_of(7))
        self.audit.assert_not_called()

        response, payload = self._call("apply_trial_limit", client, force="1")

        self.assertEqual(response.status_code, 409)
        self.assertIs(payload["manual"], True)
        client.update_user.assert_not_called()
        self.assertIsNone(self._marker_of(7))
        self.audit.assert_not_called()

        response, payload = self._call("apply_trial_limit", client, override_manual="1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["result"]["outcome"], "applied")
        self.assertEqual(client.update_user.call_args.args[0].traffic_limit_bytes, 5 * 1024**3)
        audit = self._audit_actions()[0][1]
        self.assertIs(audit["manual_overridden"], True)
        self.assertIs(audit["override_manual"], True)
        self.assertIs(audit["forced"], False)
        self.assertIs(audit["admin_limit_overridden"], False)
        self.assertIsNotNone(self._marker_of(7))

    def test_apply_trial_limit_paid_and_manual_need_separate_confirmations(self):
        """Плативший клиент с ручным капом владельца: 409 несёт ОБА флага;
        force=1 (ответ на «платил») ручной лимит не трогает — снова 409 manual,
        панель и маркеры не тронуты; override_manual=1 сам по себе не отвечает
        на «платил»; применяется только с обоими флагами."""
        self._yk_payment(self.user)
        self.session.commit()
        client = self._client(self._panel(1024**3, 1))

        response, payload = self._call("apply_trial_limit", client)

        self.assertEqual(response.status_code, 409)
        self.assertIs(payload["paid"], True)
        self.assertIs(payload["manual"], True)
        self.assertIs(payload["admin_limit"], False)
        self.assertIn("Клиент платил", payload["paid_message"])
        self.assertIn("1 ГиБ · ежедневно", payload["manual_message"])
        self.assertIn("Клиент платил", payload["message"])
        self.assertIn("ручной лимит", payload["message"])

        response, payload = self._call("apply_trial_limit", client, force="1")

        self.assertEqual(response.status_code, 409)
        self.assertIs(payload["paid"], False)
        self.assertIs(payload["manual"], True)
        self.assertNotIn("Клиент платил", payload["message"])
        client.update_user.assert_not_called()
        self.assertIsNone(self._marker_of(7))
        self.audit.assert_not_called()

        response, payload = self._call("apply_trial_limit", client, override_manual="1")

        self.assertEqual(response.status_code, 409)
        self.assertIs(payload["paid"], True)
        self.assertIs(payload["manual"], False)
        client.update_user.assert_not_called()
        self.assertIsNone(self._marker_of(7))

        response, payload = self._call(
            "apply_trial_limit", client, force="1", override_manual="1"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["result"]["outcome"], "applied")
        self.assertIs(payload["result"]["never_paid"], False)
        client.update_user.assert_called_once()
        self.assertEqual(client.update_user.call_args.args[0].traffic_limit_bytes, 5 * 1024**3)
        marker = self._marker_of(7)
        self.assertEqual((marker.reason, marker.release_on), ("trial", "payment"))
        audit = self._audit_actions()[0][1]
        self.assertIs(audit["forced"], True)
        self.assertIs(audit["override_manual"], True)
        self.assertIs(audit["manual_overridden"], True)
        self.assertIs(audit["never_paid"], False)

    def test_apply_trial_limit_refuses_bot_admin_limit_without_override(self):
        """Маркер из кнопки алерта бота (ip_abuse, release_on='manual'):
        оплата его не снимает, и карточка без отдельного подтверждения его не
        перезаписывает — 409 {"admin_limit": true}; force=1 не помогает; только
        override_admin_limit=1 заменяет его лимитом пробного (trial/payment)."""
        self._marker(
            self.user,
            limit_bytes=1024**3,
            strategy="DAY",
            reason="ip_abuse",
            release_on="manual",
            applied_by="bot:admin:1",
        )
        self.session.commit()
        client = self._client(self._panel(1024**3, 1))

        response, payload = self._call("apply_trial_limit", client)

        self.assertEqual(response.status_code, 409)
        self.assertIs(payload["admin_limit"], True)
        self.assertIs(payload["manual"], False)
        self.assertIs(payload["paid"], False)
        self.assertIn("ip_abuse", payload["admin_limit_message"])
        self.assertIn("bot:admin:1", payload["admin_limit_message"])
        self.assertIn("1 ГиБ · ежедневно", payload["message"])
        client.update_user.assert_not_called()
        self.audit.assert_not_called()

        response, payload = self._call("apply_trial_limit", client, force="1", override_manual="1")

        self.assertEqual(response.status_code, 409)
        self.assertIs(payload["admin_limit"], True)
        client.update_user.assert_not_called()
        marker = self._marker_of(7)
        self.assertEqual((marker.reason, marker.release_on, marker.applied_by), ("ip_abuse", "manual", "bot:admin:1"))
        self.assertEqual(marker.limit_bytes, 1024**3)

        response, payload = self._call("apply_trial_limit", client, override_admin_limit="1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["result"]["outcome"], "applied")
        self.assertEqual(client.update_user.call_args.args[0].traffic_limit_bytes, 5 * 1024**3)
        marker = self._marker_of(7)
        self.assertEqual((marker.reason, marker.release_on), ("trial", "payment"))
        self.assertTrue(marker.applied_by.startswith("site:admin:"))
        audit = self._audit_actions()[0][1]
        self.assertIs(audit["override_admin_limit"], True)
        self.assertIs(audit["admin_limit_overridden"], True)
        self.assertIs(audit["manual_overridden"], False)
        self.assertEqual(self._event_types(), ["traffic_limit_applied"])

    def test_apply_trial_limit_keeps_bot_admin_marker_when_panel_already_equal(self):
        """Маркер бота (release_on='manual') с ровно лимитом пробных: unchanged,
        release_on не перезаписывается в payment."""
        self._marker(self.user, reason="traffic_abuse", release_on="manual", applied_by="bot:admin:1")
        self.session.commit()
        client = self._client(self._panel(5 * 1024**3, 1))

        response, payload = self._call("apply_trial_limit", client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["result"]["outcome"], "unchanged")
        client.update_user.assert_not_called()
        marker = self._marker_of(7)
        self.assertEqual((marker.reason, marker.release_on), ("traffic_abuse", "manual"))

    def test_apply_drops_stale_marker_when_owner_changed_limit_by_hand(self):
        """Маркер есть, а панель отличается — владелец менял руками: маркер
        снимается (с логом), лимит считается ручным, панель не трогается."""
        self._marker(self.user, limit_bytes=5 * 1024**3, strategy="DAY")
        self.session.commit()
        client = self._client(self._panel(1024**3, 0))  # 1 ГиБ, NO_RESET — руками

        with self.assertLogs(level="WARNING") as logs:
            response, payload = self._call("apply_trial_limit", client)

        self.assertEqual(response.status_code, 409)
        self.assertIs(payload["manual"], True)
        self.assertIsNone(self._marker_of(7))
        self.assertTrue(any("лимит ручной" in line for line in logs.output))
        client.update_user.assert_not_called()

    def test_remove_traffic_limit_restores_as_before_without_squads(self):
        import proto.rwmanager_pb2 as rw_proto

        self._marker(self.user)
        self.session.commit()
        client = self._client(self._panel(5 * 1024**3, 1, rw_proto.UserStatus.LIMITED))

        response, payload = self._call("remove_traffic_limit", client)

        self.assertEqual(response.status_code, 200)
        self.assertIs(payload["result"]["rwms_updated"], True)
        self.assertEqual(payload["result"]["outcome"], "released")
        request = client.update_user.call_args.args[0]
        self.assertEqual(request.uuid, "panel-uuid")
        self.assertTrue(request.HasField("traffic_limit_bytes"))
        self.assertEqual(request.traffic_limit_bytes, 0)
        self.assertTrue(request.HasField("traffic_limit_strategy"))
        self.assertEqual(request.traffic_limit_strategy, rw_proto.TrafficLimitStrategy.NO_RESET)
        self.assertTrue(request.HasField("status"))
        self.assertEqual(request.status, rw_proto.UserStatus.ACTIVE)
        # Пустой список сквадов RWMS трактует как «не менять» — ban-сквад остаётся.
        self.assertEqual(list(request.active_internal_squads), [])
        self.assertEqual(self._audit_actions()[0][0], "remove_traffic_limit")
        self.assertEqual(self._audit_actions()[0][1]["outcome"], "released")
        # Маркер удалён, событие traffic_limit_released записано.
        self.assertIsNone(self._marker_of(7))
        self.assertEqual(self._event_types(), ["traffic_limit_released"])
        self.assertIn("маркер удалён", payload["result"]["action_label"])

    def test_remove_traffic_limit_never_reactivates_disabled_subscription(self):
        import proto.rwmanager_pb2 as rw_proto

        self._marker(self.user)
        self.session.commit()
        client = self._client(self._panel(5 * 1024**3, 1, rw_proto.UserStatus.DISABLED))

        response, payload = self._call("remove_traffic_limit", client)

        self.assertEqual(response.status_code, 200)
        request = client.update_user.call_args.args[0]
        self.assertEqual(request.traffic_limit_bytes, 0)
        self.assertFalse(request.HasField("status"))
        self.assertIn("DISABLED не менялся", payload["result"]["action_label"])
        self.assertIsNone(self._marker_of(7))

    def test_remove_traffic_limit_skips_manual_limit(self):
        """Лимит в панели без маркера — ручной кап владельца: не снимаем,
        панель не трогаем, отвечаем 200 с пометкой (manual), аудит пишем."""
        client = self._client(self._panel(5 * 1024**3, 1, 2))

        response, payload = self._call("remove_traffic_limit", client)

        self.assertEqual(response.status_code, 200)
        self.assertIs(payload["result"]["rwms_updated"], False)
        self.assertIs(payload["result"]["manual"], True)
        self.assertEqual(payload["result"]["outcome"], "skipped_manual")
        self.assertTrue(payload["result"]["action_label"].startswith("Не снят"))
        self.assertIn("владелец", payload["result"]["action_label"])
        client.update_user.assert_not_called()
        self.assertEqual(self._audit_actions()[0][1]["outcome"], "skipped_manual")
        self.events.assert_not_called()

        # Маркер не совпадает с панелью (владелец сменил лимит): снимается
        # только маркер, панель не трогаем.
        self._marker(self.user, limit_bytes=1024**3, strategy="DAY")
        self.session.commit()
        with self.assertLogs(level="WARNING"):
            response, payload = self._call("remove_traffic_limit", client)
        self.assertEqual(payload["result"]["outcome"], "skipped_manual")
        client.update_user.assert_not_called()
        self.assertIsNone(self._marker_of(7))

    def test_remove_traffic_limit_is_noop_without_limit(self):
        client = self._client(self._panel())

        response, payload = self._call("remove_traffic_limit", client)

        self.assertEqual(response.status_code, 200)
        self.assertIs(payload["result"]["rwms_updated"], False)
        self.assertEqual(payload["result"]["outcome"], "unchanged")
        client.update_user.assert_not_called()

    def test_limit_actions_read_panel_strictly(self):
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = RwmsUnavailableError(
            "42", None, "panel down"
        )
        with self.assertLogs(level="WARNING"):
            response, payload = self._call("apply_trial_limit", client)
        self.assertEqual(response.status_code, 503)
        client.update_user.assert_not_called()
        client.add_user.assert_not_called()

        client = self._client(None)
        response, payload = self._call("remove_traffic_limit", client)
        self.assertEqual(response.status_code, 404)
        client.add_user.assert_not_called()

        # RWMS не принял снятие: 502, маркер остаётся (лимит в панели тоже).
        self._marker(self.user, limit_bytes=1, strategy="DAY")
        self.session.commit()
        client = self._client(self._panel(1, 1, 2), update_result=None)
        response, payload = self._call("remove_traffic_limit", client)
        self.assertEqual(response.status_code, 502)
        self.assertIsNotNone(self._marker_of(7))
        self.events.assert_not_called()

        # RWMS не принял постановку: 502, маркер откатывается вместе с savepoint.
        from common.models.db import ManagedTrafficLimit

        self.session.query(ManagedTrafficLimit).delete()
        self.session.commit()
        client = self._client(self._panel(), update_result=None)
        response, payload = self._call("apply_trial_limit", client)
        self.assertEqual(response.status_code, 502)
        self.assertIsNone(self._marker_of(7))
        self.events.assert_not_called()

    def test_limit_actions_require_marker_table(self):
        """Без таблицы managed_traffic_limits (миграция не накачена) действия
        отвечают 503 и панель не трогают — иначе ручные лимиты владельца были
        бы неотличимы от наших."""
        self._drop_marker_table()
        client = self._client(self._panel(5 * 1024**3, 1, 2))

        for action in ("apply_trial_limit", "remove_traffic_limit"):
            with self.assertLogs(level="WARNING"):
                response, payload = self._call(action, client)
            self.assertEqual(response.status_code, 503, action)
            self.assertIn("managed_traffic_limits", payload["message"])
        client.update_user.assert_not_called()
        client.get_user_by_username_strict.assert_not_called()
        self.audit.assert_not_called()

    def test_recreate_on_not_found_applies_limit_only_for_never_paid(self):
        """Продление при достоверном NOT_FOUND пересоздаёт подписку (как
        раньше); лимит пробного попадает в AddUser только при включённом
        тумблере И never_paid — и тогда же пишется маркер site:admin:<login>."""
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = None
        client.add_user.return_value = SimpleNamespace(uuid="recreated")
        self._set("trial_traffic_limit_enabled", "1")
        self._set("trial_traffic_limit_gb", "2")
        self._set("trial_traffic_limit_strategy", "week")
        self.session.commit()

        with self.assertLogs(level="WARNING"):
            response, payload = self._call("extend", client, days="3")

        self.assertEqual(response.status_code, 200)
        add_request = client.add_user.call_args.args[0]
        self.assertTrue(add_request.HasField("traffic_limit_bytes"))
        self.assertEqual(add_request.traffic_limit_bytes, 2 * 1024**3)
        marker = self._marker_of(7)
        self.assertEqual((marker.limit_bytes, marker.strategy), (2 * 1024**3, "WEEK"))
        self.assertTrue(marker.applied_by.startswith("site:admin:"))
        self.assertEqual(self._event_types(), ["traffic_limit_applied"])

        # Платившему лимит не ставится и маркер не пишется.
        from common.models.db import ManagedTrafficLimit

        self.session.query(ManagedTrafficLimit).delete()
        self._yk_payment(self.user)
        self.session.commit()
        client.add_user.reset_mock()
        with self.assertLogs(level="WARNING"):
            response, payload = self._call("extend", client, days="3")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(client.add_user.call_args.args[0].HasField("traffic_limit_bytes"))
        self.assertIsNone(self._marker_of(7))

        # Выключенный тумблер — как раньше, даже для never_paid.
        self.session.query(YkPayment).delete()
        self._set("trial_traffic_limit_enabled", "0")
        self.session.commit()
        client.add_user.reset_mock()
        with self.assertLogs(level="WARNING"):
            self._call("extend", client, days="3")
        self.assertFalse(client.add_user.call_args.args[0].HasField("traffic_limit_bytes"))
        self.assertIsNone(self._marker_of(7))

    def test_push_to_panel_keeps_panel_traffic_limit_untouched(self):
        """Синхронизация «БД → панель» обновляет срок/статус/сквады, но не
        передаёт traffic_limit_bytes/traffic_limit_strategy — лимит и
        стратегия сброса ограниченного триала в панели остаются как есть."""
        import proto.rwmanager_pb2 as rw_proto

        from engine.views import support_admin_api_rwms_sync

        client = mock.Mock()
        client.get_user_by_username.return_value = SimpleNamespace(
            uuid="panel-uuid",
            status=rw_proto.UserStatus.ACTIVE,
            active_internal_squads=[SimpleNamespace(uuid="squad-1")],
            traffic_limit_bytes=5 * 1024**3,
            traffic_limit_strategy=rw_proto.TrafficLimitStrategy.DAY,
        )
        client.update_user.return_value = SimpleNamespace(uuid="panel-uuid")
        session = self._Session()
        user = SimpleNamespace(
            id=7, username="42", email=None, telegram_id=42,
            expire_at=datetime(2030, 1, 1, 12, 0, 0), autopay_allow=True,
        )
        request = RequestFactory().post(
            "/support-admin/api/rwms-sync/", data={"q": "42", "action": "push_to_panel"}
        )
        request.session = {}
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=session),
            mock.patch("engine.views.admin_find_user", return_value=user),
            mock.patch("engine.views.rwms_client", client),
        ):
            response = support_admin_api_rwms_sync(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["result"]["synced"], "to_panel")
        client.update_user.assert_called_once()
        request = client.update_user.call_args.args[0]
        self.assertEqual(request.uuid, "panel-uuid")
        self.assertFalse(request.HasField("traffic_limit_strategy"))
        self.assertFalse(request.HasField("traffic_limit_bytes"))
        self.assertTrue(request.HasField("expire_at"))
        self.assertEqual(request.status, rw_proto.UserStatus.ACTIVE)
        self.assertEqual(list(request.active_internal_squads), ["squad-1"])
        client.add_user.assert_not_called()
        self.assertEqual(self._fake_session_audit_actions(session)[0][0], "rwms_sync_push")
        self.assertEqual(session.commits, 1)

    def test_bulk_extend_keeps_panel_traffic_limit_untouched(self):
        """Массовое продление (bulk extend_days) — тот же контракт: лимит и
        стратегия сброса панели не передаются, не снимаются и не ломаются."""
        import proto.rwmanager_pb2 as rw_proto

        from engine.views import admin_bulk_extend

        client = mock.Mock()
        client.get_user_by_username.return_value = SimpleNamespace(
            uuid="panel-uuid",
            email="user@example.com",
            telegram_id=42,
            status=rw_proto.UserStatus.ACTIVE,
            active_internal_squads=[SimpleNamespace(uuid="squad-1")],
            traffic_limit_bytes=5 * 1024**3,
            traffic_limit_strategy=rw_proto.TrafficLimitStrategy.DAY,
        )
        client.update_user.return_value = SimpleNamespace(uuid="panel-uuid")
        user = SimpleNamespace(
            id=7, username="42", expire_at=datetime(2030, 1, 1, 12, 0, 0)
        )

        with mock.patch("engine.views.rwms_client", client):
            message = admin_bulk_extend(self._Session(), user, 3)

        self.assertTrue(message.startswith("продлено до "))
        self.assertEqual(user.expire_at, datetime(2030, 1, 4, 12, 0, 0))
        client.update_user.assert_called_once()
        request = client.update_user.call_args.args[0]
        self.assertEqual(request.uuid, "panel-uuid")
        self.assertFalse(request.HasField("traffic_limit_strategy"))
        self.assertFalse(request.HasField("traffic_limit_bytes"))
        self.assertTrue(request.HasField("expire_at"))
        self.assertEqual(request.status, rw_proto.UserStatus.ACTIVE)
        self.assertEqual(request.email, "user@example.com")
        self.assertEqual(list(request.active_internal_squads), ["squad-1"])
        client.add_user.assert_not_called()


class AntiabuseBulkListTests(_AntiabuseSqliteMixin, SimpleTestCase):
    """Массовые операции по списку пользователей (support-admin/api/bulk/):
    платившие и ручные лимиты владельца пропускаются (applied не растёт),
    маркер site:bulk пишется в той же транзакции."""

    def setUp(self):
        super().setUp()
        self.audit = mock.Mock()
        self.events = mock.Mock()
        self.user = self._user(
            7, "42", telegram_id=42, expire_at=datetime(2030, 1, 1), autopay_allow=True
        )
        self.session.commit()

    def _call(self, action, client, dry_run, ids="42"):
        from engine.views import support_admin_api_bulk

        request = RequestFactory().post(
            "/support-admin/api/bulk/",
            data={"ids": ids, "action": action, "dry_run": "1" if dry_run else "0"},
        )
        request.session = {}
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=self.session),
            mock.patch("engine.views.rwms_client", client),
            mock.patch("engine.views.admin_audit_write", self.audit),
            mock.patch("engine.views.add_traffic_limit_event", self.events),
        ):
            response = support_admin_api_bulk(request)
        return response.status_code, json.loads(response.content)

    def _panel(self, limit_bytes=0, strategy=0, status=0):
        return SimpleNamespace(
            uuid="panel-uuid",
            traffic_limit_bytes=limit_bytes,
            traffic_limit_strategy=strategy,
            status=status,
        )

    def test_bulk_actions_registered(self):
        from engine.views import BULK_ACTIONS

        self.assertIn("apply_trial_limit", BULK_ACTIONS)
        self.assertIn("remove_traffic_limit", BULK_ACTIONS)

    def test_dry_run_previews_limit_without_touching_panel(self):
        client = mock.Mock()

        status, payload = self._call("apply_trial_limit", client, dry_run=True)

        self.assertEqual(payload["status"], "ok")
        self.assertIn("5 ГиБ · ежедневно", payload["result"]["rows"][0]["message"])
        self.assertIn("ручной лимит панели не трогается", payload["result"]["rows"][0]["message"])
        status, payload = self._call("remove_traffic_limit", client, dry_run=True)
        self.assertIn("сквады не меняются", payload["result"]["rows"][0]["message"])
        self.assertIn("только управляемый", payload["result"]["rows"][0]["message"])
        client.get_user_by_username_strict.assert_not_called()
        client.update_user.assert_not_called()

    def test_apply_skips_paid_and_updates_never_paid(self):
        import proto.rwmanager_pb2 as rw_proto

        paid = self._user(8, "paid", expire_at=datetime(2030, 1, 1), autopay_allow=True)
        self._yk_payment(paid)
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = self._panel()
        client.update_user.return_value = SimpleNamespace(uuid="panel-uuid")

        status, payload = self._call("apply_trial_limit", client, dry_run=False, ids="paid")

        row = payload["result"]["rows"][0]
        self.assertIn("платил", row["message"])
        self.assertEqual(row["outcome"], "skipped_paid")
        # Пропущенный платящий — не «применено».
        self.assertEqual(payload["result"]["applied"], 0)
        self.assertEqual(payload["result"]["skipped_paid"], 1)
        client.update_user.assert_not_called()
        self.assertIsNone(self._marker_of(8))

        status, payload = self._call("apply_trial_limit", client, dry_run=False)

        self.assertEqual(payload["result"]["applied"], 1)
        self.assertEqual(payload["result"]["skipped_paid"], 0)
        request = client.update_user.call_args.args[0]
        self.assertEqual(request.traffic_limit_bytes, 5 * 1024**3)
        self.assertEqual(request.traffic_limit_strategy, rw_proto.TrafficLimitStrategy.DAY)
        self.assertEqual(list(request.active_internal_squads), [])
        marker = self._marker_of(7)
        self.assertEqual(marker.applied_by, "site:bulk")
        self.assertEqual((marker.reason, marker.release_on), ("trial", "payment"))
        audit = self.audit.call_args_list[-1]
        self.assertEqual(audit.args[2], "bulk_apply_trial_limit")
        self.assertEqual(audit.kwargs["limit_gb"], 5.0)
        self.assertEqual(self.audit.call_args_list[0].kwargs["skipped_paid"], 1)
        self.assertEqual([c.args[2] for c in self.events.call_args_list], ["traffic_limit_applied"])

    def test_apply_skips_manual_limit_and_marks_equal_limit(self):
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = self._panel(1024**3, 1)

        status, payload = self._call("apply_trial_limit", client, dry_run=False)

        self.assertEqual(payload["result"]["applied"], 0)
        self.assertEqual(payload["result"]["skipped_manual"], 1)
        self.assertEqual(payload["result"]["rows"][0]["outcome"], "skipped_manual")
        self.assertIn("ручной лимит панели", payload["result"]["rows"][0]["message"])
        client.update_user.assert_not_called()
        self.assertIsNone(self._marker_of(7))

        client.get_user_by_username_strict.return_value = self._panel(5 * 1024**3, 1)
        status, payload = self._call("apply_trial_limit", client, dry_run=False)

        self.assertEqual(payload["result"]["applied"], 1)
        self.assertEqual(payload["result"]["rows"][0]["outcome"], "marked")
        client.update_user.assert_not_called()
        self.assertEqual(self._marker_of(7).applied_by, "site:bulk")

    def test_apply_skips_bot_admin_limit_marker(self):
        """Маркер из кнопки алерта бота (release_on='manual') массовая операция
        по списку не перезаписывает: skipped_admin_limit, панель и маркер не
        тронуты, applied не растёт."""
        self._marker(
            self.user,
            limit_bytes=1024**3,
            strategy="DAY",
            reason="traffic_abuse",
            release_on="manual",
            applied_by="bot:admin:1",
        )
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = self._panel(1024**3, 1)

        status, payload = self._call("apply_trial_limit", client, dry_run=False)

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["applied"], 0)
        self.assertEqual(payload["result"]["skipped_admin_limit"], 1)
        self.assertEqual(payload["result"]["skipped_manual"], 0)
        row = payload["result"]["rows"][0]
        self.assertEqual(row["outcome"], "skipped_admin_limit")
        self.assertIn("лимит админа из бота", row["message"])
        self.assertIn("bot:admin:1", row["message"])
        client.update_user.assert_not_called()
        marker = self._marker_of(7)
        self.assertEqual((marker.reason, marker.release_on, marker.limit_bytes), ("traffic_abuse", "manual", 1024**3))
        self.assertEqual(self.audit.call_args.kwargs["skipped_admin_limit"], 1)
        self.events.assert_not_called()

    def test_remove_rows_and_rwms_outage_are_reported_per_row(self):
        self._marker(self.user, limit_bytes=10, strategy="DAY")
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = self._panel(10, 1, 2)
        client.update_user.return_value = SimpleNamespace(uuid="panel-uuid")

        status, payload = self._call("remove_traffic_limit", client, dry_run=False)

        self.assertEqual(payload["result"]["applied"], 1)
        self.assertEqual(client.update_user.call_args.args[0].traffic_limit_bytes, 0)
        self.assertIsNone(self._marker_of(7))
        self.assertEqual([c.args[2] for c in self.events.call_args_list], ["traffic_limit_released"])

        # Без маркера лимит ручной — не снимаем.
        client.update_user.reset_mock()
        status, payload = self._call("remove_traffic_limit", client, dry_run=False)
        self.assertEqual(payload["result"]["applied"], 0)
        self.assertEqual(payload["result"]["skipped_manual"], 1)
        client.update_user.assert_not_called()

        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = RwmsUnavailableError("42", None, "down")
        with self.assertLogs(level="ERROR"):
            status, payload = self._call("remove_traffic_limit", client, dry_run=False)
        self.assertEqual(payload["result"]["applied"], 0)
        self.assertIs(payload["result"]["rows"][0]["ok"], False)
        client.add_user.assert_not_called()

    def test_bulk_limit_actions_require_marker_table(self):
        self._drop_marker_table()
        client = mock.Mock()

        for action in ("apply_trial_limit", "remove_traffic_limit"):
            with self.assertLogs(level="WARNING"):
                status, payload = self._call(action, client, dry_run=False)
            self.assertEqual(status, 503, action)
            self.assertIn("managed_traffic_limits", payload["message"])
        client.get_user_by_username_strict.assert_not_called()
        # Предпросмотр таблицы не требует.
        status, payload = self._call("apply_trial_limit", client, dry_run=True)
        self.assertEqual(status, 200)


class AntiabuseSegmentBulkTests(_AntiabuseSqliteMixin, SimpleTestCase):
    """Массовые операции по сегменту (support-admin/api/antiabuse-bulk/)."""

    def setUp(self):
        super().setUp()
        self.audit = mock.Mock()
        self.events = mock.Mock()

    def _post(self, client, **data):
        from engine.views import support_admin_api_antiabuse_bulk

        request = RequestFactory().post("/support-admin/api/antiabuse-bulk/", data=data)
        request.session = {}
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=self.session),
            mock.patch("engine.views.rwms_client", client),
            mock.patch("engine.views.admin_audit_write", self.audit),
            mock.patch("engine.views.add_traffic_limit_event", self.events),
        ):
            response = support_admin_api_antiabuse_bulk(request)
        return response.status_code, json.loads(response.content)

    def _paid_users(self):
        p1 = self._user(1, "p1")
        p2 = self._user(2, "p2")
        p3 = self._user(3, "p3")
        for user in (p1, p2, p3):
            self._yk_payment(user)
        self._user(4, "trial")
        self.session.commit()

    def test_dry_run_counts_segment_without_panel_calls(self):
        self._paid_users()
        client = mock.Mock()

        status, payload = self._post(client, action="remove_paid_limit", dry_run="1")

        self.assertEqual(status, 200)
        result = payload["result"]
        self.assertIs(result["dry_run"], True)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["preview"], ["p1", "p2", "p3"])
        self.assertIsNone(result["limit_label"])
        client.get_user_by_username_strict.assert_not_called()
        client.update_user.assert_not_called()
        self.audit.assert_not_called()

        status, payload = self._post(client, action="remove_trial_limit", dry_run="1")
        self.assertEqual(payload["result"]["total"], 1)
        self.assertEqual(payload["result"]["preview"], ["trial"])

    def test_remove_processes_in_batches_and_skips_unlimited_and_manual(self):
        """Снимается только управляемый лимит (маркер + панель совпадают):
        p1 — маркер есть → снят; p2 — лимита нет → пропуск; p3 — лимит без
        маркера (ручной кап владельца) → skipped_manual, панель не тронута."""
        import proto.rwmanager_pb2 as rw_proto

        self._paid_users()
        self._marker(self.session.get(User, 1), limit_bytes=5 * 1024**3, strategy="DAY")
        self.session.commit()
        panel = {
            "p1": SimpleNamespace(uuid="u1", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=2),
            "p2": SimpleNamespace(uuid="u2", traffic_limit_bytes=0, traffic_limit_strategy=0, status=0),
            "p3": SimpleNamespace(uuid="u3", traffic_limit_bytes=1024, traffic_limit_strategy=1, status=0),
        }
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = lambda name: panel[name]
        client.update_user.return_value = SimpleNamespace()

        status, payload = self._post(
            client, action="remove_paid_limit", dry_run="0", batch_size="2", after_id="0"
        )

        self.assertEqual(status, 200)
        result = payload["result"]
        self.assertEqual((result["processed"], result["applied"], result["skipped"], result["failed"]), (2, 1, 1, 0))
        self.assertEqual(result["skipped_manual"], 0)
        self.assertIs(result["done"], False)
        self.assertEqual(result["next_after_id"], 2)
        self.assertIsNone(self._marker_of(1))

        status, payload = self._post(
            client, action="remove_paid_limit", dry_run="0", batch_size="2", after_id="2"
        )

        result = payload["result"]
        self.assertEqual((result["processed"], result["applied"], result["skipped_manual"]), (1, 0, 1))
        self.assertIs(result["done"], True)
        requests = [call.args[0] for call in client.update_user.call_args_list]
        self.assertEqual([r.uuid for r in requests], ["u1"])
        for request in requests:
            self.assertEqual(request.traffic_limit_bytes, 0)
            self.assertEqual(request.traffic_limit_strategy, rw_proto.TrafficLimitStrategy.NO_RESET)
            self.assertEqual(request.status, rw_proto.UserStatus.ACTIVE)
            self.assertEqual(list(request.active_internal_squads), [])
        self.assertEqual(self.audit.call_count, 2)
        self.assertEqual(self.audit.call_args_list[0].args[2], "antiabuse_bulk_remove_paid_limit")
        self.assertEqual(self.audit.call_args_list[1].kwargs["skipped_manual"], 1)
        self.assertEqual([c.args[2] for c in self.events.call_args_list], ["traffic_limit_released"])
        client.add_user.assert_not_called()

    def test_remove_drops_stale_marker_without_touching_panel(self):
        """Маркер не совпадает с панелью (владелец сменил лимит руками):
        маркер снимается, панель не трогается."""
        self._paid_users()
        self._marker(self.session.get(User, 1), limit_bytes=1024**3, strategy="DAY")
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = SimpleNamespace(
            uuid="u1", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=2
        )

        with self.assertLogs(level="WARNING"):
            status, payload = self._post(client, action="remove_paid_limit", dry_run="0", batch_size="1")

        self.assertEqual(status, 200)
        self.assertEqual((payload["result"]["applied"], payload["result"]["skipped_manual"]), (0, 1))
        client.update_user.assert_not_called()
        self.assertIsNone(self._marker_of(1))

    def test_bulk_requires_marker_table(self):
        self._paid_users()
        self._drop_marker_table()
        client = mock.Mock()

        with self.assertLogs(level="WARNING"):
            status, payload = self._post(client, action="remove_paid_limit", dry_run="0")

        self.assertEqual(status, 503)
        self.assertIn("managed_traffic_limits", payload["message"])
        client.get_user_by_username_strict.assert_not_called()
        self.audit.assert_not_called()
        # Предпросмотр (только подсчёт по БД) таблицы маркеров не требует.
        status, payload = self._post(client, action="remove_paid_limit", dry_run="1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["total"], 3)

    def test_rwms_unavailable_returns_503_with_partial_progress(self):
        self._paid_users()
        self._marker(self.session.get(User, 1), limit_bytes=10, strategy="DAY")
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = [
            SimpleNamespace(uuid="u1", traffic_limit_bytes=10, traffic_limit_strategy=1, status=2),
            RwmsUnavailableError("p2", None, "down"),
        ]
        client.update_user.return_value = SimpleNamespace()

        with self.assertLogs(level="WARNING"):
            status, payload = self._post(client, action="remove_paid_limit", dry_run="0")

        self.assertEqual(status, 503)
        self.assertIn("прогресс сохранён", payload["message"])
        result = payload["result"]
        self.assertEqual((result["processed"], result["applied"]), (1, 1))
        self.assertEqual(result["next_after_id"], 1)
        self.assertIs(result["done"], False)
        self.assertEqual(client.update_user.call_count, 1)
        self.assertIs(self.audit.call_args.kwargs["rwms_unavailable"], True)

    def test_apply_trial_limit_requires_enabled_toggle_and_uses_segment(self):
        import proto.rwmanager_pb2 as rw_proto

        self._user(4, "trial")
        self.session.commit()
        client = mock.Mock()

        status, payload = self._post(client, action="apply_trial_limit", dry_run="1")

        self.assertEqual(status, 400)
        self.assertIn("включите", payload["message"])
        client.get_user_by_username_strict.assert_not_called()

        self._set("trial_traffic_limit_enabled", "1")
        self._set("trial_traffic_limit_gb", "0.5")
        self.session.commit()
        client.get_user_by_username_strict.return_value = SimpleNamespace(
            uuid="u4", traffic_limit_bytes=0, traffic_limit_strategy=0, status=0
        )
        client.update_user.return_value = SimpleNamespace()
        # trial_active использует now() AT TIME ZONE (Postgres) — на SQLite
        # подменяем условие сегмента, сам конвейер порций проверяем как есть.
        with mock.patch("engine.views.antiabuse_bulk_where_sql", return_value="u.username = 'trial'"):
            status, payload = self._post(client, action="apply_trial_limit", dry_run="1")
            self.assertEqual(payload["result"]["total"], 1)
            self.assertEqual(payload["result"]["limit_label"], "512 МиБ · ежедневно".replace("512 МиБ", "0.5 ГиБ"))
            status, payload = self._post(client, action="apply_trial_limit", dry_run="0")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["applied"], 1)
        self.assertEqual((payload["result"]["marked"], payload["result"]["skipped_paid"]), (0, 0))
        request = client.update_user.call_args.args[0]
        self.assertEqual(request.traffic_limit_bytes, 536870912)
        self.assertEqual(request.traffic_limit_strategy, rw_proto.TrafficLimitStrategy.DAY)
        self.assertFalse(request.HasField("status"))
        marker = self._marker_of(4)
        self.assertEqual((marker.limit_bytes, marker.strategy, marker.applied_by), (536870912, "DAY", "site:bulk"))
        self.assertEqual([c.args[2] for c in self.events.call_args_list], ["traffic_limit_applied"])

    def test_apply_skips_paid_and_manual_marks_equal(self):
        """apply по сегменту: платившему лимит не ставится (skipped_paid,
        applied не растёт), ручной кап владельца пропускается (skipped_manual),
        панель с ровно нашим лимитом без маркера — только помечается (marked)."""
        trial = self._user(1, "trial")
        paid = self._user(2, "paid")
        self._user(3, "manual")
        self._user(4, "equal")
        self._yk_payment(paid)
        self._set("trial_traffic_limit_enabled", "1")
        self.session.commit()
        panel = {
            "trial": SimpleNamespace(uuid="u1", traffic_limit_bytes=0, traffic_limit_strategy=0, status=0),
            "paid": SimpleNamespace(uuid="u2", traffic_limit_bytes=0, traffic_limit_strategy=0, status=0),
            "manual": SimpleNamespace(uuid="u3", traffic_limit_bytes=1024**3, traffic_limit_strategy=1, status=0),
            "equal": SimpleNamespace(uuid="u4", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=0),
        }
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = lambda name: panel[name]
        client.update_user.return_value = SimpleNamespace()

        with mock.patch(
            "engine.views.antiabuse_bulk_where_sql",
            return_value="u.username IN ('trial', 'paid', 'manual', 'equal')",
        ):
            status, payload = self._post(client, action="apply_trial_limit", dry_run="0")

        self.assertEqual(status, 200)
        result = payload["result"]
        self.assertEqual(
            (result["processed"], result["applied"], result["marked"], result["skipped_paid"], result["skipped_manual"]),
            (4, 1, 1, 1, 1),
        )
        self.assertEqual([c.args[0].uuid for c in client.update_user.call_args_list], ["u1"])
        self.assertIsNotNone(self._marker_of(1))
        self.assertIsNone(self._marker_of(2))
        self.assertIsNone(self._marker_of(3))
        self.assertEqual(self._marker_of(4).applied_by, "site:bulk")
        self.assertEqual(self.audit.call_args.kwargs["skipped_paid"], 1)
        self.assertEqual(self.audit.call_args.kwargs["marked"], 1)

    def test_apply_skips_bot_admin_limit_markers_with_counter(self):
        """Сегментный apply не перезаписывает маркеры бота с release_on='manual':
        лимит с другой сигнатурой — skipped_admin_limit (панель и маркер не
        тронуты); с ровно лимитом пробных — unchanged, release_on остаётся."""
        self._user(1, "trial")
        self._user(2, "adminlim")
        self._user(3, "adminequal")
        self._marker(
            self.session.get(User, 2),
            limit_bytes=1024**3,
            strategy="DAY",
            reason="ip_abuse",
            release_on="manual",
            applied_by="bot:admin:1",
        )
        self._marker(
            self.session.get(User, 3),
            reason="traffic_abuse",
            release_on="manual",
            applied_by="bot:admin:1",
        )
        self._set("trial_traffic_limit_enabled", "1")
        self.session.commit()
        panel = {
            "trial": SimpleNamespace(uuid="u1", traffic_limit_bytes=0, traffic_limit_strategy=0, status=0),
            "adminlim": SimpleNamespace(uuid="u2", traffic_limit_bytes=1024**3, traffic_limit_strategy=1, status=0),
            "adminequal": SimpleNamespace(uuid="u3", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=0),
        }
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = lambda name: panel[name]
        client.update_user.return_value = SimpleNamespace()

        with mock.patch(
            "engine.views.antiabuse_bulk_where_sql",
            return_value="u.username IN ('trial', 'adminlim', 'adminequal')",
        ):
            status, payload = self._post(client, action="apply_trial_limit", dry_run="0")

        self.assertEqual(status, 200)
        result = payload["result"]
        self.assertEqual(
            (result["processed"], result["applied"], result["skipped_admin_limit"], result["skipped_manual"], result["skipped"]),
            (3, 1, 1, 0, 1),
        )
        self.assertEqual([c.args[0].uuid for c in client.update_user.call_args_list], ["u1"])
        marker = self._marker_of(2)
        self.assertEqual((marker.reason, marker.release_on, marker.limit_bytes), ("ip_abuse", "manual", 1024**3))
        marker = self._marker_of(3)
        self.assertEqual((marker.reason, marker.release_on), ("traffic_abuse", "manual"))
        self.assertEqual(self.audit.call_args.kwargs["skipped_admin_limit"], 1)
        self.assertEqual([c.args[2] for c in self.events.call_args_list], ["traffic_limit_applied"])

    def test_apply_commits_marker_per_user_right_after_update(self):
        """Маркер каждого пользователя коммитится сразу после его UpdateUser
        (короткая транзакция), а не раз на порцию: сбой commit на N-м
        пользователе не оставит в панели N-1 лимитов без маркеров. Проверяем
        порядок commit/UpdateUser (сам откат на SQLite не воспроизвести:
        pysqlite фиксирует транзакцию на RELEASE внешнего SAVEPOINT)."""
        self._user(1, "t1")
        self._user(2, "t2")
        self._set("trial_traffic_limit_enabled", "1")
        self.session.commit()
        timeline = []
        real_commit = self.session.commit

        def commit():
            timeline.append("commit")
            real_commit()

        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = lambda name: SimpleNamespace(
            uuid=name, traffic_limit_bytes=0, traffic_limit_strategy=0, status=0
        )

        def update_user(request):
            timeline.append(f"update:{request.uuid}")
            return SimpleNamespace()

        client.update_user.side_effect = update_user

        with (
            mock.patch("engine.views.antiabuse_bulk_where_sql", return_value="u.username IN ('t1', 't2')"),
            mock.patch.object(self.session, "commit", side_effect=commit),
        ):
            status, payload = self._post(client, action="apply_trial_limit", dry_run="0")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["applied"], 2)
        # UpdateUser → commit → UpdateUser → commit → commit аудита порции.
        self.assertEqual(timeline, ["update:t1", "commit", "update:t2", "commit", "commit"])
        self.assertIsNotNone(self._marker_of(1))
        self.assertIsNotNone(self._marker_of(2))
        self.assertEqual(self.audit.call_count, 1)

    def test_rejects_unknown_action_and_bad_params(self):
        client = mock.Mock()
        status, payload = self._post(client, action="delete_everyone", dry_run="1")
        self.assertEqual(status, 400)
        status, payload = self._post(client, action="remove_paid_limit", after_id="x")
        self.assertEqual(status, 400)

    def test_segments_come_from_common_registry(self):
        from common.models.segments import segment_where_sql
        from engine.views import ANTIABUSE_BULK_ACTIONS, antiabuse_bulk_where_sql

        self.assertEqual(
            {key: value[0] for key, value in ANTIABUSE_BULK_ACTIONS.items()},
            {
                "apply_trial_limit": "trial_active",
                "remove_trial_limit": "never_paid",
                "remove_paid_limit": "paid_any",
            },
        )
        self.assertEqual(
            antiabuse_bulk_where_sql("apply_trial_limit"),
            segment_where_sql("trial_active", requires_telegram=False),
        )
        self.assertNotIn("telegram_id IS NOT NULL", antiabuse_bulk_where_sql("remove_trial_limit"))


class AntiabuseIpguardAlertsTests(_AntiabuseSqliteMixin, SimpleTestCase):
    def _get(self, client):
        from engine.views import support_admin_api_ipguard_alerts

        request = RequestFactory().get("/support-admin/api/ipguard-alerts/")
        request.session = {}
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=self.session),
            mock.patch("engine.views.rwms_client", client),
        ):
            response = support_admin_api_ipguard_alerts(request)
        return response.status_code, json.loads(response.content)

    def _seed(self):
        from common.models.db import IpAlert
        from common.models.db import UserIpObservation

        self._user(1, "123456")
        self.session.add_all([
            IpAlert(id=1, username="777", level="alert", unique_ip_count=30, unique_subnet_count=25,
                    threshold=10, window_hours=24, created_at=datetime(2026, 9, 1, 12, 0, 0)),
            IpAlert(id=2, username="777", level="suspicious", unique_ip_count=12, unique_subnet_count=11,
                    threshold=10, window_hours=24, created_at=datetime(2026, 9, 1, 6, 0, 0)),
            IpAlert(id=3, username="not-a-number", level="alert", unique_ip_count=5, unique_subnet_count=5,
                    threshold=1, window_hours=24, created_at=datetime(2026, 8, 30, 6, 0, 0)),
            UserIpObservation(id=1, username="777", ip="1.2.3.4", subnet="1.2.3.0/24", node="nl1", hits=1,
                              first_seen=datetime(2026, 9, 1, 11, 0, 0), last_seen=datetime(2026, 9, 1, 11, 0, 0)),
            UserIpObservation(id=2, username="777", ip="5.6.7.8", subnet="5.6.7.0/24", node="de1", hits=1,
                              first_seen=datetime(2026, 8, 31, 1, 0, 0), last_seen=datetime(2026, 8, 31, 1, 0, 0)),
            UserIpObservation(id=3, username="777", ip="9.9.9.9", subnet="9.9.9.0/24", node="fi1", hits=1,
                              first_seen=datetime(2026, 8, 31, 20, 0, 0), last_seen=datetime(2026, 8, 31, 20, 0, 0)),
        ])
        self.session.commit()

    def test_alerts_resolve_panel_ids_with_per_request_cache(self):
        self._seed()
        client = mock.Mock()
        client.get_user_by_id.return_value = SimpleNamespace(username="123456", uuid="uuid-1")

        status, payload = self._get(client)

        self.assertEqual(status, 200)
        rows = payload["result"]["alerts"]
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])
        # Один RPC на уникальный числовой ID; нечисловой username не резолвится.
        client.get_user_by_id.assert_called_once_with(777)
        self.assertEqual(rows[0]["panel_id"], "777")
        self.assertEqual(rows[0]["username"], "123456")
        self.assertEqual(rows[0]["user_uuid"], "uuid-1")
        self.assertIs(rows[0]["local_user"], True)
        self.assertEqual(rows[0]["level"], "alert")
        self.assertEqual(rows[0]["unique_subnet_count"], 25)
        self.assertEqual(rows[0]["created_at"], "01.09.2026 15:00")
        # Ноды — только из окна алерта (24 ч до created_at): de1 старше.
        self.assertEqual(rows[0]["nodes"], ["fi1", "nl1"])
        self.assertEqual(rows[1]["nodes"], ["fi1"])
        self.assertIsNone(rows[2]["username"])
        self.assertIs(rows[2]["local_user"], False)
        self.assertEqual(payload["result"]["limit"], 50)

    def test_alerts_degrade_to_panel_id_when_rwms_unavailable(self):
        self._seed()
        client = mock.Mock()
        client.get_user_by_id.return_value = None

        status, payload = self._get(client)

        self.assertEqual(status, 200)
        rows = payload["result"]["alerts"]
        self.assertEqual(rows[0]["panel_id"], "777")
        self.assertIsNone(rows[0]["username"])
        self.assertIs(rows[0]["local_user"], False)

        client = mock.Mock()
        client.get_user_by_id.side_effect = RuntimeError("boom")
        with self.assertLogs(level="ERROR"):
            status, payload = self._get(client)
        self.assertEqual(status, 200)
        self.assertIsNone(payload["result"]["alerts"][0]["username"])

    def test_resolved_username_missing_locally_is_flagged(self):
        self._seed()
        client = mock.Mock()
        client.get_user_by_id.return_value = SimpleNamespace(username="stranger", uuid="u")

        status, payload = self._get(client)

        row = payload["result"]["alerts"][0]
        self.assertEqual(row["username"], "stranger")
        self.assertIs(row["local_user"], False)

    def test_endpoint_is_read_only_get(self):
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_ipguard_alerts)
        self.assertIn('if request.method != "GET":', src)
        for name in ("update_user", "add_user", "delete"):
            self.assertNotIn(name, src)


class AntiabuseSettingsEndpointTests(_AntiabuseSqliteMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.audit = mock.Mock()

    def _request(self, method="GET", **data):
        from engine.views import support_admin_api_antiabuse

        factory = RequestFactory()
        if method == "POST":
            request = factory.post("/support-admin/api/antiabuse/", data=data)
        elif method == "GET":
            request = factory.get("/support-admin/api/antiabuse/")
        else:
            request = factory.generic(method, "/support-admin/api/antiabuse/")
        request.session = {}
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=self.session),
            mock.patch("engine.views.admin_audit_write", self.audit),
        ):
            response = support_admin_api_antiabuse(request)
        return response.status_code, json.loads(response.content)

    def test_get_returns_common_defaults_when_nothing_is_set(self):
        status, payload = self._request()

        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertIs(effective["trial_traffic_limit_enabled"], False)
        self.assertEqual(effective["trial_traffic_limit_gb"], 5.0)
        self.assertEqual(effective["trial_traffic_limit_bytes"], 5 * 1024**3)
        self.assertEqual(effective["trial_traffic_limit_strategy"], "day")
        self.assertEqual(effective["trial_traffic_limit_label"], "5 ГиБ · ежедневно")
        self.assertIs(effective["ipguard_alerts_enabled"], False)
        # v4: период прогона виден в админке (раньше жил только в env
        # коллектора), суточный слой по умолчанию разрешён — иначе включение
        # рубильника изменило бы поведение уже работающих установок.
        self.assertEqual(effective["ipguard_check_interval_seconds"], 300)
        self.assertIs(effective["ipguard_subnets_enabled"], True)
        self.assertEqual(effective["ipguard_alert_segment"], "never_paid")
        self.assertEqual(effective["ipguard_subnets_per_hwid"], 10)
        self.assertEqual(effective["ipguard_window_hours"], 24)
        self.assertEqual(effective["ipguard_alert_cooldown_hours"], 6)
        self.assertIs(effective["ipguard_warnings_enabled"], False)
        self.assertEqual(effective["ipguard_warning_subnets_per_hwid"], 3)
        # v3: всплеск, гео, автобан с предохранителями, исключения адресов.
        self.assertIs(effective["ipguard_burst_enabled"], False)
        self.assertEqual(effective["ipguard_burst_window_minutes"], 2)
        self.assertEqual(effective["ipguard_burst_ips_per_hwid"], 2)
        self.assertEqual(effective["ipguard_burst_confirmations"], 2)
        # Нижняя граница по подсетям для всплеска: дефолт common, а не 1 —
        # иначе первый же ложняк с ротацией CGNAT повторился бы из коробки.
        self.assertEqual(effective["ipguard_burst_min_subnets"], 2)
        self.assertIs(effective["ipguard_geo_enabled"], False)
        self.assertEqual(effective["ipguard_geo_window_minutes"], 5)
        self.assertEqual(effective["ipguard_geo_min_regions"], 2)
        self.assertIs(effective["ipguard_autoban_enabled"], False)
        self.assertEqual(effective["ipguard_autoban_segment"], "never_paid")
        self.assertEqual(
            effective["ipguard_autoban_segment_label"], "Только пробные без платежа"
        )
        self.assertEqual(effective["ipguard_autoban_steps_minutes"], "15,60,1440")
        self.assertEqual(
            effective["ipguard_autoban_steps_label"], "15 мин → 60 мин → 1440 мин"
        )
        self.assertEqual(effective["ipguard_autoban_escalation_window_hours"], 24)
        self.assertEqual(effective["ipguard_autoban_max_per_hour"], 10)
        # Порог автобана — абсолютный по суточному слою, гистерезис по прогонам,
        # пробный режим по умолчанию ВКЛЮЧЁН: бан не выполняется из коробки.
        self.assertEqual(effective["ipguard_autoban_min_subnets"], 30)
        self.assertEqual(effective["ipguard_autoban_confirmations"], 2)
        self.assertIs(effective["ipguard_autoban_dry_run"], True)
        self.assertEqual(effective["ipguard_max_alerts_per_hour"], 30)
        self.assertEqual(effective["ipguard_excluded_ips"], "")
        self.assertEqual(effective["ipguard_excluded_ips_count"], 0)
        self.assertEqual(effective["ipguard_excluded_ips_label"], "не заданы")
        self.assertEqual(effective["ipguard_excluded_usernames"], "")
        self.assertEqual(effective["ipguard_excluded_usernames_list"], [])
        self.assertEqual(effective["ipguard_excluded_usernames_count"], 0)
        self.assertEqual(effective["ipguard_excluded_usernames_label"], "не заданы")
        self.assertIs(payload["managed_limits_available"], True)
        self.assertEqual(len(payload["settings"]), 31)
        self.assertEqual(
            [item["key"] for item in payload["settings"]][-2:],
            ["ipguard_excluded_ips", "ipguard_excluded_usernames"],
        )
        # Ключи автобана — подряд, в порядке полей карточки: предохранитель,
        # порог, подтверждения, пробный режим, потолок алертов.
        self.assertEqual(
            [item["key"] for item in payload["settings"]][24:29],
            [
                "ipguard_autoban_max_per_hour",
                "ipguard_autoban_min_subnets",
                "ipguard_autoban_confirmations",
                "ipguard_autoban_dry_run",
                "ipguard_max_alerts_per_hour",
            ],
        )
        # Минимум подсетей — поле карточки всплеска, поэтому идёт сразу за
        # подтверждениями, а не в хвосте списка.
        self.assertEqual(
            [item["key"] for item in payload["settings"]][15:18],
            [
                "ipguard_burst_confirmations",
                "ipguard_burst_min_subnets",
                "ipguard_geo_enabled",
            ],
        )
        # Порядок ключей = порядок карточек: рубильник (тумблер + период)
        # первым, суточный слой со своим тумблером — следом.
        self.assertEqual(
            [item["key"] for item in payload["settings"]][3:6],
            [
                "ipguard_alerts_enabled",
                "ipguard_check_interval_seconds",
                "ipguard_subnets_enabled",
            ],
        )
        self.assertTrue(all(item["is_set"] is False for item in payload["settings"]))
        self.assertEqual(
            [item["value"] for item in payload["strategies"]],
            ["no_reset", "day", "week", "month", "month_rolling"],
        )
        self.assertEqual(
            [item["label"] for item in payload["strategies"]],
            ["Никогда", "Ежедневно", "Еженедельно", "Ежемесячно", "Ежемесячно по дате создания"],
        )
        self.assertEqual([item["value"] for item in payload["segments"]], ["never_paid", "all"])

    def test_set_limit_in_mib_is_stored_as_gib(self):
        from common.models.db import SystemSetting

        status, payload = self._request(
            "POST", action="trial_limit_set", limit_value="512", limit_unit="mib",
            strategy="month_rolling",
        )

        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertEqual(effective["trial_traffic_limit_gb"], 0.5)
        self.assertEqual(effective["trial_traffic_limit_bytes"], 536870912)
        self.assertEqual(effective["trial_traffic_limit_strategy"], "month_rolling")
        self.assertEqual(self.session.get(SystemSetting, "trial_traffic_limit_gb").value, "0.5")
        self.assertEqual(
            [call.args[2] for call in self.audit.call_args_list], ["setting_save", "setting_save"]
        )
        self.assertEqual(
            {call.kwargs["target"] for call in self.audit.call_args_list},
            {"trial_traffic_limit_gb", "trial_traffic_limit_strategy"},
        )

        # 100 МиБ → 0.09765625 ГиБ без потери точности → ровно 104857600 байт.
        status, payload = self._request(
            "POST", action="trial_limit_set", limit_value="100", limit_unit="mib"
        )
        self.assertEqual(payload["effective"]["trial_traffic_limit_bytes"], 104857600)
        # Пустой лимит + стратегия: лимит не трогаем.
        status, payload = self._request("POST", action="trial_limit_set", strategy="week")
        self.assertEqual(payload["effective"]["trial_traffic_limit_bytes"], 104857600)
        self.assertEqual(payload["effective"]["trial_traffic_limit_strategy"], "week")

    def test_toggle_requires_valid_dependent_values(self):
        self._set("trial_traffic_limit_gb", "abc")
        self.session.commit()

        status, payload = self._request("POST", action="trial_limit_enable")

        self.assertEqual(status, 400)
        self.assertIn("trial_traffic_limit_gb", payload["message"])
        status, payload = self._request()
        self.assertIs(payload["effective"]["trial_traffic_limit_enabled"], False)

        self._set("trial_traffic_limit_gb", "3")
        self.session.commit()
        status, payload = self._request("POST", action="trial_limit_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["trial_traffic_limit_enabled"], True)
        status, payload = self._request("POST", action="trial_limit_disable")
        self.assertIs(payload["effective"]["trial_traffic_limit_enabled"], False)

    def test_ipguard_set_and_toggle(self):
        status, payload = self._request(
            "POST", action="ipguard_set", segment="all", subnets_per_hwid="4",
            window_hours="12", cooldown_hours="",
        )

        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertEqual(effective["ipguard_alert_segment"], "all")
        self.assertEqual(effective["ipguard_alert_segment_label"], "Все подписки")
        self.assertEqual(effective["ipguard_subnets_per_hwid"], 4)
        self.assertEqual(effective["ipguard_window_hours"], 12)
        self.assertEqual(effective["ipguard_alert_cooldown_hours"], 6)

        status, payload = self._request("POST", action="ipguard_set", subnets_per_hwid="0")
        self.assertEqual(status, 400)
        status, payload = self._request("POST", action="ipguard_set", segment="everyone")
        self.assertEqual(status, 400)

        status, payload = self._request("POST", action="ipguard_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_alerts_enabled"], True)
        status, payload = self._request("POST", action="ipguard_disable")
        self.assertIs(payload["effective"]["ipguard_alerts_enabled"], False)

    def test_ipguard_master_set_period(self):
        """Период прогона правится из админки: раньше он жил только в env
        коллектора, и по вкладке нельзя было понять, как часто слои вообще
        смотрят на данные."""
        from common.models.db import SystemSetting

        status, payload = self._request(
            "POST", action="ipguard_master_set", check_interval_seconds="60"
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["effective"]["ipguard_check_interval_seconds"], 60)
        self.assertEqual(
            self.session.get(SystemSetting, "ipguard_check_interval_seconds").value,
            "60",
        )
        # Пустое поле — «не трогать», как в остальных формах вкладки.
        status, payload = self._request(
            "POST", action="ipguard_master_set", check_interval_seconds=""
        )
        self.assertEqual(status, 400)
        self.assertIn("Нет значений", payload["message"])
        status, payload = self._request()
        self.assertEqual(payload["effective"]["ipguard_check_interval_seconds"], 60)

        for bad in ("0", "-5", "пять"):
            status, payload = self._request(
                "POST", action="ipguard_master_set", check_interval_seconds=bad
            )
            self.assertEqual(status, 400, bad)
            self.assertIn("ipguard_check_interval_seconds", payload["message"])
        status, payload = self._request()
        self.assertEqual(payload["effective"]["ipguard_check_interval_seconds"], 60)

    def test_ipguard_subnets_toggle_is_independent_of_master(self):
        """Требование владельца: отключение алертов по подсетям не должно
        отключать остальные слои. Тумблеры отдельные и не конфликтуют."""
        status, payload = self._request("POST", action="ipguard_subnets_disable")

        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertIs(effective["ipguard_subnets_enabled"], False)
        # Главный рубильник и слои — не тронуты.
        self.assertIs(effective["ipguard_alerts_enabled"], False)
        self.assertIs(effective["ipguard_burst_enabled"], False)
        self.assertIs(effective["ipguard_geo_enabled"], False)

        status, payload = self._request("POST", action="ipguard_enable")
        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertIs(effective["ipguard_alerts_enabled"], True)
        # Включение главного рубильника не воскрешает выключенный слой.
        self.assertIs(effective["ipguard_subnets_enabled"], False)

        status, payload = self._request("POST", action="ipguard_subnets_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_subnets_enabled"], True)
        self.assertIs(payload["effective"]["ipguard_alerts_enabled"], True)

        # И наоборот: выключение рубильника не трогает тумблер слоя — иначе
        # аварийный стоп молча терял бы настройку.
        status, payload = self._request("POST", action="ipguard_disable")
        self.assertIs(payload["effective"]["ipguard_alerts_enabled"], False)
        self.assertIs(payload["effective"]["ipguard_subnets_enabled"], True)

    def test_ipguard_toggles_require_valid_dependent_values(self):
        """Включение (любого из двух тумблеров суточного слоя) с битым
        значением в БД запрещено: иначе ip-guard молча уехал бы в дефолт, и
        админ считал бы не свои числа. Выключение не проверяет ничего."""
        self._set("ipguard_check_interval_seconds", "мгновенно")
        self.session.commit()

        status, payload = self._request("POST", action="ipguard_enable")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_check_interval_seconds", payload["message"])
        status, payload = self._request("POST", action="ipguard_subnets_enable")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_check_interval_seconds", payload["message"])
        # Аварийный стоп обязан работать всегда.
        status, _ = self._request("POST", action="ipguard_disable")
        self.assertEqual(status, 200)
        status, _ = self._request("POST", action="ipguard_subnets_disable")
        self.assertEqual(status, 200)

        self._set("ipguard_check_interval_seconds", "120")
        self.session.commit()
        status, payload = self._request("POST", action="ipguard_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_alerts_enabled"], True)
        self.assertEqual(payload["effective"]["ipguard_check_interval_seconds"], 120)

    def test_ipguard_burst_set_and_toggle(self):
        status, payload = self._request(
            "POST", action="ipguard_burst_set", burst_window_minutes="3",
            burst_ips_per_hwid="4", burst_confirmations="",
        )

        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertEqual(effective["ipguard_burst_window_minutes"], 3)
        self.assertEqual(effective["ipguard_burst_ips_per_hwid"], 4)
        # Пустое поле оставляет текущее значение (здесь — дефолт common).
        self.assertEqual(effective["ipguard_burst_confirmations"], 2)

        status, _ = self._request(
            "POST", action="ipguard_burst_set", burst_window_minutes="0"
        )
        self.assertEqual(status, 400)
        status, _ = self._request(
            "POST", action="ipguard_burst_set", burst_confirmations="две"
        )
        self.assertEqual(status, 400)

        status, payload = self._request("POST", action="ipguard_burst_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_burst_enabled"], True)
        status, payload = self._request("POST", action="ipguard_burst_disable")
        self.assertIs(payload["effective"]["ipguard_burst_enabled"], False)

        # Битое значение в БД не даёт включить слой (контракт парной проверки).
        self._set("ipguard_burst_ips_per_hwid", "0")
        status, payload = self._request("POST", action="ipguard_burst_enable")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_burst_ips_per_hwid", payload["message"])

    def test_ipguard_burst_min_subnets_set_via_endpoint(self):
        # Минимум разных подсетей — поле той же формы ipguard_burst_set;
        # пустое поле не трогает значение, соседние поля не задеваются.
        status, payload = self._request(
            "POST", action="ipguard_burst_set", burst_min_subnets="3"
        )
        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertEqual(effective["ipguard_burst_min_subnets"], 3)
        self.assertEqual(effective["ipguard_burst_confirmations"], 2)
        raw = {item["key"]: item for item in payload["settings"]}
        self.assertIs(raw["ipguard_burst_min_subnets"]["is_set"], True)
        self.assertEqual(raw["ipguard_burst_min_subnets"]["value"], "3")

        status, payload = self._request(
            "POST", action="ipguard_burst_set", burst_min_subnets="",
            burst_confirmations="4",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["effective"]["ipguard_burst_min_subnets"], 3)
        self.assertEqual(payload["effective"]["ipguard_burst_confirmations"], 4)

        # 1 — допустимое значение: «считать по голым IP, как было».
        status, payload = self._request(
            "POST", action="ipguard_burst_set", burst_min_subnets="1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["effective"]["ipguard_burst_min_subnets"], 1)

        # Битые значения отклоняются общим валидатором common (POSITIVE_INT),
        # сохранённое при этом не меняется.
        for broken in ("0", "abc", "-2", "1.5"):
            status, payload = self._request(
                "POST", action="ipguard_burst_set", burst_min_subnets=broken
            )
            self.assertEqual(status, 400, broken)
            self.assertIn("ipguard_burst_min_subnets", payload["message"], broken)
        status, payload = self._request()
        self.assertEqual(payload["effective"]["ipguard_burst_min_subnets"], 1)

        # Битое значение в БД не даёт включить слой — как и у остальных
        # полей всплеска, иначе админ увидел бы молча подставленный дефолт.
        self._set("ipguard_burst_min_subnets", "0")
        status, payload = self._request("POST", action="ipguard_burst_enable")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_burst_min_subnets", payload["message"])

    def test_ipguard_geo_set_and_toggle(self):
        status, payload = self._request(
            "POST", action="ipguard_geo_set", geo_window_minutes="7", geo_min_regions="3"
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["effective"]["ipguard_geo_window_minutes"], 7)
        self.assertEqual(payload["effective"]["ipguard_geo_min_regions"], 3)

        status, _ = self._request(
            "POST", action="ipguard_geo_set", geo_min_regions="-1"
        )
        self.assertEqual(status, 400)

        status, payload = self._request("POST", action="ipguard_geo_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_geo_enabled"], True)
        status, payload = self._request("POST", action="ipguard_geo_disable")
        self.assertIs(payload["effective"]["ipguard_geo_enabled"], False)

        self._set("ipguard_geo_window_minutes", "abc")
        status, payload = self._request("POST", action="ipguard_geo_enable")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_geo_window_minutes", payload["message"])

    def test_ipguard_autoban_set_and_toggle(self):
        status, payload = self._request(
            "POST", action="ipguard_autoban_set", autoban_segment="all",
            autoban_steps_minutes="30, 120, 2880",
            autoban_escalation_window_hours="48", autoban_max_per_hour="5",
            max_alerts_per_hour="20",
        )

        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertEqual(effective["ipguard_autoban_segment"], "all")
        self.assertEqual(effective["ipguard_autoban_segment_label"], "Все подписки")
        self.assertEqual(effective["ipguard_autoban_steps_minutes"], "30,120,2880")
        self.assertEqual(
            effective["ipguard_autoban_steps_label"], "30 мин → 120 мин → 2880 мин"
        )
        self.assertEqual(effective["ipguard_autoban_escalation_window_hours"], 48)
        self.assertEqual(effective["ipguard_autoban_max_per_hour"], 5)
        self.assertEqual(effective["ipguard_max_alerts_per_hour"], 20)

        # Мусор в лестнице отвергается, а не «сохраняется и молча заменяется
        # дефолтом» парсером common.
        for steps in ("0", "-15", "20160", "15,абв"):
            status, _ = self._request(
                "POST", action="ipguard_autoban_set", autoban_steps_minutes=steps
            )
            self.assertEqual(status, 400, steps)
        status, _ = self._request(
            "POST", action="ipguard_autoban_set", autoban_segment="everyone"
        )
        self.assertEqual(status, 400)
        # Прежнее значение осталось нетронутым.
        _, payload = self._request()
        self.assertEqual(
            payload["effective"]["ipguard_autoban_steps_minutes"], "30,120,2880"
        )

        status, payload = self._request("POST", action="ipguard_autoban_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_autoban_enabled"], True)
        status, payload = self._request("POST", action="ipguard_autoban_disable")
        self.assertIs(payload["effective"]["ipguard_autoban_enabled"], False)

    def test_ipguard_autoban_threshold_and_confirmations_set(self):
        """Порог автобана — одно абсолютное число подсетей за окно и
        гистерезис по прогонам: сохраняются той же формой, что и лестница;
        мусор отвергается, значение не меняется."""
        from common.models.db import SystemSetting

        status, payload = self._request(
            "POST", action="ipguard_autoban_set", autoban_min_subnets="40",
            autoban_confirmations="3",
        )

        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertEqual(effective["ipguard_autoban_min_subnets"], 40)
        self.assertEqual(effective["ipguard_autoban_confirmations"], 3)
        self.assertEqual(
            self.session.get(SystemSetting, "ipguard_autoban_min_subnets").value, "40"
        )
        self.assertEqual(
            self.session.get(SystemSetting, "ipguard_autoban_confirmations").value, "3"
        )
        # Остальные поля карточки формой не тронуты (пустое = «не трогать»).
        self.assertEqual(effective["ipguard_autoban_steps_minutes"], "15,60,1440")

        for field in ("autoban_min_subnets", "autoban_confirmations"):
            for bad in ("0", "abc", "-1", "1.5"):
                status, payload = self._request(
                    "POST", action="ipguard_autoban_set", **{field: bad}
                )
                self.assertEqual(status, 400, (field, bad))
                self.assertIn(f"ipguard_{field}", payload["message"], (field, bad))
        _, payload = self._request()
        self.assertEqual(payload["effective"]["ipguard_autoban_min_subnets"], 40)
        self.assertEqual(payload["effective"]["ipguard_autoban_confirmations"], 3)
        self.assertEqual(
            self.session.get(SystemSetting, "ipguard_autoban_min_subnets").value, "40"
        )

    def test_ipguard_autoban_dry_run_toggle(self):
        """Пробный режим: по умолчанию включён, выключается и включается
        отдельными действиями; в аудит уходит setting_save по ключу."""
        from common.models.db import SystemSetting

        status, payload = self._request("POST", action="ipguard_autoban_dry_run_disable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_autoban_dry_run"], False)
        self.assertEqual(
            self.session.get(SystemSetting, "ipguard_autoban_dry_run").value, "0"
        )
        status, payload = self._request("POST", action="ipguard_autoban_dry_run_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_autoban_dry_run"], True)
        self.assertEqual(
            self.session.get(SystemSetting, "ipguard_autoban_dry_run").value, "1"
        )
        self.assertEqual(
            [call.kwargs["target"] for call in self.audit.call_args_list],
            ["ipguard_autoban_dry_run", "ipguard_autoban_dry_run"],
        )

    def test_autoban_cannot_be_enabled_with_broken_threshold_in_db(self):
        """Битый порог/подтверждения в БД молча ушли бы в дефолт common —
        включение автобана и выключение пробного режима запрещены до
        исправления; выключение автобана и включение пробного режима — нет."""
        from common.models.db import SystemSetting

        # Эндпоинт откатывает сессию на ошибке, а _set только flush-ит —
        # битое значение выставляется заново перед каждой проверкой.
        self._set("ipguard_autoban_min_subnets", "abc")
        status, payload = self._request("POST", action="ipguard_autoban_enable")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_autoban_min_subnets", payload["message"])
        self._set("ipguard_autoban_min_subnets", "abc")
        status, payload = self._request("POST", action="ipguard_autoban_dry_run_disable")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_autoban_min_subnets", payload["message"])
        _, payload = self._request()
        self.assertIs(payload["effective"]["ipguard_autoban_enabled"], False)
        self.assertIs(payload["effective"]["ipguard_autoban_dry_run"], True)
        self._set("ipguard_autoban_min_subnets", "abc")
        status, _ = self._request("POST", action="ipguard_autoban_disable")
        self.assertEqual(status, 200)
        self._set("ipguard_autoban_min_subnets", "abc")
        status, _ = self._request("POST", action="ipguard_autoban_dry_run_enable")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.session.get(SystemSetting, "ipguard_autoban_min_subnets").value, "abc"
        )

        # Починка тем же сохранением — и включение проходит.
        status, _ = self._request(
            "POST", action="ipguard_autoban_set", autoban_min_subnets="30"
        )
        self.assertEqual(status, 200)
        self._set("ipguard_autoban_confirmations", "0")
        status, payload = self._request("POST", action="ipguard_autoban_enable")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_autoban_confirmations", payload["message"])
        self._set("ipguard_autoban_confirmations", "2")
        status, payload = self._request("POST", action="ipguard_autoban_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_autoban_enabled"], True)
        status, payload = self._request("POST", action="ipguard_autoban_dry_run_disable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_autoban_dry_run"], False)

    def test_ipguard_excluded_usernames_set_and_clear(self):
        from common.models.db import SystemSetting

        status, payload = self._request(
            "POST", action="ipguard_excluded_usernames_set",
            excluded_usernames="594514115, 7715572734",
        )

        self.assertEqual(status, 200)
        effective = payload["effective"]
        self.assertEqual(effective["ipguard_excluded_usernames"], "594514115,7715572734")
        self.assertEqual(
            effective["ipguard_excluded_usernames_list"], ["594514115", "7715572734"]
        )
        self.assertEqual(effective["ipguard_excluded_usernames_count"], 2)
        self.assertEqual(
            effective["ipguard_excluded_usernames_label"], "594514115, 7715572734"
        )
        self.assertEqual(
            self.session.get(SystemSetting, "ipguard_excluded_usernames").value,
            "594514115,7715572734",
        )
        # Список подписок не трогает список адресов и наоборот.
        self.assertEqual(effective["ipguard_excluded_ips_count"], 0)

        # Мусор отвергается с указанием записи и именем ключа, значение не
        # меняется.
        for bad in ("594514115, bad name", "<script>alert(1)</script>", "a" * 65):
            status, payload = self._request(
                "POST", action="ipguard_excluded_usernames_set", excluded_usernames=bad
            )
            self.assertEqual(status, 400, bad)
            self.assertIn("ipguard_excluded_usernames", payload["message"], bad)
        status, payload = self._request(
            "POST", action="ipguard_excluded_usernames_set",
            excluded_usernames="594514115, bad name",
        )
        self.assertIn("bad name", payload["message"])
        too_long = ", ".join(str(1_000_000_000 + i) for i in range(60))
        status, payload = self._request(
            "POST", action="ipguard_excluded_usernames_set", excluded_usernames=too_long
        )
        self.assertEqual(status, 400)
        self.assertIn("длиннее", payload["message"])
        _, payload = self._request()
        self.assertEqual(payload["effective"]["ipguard_excluded_usernames_count"], 2)

        # Пустое поле — «не трогать».
        status, payload = self._request("POST", action="ipguard_excluded_usernames_set")
        self.assertEqual(status, 400)
        self.assertEqual(payload["message"], "Нет значений для сохранения")

        # Очистка — отдельным действием.
        status, payload = self._request("POST", action="ipguard_excluded_usernames_clear")
        self.assertEqual(status, 200)
        self.assertEqual(payload["effective"]["ipguard_excluded_usernames"], "")
        self.assertEqual(payload["effective"]["ipguard_excluded_usernames_count"], 0)

    def test_autoban_cannot_be_enabled_with_broken_steps_in_db(self):
        """Автобан трогает живые подписки: включение при лестнице, которую
        ip-guard прочитает не так, как задумал админ, запрещено."""
        self._set("ipguard_autoban_steps_minutes", "0")

        status, payload = self._request("POST", action="ipguard_autoban_enable")

        self.assertEqual(status, 400)
        self.assertIn("ipguard_autoban_steps_minutes", payload["message"])
        # Выключение не блокируется никогда.
        status, _ = self._request("POST", action="ipguard_autoban_disable")
        self.assertEqual(status, 200)
        # Лестницу можно починить тем же сохранением, что включает автобан.
        status, payload = self._request(
            "POST", action="ipguard_autoban_set", autoban_steps_minutes="15,60"
        )
        self.assertEqual(status, 200)
        status, payload = self._request("POST", action="ipguard_autoban_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_autoban_enabled"], True)

    def test_ipguard_excluded_ips_set_and_clear(self):
        status, payload = self._request(
            "POST", action="ipguard_excluded_set",
            excluded_ips="37.143.13.212, 10.0.0.0/8",
        )

        self.assertEqual(status, 200)
        effective = payload["effective"]
        # Одиночный адрес канонизируется в /32 — так его видит ip-guard.
        self.assertEqual(effective["ipguard_excluded_ips"], "37.143.13.212/32,10.0.0.0/8")
        self.assertEqual(effective["ipguard_excluded_ips_count"], 2)
        self.assertEqual(
            effective["ipguard_excluded_ips_label"], "37.143.13.212/32, 10.0.0.0/8"
        )

        # Опечатка отвергается с указанием записи, а не выбрасывается молча.
        status, payload = self._request(
            "POST", action="ipguard_excluded_set", excluded_ips="10.0.0.0/8, 37.143.13.2.12"
        )
        self.assertEqual(status, 400)
        self.assertIn("37.143.13.2.12", payload["message"])

        # Пустое поле — «не трогать» (как во всех *_set формах вкладки).
        status, payload = self._request("POST", action="ipguard_excluded_set")
        self.assertEqual(status, 400)
        self.assertEqual(payload["message"], "Нет значений для сохранения")
        _, payload = self._request()
        self.assertEqual(payload["effective"]["ipguard_excluded_ips_count"], 2)

        # Очистка — отдельным действием.
        status, payload = self._request("POST", action="ipguard_excluded_clear")
        self.assertEqual(status, 200)
        self.assertEqual(payload["effective"]["ipguard_excluded_ips"], "")
        self.assertEqual(payload["effective"]["ipguard_excluded_ips_count"], 0)

    def test_ipguard_warning_threshold_pair(self):
        """Порог предупреждения строго меньше порога алерта: второе значение
        берётся из этого же сохранения (в любом порядке полей), иначе из БД,
        иначе дефолт; тумблер предупреждений включается только при валидной
        паре."""
        # Дефолт предупреждения 3: алерт 3 не подходит (не строго меньше).
        status, payload = self._request("POST", action="ipguard_set", subnets_per_hwid="3")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_warning_subnets_per_hwid=3", payload["message"])
        self.assertIn("ipguard_subnets_per_hwid=3", payload["message"])
        self.audit.assert_not_called()

        # Пара в одном сохранении валидна независимо от порядка ключей.
        status, payload = self._request(
            "POST", action="ipguard_set", subnets_per_hwid="3", warning_subnets_per_hwid="2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["effective"]["ipguard_subnets_per_hwid"], 3)
        self.assertEqual(payload["effective"]["ipguard_warning_subnets_per_hwid"], 2)

        # Одно поле — второе из БД: предупреждение 3 при алерте 3 — отказ.
        status, payload = self._request("POST", action="ipguard_set", warning_subnets_per_hwid="3")
        self.assertEqual(status, 400)
        status, payload = self._request("POST", action="ipguard_set", warning_subnets_per_hwid="0")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_warning_subnets_per_hwid", payload["message"])
        status, payload = self._request()
        self.assertEqual(payload["effective"]["ipguard_warning_subnets_per_hwid"], 2)

        status, payload = self._request("POST", action="ipguard_warnings_enable")
        self.assertEqual(status, 200)
        self.assertIs(payload["effective"]["ipguard_warnings_enabled"], True)
        status, payload = self._request("POST", action="ipguard_warnings_disable")
        self.assertIs(payload["effective"]["ipguard_warnings_enabled"], False)

        # Битая пара в БД (руками) не даёт включить предупреждения.
        self._set("ipguard_warning_subnets_per_hwid", "5")
        self.session.commit()
        status, payload = self._request("POST", action="ipguard_warnings_enable")
        self.assertEqual(status, 400)
        self.assertIn("ipguard_warning_subnets_per_hwid=5", payload["message"])
        status, payload = self._request("POST", action="ipguard_warnings_disable")
        self.assertEqual(status, 200)

    def test_unchanged_values_are_not_rewritten(self):
        """Формы предзаполнены и шлют все поля разом: значение, равное
        сохранённому, не перезаписывается и в аудит не попадает."""
        status, payload = self._request(
            "POST", action="trial_limit_set", limit_value="5", limit_unit="gib", strategy="day"
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.audit.call_count, 2)

        status, payload = self._request(
            "POST", action="trial_limit_set", limit_value="5", limit_unit="gib", strategy="day"
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.audit.call_count, 2)
        self.assertEqual(payload["effective"]["trial_traffic_limit_gb"], 5.0)

        status, payload = self._request(
            "POST", action="ipguard_set", segment="never_paid", subnets_per_hwid="10",
            warning_subnets_per_hwid="3", window_hours="24", cooldown_hours="6",
        )
        self.assertEqual(status, 200)
        # Дефолты без строк в БД записываются один раз…
        self.assertEqual(self.audit.call_count, 7)
        status, payload = self._request(
            "POST", action="ipguard_set", segment="never_paid", subnets_per_hwid="10",
            warning_subnets_per_hwid="3", window_hours="24", cooldown_hours="6",
        )
        # …повтор — без записи и аудита.
        self.assertEqual(self.audit.call_count, 7)

    def test_raw_runtime_settings_endpoint_validates_ipguard_pair(self):
        from engine.views import support_admin_api_runtime_settings

        def save(key, value):
            request = RequestFactory().post(
                "/support-admin/api/runtime-settings/",
                data={"action": "save", "key": key, "value": value},
            )
            request.session = {}
            with (
                mock.patch("engine.views.require_support_admin_role", return_value=None),
                mock.patch("engine.views.session_factory", return_value=self.session),
                mock.patch("engine.views.admin_audit_write", self.audit),
            ):
                response = support_admin_api_runtime_settings(request)
            return response.status_code, json.loads(response.content)

        status, payload = save("ipguard_subnets_per_hwid", "3")
        self.assertEqual(status, 400)
        self.assertIn("строго меньше", payload["message"])
        status, payload = save("ipguard_subnets_per_hwid", "4")
        self.assertEqual(status, 200)
        status, payload = save("ipguard_warning_subnets_per_hwid", "4")
        self.assertEqual(status, 400)
        status, payload = save("ipguard_warning_subnets_per_hwid", "1")
        self.assertEqual(status, 200)
        status, payload = save("ipguard_warnings_enabled", "1")
        self.assertEqual(status, 200)

    def test_managed_limits_flag_reflects_missing_table(self):
        self._drop_marker_table()
        with self.assertLogs(level="WARNING"):
            status, payload = self._request()
        self.assertEqual(status, 200)
        self.assertIs(payload["managed_limits_available"], False)

    def test_invalid_requests(self):
        status, payload = self._request("POST", action="nuke")
        self.assertEqual(status, 400)
        status, payload = self._request("POST", action="trial_limit_set")
        self.assertEqual(status, 400)
        self.assertIn("Нет значений", payload["message"])
        status, payload = self._request("POST", action="trial_limit_set", limit_value="x")
        self.assertEqual(status, 400)
        status, payload = self._request("POST", action="trial_limit_set", limit_value="1", limit_unit="tb")
        self.assertEqual(status, 400)
        status, payload = self._request("POST", action="trial_limit_set", limit_value="0")
        self.assertEqual(status, 400)
        # nan/inf проходят float(), но в БД попасть не должны.
        for limit_value, limit_unit in (
            ("nan", "gib"), ("inf", "gib"), ("Infinity", "mib"),
            # Вне диапазона: 1e13 МиБ ≈ 9.8e9 ГиБ переполнили бы int64 байт.
            ("1e13", "mib"), ("9000000000", "gib"),
            # Округляется до «0» при 10 знаках.
            ("0.00000000001", "gib"), ("0.00000001", "mib"),
        ):
            status, payload = self._request(
                "POST", action="trial_limit_set", limit_value=limit_value,
                limit_unit=limit_unit,
            )
            self.assertEqual(status, 400, (limit_value, limit_unit))
            self.assertIn("trial_traffic_limit_gb", payload["message"], limit_value)
        status, payload = self._request("PUT")
        self.assertEqual(status, 405)
        self.audit.assert_not_called()
        status, payload = self._request()
        self.assertEqual(payload["effective"]["trial_traffic_limit_gb"], 5.0)


class AntiabuseValidationTests(_AntiabuseSqliteMixin, SimpleTestCase):
    def test_pair_validation_only_guards_enabling(self):
        from engine.views import admin_validate_antiabuse_setting_pair as validate

        self.assertIsNone(validate(self.session, "trial_traffic_limit_enabled", "0"))
        self.assertIsNone(validate(self.session, "trial_traffic_limit_enabled", "1"))
        self.assertIsNone(validate(self.session, "ipguard_alerts_enabled", "1"))
        self.assertIsNone(validate(self.session, "trial_traffic_limit_gb", "5"))

        self._set("trial_traffic_limit_strategy", "quarter")
        error = validate(self.session, "trial_traffic_limit_enabled", "1")
        self.assertIn("trial_traffic_limit_strategy", error)
        self.assertIsNone(validate(self.session, "trial_traffic_limit_enabled", "0"))
        self.assertIsNone(validate(self.session, "ipguard_alerts_enabled", "1"))

        self._set("ipguard_window_hours", "0")
        error = validate(self.session, "ipguard_alerts_enabled", "1")
        self.assertIn("ipguard_window_hours", error)

    def test_runtime_settings_endpoint_uses_pair_validation(self):
        import inspect

        from engine import views

        src = inspect.getsource(views.support_admin_api_runtime_settings)
        self.assertIn("admin_validate_antiabuse_setting_pair(", src)

    def test_ipguard_threshold_pair_validation(self):
        from engine.views import admin_validate_antiabuse_setting_pair as validate

        # Дефолты: алерт 10, предупреждение 3.
        self.assertIsNone(validate(self.session, "ipguard_subnets_per_hwid", "4"))
        self.assertIsNotNone(validate(self.session, "ipguard_subnets_per_hwid", "3"))
        self.assertIsNone(validate(self.session, "ipguard_warning_subnets_per_hwid", "9"))
        self.assertIsNotNone(validate(self.session, "ipguard_warning_subnets_per_hwid", "10"))
        self.assertIsNone(validate(self.session, "ipguard_warnings_enabled", "1"))
        self.assertIsNone(validate(self.session, "ipguard_warnings_enabled", "0"))
        # Значение из этого же сохранения важнее БД/дефолта.
        pending = {"ipguard_warning_subnets_per_hwid": "2", "ipguard_subnets_per_hwid": "3"}
        self.assertIsNone(validate(self.session, "ipguard_subnets_per_hwid", "3", pending))
        self.assertIsNone(validate(self.session, "ipguard_warning_subnets_per_hwid", "2", pending))
        # Значение из БД.
        self._set("ipguard_subnets_per_hwid", "4")
        self.assertIsNotNone(validate(self.session, "ipguard_warning_subnets_per_hwid", "4"))
        self.assertIsNone(validate(self.session, "ipguard_warning_subnets_per_hwid", "3"))
        self._set("ipguard_warning_subnets_per_hwid", "abc")
        error = validate(self.session, "ipguard_warnings_enabled", "1")
        self.assertIn("ipguard_warning_subnets_per_hwid=abc", error)
        self.assertIsNone(validate(self.session, "ipguard_warnings_enabled", "0"))

    def test_ipguard_autoban_steps_validation(self):
        """Лестница наказаний: общий CSV-int валидатор пропускает «0» и
        отрицательные, а parse_ipguard_autoban_steps молча меняет их на
        дефолт (15,60,1440) — строгая проверка не даёт сохранить значение,
        которое ip-guard прочитает иначе."""
        from engine.views import admin_validate_antiabuse_setting_pair as validate
        from engine.views import admin_validate_ipguard_autoban_steps_value as steps_ok

        self.assertIsNone(steps_ok("15,60,1440"))
        self.assertIsNone(steps_ok("15, 60"))
        self.assertIsNone(steps_ok("10080"))
        self.assertIsNotNone(steps_ok(""))
        self.assertIn("0", steps_ok("0"))
        self.assertIsNotNone(steps_ok("-15"))
        self.assertIsNotNone(steps_ok("10081"))
        self.assertIsNotNone(steps_ok("15,abc"))
        self.assertIsNotNone(steps_ok("1,2,3,4,5,6,7,8,9,10,11"))

        # Через парную валидацию (её зовут и форма, и raw-эндпоинт).
        self.assertIsNone(validate(self.session, "ipguard_autoban_steps_minutes", "15,60"))
        self.assertIsNotNone(validate(self.session, "ipguard_autoban_steps_minutes", "0"))
        # Включение автобана при битой лестнице в БД запрещено, выключение — нет.
        self._set("ipguard_autoban_steps_minutes", "0")
        error = validate(self.session, "ipguard_autoban_enabled", "1")
        self.assertIn("ipguard_autoban_steps_minutes", error)
        self.assertIsNone(validate(self.session, "ipguard_autoban_enabled", "0"))
        # Значение из этого же сохранения важнее того, что лежит в БД.
        pending = {"ipguard_autoban_steps_minutes": "15,60"}
        self.assertIsNone(validate(self.session, "ipguard_autoban_enabled", "1", pending))
        # Битый сегмент/предохранитель тоже блокируют включение.
        self._set("ipguard_autoban_steps_minutes", "15,60")
        self._set("ipguard_autoban_max_per_hour", "0")
        self.assertIn(
            "ipguard_autoban_max_per_hour",
            validate(self.session, "ipguard_autoban_enabled", "1"),
        )

    def test_ipguard_excluded_ips_validation(self):
        """parse_ipguard_excluded_ips выбрасывает нераспознанные записи молча —
        на входе называем конкретную запись, иначе опечатка «тихо исчезает»."""
        from engine.views import admin_validate_antiabuse_setting_pair as validate
        from engine.views import admin_validate_ipguard_excluded_ips_value as ips_ok

        self.assertIsNone(ips_ok(""))
        self.assertIsNone(ips_ok("37.143.13.212"))
        self.assertIsNone(ips_ok("37.143.13.212, 10.0.0.0/8, 2a01:4f8::/32"))
        self.assertIn("10.0.0.0/64", ips_ok("10.0.0.0/64"))
        self.assertIn("37.143.13.2.12", ips_ok("10.0.0.0/8, 37.143.13.2.12"))
        self.assertIsNotNone(ips_ok(",".join(["10.0.0.1"] * 101)))

        # Слишком широкая маска = тихое выключение ip-guard: под 0.0.0.0/0
        # попадает весь интернет, наблюдения перестают записываться, детектор
        # слепнет. Одна опечатка («/0» вместо «/32») не должна так стоить.
        for broad in ("37.143.13.212/0", "0.0.0.0/0", "10.0.0.0/4", "2a01:4f8::/16"):
            self.assertIn("широкая маска", ips_ok(broad) or "", broad)
        self.assertIsNone(ips_ok("10.0.0.0/8"))
        self.assertIsNone(ips_ok("2a01:4f8::/32"))

        # system_settings.value — VARCHAR(512): длинный список иначе падает на
        # вставке и не сохраняется вовсе.
        long_list = ", ".join(f"10.0.{i}.0/24" for i in range(100))
        self.assertIn("длиннее", ips_ok(long_list) or "")

        self.assertIsNone(validate(self.session, "ipguard_excluded_ips", ""))
        self.assertIsNone(
            validate(self.session, "ipguard_excluded_ips", "37.143.13.212,10.0.0.0/8")
        )
        self.assertIsNotNone(validate(self.session, "ipguard_excluded_ips", "мост"))

    def test_ipguard_excluded_usernames_validation(self):
        """parse_ipguard_excluded_usernames выбрасывает мусор молча — на входе
        называем конкретную запись; пустое = очистка; длина под VARCHAR(512)."""
        from engine.views import admin_validate_antiabuse_setting_pair as validate
        from engine.views import admin_validate_ipguard_excluded_usernames_value as ok

        self.assertIsNone(ok(""))
        self.assertIsNone(ok("594514115"))
        self.assertIsNone(ok("594514115, 7715572734"))
        self.assertIsNone(ok("user_name.1@x-y"))
        self.assertIsNone(ok("a" * 64))
        self.assertIn("bad name", ok("594514115, bad name"))
        self.assertIn("<script>", ok("<script>alert(1)</script>"))
        self.assertIsNotNone(ok("a" * 65))
        self.assertIsNotNone(ok("юзер"))
        self.assertIn("не больше 200", ok(",".join(str(i) for i in range(201))))
        self.assertIn("длиннее", ok(", ".join(str(1_000_000_000 + i) for i in range(60))))

        self.assertIsNone(validate(self.session, "ipguard_excluded_usernames", ""))
        self.assertIsNone(
            validate(self.session, "ipguard_excluded_usernames", "594514115,7715572734")
        )
        self.assertIsNotNone(
            validate(self.session, "ipguard_excluded_usernames", "bad name")
        )

    def test_ipguard_autoban_threshold_pair_validation(self):
        """Битый порог/подтверждения в БД блокируют включение автобана и
        выключение пробного режима; значение из этого же сохранения важнее."""
        from engine.views import admin_validate_antiabuse_setting_pair as validate

        self.assertIsNone(validate(self.session, "ipguard_autoban_enabled", "1"))
        self.assertIsNone(validate(self.session, "ipguard_autoban_dry_run", "0"))
        self._set("ipguard_autoban_min_subnets", "0")
        self.assertIn(
            "ipguard_autoban_min_subnets",
            validate(self.session, "ipguard_autoban_enabled", "1"),
        )
        self.assertIn(
            "ipguard_autoban_min_subnets",
            validate(self.session, "ipguard_autoban_dry_run", "0"),
        )
        self.assertIsNone(validate(self.session, "ipguard_autoban_enabled", "0"))
        self.assertIsNone(validate(self.session, "ipguard_autoban_dry_run", "1"))
        self._set("ipguard_autoban_min_subnets", "30")
        self._set("ipguard_autoban_confirmations", "abc")
        self.assertIn(
            "ipguard_autoban_confirmations",
            validate(self.session, "ipguard_autoban_enabled", "1"),
        )

    def test_generic_validator_handles_new_ipguard_keys(self):
        self.assertEqual(admin_validate_runtime_setting("ipguard_burst_enabled", "да"), ("1", None))
        self.assertEqual(admin_validate_runtime_setting("ipguard_geo_enabled", "выкл"), ("0", None))
        self.assertEqual(
            admin_validate_runtime_setting("ipguard_autoban_segment", "NEVER_PAID"),
            ("never_paid", None),
        )
        self.assertIsNotNone(admin_validate_runtime_setting("ipguard_autoban_segment", "trial")[1])
        self.assertEqual(
            admin_validate_runtime_setting("ipguard_autoban_steps_minutes", "15, 60 ,1440"),
            ("15,60,1440", None),
        )
        self.assertEqual(
            admin_validate_runtime_setting("ipguard_excluded_ips", "10.0.0.0/8 , 1.2.3.4"),
            ("10.0.0.0/8,1.2.3.4", None),
        )
        for key in (
            "ipguard_burst_window_minutes",
            "ipguard_burst_ips_per_hwid",
            "ipguard_burst_confirmations",
            "ipguard_burst_min_subnets",
            "ipguard_geo_window_minutes",
            "ipguard_geo_min_regions",
            "ipguard_autoban_escalation_window_hours",
            "ipguard_autoban_max_per_hour",
            "ipguard_autoban_min_subnets",
            "ipguard_autoban_confirmations",
            "ipguard_max_alerts_per_hour",
        ):
            self.assertEqual(admin_validate_runtime_setting(key, "3"), ("3", None), key)
            self.assertIsNotNone(admin_validate_runtime_setting(key, "0")[1], key)
            self.assertEqual(admin_runtime_setting_type(key), "int", key)
        self.assertEqual(admin_runtime_setting_type("ipguard_autoban_steps_minutes"), "csv_int")
        self.assertEqual(admin_runtime_setting_type("ipguard_excluded_ips"), "csv")
        self.assertEqual(admin_runtime_setting_type("ipguard_excluded_usernames"), "csv")
        self.assertEqual(admin_runtime_setting_type("ipguard_autoban_dry_run"), "bool")
        self.assertEqual(admin_validate_runtime_setting("ipguard_autoban_dry_run", "вкл"), ("1", None))
        self.assertEqual(
            admin_validate_runtime_setting("ipguard_excluded_usernames", "594514115 , 7715572734"),
            ("594514115,7715572734", None),
        )
        self.assertEqual(admin_runtime_setting_type("ipguard_autoban_segment"), "enum")

    def test_generic_validator_handles_antiabuse_keys(self):
        self.assertEqual(admin_validate_runtime_setting("trial_traffic_limit_gb", "0,5"), ("0.5", None))
        self.assertEqual(
            admin_validate_runtime_setting("trial_traffic_limit_gb", "0.09765625"),
            ("0.09765625", None),
        )
        self.assertEqual(
            admin_validate_runtime_setting("trial_traffic_limit_strategy", "MONTH_ROLLING"),
            ("month_rolling", None),
        )
        self.assertEqual(admin_validate_runtime_setting("ipguard_alert_segment", "ALL"), ("all", None))
        self.assertEqual(admin_validate_runtime_setting("trial_traffic_limit_enabled", "on"), ("1", None))
        self.assertIsNotNone(admin_validate_runtime_setting("ipguard_subnets_per_hwid", "0")[1])
        self.assertIsNotNone(admin_validate_runtime_setting("trial_traffic_limit_gb", "0")[1])
        self.assertIsNotNone(admin_validate_runtime_setting("trial_traffic_limit_strategy", "yearly")[1])
        self.assertEqual(admin_runtime_setting_type("trial_traffic_limit_strategy"), "enum")
        self.assertEqual(admin_runtime_setting_type("trial_traffic_limit_gb"), "float")


    def test_float_validator_rejects_nan_and_inf_for_all_float_keys(self):
        """float() принимает "nan"/"inf": без проверки они сохранялись в БД
        мусором, а parse_positive_float_setting в сервисах молча уходил на
        дефолт (5 ГиБ) — админ считал, что задал другое значение."""
        from common.models.settings import POSITIVE_FLOAT_RUNTIME_SETTINGS

        self.assertIn("traffic_usage_suspicious_gb", POSITIVE_FLOAT_RUNTIME_SETTINGS)
        for key in POSITIVE_FLOAT_RUNTIME_SETTINGS:
            for raw in ("nan", "NaN", "inf", "-inf", "Infinity", "+inf"):
                normalized, error = admin_validate_runtime_setting(key, raw)
                self.assertIsNone(normalized, (key, raw))
                self.assertIn("конечным", error, (key, raw))
            self.assertEqual(admin_validate_runtime_setting(key, "2.5"), ("2.5", None))

    def test_trial_limit_gb_bounds_fit_int64(self):
        from common.models.settings import trial_traffic_limit_bytes

        from engine.views import TRIAL_TRAFFIC_LIMIT_MAX_GB

        # Верхняя граница: байты с большим запасом внутри int64 proto.
        self.assertEqual(TRIAL_TRAFFIC_LIMIT_MAX_GB, 100_000)
        self.assertLess(trial_traffic_limit_bytes(TRIAL_TRAFFIC_LIMIT_MAX_GB), 2**62)
        self.assertEqual(
            admin_validate_runtime_setting("trial_traffic_limit_gb", "100000"),
            ("100000", None),
        )
        for raw in ("100000.0000000001", "100001", "9000000000", "1e13"):
            normalized, error = admin_validate_runtime_setting("trial_traffic_limit_gb", raw)
            self.assertIsNone(normalized, raw)
            self.assertIn("100000 ГиБ", error, raw)
        # Нижняя граница: значение, округляющееся до «0» при 10 знаках.
        for raw in ("0.00000000001", "1e-12"):
            normalized, error = admin_validate_runtime_setting("trial_traffic_limit_gb", raw)
            self.assertIsNone(normalized, raw)
            self.assertIn("слишком мало", error, raw)
        self.assertEqual(
            admin_validate_runtime_setting("trial_traffic_limit_gb", "0.001"),
            ("0.001", None),
        )
        # Верхняя граница — только для лимита пробных, остальные float без неё.
        self.assertEqual(
            admin_validate_runtime_setting("traffic_usage_alert_gb", "9000000000"),
            ("9000000000", None),
        )

    def test_pair_validation_rejects_broken_limit_in_db(self):
        """Битый лимит в БД (nan / вне диапазона) не даёт включить тумблер."""
        from engine.views import admin_validate_antiabuse_setting_pair as validate

        for raw in ("nan", "inf", "1e13", "0"):
            self._set("trial_traffic_limit_gb", raw)
            error = validate(self.session, "trial_traffic_limit_enabled", "1")
            self.assertIsNotNone(error, raw)
            self.assertIn("trial_traffic_limit_gb", error, raw)
            self.assertIsNone(validate(self.session, "trial_traffic_limit_enabled", "0"))
        self._set("trial_traffic_limit_gb", "100000")
        self.assertIsNone(validate(self.session, "trial_traffic_limit_enabled", "1"))


class AntiabuseTrafficPayloadTests(SimpleTestCase):
    def test_payload_exposes_limit_strategy_status_and_hwid(self):
        from engine import views

        rwms = mock.Mock()
        rwms.get_user_by_username.return_value = SimpleNamespace(
            uuid="u1",
            used_traffic_bytes=1,
            lifetime_used_traffic_bytes=2,
            traffic_limit_bytes=5 * 1024**3,
            traffic_limit_strategy=4,
            status=2,
        )
        rwms.get_user_hwid_devices.return_value = SimpleNamespace(total=3, devices=[])

        payload = views.admin_rwms_traffic_payload("42", client=rwms)

        self.assertEqual(payload["traffic_limit_bytes"], 5 * 1024**3)
        self.assertEqual(payload["traffic_limit_strategy"], "month_rolling")
        self.assertEqual(payload["traffic_limit_strategy_label"], "Ежемесячно по дате создания")
        self.assertEqual(payload["status"], "LIMITED")
        self.assertIs(payload["is_limited"], True)
        self.assertEqual(payload["hwid_devices"], 3)
        rwms.get_user_hwid_devices.assert_called_once_with("u1")

    def test_payload_survives_hwid_failure_and_unknown_strategy(self):
        from engine import views

        rwms = mock.Mock()
        rwms.get_user_by_username.return_value = SimpleNamespace(
            uuid="u1",
            used_traffic_bytes=1,
            lifetime_used_traffic_bytes=2,
            traffic_limit_bytes=0,
            traffic_limit_strategy=9,
            status=0,
        )
        rwms.get_user_hwid_devices.side_effect = RuntimeError("down")

        with self.assertLogs(level="ERROR"):
            payload = views.admin_rwms_traffic_payload("42", client=rwms)

        self.assertTrue(payload["available"])
        self.assertIsNone(payload["hwid_devices"])
        self.assertEqual(payload["traffic_limit_strategy"], "9")
        self.assertEqual(payload["status"], "ACTIVE")
        self.assertIs(payload["is_limited"], False)

        rwms.get_user_hwid_devices.side_effect = None
        rwms.get_user_hwid_devices.return_value = None
        payload = views.admin_rwms_traffic_payload("42", client=rwms)
        self.assertIsNone(payload["hwid_devices"])


class AntiabuseTemplateAndDocsTests(SimpleTestCase):
    def test_template_has_antiabuse_tab_forms_bulk_and_alerts(self):
        template = template_source("engine/templates/admin_dashboard.html")
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        for needle in (
            'data-subtab="sys-antiabuse"',
            'id="subpanel-sys-antiabuse"',
            'id="antiabuse-trial-form"',
            'id="antiabuse-ipguard-form"',
            'id="antiabuse-trial-toggle"',
            'id="antiabuse-ipguard-toggle"',
            'name="limit_unit"',
            '<option value="mib">МиБ</option>',
            "data-antiabuse-strategy-select",
            "data-antiabuse-segment-select",
            'name="subnets_per_hwid"',
            'name="window_hours"',
            'name="cooldown_hours"',
            "Выключено — новые пробные создаются без лимита",
            "Выключено — молчат все слои сразу",
            'data-antiabuse-bulk-preview="apply_trial_limit"',
            'data-antiabuse-bulk-apply="remove_trial_limit"',
            'data-antiabuse-bulk-apply="remove_paid_limit"',
            'id="antiabuse-alerts-table"',
            'id="antiabuse-ban-hours"',
            "data-ipguard-temp-ban",
            "data-ipguard-open-card",
            "async function loadIpguardAlerts",
            "async function antiabuseBulkApply",
            "async function antiabuseBulkPreview",
            "async function submitAntiabuseForm",
            "const ANTIABUSE_BULK_BATCH = 50",
            "data-antiabuse-url=",
            "data-antiabuse-bulk-url=",
            "data-ipguard-alerts-url=",
            'data-settings-group="antiabuse"',
            "{slug: 'antiabuse', keys: ['trial_traffic_limit_enabled', 'trial_traffic_limit_gb', 'trial_traffic_limit_strategy', 'ipguard_alerts_enabled', 'ipguard_check_interval_seconds', 'ipguard_subnets_enabled', 'ipguard_alert_segment', 'ipguard_subnets_per_hwid', 'ipguard_window_hours', 'ipguard_alert_cooldown_hours', 'ipguard_warnings_enabled', 'ipguard_warning_subnets_per_hwid', 'ipguard_burst_enabled', 'ipguard_burst_window_minutes', 'ipguard_burst_ips_per_hwid', 'ipguard_burst_confirmations', 'ipguard_burst_min_subnets', 'ipguard_geo_enabled', 'ipguard_geo_window_minutes', 'ipguard_geo_min_regions', 'ipguard_autoban_enabled', 'ipguard_autoban_segment', 'ipguard_autoban_steps_minutes', 'ipguard_autoban_escalation_window_hours', 'ipguard_autoban_max_per_hour', 'ipguard_autoban_min_subnets', 'ipguard_autoban_confirmations', 'ipguard_autoban_dry_run', 'ipguard_max_alerts_per_hour', 'ipguard_excluded_ips', 'ipguard_excluded_usernames']}",
            "loadAntiabuse();",
            # v2: предупреждения ip-guard, backfill маркеров, предзаполнение.
            'id="antiabuse-ipguard-warnings-toggle"',
            'value="ipguard_warnings_enable"',
            'name="warning_subnets_per_hwid"',
            'data-antiabuse-state="ipguard_warnings"',
            'data-antiabuse-current="ipguard_warning_subnets_per_hwid"',
            "data-antiabuse-backfill-url=",
            'data-antiabuse-backfill="1"',
            'data-antiabuse-backfill="0"',
            'data-antiabuse-bulk-result="backfill_markers"',
            'id="antiabuse-backfill-status"',
            "async function antiabuseBackfillRun",
            "function antiabuseSetField(",
            "antiabuseSetField(ipguardForm, 'warning_subnets_per_hwid', effective.ipguard_warning_subnets_per_hwid)",
            "antiabuseOptionsHtml(payload.strategies, effective.trial_traffic_limit_strategy)",
            "antiabuseSetField(trialForm, 'limit_unit', 'mib')",
            "// Перечитываем GET после POST",
            "loadAntiabuse(true);\n            showAdminToast('Антиабьюз обновлён');",
            "totals.skippedPaid += result.skipped_paid || 0;",
            "totals.skippedManual += result.skipped_manual || 0;",
            "totals.skippedAdminLimit += result.skipped_admin_limit || 0;",
            "лимитов админа из бота (не тронуты) ${totals.skippedAdminLimit}",
            # Backfill: платившие с ровно лимитом пробного — отдельная группа.
            "totals.markedPaid += result.marked_paid || 0;",
            "платившие с лимитом пробного: ${totals.markedPaid}",
            "сняты страховкой user-notify/оплатой",
            "applyButton.disabled = !(dryRun && totals.marked + totals.markedPaid > 0);",
            "managed_limits_available === false",
            "if (event.target.matches('[data-antiabuse-form]')) submitAntiabuseForm(event);",
            "formData.append('action', 'temp_ban');",
            # v3: карточки «всплеск», «гео», «автобан», «исключения адресов».
            'id="antiabuse-burst-form"',
            'id="antiabuse-burst-toggle"',
            'value="ipguard_burst_enable"',
            'value="ipguard_burst_set"',
            'name="burst_window_minutes"',
            'name="burst_ips_per_hwid"',
            'name="burst_confirmations"',
            'data-antiabuse-state="ipguard_burst"',
            'data-antiabuse-current="ipguard_burst_ips_per_hwid"',
            "Всплеск: одновременные подключения",
            # Минимум разных подсетей: поле формы, «Сейчас», предзаполнение и
            # фраза в подсказке «Как это работает».
            'name="burst_min_subnets"',
            'data-antiabuse-current="ipguard_burst_min_subnets"',
            "Минимум разных подсетей",
            "1 — считать по голым IP",
            "antiabuseSetField(burstForm, 'burst_min_subnets', effective.ipguard_burst_min_subnets)",
            "antiabuseSetCurrent(burstForm, 'ipguard_burst_min_subnets', effective.ipguard_burst_min_subnets ?? '—')",
            "иначе это ротация оператора, не всплеск",
            'id="antiabuse-geo-form"',
            'id="antiabuse-geo-toggle"',
            'value="ipguard_geo_enable"',
            'value="ipguard_geo_set"',
            'name="geo_window_minutes"',
            'name="geo_min_regions"',
            'data-antiabuse-state="ipguard_geo"',
            'data-antiabuse-current="ipguard_geo_min_regions"',
            "Гео: подключения из разных мест",
            "geo_status",
            'id="antiabuse-autoban-form"',
            'id="antiabuse-autoban-toggle"',
            'value="ipguard_autoban_enable"',
            'value="ipguard_autoban_set"',
            "data-antiabuse-autoban-segment-select",
            'name="autoban_segment"',
            'name="autoban_steps_minutes"',
            'name="autoban_escalation_window_hours"',
            'name="autoban_max_per_hour"',
            'name="max_alerts_per_hour"',
            'data-antiabuse-state="ipguard_autoban"',
            'data-antiabuse-current="ipguard_max_alerts_per_hour"',
            "Автобан и предохранители",
            "включённый автобан банит подписки автоматически",
            'id="antiabuse-excluded-form"',
            'value="ipguard_excluded_set"',
            'value="ipguard_excluded_clear"',
            'name="excluded_ips"',
            'data-antiabuse-current="ipguard_excluded_ips"',
            "Исключения адресов",
            "37.143.13.212, 10.0.0.0/8",
            # Слоты статуса новых карточек обязаны чиститься в renderAntiabuse,
            # иначе ошибка загрузки повиснет навсегда.
            'id="antiabuse-burst-status"',
            'id="antiabuse-geo-status"',
            'id="antiabuse-autoban-status"',
            'id="antiabuse-excluded-status"',
            "'antiabuse-burst-status', 'antiabuse-geo-status', 'antiabuse-autoban-status', 'antiabuse-excluded-status'",
            "ipguard_autoban_enable: 'ВКЛЮЧИТЬ АВТОБАН?",
            "ipguard_excluded_clear: 'Очистить список исключений ip-guard?",
            # v4: карточка-рубильник с периодом прогона, отдельный тумблер
            # суточного слоя и подсказки «Как это работает» с живыми числами.
            'id="antiabuse-ipguard-master-form"',
            'id="antiabuse-ipguard-master-status"',
            'value="ipguard_master_set"',
            'name="check_interval_seconds"',
            'data-antiabuse-current="ipguard_check_interval_seconds"',
            'id="antiabuse-ipguard-subnets-toggle"',
            'value="ipguard_subnets_enable"',
            'data-antiabuse-state="ipguard_subnets"',
            "ipguard_subnets_disable: 'Выключить алерты по подсетям?",
            "'antiabuse-ipguard-master-status', 'antiabuse-ipguard-status'",
            '<details class="antifraud-how">',
            'data-antiabuse-how="master"',
            'data-antiabuse-how="subnets"',
            'data-antiabuse-how="burst"',
            'data-antiabuse-how="geo"',
            'data-antiabuse-how="autoban"',
            'data-antiabuse-blind="burst"',
            'data-antiabuse-blind="geo"',
            "function antiabuseRenderIpguardHints(",
            "function antiabuseSetBlindWarning(",
            # «Включено» на карточке слоя при выключенном рубильнике — ровно та
            # путаница, из-за которой слой считают сломанным.
            "function antiabuseMasterOffNote(",
            "Но главный рубильник ip-guard выключен, поэтому ${tail}",
            "antiabuseMasterOffNote('ни алертов, ни наказаний нет')",
            "и ${missedPhrase}, останется незамеченным",
            "if (event.target.closest('[data-antiabuse-form]')) antiabuseRenderIpguardHints();",
            # v5: автобан по абсолютному порогу суточного слоя, гистерезис по
            # прогонам, пробный режим с бейджем на виду, исключённые подписки.
            'name="autoban_min_subnets"',
            'name="autoban_confirmations"',
            'data-antiabuse-current="ipguard_autoban_min_subnets"',
            'data-antiabuse-current="ipguard_autoban_confirmations"',
            "Бан с N подсетей за окно",
            "Подтверждений подряд",
            'id="antiabuse-autoban-dry-run-toggle"',
            'value="ipguard_autoban_dry_run_enable"',
            'data-antiabuse-state="ipguard_autoban_dry_run"',
            "Пробный режим (не банить, только помечать в алерте)",
            '<div class="antifraud-warning" data-antiabuse-dry-run role="status" hidden>',
            "Пробный режим: баны не выполняются",
            "в боевом режиме был бы бан",
            "Всплеск и гео никогда не банят",
            "antiabuseSetField(autobanForm, 'autoban_min_subnets', effective.ipguard_autoban_min_subnets)",
            "antiabuseSetField(autobanForm, 'autoban_confirmations', effective.ipguard_autoban_confirmations)",
            "antiabuseSetToggle(autobanForm, 'antiabuse-autoban-dry-run-toggle', Boolean(effective.ipguard_autoban_dry_run), 'ipguard_autoban_dry_run_enable', 'ipguard_autoban_dry_run_disable', 'ipguard_autoban_dry_run', false)",
            "if (dryRunBox) dryRunBox.hidden = !dryRun;",
            "всплеск и гео не банят",
            "ipguard_autoban_dry_run_disable: 'ВЫКЛЮЧИТЬ ПРОБНЫЙ РЕЖИМ?",
            'id="antiabuse-excluded-usernames-form"',
            'id="antiabuse-excluded-usernames-status"',
            'value="ipguard_excluded_usernames_set"',
            'value="ipguard_excluded_usernames_clear"',
            'name="excluded_usernames"',
            'data-antiabuse-current="ipguard_excluded_usernames"',
            "Исключённые подписки",
            "594514115, 7715572734",
            "antiabuseSetField(excludedUsernamesForm, 'excluded_usernames', effective.ipguard_excluded_usernames)",
            "'antiabuse-excluded-status', 'antiabuse-excluded-usernames-status'",
            "ipguard_excluded_usernames_clear: 'Очистить список исключённых подписок?",
        ):
            self.assertIn(needle, template, needle)
        for needle in (
            ".antiabuse-stack",
            ".antiabuse-bulk-row",
            ".client-summary-stat.is-limited",
            '[data-antiabuse-substate="on"]',
            ".client-summary-note.is-managed",
            ".client-summary-note.is-manual",
            ".antifraud-how",
            ".antifraud-how-now",
            ".antifraud-warning",
            # display:flex перебивает служебный hidden — без правила скрытое
            # предупреждение о слепоте осталось бы видимым всегда.
            ".antifraud-warning[hidden]",
        ):
            self.assertIn(needle, css, needle)
        # Селекты без «— без изменений —»: форма предзаполнена актуальным значением.
        self.assertNotIn("— без изменений —", template)

    def test_antiabuse_forms_submit_on_enter_instead_of_toggling(self):
        """В каждой форме вкладки скрытая кнопка *_set стоит ДО тумблера: иначе
        Enter в числовом поле отправлял бы форму с action первой submit-кнопки,
        то есть включал бы слой (для автобана — автоматические баны)."""
        template = template_source("engine/templates/admin_dashboard.html")

        for form_id, toggle_id, implicit_action in (
            ("antiabuse-trial-form", "antiabuse-trial-toggle", "trial_limit_set"),
            # Рубильник — отдельная форма: Enter в поле периода прогона не
            # должен включать ip-guard целиком.
            (
                "antiabuse-ipguard-master-form",
                "antiabuse-ipguard-toggle",
                "ipguard_master_set",
            ),
            (
                "antiabuse-ipguard-form",
                "antiabuse-ipguard-subnets-toggle",
                "ipguard_set",
            ),
            ("antiabuse-burst-form", "antiabuse-burst-toggle", "ipguard_burst_set"),
            ("antiabuse-geo-form", "antiabuse-geo-toggle", "ipguard_geo_set"),
            ("antiabuse-autoban-form", "antiabuse-autoban-toggle", "ipguard_autoban_set"),
        ):
            start = template.index(f'id="{form_id}"')
            form = template[start : template.index("</form>", start)]
            self.assertIn(
                f'value="{implicit_action}" class="antifraud-implicit-submit"', form
            )
            self.assertLess(
                form.index("antifraud-implicit-submit"),
                form.index(f'id="{toggle_id}"'),
                form_id,
            )
        # У карточки исключений тумблера нет, но неявная отправка всё равно
        # обязана быть «Сохранить», а не «Очистить список».
        start = template.index('id="antiabuse-excluded-form"')
        excluded_form = template[start : template.index("</form>", start)]
        self.assertIn(
            'value="ipguard_excluded_set" class="antifraud-implicit-submit"',
            excluded_form,
        )
        self.assertLess(
            excluded_form.index("antifraud-implicit-submit"),
            excluded_form.index('value="ipguard_excluded_clear"'),
        )
        # Исключённые подписки — отдельная форма в той же карточке: Enter в
        # ней не должен ни очищать список, ни трогать список адресов.
        start = template.index('id="antiabuse-excluded-usernames-form"')
        usernames_form = template[start : template.index("</form>", start)]
        self.assertIn(
            'value="ipguard_excluded_usernames_set" class="antifraud-implicit-submit"',
            usernames_form,
        )
        self.assertLess(
            usernames_form.index("antifraud-implicit-submit"),
            usernames_form.index('value="ipguard_excluded_usernames_clear"'),
        )
        self.assertNotIn('name="excluded_ips"', usernames_form)
        # Тумблер пробного режима стоит ПОСЛЕ скрытой кнопки *_set и не
        # является первой submit-кнопкой формы автобана.
        start = template.index('id="antiabuse-autoban-form"')
        autoban_form = template[start : template.index("</form>", start)]
        self.assertLess(
            autoban_form.index("antifraud-implicit-submit"),
            autoban_form.index('id="antiabuse-autoban-dry-run-toggle"'),
        )

    def test_client_card_exposes_limit_and_actions(self):
        template = template_source("engine/templates/admin_dashboard.html")

        for needle in (
            'data-client-sub-action="apply_trial_limit"',
            'data-client-sub-action="remove_traffic_limit"',
            "apply_trial_limit: 'Применить к подписке лимит трафика пробного",
            "remove_traffic_limit: 'Снять лимит трафика (0, без сброса, статус ACTIVE у ограниченных)?",
            "data-client-traffic-limit-value",
            "data-client-traffic-limit-meta",
            "traffic.is_limited",
            "traffic.hwid_devices",
            "traffic.traffic_limit_strategy_label",
            "paid: Boolean(errorPayload.paid),",
            # Подсказка при force=1 платившему: страховка user-notify снимет лимит.
            "страховка user-notify снимет этот лимит у платившего клиента в следующем же цикле",
            '<option value="apply_trial_limit">',
            '<option value="remove_traffic_limit">',
            # v2: ручной лимит владельца — 409 manual с отдельным подтверждением;
            # подпись «управляемый / ручной» в карточке; ГиБ/МиБ по 1024.
            "manual: Boolean(errorPayload.manual),",
            "Заменить ручной лимит панели лимитом пробного?",
            # v2.1: три независимых подтверждения — force / override_manual /
            # override_admin_limit; повтор со всеми подтверждёнными флагами.
            "adminLimit: Boolean(errorPayload.admin_limit),",
            "if (refusal.paid && !extra.force) {",
            "next.force = '1';",
            "if (refusal.manual && !extra.override_manual) {",
            "Ручной лимит владельца будет ПЕРЕЗАПИСАН: лимит станет управляемым (release_on=payment)",
            "next.override_manual = '1';",
            "if (refusal.adminLimit && !extra.override_admin_limit) {",
            "Заменить лимит админа из бота лимитом пробного?",
            "next.override_admin_limit = '1';",
            "clientSubscriptionAction(action, next);",
            "const confirmedRetry = Boolean(extra.force || extra.override_manual || extra.override_admin_limit);",
            "лимитов админа из бота (не тронуты) ${result.skipped_admin_limit}",
            "traffic.managed_limit",
            "is-managed",
            "ручной лимит владельца в панели не трогается",
            "const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'];",
            "пропущено платящих ${result.skipped_paid}",
        ):
            self.assertIn(needle, template, needle)
        self.assertNotIn("['Б', 'КБ', 'МБ', 'ГБ', 'ТБ']", template)

    def test_endpoints_routed_and_admin_only(self):
        import inspect

        from django.urls import reverse

        from engine import views

        self.assertEqual(reverse("support_admin_api_antiabuse"), "/support-admin/api/antiabuse/")
        self.assertEqual(
            reverse("support_admin_api_antiabuse_bulk"), "/support-admin/api/antiabuse-bulk/"
        )
        self.assertEqual(
            reverse("support_admin_api_ipguard_alerts"), "/support-admin/api/ipguard-alerts/"
        )
        self.assertEqual(
            reverse("support_admin_api_antiabuse_backfill"),
            "/support-admin/api/antiabuse-backfill/",
        )
        for func in (
            views.support_admin_api_antiabuse,
            views.support_admin_api_antiabuse_bulk,
            views.support_admin_api_antiabuse_backfill,
            views.support_admin_api_ipguard_alerts,
        ):
            self.assertIn(
                "require_support_admin_role(request, SUPPORT_ADMIN_ROLE_ADMIN)",
                inspect.getsource(func),
                func.__name__,
            )

    def test_limit_helpers_never_delete_recreate_or_touch_squads(self):
        import inspect

        from engine import views

        for func in (views.admin_rwms_apply_trial_limit, views.admin_rwms_remove_traffic_limit):
            src = inspect.getsource(func)
            self.assertNotIn("add_user", src)
            self.assertNotIn("active_internal_squads=", src)
            self.assertNotIn("delete", src.lower())
        bulk_src = inspect.getsource(views.support_admin_api_antiabuse_bulk)
        self.assertNotIn("add_user", bulk_src)
        self.assertNotIn("create_user", bulk_src)
        self.assertIn("get_user_by_username_strict", bulk_src)
        # Backfill только помечает: панель не пишется вовсе.
        backfill_src = inspect.getsource(views.support_admin_api_antiabuse_backfill)
        for forbidden in ("add_user", "create_user", "update_user", "delete_managed_limit"):
            self.assertNotIn(forbidden, backfill_src, forbidden)
        self.assertIn("get_user_by_username_strict", backfill_src)
        # Снятие — только при маркере (resolve → is_managed), маркер удаляется
        # после UpdateUser; ручной лимит — skipped_manual.
        release_src = inspect.getsource(views.admin_release_managed_limit)
        self.assertLess(release_src.index("resolve_managed_limit("), release_src.index("admin_rwms_remove_traffic_limit("))
        self.assertLess(release_src.index("admin_rwms_remove_traffic_limit("), release_src.index("delete_managed_limit("))
        self.assertIn('"skipped_manual"', release_src)

    def test_readme_documents_antiabuse(self):
        readme = Path("README.md").read_text()

        for needle in (
            "## Админка: Антиабьюз",
            "trial_traffic_limit_enabled",
            "ipguard_alerts_enabled",
            "support-admin/api/antiabuse/",
            "support-admin/api/antiabuse-bulk/",
            "support-admin/api/ipguard-alerts/",
            "apply_trial_limit",
            "remove_traffic_limit",
            "Продления не трогают лимит",
            "Продление не снимает и не ломает лимит трафика",
            "страховка user-notify снимет такой лимит",
            "(0; 100 000] ГиБ",
            "### Порядок включения / выключения",
            # v2: маркеры, предупреждения ip-guard, backfill, миграция.
            "managed_traffic_limits",
            "ipguard_warnings_enabled",
            "ipguard_warning_subnets_per_hwid",
            "support-admin/api/antiabuse-backfill/",
            "Пометить существующие лимиты пробных как управляемые",
            "Ручные лимиты владельца неприкосновенны",
            "site:admin:<login>",
            "traffic_limit_applied",
            "CREATE TABLE managed_traffic_limits",
            # v2.1: раздельные подтверждения карточки, лимиты админа из бота,
            # backfill платившим, commit на пользователя в сегментном apply.
            "`override_manual=1`",
            "`override_admin_limit=1`",
            "сам по себе ручной лимит НЕ заменяет — ответ остаётся `409 {\"manual\": true}`",
            "`skipped_admin_limit`",
            "`marked_paid`",
            "лимитом пробного: N — будут помечены и сняты страховкой user-notify/оплатой",
            "ANTIABUSE_BACKFILL_ROWS_SQL",
            "коммитятся сразу после его\n  `UpdateUser`",
            # v3: формы всплеска/гео/автобана/исключений вместо raw-списка.
            "ipguard_burst_enabled",
            "ipguard_burst_confirmations",
            "`ipguard_burst_min_subnets`",
            "ipguard_geo_min_regions",
            "ipguard_autoban_steps_minutes",
            "ipguard_autoban_max_per_hour",
            "ipguard_max_alerts_per_hour",
            "ipguard_excluded_ips",
            "`ipguard_excluded_set` / `ipguard_excluded_clear`",
            "parse_ipguard_autoban_steps",
            "parse_ipguard_excluded_ips",
            "Включить автобан при битой\nлестнице в БД нельзя",
            "geo_status",
            # v4: рубильник над слоями, период прогона, отдельный тумблер
            # суточного слоя и подсказки с предупреждением о слепоте.
            "ipguard_check_interval_seconds",
            "ipguard_subnets_enabled",
            "`ipguard_master_set`",
            "`ipguard_subnets_enable` /\n`ipguard_subnets_disable`",
            "Как это работает",
            "смотрит 1 минуту из каждых 5 — он слеп 4 минуты из 5",
            # v5: автобан по абсолютному порогу суточного слоя, гистерезис,
            # пробный режим, исключённые подписки.
            "`ipguard_autoban_min_subnets`",
            "`ipguard_autoban_confirmations`",
            "`ipguard_autoban_dry_run`",
            "`ipguard_excluded_usernames`",
            "`ipguard_autoban_dry_run_enable` /\n`ipguard_autoban_dry_run_disable`",
            "`ipguard_excluded_usernames_set` /\n`ipguard_excluded_usernames_clear`",
            "parse_ipguard_excluded_usernames",
            "в боевом режиме был бы бан",
            "Всплеск и гео",
        ):
            self.assertIn(needle, readme, needle)



class AntiabuseIslandBrandingTests(SimpleTestCase):
    """Антиабьюз приехал обратным портом из зеркального стека Monkey Village.

    Village и Island — зеркальные проекты, правки ходят в обе стороны, поэтому
    в перенесённом коде и документации не должно остаться village-брендинга:
    имена соседних сервисов (`monkey-island-payment`, а не `wata-webhook`),
    домены, `MV_`-переменные и village-овская alembic-ревизия сбивают с толку
    и на инциденте уводят дежурного не в тот репозиторий.
    """

    PORTED_SOURCES = (
        "engine/rwms_helpers.py",
        "engine/sql_helpers.py",
        "engine/views.py",
        "engine/infra.py",
        "mobile_api/provisioning.py",
        "engine/templates/admin_dashboard.html",
        "engine/static/css/admin_dashboard.css",
    )

    VILLAGE_TOKENS = ("monkey-village", "monkeyvillage", "mv.fornex", "MV_")

    def _antiabuse_readme_section(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        start = readme.index("## Админка: Антиабьюз")
        end = readme.index("\n## ", start + 1)
        return readme[start:end]

    def test_ported_sources_carry_no_village_branding(self):
        for path in self.PORTED_SOURCES:
            text = Path(path).read_text(encoding="utf-8")
            for token in self.VILLAGE_TOKENS:
                self.assertNotIn(token, text, f"{path}: {token}")

    def test_readme_antiabuse_section_names_island_services(self):
        section = self._antiabuse_readme_section()
        # Соседний сервис оплаты на острове называется monkey-island-payment.
        self.assertIn("monkey-island-payment", section)
        self.assertNotIn("wata-webhook", section)
        for token in self.VILLAGE_TOKENS:
            self.assertNotIn(token, section, token)

    def test_readme_antiabuse_section_points_at_island_alembic_head(self):
        section = self._antiabuse_readme_section()
        # Миграцию managed_traffic_limits владелец пишет поверх головы ОСТРОВА.
        self.assertIn("island alembic head `be91cc5b23a5`", section)
        self.assertNotIn("4459b8ea9544", section)


class AntiabuseManagedLimitHelpersTests(_AntiabuseSqliteMixin, SimpleTestCase):
    """engine/rwms_helpers.py: маркеры управляемых лимитов, проба таблицы,
    правило adoption, события event_logs."""

    def _limit(self, gb=5.0, strategy="day"):
        from engine.rwms_helpers import TrialTrafficLimit

        return TrialTrafficLimit(
            limit_gb=gb,
            limit_bytes=int(gb * 1024**3),
            strategy_key=strategy,
            strategy_name=strategy.upper(),
        )

    def test_table_available_probe_keeps_session_usable(self):
        from engine.rwms_helpers import managed_limits_table_available

        self._user(1, "u")
        self.session.commit()
        self.assertTrue(managed_limits_table_available(self.session))
        self._drop_marker_table()
        with self.assertLogs(level="WARNING") as logs:
            self.assertFalse(managed_limits_table_available(self.session))
        self.assertTrue(any("миграция не накачена" in line for line in logs.output))
        # Транзакция вызывающего кода цела: обычные запросы работают.
        self.assertEqual(self.session.get(User, 1).username, "u")

    def test_record_marker_writes_trial_payment_marker_in_session(self):
        from engine.rwms_helpers import record_trial_limit_marker

        user = self._user(1, "u")
        marker = record_trial_limit_marker(self.session, user, self._limit(0.5, "week"))
        self.assertIsNotNone(marker)
        self.session.commit()
        stored = self._marker_of(1)
        self.assertEqual(stored.limit_bytes, 536870912)
        self.assertEqual(stored.strategy, "WEEK")
        self.assertEqual((stored.reason, stored.release_on, stored.applied_by), ("trial", "payment", "site:register"))
        # Повтор с другим applied_by — upsert той же записи.
        record_trial_limit_marker(self.session, user, self._limit(), "site:admin:root")
        self.session.commit()
        stored = self._marker_of(1)
        self.assertEqual((stored.limit_bytes, stored.applied_by), (5 * 1024**3, "site:admin:root"))
        # Без лимита / без пользователя — ничего не пишем.
        self.assertIsNone(record_trial_limit_marker(self.session, user, None))
        self.assertIsNone(record_trial_limit_marker(self.session, None, self._limit()))
        self.assertIsNone(record_trial_limit_marker(self.session, SimpleNamespace(id=None), self._limit()))

    def test_record_marker_degrades_without_table(self):
        from engine.rwms_helpers import record_trial_limit_marker

        self._user(1, "u")
        self.session.commit()
        self._drop_marker_table()
        user = self.session.get(User, 1)
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(record_trial_limit_marker(self.session, user, self._limit()))
        self.assertTrue(any("WITHOUT managed marker" in line for line in logs.output))
        self.assertEqual(self.session.get(User, 1).username, "u")

    def test_panel_limit_matches_and_adoption_rule(self):
        from engine.rwms_helpers import adopted_trial_limit
        from engine.rwms_helpers import panel_limit_matches

        limit = self._limit()
        self.assertTrue(panel_limit_matches(SimpleNamespace(traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1), limit))
        self.assertTrue(panel_limit_matches(SimpleNamespace(traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy="DAY"), limit))
        self.assertFalse(panel_limit_matches(SimpleNamespace(traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=0), limit))
        self.assertFalse(panel_limit_matches(SimpleNamespace(traffic_limit_bytes=1024**3, traffic_limit_strategy=1), limit))
        self.assertFalse(panel_limit_matches(SimpleNamespace(traffic_limit_bytes=0, traffic_limit_strategy=1), limit))
        self.assertFalse(panel_limit_matches(SimpleNamespace(), limit))
        self.assertFalse(panel_limit_matches(SimpleNamespace(traffic_limit_bytes="x", traffic_limit_strategy=1), limit))
        self.assertFalse(panel_limit_matches(None, limit))
        self.assertFalse(panel_limit_matches(SimpleNamespace(traffic_limit_bytes=1), None))

        panel = SimpleNamespace(traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1)
        # Тумблер выключен — adoption маркер не пишет.
        self.assertIsNone(adopted_trial_limit(self.session, panel))
        self._set("trial_traffic_limit_enabled", "1")
        adopted = adopted_trial_limit(self.session, panel)
        self.assertEqual((adopted.limit_bytes, adopted.strategy_name), (5 * 1024**3, "DAY"))
        # Другой лимит в панели — ручной, не наш.
        self.assertIsNone(adopted_trial_limit(self.session, SimpleNamespace(traffic_limit_bytes=1024**3, traffic_limit_strategy=1)))

    def test_add_traffic_limit_event_payload(self):
        from engine.rwms_helpers import add_traffic_limit_event

        marker = SimpleNamespace(
            limit_bytes=5 * 1024**3, strategy="DAY", reason="trial", release_on="payment", applied_by="bot:start"
        )
        session = SimpleNamespace(added=[])
        session.add = session.added.append

        event = add_traffic_limit_event(session, 7, "traffic_limit_applied", marker, changed=True)

        self.assertEqual(event.user_id, 7)
        self.assertEqual(event.event_type, "traffic_limit_applied")
        self.assertEqual(
            event.event_payload,
            {"reason": "trial", "applied_by": "bot:start", "limit": 5 * 1024**3, "strategy": "DAY", "release_on": "payment", "changed": True},
        )
        self.assertEqual(session.added, [event])
        self.assertIsNone(add_traffic_limit_event(session, 7, "traffic_limit_released", None))
        self.assertIsNone(add_traffic_limit_event(session, None, "traffic_limit_released", marker))
        with self.assertRaises(ValueError):
            add_traffic_limit_event(session, 7, "limit_changed", marker)
        self.assertEqual(len(session.added), 1)

    def test_admin_payload_helpers(self):
        from engine.views import admin_bytes_label
        from engine.views import admin_managed_limit_payload
        from engine.views import admin_marker_snapshot

        self.assertEqual(admin_bytes_label(536870912), "512 МиБ")
        self.assertEqual(admin_bytes_label(5 * 1024**3), "5 ГиБ")
        self.assertEqual(admin_bytes_label(int(1.5 * 1024**3)), "1.5 ГиБ")
        self.assertEqual(admin_bytes_label(0), "0")
        self.assertEqual(admin_bytes_label("x"), "0")

        user = self._user(1, "u")
        marker = admin_marker_snapshot(self._marker(user, applied_by="bot:start"))
        self.assertIsNone(admin_marker_snapshot(None))

        managed = admin_managed_limit_payload(marker, 5 * 1024**3, "day")
        self.assertEqual(managed["kind"], "managed")
        self.assertIs(managed["managed"], True)
        self.assertIn("Управляемый лимит (trial, с ", managed["label"])
        self.assertIn("bot:start", managed["label"])
        self.assertIn("снимается оплатой", managed["label"])
        self.assertEqual(managed["marker"]["limit_label"], "5 ГиБ")
        self.assertEqual(managed["marker"]["strategy"], "day")

        manual = admin_managed_limit_payload(marker, 1024**3, "day")
        self.assertEqual(manual["kind"], "manual")
        self.assertIn("владелец", manual["label"])
        self.assertIn("маркер не совпадает", manual["label"])
        self.assertEqual(admin_managed_limit_payload(None, 1024**3, "day")["kind"], "manual")
        self.assertEqual(admin_managed_limit_payload(None, 0, None)["kind"], "none")
        self.assertEqual(admin_managed_limit_payload(marker, None, None)["kind"], "unknown")
        self.assertIn("есть маркер", admin_managed_limit_payload(marker, None, None)["label"])
        unavailable = admin_managed_limit_payload(None, 5 * 1024**3, "day", available=False)
        self.assertEqual((unavailable["kind"], unavailable["available"]), ("unknown", False))
        self.assertIn("managed_traffic_limits", unavailable["label"])


class AntiabuseTrafficEndpointTests(_AntiabuseSqliteMixin, SimpleTestCase):
    """support-admin/api/user-traffic/: карточка клиента показывает
    «управляемый лимит (trial, с <дата>, кем)» либо «ручной лимит панели»;
    без таблицы маркеров карточка не падает."""

    def _get(self, client, q="42"):
        from engine.views import support_admin_api_user_traffic

        request = RequestFactory().get("/support-admin/api/user-traffic/", {"q": q})
        request.session = {}
        with (
            mock.patch("engine.views.require_support_admin", return_value=None),
            mock.patch("engine.views.session_factory", return_value=self.session),
            mock.patch("engine.views.rwms_client", client),
        ):
            response = support_admin_api_user_traffic(request)
        return response.status_code, json.loads(response.content)

    def _client(self, limit_bytes, strategy=1, status=0):
        client = mock.Mock()
        client.get_user_by_username.return_value = SimpleNamespace(
            uuid="u1",
            used_traffic_bytes=1,
            lifetime_used_traffic_bytes=2,
            traffic_limit_bytes=limit_bytes,
            traffic_limit_strategy=strategy,
            status=status,
        )
        client.get_user_hwid_devices.return_value = SimpleNamespace(total=1)
        return client

    def setUp(self):
        super().setUp()
        self.user = self._user(7, "42")
        self.session.commit()

    def test_managed_manual_and_none(self):
        self._marker(self.user, applied_by="bot:start")
        self.session.commit()

        status, payload = self._get(self._client(5 * 1024**3, 1, 2))
        self.assertEqual(status, 200)
        managed = payload["result"]["managed_limit"]
        self.assertEqual(managed["kind"], "managed")
        self.assertEqual(managed["marker"]["applied_by"], "bot:start")
        self.assertEqual(managed["marker"]["reason"], "trial")
        self.assertIs(payload["result"]["is_limited"], True)

        # Владелец сменил лимит руками: ручной (маркер на GET не удаляем).
        status, payload = self._get(self._client(1024**3, 1))
        self.assertEqual(payload["result"]["managed_limit"]["kind"], "manual")
        self.assertIsNotNone(self._marker_of(7))

        from common.models.db import ManagedTrafficLimit

        self.session.query(ManagedTrafficLimit).delete()
        self.session.commit()
        status, payload = self._get(self._client(1024**3, 1))
        self.assertEqual(payload["result"]["managed_limit"]["kind"], "manual")
        self.assertIsNone(payload["result"]["managed_limit"]["marker"])
        status, payload = self._get(self._client(0, 0))
        self.assertEqual(payload["result"]["managed_limit"]["kind"], "none")

    def test_panel_unavailable_and_missing_table(self):
        client = mock.Mock()
        client.get_user_by_username.return_value = None
        status, payload = self._get(client)
        self.assertEqual(status, 200)
        self.assertIs(payload["result"]["available"], False)
        self.assertEqual(payload["result"]["managed_limit"]["kind"], "unknown")

        status, payload = self._get(client, q="nobody")
        self.assertEqual(status, 404)

        self._drop_marker_table()
        with self.assertLogs(level="WARNING"):
            status, payload = self._get(self._client(5 * 1024**3, 1))
        self.assertEqual(status, 200)
        self.assertIs(payload["result"]["available"], True)
        managed = payload["result"]["managed_limit"]
        self.assertIs(managed["available"], False)
        self.assertEqual(managed["kind"], "unknown")
        self.assertIn("managed_traffic_limits", managed["label"])


class AntiabuseBackfillTests(_AntiabuseSqliteMixin, SimpleTestCase):
    """support-admin/api/antiabuse-backfill/: «Пометить существующие лимиты
    пробных как управляемые» — все пользователи, панель ровно с текущим
    лимитом пробных (байты И стратегия) → маркер backfill (never_paid —
    marked, платившие — marked_paid); dry-run, порции, аудит."""

    def setUp(self):
        super().setUp()
        self.audit = mock.Mock()
        self.events = mock.Mock()

    def _post(self, client, **data):
        from engine.views import support_admin_api_antiabuse_backfill

        request = RequestFactory().post("/support-admin/api/antiabuse-backfill/", data=data)
        request.session = {}
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.session_factory", return_value=self.session),
            mock.patch("engine.views.rwms_client", client),
            mock.patch("engine.views.admin_audit_write", self.audit),
            mock.patch("engine.views.add_traffic_limit_event", self.events),
        ):
            response = support_admin_api_antiabuse_backfill(request)
        return response.status_code, json.loads(response.content)

    def _fixture(self):
        t1 = self._user(1, "t1")  # наш лимит без маркера → marked
        self._user(2, "t2")  # ручной кап 1 ГиБ → skipped
        self._user(3, "t3")  # без лимита → skipped
        t4 = self._user(4, "t4")  # уже управляемый → already
        self._user(5, "t5")  # нет в панели → missing
        paid = self._user(6, "paid")  # платил, ровно лимит пробного → marked_paid
        self._yk_payment(paid)
        paid_cap = self._user(7, "paidcap")  # платил, ручной кап 1 ГиБ → skipped
        self._wata_payment(paid_cap)
        self._marker(t4)
        self.session.commit()
        panel = {
            "t1": SimpleNamespace(uuid="u1", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=0),
            "t2": SimpleNamespace(uuid="u2", traffic_limit_bytes=1024**3, traffic_limit_strategy=1, status=0),
            "t3": SimpleNamespace(uuid="u3", traffic_limit_bytes=0, traffic_limit_strategy=0, status=0),
            "t4": SimpleNamespace(uuid="u4", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=2),
            "t5": None,
            "paid": SimpleNamespace(uuid="u6", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=0),
            "paidcap": SimpleNamespace(uuid="u7", traffic_limit_bytes=1024**3, traffic_limit_strategy=1, status=0),
        }
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = lambda name: panel[name]
        return client, t1

    def test_dry_run_counts_without_writing(self):
        client, t1 = self._fixture()

        status, payload = self._post(client, dry_run="1")

        self.assertEqual(status, 200)
        result = payload["result"]
        self.assertIs(result["dry_run"], True)
        self.assertEqual(result["action_label"], "Пометить существующие лимиты пробных как управляемые")
        self.assertEqual(result["limit_label"], "5 ГиБ · ежедневно")
        self.assertEqual(
            (result["processed"], result["marked"], result["marked_paid"], result["already"], result["skipped"], result["missing"]),
            (7, 1, 1, 1, 3, 1),
        )
        self.assertIs(result["done"], True)
        self.assertIsNone(self._marker_of(1))
        self.assertIsNone(self._marker_of(6))
        # Платившие тоже проходят по панели: их лимит пробного — отдельная группа.
        self.assertEqual(client.get_user_by_username_strict.call_count, 7)
        self.assertIn(mock.call("paid"), client.get_user_by_username_strict.call_args_list)
        client.update_user.assert_not_called()
        self.audit.assert_not_called()
        self.events.assert_not_called()

    def test_apply_marks_matching_limits_in_batches(self):
        client, t1 = self._fixture()

        status, payload = self._post(client, dry_run="0", batch_size="3")

        self.assertEqual(status, 200)
        result = payload["result"]
        self.assertEqual((result["processed"], result["marked"], result["marked_paid"], result["skipped"]), (3, 1, 0, 2))
        self.assertIs(result["done"], False)
        self.assertEqual(result["next_after_id"], 3)
        marker = self._marker_of(1)
        self.assertEqual((marker.limit_bytes, marker.strategy), (5 * 1024**3, "DAY"))
        self.assertEqual((marker.reason, marker.release_on, marker.applied_by), ("trial", "payment", "backfill"))
        self.assertEqual(self.audit.call_args.args[2], "antiabuse_backfill")
        self.assertEqual(self.audit.call_args.kwargs["strategy"], "day")
        self.assertEqual(self.audit.call_args.kwargs["marked_paid"], 0)
        events = self.events.call_args_list
        self.assertEqual([c.args[2] for c in events], ["traffic_limit_applied"])
        self.assertIs(events[0].kwargs["backfill"], True)
        self.assertIs(events[0].kwargs["never_paid"], True)

        status, payload = self._post(client, dry_run="0", batch_size="5", after_id="3")

        result = payload["result"]
        self.assertEqual(
            (result["processed"], result["marked"], result["marked_paid"], result["already"], result["skipped"], result["missing"]),
            (4, 0, 1, 1, 1, 1),
        )
        self.assertIs(result["done"], True)
        # Уже управляемый маркер не перезаписан (applied_by прежний).
        self.assertEqual(self._marker_of(4).applied_by, "bot:start")
        # Плативший с ровно лимитом пробного помечен trial/payment/backfill —
        # дальше его снимет страховка user-notify / следующая оплата.
        marker = self._marker_of(6)
        self.assertEqual((marker.limit_bytes, marker.strategy), (5 * 1024**3, "DAY"))
        self.assertEqual((marker.reason, marker.release_on, marker.applied_by), ("trial", "payment", "backfill"))
        self.assertIs(self.events.call_args_list[-1].kwargs["never_paid"], False)
        self.assertEqual(self.audit.call_args.kwargs["marked_paid"], 1)
        self.assertEqual(self.audit.call_args.kwargs["target"], "1/4 users")
        # Ручной кап платившего (другая сигнатура) не помечен.
        self.assertIsNone(self._marker_of(7))
        client.update_user.assert_not_called()
        client.add_user.assert_not_called()

    def test_bot_admin_marker_is_not_overwritten_when_managed(self):
        """Маркер бота (release_on='manual') с ровно лимитом пробных — already:
        backfill не переводит его в payment."""
        t1 = self._user(1, "t1")
        self._marker(t1, reason="ip_abuse", release_on="manual", applied_by="bot:admin:1")
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = SimpleNamespace(
            uuid="u1", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=0
        )

        status, payload = self._post(client, dry_run="0")

        self.assertEqual((payload["result"]["marked"], payload["result"]["already"]), (0, 1))
        marker = self._marker_of(1)
        self.assertEqual((marker.reason, marker.release_on, marker.applied_by), ("ip_abuse", "manual", "bot:admin:1"))

    def test_stale_marker_is_overwritten_when_panel_matches(self):
        """Маркер с другим лимитом, а панель — ровно наш текущий: маркер
        обновляется (backfill), панель не трогаем."""
        t1 = self._user(1, "t1")
        self._marker(t1, limit_bytes=1024**3, strategy="WEEK", applied_by="bot:start")
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = SimpleNamespace(
            uuid="u1", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=0
        )

        status, payload = self._post(client, dry_run="0")

        self.assertEqual(payload["result"]["marked"], 1)
        marker = self._marker_of(1)
        self.assertEqual((marker.limit_bytes, marker.strategy, marker.applied_by), (5 * 1024**3, "DAY", "backfill"))

    def test_rwms_unavailable_returns_503_with_progress(self):
        self._user(1, "t1")
        self._user(2, "t2")
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = [
            SimpleNamespace(uuid="u1", traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1, status=0),
            RwmsUnavailableError("t2", None, "down"),
        ]

        with self.assertLogs(level="WARNING"):
            status, payload = self._post(client, dry_run="0")

        self.assertEqual(status, 503)
        self.assertIn("прогресс сохранён", payload["message"])
        self.assertEqual((payload["result"]["processed"], payload["result"]["marked"]), (1, 1))
        self.assertEqual(payload["result"]["next_after_id"], 1)
        self.assertIs(payload["result"]["done"], False)
        self.assertIsNotNone(self._marker_of(1))
        self.assertIs(self.audit.call_args.kwargs["rwms_unavailable"], True)

    def test_requires_marker_table_and_validates_params(self):
        client = mock.Mock()
        status, payload = self._post(client, dry_run="1", after_id="x")
        self.assertEqual(status, 400)

        from engine.views import support_admin_api_antiabuse_backfill

        request = RequestFactory().get("/support-admin/api/antiabuse-backfill/")
        request.session = {}
        with mock.patch("engine.views.require_support_admin_role", return_value=None):
            self.assertEqual(support_admin_api_antiabuse_backfill(request).status_code, 405)

        self._drop_marker_table()
        with self.assertLogs(level="WARNING"):
            status, payload = self._post(client, dry_run="1")
        self.assertEqual(status, 503)
        self.assertIn("managed_traffic_limits", payload["message"])
        client.get_user_by_username_strict.assert_not_called()

    def test_rows_sql_covers_paid_and_never_paid_regardless_of_block_and_telegram(self):
        """Backfill идёт по всем пользователям; флаг «платил» — тот же
        PAYS_EXISTS_SQL, что у сегментов never_paid/paid_any; без фильтра
        блокировок и Telegram."""
        from common.models.segments import PAYS_EXISTS_SQL
        from engine.views import ANTIABUSE_BACKFILL_ROWS_SQL

        self.assertIn(f"({PAYS_EXISTS_SQL}) AS has_payment", ANTIABUSE_BACKFILL_ROWS_SQL)
        self.assertIn("WHERE u.id > :after_id ORDER BY u.id LIMIT :limit", ANTIABUSE_BACKFILL_ROWS_SQL)
        self.assertNotIn("user_blocks", ANTIABUSE_BACKFILL_ROWS_SQL)
        self.assertNotIn("telegram_id", ANTIABUSE_BACKFILL_ROWS_SQL)
        self.assertNotIn("NOT (", ANTIABUSE_BACKFILL_ROWS_SQL)


class AntiabuseSiteRegistrationMarkerTests(_AntiabuseSqliteMixin, SimpleTestCase):
    """create_site_user: маркер пишется в ТОЙ ЖЕ сессии, что и строка users
    (реальная SQLite-сессия), при adoption — только по правилу backfill."""

    def _create(self, client, email="limit@example.com"):
        context = {"referrer": None, "ymid": None, "traffic_source": None}
        with (
            mock.patch("engine.views.get_registration_context", return_value=context),
            mock.patch("engine.views.rwms_client", client),
            mock.patch("engine.views.add_user_to_traffic_progress"),
            mock.patch("engine.views.add_event_log"),
            mock.patch("engine.views.add_traffic_limit_event") as events,
            mock.patch("engine.views.should_create_trial_for_channel", return_value=True),
        ):
            user = create_site_user(self.session, email, SimpleNamespace())
        return user, events

    def test_new_trial_writes_marker_when_limit_enabled(self):
        self._set("trial_traffic_limit_enabled", "1")
        self._set("trial_traffic_limit_gb", "0.5")
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = None
        created = {}

        def create_user(**kwargs):
            created.update(kwargs)
            return _FakeSiteRwUser(username=kwargs["username"], traffic_limit_bytes=536870912, traffic_limit_strategy=1)

        with mock.patch("engine.views.create_user", side_effect=create_user):
            user, events = self._create(client)

        self.assertEqual(created["traffic_limit"].limit_bytes, 536870912)
        # Маркер в сессии ещё до commit (та же транзакция, что и users).
        from common.models.db import ManagedTrafficLimit

        marker = self.session.get(ManagedTrafficLimit, user.id)
        self.assertEqual((marker.limit_bytes, marker.strategy, marker.applied_by), (536870912, "DAY", "site:register"))
        self.assertEqual(events.call_args.args[2], "traffic_limit_applied")
        self.assertEqual(events.call_args.kwargs["creation_channel"], "site")
        self.session.commit()
        self.assertIsNotNone(self._marker_of(user.id))

    def test_new_trial_without_limit_writes_no_marker(self):
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = None
        with mock.patch(
            "engine.views.create_user",
            side_effect=lambda **kwargs: _FakeSiteRwUser(username=kwargs["username"]),
        ):
            user, events = self._create(client)
        self.session.commit()
        self.assertIsNone(self._marker_of(user.id))
        events.assert_called_once()
        self.assertIsNone(events.call_args.args[3])

    def test_adoption_marks_only_exact_trial_limit(self):
        email = "crash@example.com"
        username = site_registration_username(email)
        self._set("trial_traffic_limit_enabled", "1")
        self.session.commit()
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            username=username, email=email, traffic_limit_bytes=5 * 1024**3, traffic_limit_strategy=1
        )

        with mock.patch("engine.views.create_user") as create_user:
            user, events = self._create(client, email=email)

        create_user.assert_not_called()
        client.add_user.assert_not_called()
        self.session.commit()
        self.assertEqual(self._marker_of(user.id).applied_by, "site:register")
        self.assertIs(events.call_args.kwargs["adopted"], True)

        # Другой лимит в панели (ручной) — маркера нет.
        email2 = "crash2@example.com"
        client.get_user_by_username_strict.return_value = _FakeSiteRwUser(
            username=site_registration_username(email2), email=email2,
            traffic_limit_bytes=1024**3, traffic_limit_strategy=1,
        )
        with mock.patch("engine.views.create_user"):
            user2, events = self._create(client, email=email2)
        self.session.commit()
        self.assertIsNone(self._marker_of(user2.id))


class AdminInlineJsSmokeTests(SimpleTestCase):
    """Рендер-функции админки реально выполняются, а не только парсятся.

    Инлайн-JS админки живёт в шаблоне и питоновскими тестами не покрывается;
    маркерные проверки ловят только наличие строк. 09.09.2026 так уехал в
    прод ReferenceError (обращение к `checks` вместо `censorChecksCache`):
    синтаксис валиден, `node --check` проходит, а вкладка «Замеры ТСПУ»
    показывает только «Ошибка запроса списка проверок» — исключение глотал
    try/catch загрузчика. Здесь функции вырезаются из шаблона и ВЫПОЛНЯЮТСЯ
    в node с заглушками.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.template = template_source("engine/templates/admin_dashboard.html")

    def _region(self, start_marker, end_marker):
        start = self.template.index(start_marker)
        end = self.template.index(end_marker, start)
        return self.template[start:end]

    def _run_node(self, source):
        node = shutil.which("node")
        if not node:
            self.skipTest("node не установлен")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(source)
            path = handle.name
        try:
            result = subprocess.run(
                [node, path], capture_output=True, text=True, timeout=30
            )
        finally:
            os.unlink(path)
        self.assertEqual(
            result.returncode, 0,
            f"node упал:\n{result.stdout}\n{result.stderr}",
        )
        return result.stdout

    def test_censor_checks_table_renders(self):
        region = self._region(
            "        function renderCensorChecks(target) {",
            "        async function censorCheckAction",
        )
        source = """
const escapeHtml = (v) => String(v ?? '');
const CENSOR_INTERVAL_LABELS = {};
const censorHasDefaultKey = true;
const csrfToken = 'x';
const main = {dataset: {censorChecksUrl: '/'}};
function censorRunStatusHtml(run) { return run ? 'run' : 'нет'; }
function showAdminToast() {}
function loadCensorChecks() {}
let censorChecksCache = [];
const target = {
  _v: '',
  set innerHTML(v) { this._v = v; },
  get innerHTML() { return this._v; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
""" + region + """
censorChecksCache = [
  {id: 1, target_ip: '2.58.66.198', port: 443, sni: 'de.monkora.org',
   is_enabled: true, geo_mode: false, light_mode: true, interval_minutes: null,
   alerts_enabled: false, api_key_name: null, last_run: null},
  {id: 2, target_ip: '2.58.66.143', port: 8443, sni: 'de.easyemploy.org',
   is_enabled: false, geo_mode: true, light_mode: false, interval_minutes: 360,
   alerts_enabled: true, api_key_name: 'main',
   last_run: {id: 9, created_at: '2026-09-09 10:00'}},
];
renderCensorChecks(target);
const html = target.innerHTML;
for (const marker of ['data-censor-select="1"', 'data-censor-select="2"',
                      'data-censor-select-all', 'data-censor-bulk-delete']) {
  if (!html.includes(marker)) { console.error('нет маркера ' + marker); process.exit(1); }
}
// Выделение переживает перерисовку и отмечает нужную строку
censorSelectedChecks.add(1);
renderCensorChecks(target);
if (!target.innerHTML.includes('data-censor-select="1" checked')) {
  console.error('выделение потеряно'); process.exit(1);
}
// Пропавшая из списка строка выпадает из выделения: иначе «удалить
// выделенные» однажды унесёт то, чего админ уже не видит
censorChecksCache = censorChecksCache.filter((check) => check.id !== 1);
renderCensorChecks(target);
if (censorSelectedChecks.has(1)) { console.error('висит выделение'); process.exit(1); }
censorChecksCache = [];
renderCensorChecks(target);
if (!target.innerHTML.includes('Проверок пока нет')) {
  console.error('пустой список сломан'); process.exit(1);
}
console.log('ok');
"""
        self.assertIn("ok", self._run_node(source))

    def test_infra_domain_row_renders(self):
        region = self._region(
            "        function infraDomainRowHtml(item",
            "        function infraSyncDomainEmptyState",
        )
        source = """
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"]/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
""" + region + """
const banned = new Set(['de.monkora.org']);
// Имена SNI живут у сервера: у домена нет поля ввода, только вердикт
let html = infraDomainRowHtml(
  {domain: 'de.monkora.org', snis: ['de.monkora.org'], own_name: true},
  false, banned, '2026-09-09 10:00:00');
if (html.includes('<input')) { console.error('у домена осталось поле SNI'); process.exit(1); }
if (!html.includes('ИМЯ В БАНЕ')) { console.error('нет пометки бана'); process.exit(1); }
// Несколько имён: забаненное названо поимённо, рабочие перечислены отдельно
html = infraDomainRowHtml(
  {domain: 'de.monkora.org', snis: ['de.monkora.org', 'example.org'], own_name: true},
  false, banned, '2026-09-09 10:00:00');
if (!html.includes('работают: example.org')) { console.error('нет рабочих имён'); process.exit(1); }
// Устаревший вердикт: пометок нет
html = infraDomainRowHtml(
  {domain: 'de.monkora.org', snis: ['de.monkora.org'], own_name: true},
  false, new Set(), '');
if (html.includes('В БАНЕ')) { console.error('пометка на устаревшем вердикте'); process.exit(1); }
// Строка «привязываем» и вызов старой формой (строкой вместо объекта)
html = infraDomainRowHtml('new.example.xyz', true);
if (!html.includes('Привязываем')) { console.error('pending сломан'); process.exit(1); }
console.log('ok');
"""
        self.assertIn("ok", self._run_node(source))

    def test_referral_activity_leaderboard_renders(self):
        region = self._region(
            "        function renderReferralActivity(result) {",
            "        let refActivityLoaded = false;",
        )
        source = """
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"]/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const displayDate = (iso) => iso ? iso.split('-').reverse().join('.') : '';
""" + region + """
const row = (i, extra = {}) => ({user: {id: i, username: 'ref' + i, email: '', telegram_id: String(1000 + i)},
  invited: 50 - i, connected: 30 - i, paid_users: i === 4 ? 0 : 5, revenue: i === 4 ? 0 : 1000 - i,
  bonus_days: 10 + i, lifetime_invited: 100 + i, ...extra});
const result = {period: {start: '2026-09-01', end: '2026-09-10'},
  totals: {referrers: 12, referrals: 300, revenue: 18320, bonus_days: 1604},
  rows: Array.from({length: 12}, (_, i) => row(i)), truncated: false};
// toLocaleString в node вставляет узкий неразрывный пробел — нормализуем
const html = renderReferralActivity(result).replace(/[\u202f\u00a0]/g, ' ');
for (const marker of ['@ref0', 'tg 1000', 'всего пригласил 100', '01.09.2026 — 10.09.2026',
                      '+300 рефералов', '18 320 ₽', '+10 дн бонусов', 'data-refact-user="ref0"',
                      'data-refact-show-all', 'из 12 рефереров']) {
  if (!html.includes(marker)) { console.error('нет: ' + marker); process.exit(1); }
}
// Первые 10 видимы, остальные спрятаны до «Показать всех»
if ((html.match(/refact-row is-extra/g) || []).length !== 2) { console.error('is-extra != 2'); process.exit(1); }
// Конверсия ниже процента — с десятой, ноль — гаснет
if (!html.includes('10%')) { console.error('нет конверсии'); process.exit(1); }
if (!/refact-money is-zero/.test(html)) { console.error('нулевая выручка не погашена'); process.exit(1); }
// Пусто
const empty = renderReferralActivity({period: {}, totals: {referrers: 0, referrals: 0}, rows: []});
if (!empty.includes('рефералов нет')) { console.error('нет пустого состояния'); process.exit(1); }
console.log('ok');
"""
        self.assertIn("ok", self._run_node(source))

    def test_client_referrals_tree_renders(self):
        region = self._region(
            "        function referralBonusTypeLabel(type) {",
            "        // ---------- Быстрая карточка подписки из «Трафика нод» ----------",
        )
        source = """
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"]/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const main = {dataset: {fullAdmin: '1'}};
""" + region + """
const payload = {
  user: {id: 1, username: 'demo_client'},
  referral_block: {blocked: false},
  summary: {referrals: 2, paid_referrals: 1, bonus_days: 17},
  referrals: [
    {user: {id: 2, username: 'marina_k', email: 'm@x.io'}, paid: true, bonus_days: 17,
     payments_count: 4, children_count: 1,
     bonuses: [{type: 'TRAFFIC', days: 7, created_at: '08.06.2026 14:00'}]},
    {user: {id: 3, telegram_id: 555}, paid: false, bonus_days: 0, payments_count: 0,
     children_count: 0, bonuses: []},
  ],
  graph: {nodes: [{id: 1, label: 'demo_client', root: true}, {id: 2, label: '@marina_k'},
                  {id: 3, label: 'ID 555'}, {id: 4, label: '@stepan_v'}],
          edges: [{from: 1, to: 2}, {from: 1, to: 3}, {from: 2, to: 4}]},
};
let html = clientReferralsSectionHtml(payload);
for (const marker of ['@demo_client', '2 прямых', 'data-client-ref-row', '@marina_k',
                      'Платил', 'Без оплат', '+7 дн', 'трафик', '@stepan_v', '├', '└',
                      'Бонусов по этому рефералу нет.']) {
  if (!html.includes(marker)) { console.error('нет: ' + marker); process.exit(1); }
}
if (html.includes('TRAFFIC')) { console.error('тип не переведён'); process.exit(1); }
if (!html.includes('<small>50%</small>')) { console.error('нет доли оплативших'); process.exit(1); }
// Пусто: строка статуса + сводка + пустое состояние, дерева нет
html = clientReferralsSectionHtml({user: {id: 1}, referral_block: {blocked: false},
  summary: {referrals: 0, paid_referrals: 0, bonus_days: 0}, referrals: []});
if (!html.includes('никого не пригласил')) { console.error('нет пустого состояния'); process.exit(1); }
if (html.includes('client-ref-tree-card')) { console.error('дерево в пустом состоянии'); process.exit(1); }
// Блокировка: причина и дата в строке статуса, поле ввода спрятано
html = clientReferralControlHtml({referral_block: {blocked: true, reason: 'Накрутка', updated_at: '07.09.2026 10:00'}});
if (!html.includes('Бонусы заблокированы') || !html.includes('Накрутка · 07.09.2026 10:00')) { console.error('нет причины'); process.exit(1); }
if (html.includes('data-client-referral-block-reason')) { console.error('поле причины при блокировке'); process.exit(1); }
console.log('ok');
"""
        self.assertIn("ok", self._run_node(source))

    def test_broadcast_funnel_helpers(self):
        region = self._region(
            "        function broadcastMessageHtml(text) {",
            "        function renderBroadcastPreview() {",
        )
        source = """
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"]/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
""" + region + """
// Telegram-разметка рендерится, чужие теги остаются текстом
const html = broadcastMessageHtml('<b>Скидка</b> <i>x</i> <script>1</script>');
if (html !== '<b>Скидка</b> <i>x</i> &lt;script&gt;1&lt;/script&gt;') { console.error('markup: ' + html); process.exit(1); }
// Конверсии: целые, а ниже процента — с десятой, ноль — «0%»
const rates = [broadcastRateLabel(64, 4727), broadcastRateLabel(12, 64), broadcastRateLabel(3, 1217), broadcastRateLabel(0, 10), broadcastRateLabel(5, 0)];
if (JSON.stringify(rates) !== JSON.stringify(['1%', '19%', '0,2%', '0%', null])) { console.error('rates: ' + JSON.stringify(rates)); process.exit(1); }
// Длительность по меткам «дд.мм.гггг чч:мм»
const d = [broadcastDurationLabel('07.09.2026 14:31', '07.09.2026 15:07'), broadcastDurationLabel('07.09.2026 13:48', '07.09.2026 17:28'), broadcastDurationLabel('07.09.2026 13:48', 'Нет данных'), broadcastDurationLabel('07.09.2026 13:48', '07.09.2026 13:48')];
if (JSON.stringify(d) !== JSON.stringify(['36 мин', '3 ч 40 мин', '', 'меньше минуты'])) { console.error('duration: ' + JSON.stringify(d)); process.exit(1); }
console.log('ok');
"""
        self.assertIn("ok", self._run_node(source))

    def test_infra_server_snis_panel_renders(self):
        region = self._region(
            "        function infraServerSnisPanelHtml(detail",
            "        function infraWireDomainDeleteButton",
        )
        source = """
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"]/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
""" + region + """
// Список задан: в поле — он, в строке проверки — он же, бан назван поимённо
let html = infraServerSnisPanelHtml(
  {client_snis: ['example.org', 'de.monkora.org'], probe_snis: ['example.org', 'de.monkora.org'], snis_source: 'server'},
  new Set(['de.monkora.org']), '2026-09-09 10:00:00');
if (!html.includes('value="example.org, de.monkora.org"')) { console.error('список не подставлен'); process.exit(1); }
if (!html.includes('name="client_snis"')) { console.error('нет name — автообновление сотрёт ввод'); process.exit(1); }
if (!html.includes('список сервера')) { console.error('нет источника'); process.exit(1); }
if (!html.includes('<code class="is-banned">de.monkora.org</code>')) { console.error('бан не подсвечен'); process.exit(1); }
if (!html.includes('ИМЯ В БАНЕ (2026-09-09): de.monkora.org')) { console.error('нет вердикта'); process.exit(1); }
// Список не задан: поле пустое, проверяем именами доменов — и это сказано
html = infraServerSnisPanelHtml({client_snis: [], probe_snis: ['de.monkora.org'], snis_source: 'domains'}, new Set(), '');
if (!html.includes('value=""')) { console.error('в поле подставлено производное'); process.exit(1); }
if (!html.includes('взяты имена доменов')) { console.error('нет пометки об источнике'); process.exit(1); }
if (html.includes('В БАНЕ')) { console.error('вердикт без диагностики'); process.exit(1); }
// Ни списка, ни доменов
html = infraServerSnisPanelHtml({}, null, '');
if (!html.includes('проверять нечем')) { console.error('нет пустого состояния'); process.exit(1); }
console.log('ok');
"""
        self.assertIn("ok", self._run_node(source))

    def test_client_summary_renders_three_groups(self):
        region = self._region(
            "        function clientSummaryHtml(result) {",
            "        function clientOverviewSectionHtml",
        )
        source = """
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"]/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const money = (v) => `${new Intl.NumberFormat('ru-RU').format(v || 0)} \u20bd`;
""" + region + """
// Триал из карточки, на которую жаловался владелец: имени нет, платежей нет
const trial = clientSummaryHtml({
  user: {username: '', email: '', telegram_id: '6337455795', expire_at: '16.09.2026',
         is_active: true, days_left: 7, id: 42},
  ltv: 0, autopay: {yk: true, wata: false}, first_seen: '09.09.2026', history: [],
});
// Все хуки, через которые в карточку доливаются данные RWMS, должны уцелеть:
// без них загрузка молча не находит куда писать
for (const marker of ['data-client-traffic-value', 'data-client-traffic-meta',
                      'data-client-traffic-stat', 'data-client-traffic-limit-stat',
                      'data-client-traffic-limit-value', 'data-client-traffic-limit-meta',
                      'data-client-first-connected-value', 'data-client-rwms-status',
                      'data-client-hwid', 'data-client-traffic-meter',
                      'data-client-traffic-meter-fill']) {
  if (!trial.includes(marker)) { console.error('потерян хук ' + marker); process.exit(1); }
}
// Три группы по решению админа, а не семь равнозначных плиток
for (const marker of ['client-summary-head', 'client-summary-groups',
                      '>Подписка<', '>Деньги<', 'осталось 7 дней', 'платежей не было']) {
  if (!trial.includes(marker)) { console.error('нет ' + marker); process.exit(1); }
}
const expired = clientSummaryHtml({
  user: {username: 'andrey_p', email: 'a@example.com', telegram_id: '128374551',
         expire_at: '01.09.2026', is_active: false, days_left: -8},
  ltv: 4470, autopay: {yk: false, wata: false}, first_seen: '14.02.2026',
  history: [{success: true}, {success: true}, {success: false}, {success: true}],
});
if (!expired.includes('истекла 8 дней назад')) { console.error('склонение дней'); process.exit(1); }
if (!expired.includes('3 успешных платежа')) { console.error('склонение платежей'); process.exit(1); }
if (!expired.includes('is-expired')) { console.error('состояние подписки'); process.exit(1); }
// Отсутствующий срок не должен давать «осталось undefined»
const noDate = clientSummaryHtml({
  user: {telegram_id: '1', expire_at: '', is_active: false}, ltv: 0,
  autopay: {}, first_seen: 'Нет данных', history: [],
});
if (noDate.includes('undefined') || noDate.includes('NaN')) { console.error('пустой срок'); process.exit(1); }
console.log('ok');
"""
        self.assertIn("ok", self._run_node(source))

    def test_age_label_is_human_readable(self):
        region = self._region(
            "        function infraAgeLabel(minutes) {",
            "        function infraDomainRowHtml",
        )
        source = region + """
const cases = [[null, ''], [3, '3 мин назад'], [90, '2 ч назад'], [10080, '7 дн назад']];
for (const [input, expected] of cases) {
  const got = infraAgeLabel(input);
  if (got !== expected) {
    console.error(`infraAgeLabel(${input}) = ${JSON.stringify(got)}, ждали ${JSON.stringify(expected)}`);
    process.exit(1);
  }
}
console.log('ok');
"""
        self.assertIn("ok", self._run_node(source))


class PromoCohortTimeNormalisationTests(SimpleTestCase):
    """Смешение naive и aware времени в отчёте по когорте промокода.

    09.09.2026 отчёт падал в 500 на каждом промокоде, где хотя бы один
    участник после активации платил и через ЮKassa, и через Wata:
    yk_payments.created_at это TIMESTAMP (psycopg2 отдаёт naive), а
    wata_transactions.payment_time — TIMESTAMP WITH TIME ZONE (aware), и
    min() по ним бросал TypeError. Django отдавал HTML-страницу 500,
    response.json() на клиенте бросал, и админ видел «Проверьте соединение».
    """

    def test_admin_naive_utc_converts_instead_of_stripping(self):
        from engine.views import admin_naive_utc

        self.assertIsNone(admin_naive_utc(None))
        naive = datetime(2026, 9, 9, 12, 0)
        self.assertEqual(admin_naive_utc(naive), naive)
        # Срезать tzinfo нельзя: значение в TimeZone сессии БД уехало бы на
        # смещение этой сессии
        aware = datetime(2026, 9, 9, 15, 0, tzinfo=dt_timezone(timedelta(hours=3)))
        self.assertEqual(admin_naive_utc(aware), datetime(2026, 9, 9, 12, 0))
        self.assertIsNone(admin_naive_utc(aware).tzinfo)

    def test_mixed_naive_and_aware_no_longer_raises(self):
        from engine.views import admin_naive_utc

        yk_first = datetime(2026, 8, 5, 12, 0)
        wt_first = datetime(2026, 8, 20, 9, 0, tzinfo=dt_timezone.utc)
        with self.assertRaises(TypeError):
            min(value for value in (yk_first, wt_first) if value)
        first_paid_at = min(
            (admin_naive_utc(v) for v in (yk_first, wt_first) if v), default=None
        )
        self.assertEqual(first_paid_at, datetime(2026, 8, 5, 12, 0))

    def test_cohort_sql_normalises_and_guards(self):
        source = Path("engine/views.py").read_text()
        start = source.index("def support_admin_api_promo_cohort")
        body = source[start:start + 12000]
        # Время Wata приводится к naive UTC в самом SQL: иначе граница «после
        # активации» зависит от TimeZone сессии БД
        self.assertIn("min(t.payment_time AT TIME ZONE 'UTC') AS first_at", body)
        self.assertIn("t.payment_time AT TIME ZONE 'UTC' > uses.created_at", body)
        # jsonb_array_elements роняет весь SELECT на строке, где buttons не
        # массив, а условие сканирует все рассылки
        self.assertIn("jsonb_typeof(b.buttons) = 'array'", body)
        self.assertIn("btn->>'promo_id' ~ '^[0-9]+$'", body)

    def test_client_reports_the_real_reason(self):
        template = template_source("engine/templates/admin_dashboard.html")
        # Раньше 500 и обрыв сети давали одну и ту же плашку «проверьте
        # соединение», и диагностика уходила не туда
        self.assertIn("`Сервер ответил ${response.status}`", template)
        self.assertIn("ошибка на сервере, смотрите лог сайта", template)


class MalformedEmailGuardTests(SimpleTestCase):
    """Битый email не должен ни ронять запросы к панели, ни попадать в БД.

    Инцидент 2026-09-10. Пользователь впервые вошёл в кабинет и на форме
    оплаты ввёл 'milenapanowa@yandex' — адрес без точки в домене. Браузерная
    проверка type="email" такое пропускает (по спецификации WHATWG домен без
    точки валиден), серверной проверки не было, и сайт привязал адрес к
    аккаунту. Через две минуты пришла оплата, а панель Remnawave валидирует
    email через pydantic EmailStr — UpdateUser падал, RWMS отдавал INTERNAL,
    payment считал это блипом панели и крутил оплаченное продление в ретраях
    больше трёх часов.

    Второе, более тихое последствие уже наступило в проде: регистрация с
    битым адресом «успешна» (magic link уходит), но подписки в панели нет —
    AddUser падает, RwmsClientSync.add_user глотает ошибку, и пользователь
    уходит в create_local_site_user_without_rwms.
    """

    BROKEN = "milenapanowa@yandex"

    def test_validator_rejects_the_address_from_the_incident(self):
        from engine.rwms_helpers import is_valid_email

        self.assertFalse(is_valid_email(self.BROKEN))

    def test_validator_accepts_ordinary_addresses(self):
        from engine.rwms_helpers import is_valid_email

        for email in (
            "milenapanowa@yandex.ru",
            "u@example.com",
            "first.last+tag@sub.example.co.uk",
            "79132077119@mail.ru",
        ):
            with self.subTest(email=email):
                self.assertTrue(is_valid_email(email))

    def test_validator_normalises_before_checking(self):
        from engine.rwms_helpers import is_valid_email

        # Форма может прислать адрес с пробелами и в другом регистре —
        # отвергать его из-за этого нельзя.
        self.assertTrue(is_valid_email("  User@Example.COM "))

    def test_validator_rejects_garbage(self):
        from engine.rwms_helpers import is_valid_email

        for email in (
            None,
            "",
            "no-at-sign",
            "@example.com",
            "user@",
            "user@host",
            "user@host.x",
            "with space@example.com",
        ):
            with self.subTest(email=email):
                self.assertFalse(is_valid_email(email))

    def test_invalid_email_is_left_out_of_the_panel_request(self):
        from engine.rwms_helpers import usable_panel_email

        with self.assertLogs(level="WARNING") as logs:
            result = usable_panel_email(self.BROKEN, "m123", "AddUser")

        self.assertIsNone(result)
        joined = "\n".join(logs.output)
        self.assertIn(self.BROKEN, joined)
        self.assertIn("m123", joined)

    def test_valid_email_still_reaches_the_panel_request(self):
        from engine.rwms_helpers import usable_panel_email

        self.assertEqual(
            usable_panel_email("u@example.com", "m123", "AddUser"), "u@example.com"
        )

    def test_add_user_keeps_the_email_because_it_is_identity_not_metadata(self):
        """AddUser НЕ фильтрует email — здесь он идентичность, а не метаданные.

        Подписка, созданная с пустым email, не может быть принята никогда:
        assert_subscription_owned_by_email сверяет панельный email с
        запрошенным и при пустом поле считает запись чужой, навсегда
        останавливая провижининг пользователя. Поэтому «тихо выбросить»
        битый адрес здесь опаснее, чем уронить AddUser: формат проверяется
        на входе (pay / send_magic_link / update_email / confirm_email).
        """
        from engine import rwms_helpers

        client = mock.Mock()
        client.add_user.return_value = "created"

        with mock.patch.object(
            rwms_helpers, "_internal_squads_uuids", return_value=["squad-1"]
        ):
            rwms_helpers.create_user_until(
                rwms_client=client,
                username="m123",
                expire_at=datetime.now(dt_timezone.utc) + timedelta(days=7),
                email=self.BROKEN,
            )

        request = client.add_user.call_args.args[0]
        self.assertTrue(request.HasField("email"))
        self.assertEqual(request.email, self.BROKEN)
        self.assertEqual(request.username, "m123")

    def test_adoption_guard_still_requires_a_panel_email(self):
        """Инвариант, из-за которого предыдущий тест выглядит именно так."""
        from engine.rwms_helpers import (
            RwmsSubscriptionOwnershipError,
            assert_subscription_owned_by_email,
        )

        class _PanelUserWithoutEmail:
            username = "m123"
            email = ""

            def HasField(self, name):
                return False

        with self.assertRaises(RwmsSubscriptionOwnershipError):
            assert_subscription_owned_by_email(
                _PanelUserWithoutEmail(), "u@example.com", flow="test"
            )

    def test_add_user_request_keeps_a_valid_email(self):
        from engine import rwms_helpers

        client = mock.Mock()
        client.add_user.return_value = "created"

        with mock.patch.object(
            rwms_helpers, "_internal_squads_uuids", return_value=["squad-1"]
        ):
            rwms_helpers.create_user_until(
                rwms_client=client,
                username="m123",
                expire_at=datetime.now(dt_timezone.utc) + timedelta(days=7),
                email="u@example.com",
            )

        request = client.add_user.call_args.args[0]
        self.assertTrue(request.HasField("email"))
        self.assertEqual(request.email, "u@example.com")

    def test_weak_at_sign_check_is_gone_from_panel_updates(self):
        """Раньше два места фильтровали email проверкой «есть @», которая
        пропускает ровно тот адрес, что сломал прод."""
        source = Path("engine/views.py").read_text()
        self.assertNotIn('"@" in rwms_user.email', source)
        # username берётся защитным getattr: у части объектов панели (и у
        # дублёров в тестах) этого атрибута нет, а падать на формировании
        # текста warning'а нельзя — это путь продления подписки.
        self.assertEqual(
            source.count(
                'rwms_user.email, getattr(rwms_user, "username", "?"), "UpdateUser"'
            ),
            2,
        )

    # --- Поведенческие тесты вьюх ---------------------------------------
    #
    # Проверяют реальный вызов вьюхи, а не наличие подстроки в исходнике:
    # грепающий тест остаётся зелёным при любой логической ошибке.

    class _SessionDict(dict):
        modified = False

    def _magic_link_request(self, email):
        request = RequestFactory().post("/magic/", {"email": email})
        request.session = {}
        return request

    def _fake_session_returning(self, user):
        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeBegin:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeSession:
            def __init__(self):
                self.added = []

            def begin(self):
                return FakeBegin()

            def query(self, *args, **kwargs):
                return FakeQuery()

            def add(self, obj):
                # Токен проставляется во flush(), как в настоящей сессии:
                # питоновский column default применяется на INSERT. Дубль,
                # ставивший его в add(), скрывал регресс 2026-09-12
                # (/login/magic/None/ в письме).
                self.added.append(obj)

            def commit(self):
                return None

            def rollback(self):
                return None

            def flush(self):
                for pending in self.added:
                    if isinstance(pending, MagicToken) and pending.token is None:
                        pending.token = "magic-token"

            def close(self):
                return None

        return FakeSession()

    def test_magic_link_rejects_new_registration_with_malformed_email(self):
        """Новая регистрация с опечаткой отклоняется, аккаунт не создаётся."""
        with mock.patch(
            "engine.views.session_factory",
            return_value=self._fake_session_returning(None),
        ), mock.patch("engine.views.create_site_user") as create_site, mock.patch(
            "engine.views.send_magic_link_email"
        ) as send_email:
            response = send_magic_link(self._magic_link_request(self.BROKEN))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["status"], "error")
        create_site.assert_not_called()
        send_email.assert_not_called()

    def test_magic_link_still_works_for_existing_user_with_malformed_email(self):
        """Ключевой инвариант: владельца уже записанного битого адреса нельзя
        запереть снаружи кабинета — другого входа у него фактически нет."""
        broken_user = SimpleNamespace(id=42, email=self.BROKEN, username="m123")

        with mock.patch(
            "engine.views.session_factory",
            return_value=self._fake_session_returning(broken_user),
        ), mock.patch("engine.views.create_site_user") as create_site, mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": None, "ymid": None},
        ), mock.patch("engine.views.sync_existing_user_tracking"), mock.patch(
            "engine.views.send_magic_link_email"
        ) as send_email:
            response = send_magic_link(self._magic_link_request(self.BROKEN))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"status": "ok"})
        create_site.assert_not_called()
        send_email.assert_called_once()

    def _pay_request(self, email):
        request = RequestFactory().post(
            "/pay/",
            {"email": email, "tariff_id": "month"},
            HTTP_HOST="example.com",
            HTTP_X_PAYMENT_LAUNCH="new-tab",
        )
        request.user = SimpleNamespace(is_authenticated=False, id=None)
        request.session = self._SessionDict()
        return request

    def test_pay_rejects_unknown_malformed_email(self):
        tariff = SimpleNamespace(price=100, db_tariff_id="month", description="1 месяц")

        with mock.patch(
            "engine.views.session_factory",
            return_value=self._fake_session_returning(None),
        ), mock.patch(
            "engine.views.get_runtime_actual_tariffs", return_value=[tariff]
        ), mock.patch("engine.views.create_site_user") as create_site, mock.patch(
            "engine.views.create_wata_payment_sync"
        ) as create_invoice:
            response = pay(self._pay_request(self.BROKEN))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["status"], "error")
        create_site.assert_not_called()
        create_invoice.assert_not_called()

    @override_settings(PAYMENT_GATEWAY="wata")
    def test_pay_lets_a_stored_malformed_email_through(self):
        """Дедушкина оговорка. Форма кабинета шлёт привязанный email скрытым
        полем, а ниже стоит проверка «email не совпадает с аккаунтом»: без
        оговорки владелец битого адреса не смог бы заплатить ничем."""
        tariff = SimpleNamespace(price=100, db_tariff_id="month", description="1 месяц")
        known = SimpleNamespace(id=42, email=self.BROKEN, username="m123")

        request = self._pay_request(self.BROKEN)
        request.user = SimpleNamespace(is_authenticated=True, id=42, email=self.BROKEN)
        with mock.patch("engine.views.is_user_blocked", return_value=True), mock.patch(
            "engine.views.session_factory",
            return_value=self._fake_session_returning(known),
        ), mock.patch(
            "engine.views.get_runtime_actual_tariffs", return_value=[tariff]
        ), mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": None, "ymid": None},
        ), mock.patch("engine.views.sync_existing_user_tracking"), mock.patch(
            "engine.views.create_wata_payment_sync"
        ), self.assertLogs(level="WARNING") as logs:
            response = pay(request)

        # Оговорка сработала и сказала об этом громко.
        self.assertTrue(
            any("already stored" in line for line in logs.output),
            "дедушкина оговорка должна громко логироваться",
        )
        # И платёж НЕ отклонён из-за формата адреса: причина отказа, если он
        # вообще есть, уже другая. Это и есть инвариант — владелец
        # записанного битого адреса не заперт без возможности заплатить.
        self.assertFalse(
            any("invalid email format" in line for line in logs.output),
            "платёж отклонён валидатором, хотя адрес уже привязан к аккаунту",
        )
        if response.status_code == 400:
            self.assertNotIn(
                "опечатка", json.loads(response.content).get("message", "")
            )

    def test_update_email_rejects_malformed_address(self):
        request = RequestFactory().post("/update-email/", {"email": self.BROKEN})
        request.user = SimpleNamespace(is_authenticated=True, id=42)
        request.session = self._SessionDict()

        with mock.patch("engine.views.session_factory") as session_factory:
            response = update_email(request)

        self.assertEqual(response.status_code, 302)
        self.assertIn("опечатка", request.session["email_bind_modal"]["error"])
        # До БД дело не дошло: невалидный адрес отвергнут раньше.
        session_factory.assert_not_called()


class SecurityHardeningRegressionTests(SimpleTestCase):
    class SessionDict(dict):
        modified = False

        def cycle_key(self):
            self["cycle_key_called"] = True

    def test_user_login_rotates_session_key(self):
        request = SimpleNamespace(session=self.SessionDict(), META={})

        views.authorize_user_session(request, SimpleNamespace(id=42))

        self.assertTrue(request.session["cycle_key_called"])
        self.assertEqual(request.session[views.SESSION_KEY], "42")

    def test_magic_link_endpoint_rejects_get_without_touching_database(self):
        request = RequestFactory().get("/login/send-link/")
        request.session = self.SessionDict()

        with mock.patch("engine.views.session_factory") as session_factory:
            response = send_magic_link(request)

        self.assertEqual(response.status_code, 405)
        session_factory.assert_not_called()

    def test_magic_email_delivery_failure_is_not_reported_as_success(self):
        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return None

        class FakeBegin:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeSession:
            def begin(self):
                return FakeBegin()

            def query(self, *args, **kwargs):
                return FakeQuery()

            def close(self):
                return None

        request = RequestFactory().post(
            "/login/send-link/",
            {"email": "new@example.com"},
            HTTP_HOST="example.com",
        )
        request.session = self.SessionDict()

        with mock.patch(
            "engine.views.session_factory", return_value=FakeSession()
        ), mock.patch(
            "engine.views.send_magic_link_email",
            side_effect=RuntimeError("mail transport unavailable"),
        ):
            response = send_magic_link(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["status"], "error")

    def test_email_confirmation_is_bound_to_previous_address(self):
        db_user = SimpleNamespace(
            id=42,
            email="already-changed@example.com",
            username="user-42",
        )

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return db_user

        class FakeSession:
            def query(self, *args, **kwargs):
                return FakeQuery()

            def rollback(self):
                return None

            def close(self):
                return None

        request = RequestFactory().get("/confirm-email/token/")
        request.session = self.SessionDict()
        request.user = SimpleNamespace(is_authenticated=False, id=None)
        token = views.build_email_confirmation_token(
            db_user.id,
            "next@example.com",
            "old@example.com",
        )

        with mock.patch(
            "engine.views.session_factory", return_value=FakeSession()
        ), mock.patch("engine.views.rwms_client.update_user") as update_user:
            response = views.confirm_email(request, token)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db_user.email, "already-changed@example.com")
        self.assertIn("устарела", request.session["email_bind_modal"]["error"])
        update_user.assert_not_called()

    @override_settings(TRUSTED_PROXY_NETWORKS=["127.0.0.1/32", "10.0.0.0/8"])
    def test_admin_audit_ip_ignores_client_prepended_forwarded_value(self):
        request = SimpleNamespace(
            META={
                "REMOTE_ADDR": "127.0.0.1",
                "HTTP_X_FORWARDED_FOR": "198.51.100.99, 203.0.113.7, 10.0.0.4",
            }
        )

        self.assertEqual(views.admin_client_ip(request), "203.0.113.7")

    @override_settings(
        SECRET_KEY="test-secret",
        SUPPORT_ADMIN_PASSWORD="shared-admin",
        SUPPORT_STAFF_PASSWORD="shared-support",
    )
    def test_personal_admin_role_is_refreshed_on_every_request(self):
        account = SimpleNamespace(
            login="operator",
            password_hash="encoded-password",
            role="support",
            is_active=True,
        )
        request = SimpleNamespace(
            session=self.SessionDict(
                {
                    views.SUPPORT_ADMIN_SESSION_KEY: True,
                    views.SUPPORT_ADMIN_ROLE_SESSION_KEY: views.SUPPORT_ADMIN_ROLE_ADMIN,
                    views.SUPPORT_ADMIN_ACCOUNT_SESSION_KEY: account.login,
                    views.SUPPORT_ADMIN_AUTH_HASH_SESSION_KEY: views.support_admin_auth_fingerprint(
                        f"account:{account.login}", account.password_hash
                    ),
                }
            )
        )
        db_session = mock.Mock()
        db_session.query.return_value.filter.return_value.first.return_value = account

        with mock.patch.object(views, "session_factory", return_value=db_session):
            self.assertTrue(views.validate_support_admin_session(request))

        self.assertEqual(
            request.session[views.SUPPORT_ADMIN_ROLE_SESSION_KEY],
            views.SUPPORT_ADMIN_ROLE_SUPPORT,
        )

    @override_settings(SECRET_KEY="test-secret")
    def test_disabled_or_password_changed_admin_session_is_rejected(self):
        for active, password_hash in ((False, "old-hash"), (True, "new-hash")):
            with self.subTest(active=active, password_hash=password_hash):
                account = SimpleNamespace(
                    login="operator",
                    password_hash=password_hash,
                    role="full",
                    is_active=active,
                )
                request = SimpleNamespace(
                    session=self.SessionDict(
                        {
                            views.SUPPORT_ADMIN_SESSION_KEY: True,
                            views.SUPPORT_ADMIN_ROLE_SESSION_KEY: views.SUPPORT_ADMIN_ROLE_ADMIN,
                            views.SUPPORT_ADMIN_ACCOUNT_SESSION_KEY: account.login,
                            views.SUPPORT_ADMIN_AUTH_HASH_SESSION_KEY: views.support_admin_auth_fingerprint(
                                f"account:{account.login}", "old-hash"
                            ),
                        }
                    )
                )
                db_session = mock.Mock()
                db_session.query.return_value.filter.return_value.first.return_value = account
                with mock.patch.object(
                    views, "session_factory", return_value=db_session
                ):
                    self.assertFalse(views.validate_support_admin_session(request))

    def _personal_admin_request(self, path, **headers):
        request = RequestFactory().get(path, **headers)
        request.session = self.SessionDict(
            {
                views.SUPPORT_ADMIN_SESSION_KEY: True,
                views.SUPPORT_ADMIN_ROLE_SESSION_KEY: views.SUPPORT_ADMIN_ROLE_ADMIN,
                views.SUPPORT_ADMIN_ACCOUNT_SESSION_KEY: "operator",
                views.SUPPORT_ADMIN_AUTH_HASH_SESSION_KEY: "stored-fingerprint",
            }
        )
        return request

    def test_admin_session_survives_database_outage_with_503(self):
        from sqlalchemy.exc import OperationalError

        cases = (
            ("/support-admin/api/stats/", {}, "application/json"),
            (
                "/support-admin/tickets/7/messages-json/",
                {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"},
                "application/json",
            ),
            ("/support-admin/", {}, "text/plain; charset=utf-8"),
        )
        for path, headers, expected_type in cases:
            with self.subTest(path=path):
                request = self._personal_admin_request(path, **headers)
                snapshot = dict(request.session)
                db_session = mock.Mock()
                db_session.query.side_effect = OperationalError(
                    "SELECT", {}, Exception("server closed the connection")
                )
                with mock.patch.object(
                    views, "session_factory", return_value=db_session
                ), self.assertLogs(level="ERROR"):
                    response = views.require_support_admin(request)

                self.assertEqual(response.status_code, 503)
                self.assertEqual(response["Content-Type"], expected_type)
                self.assertEqual(response["Retry-After"], "30")
                self.assertEqual(dict(request.session), snapshot)
                db_session.close.assert_called_once_with()

    @override_settings(SECRET_KEY="test-secret")
    def test_confirmed_account_deactivation_still_clears_admin_session(self):
        account = SimpleNamespace(
            login="operator", password_hash="hash", role="full", is_active=False
        )
        request = self._personal_admin_request("/support-admin/api/stats/")
        db_session = mock.Mock()
        db_session.query.return_value.filter.return_value.first.return_value = account

        with mock.patch.object(views, "session_factory", return_value=db_session):
            response = views.require_support_admin(request)

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(views.SUPPORT_ADMIN_SESSION_KEY, request.session)
        self.assertNotIn(views.SUPPORT_ADMIN_ACCOUNT_SESSION_KEY, request.session)


class SupportAttachmentSecurityTests(SimpleTestCase):
    def test_attachment_limits_are_checked_before_storage(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        png = lambda name, size: SimpleUploadedFile(
            name,
            b"\x89PNG\r\n\x1a\n" + b"x" * max(0, size - 8),
            content_type="image/png",
        )
        with override_settings(
            SUPPORT_ATTACHMENT_MAX_FILES=2,
            SUPPORT_ATTACHMENT_MAX_BYTES=20,
            SUPPORT_ATTACHMENT_TOTAL_MAX_BYTES=30,
        ):
            self.assertIn("не больше 2", views.validate_support_attachments([
                png("1.png", 8), png("2.png", 8), png("3.png", 8)
            ]))
            self.assertIn("превышает 0 МБ", views.validate_support_attachments([
                png("large.png", 21)
            ]))
            self.assertIn("Общий размер", views.validate_support_attachments([
                png("1.png", 16), png("2.png", 16)
            ]))

    @override_settings(SUPPORT_ATTACHMENT_X_ACCEL_REDIRECT=True)
    def test_safe_attachment_uses_internal_nginx_redirect(self):
        attachment = SimpleNamespace(
            content_type="video/mp4",
            file_name="clip.mp4",
            storage_path="support_attachments/7/clip.mp4",
        )
        response = views.support_attachment_file_response(
            attachment, Path("/path/need/not/exist")
        )
        self.assertEqual(
            response["X-Accel-Redirect"],
            "/_protected_support_media/support_attachments/7/clip.mp4",
        )
        self.assertEqual(response["Content-Type"], "video/mp4")

    @override_settings(SUPPORT_ATTACHMENT_X_ACCEL_REDIRECT=True)
    def test_internal_redirect_percent_encodes_non_ascii_file_names(self):
        from urllib.parse import unquote

        cases = (
            (
                "support_attachments/7/ab_Снимок_экрана_2026-09-11_в_10.00.00.png",
                "/_protected_support_media/support_attachments/7/ab_%D0%A1%D0%BD",
            ),
            (
                "/support_attachments/7/ab_café.png",
                "/_protected_support_media/support_attachments/7/ab_caf%C3%A9.png",
            ),
        )
        for storage_path, expected_prefix in cases:
            with self.subTest(storage_path=storage_path):
                attachment = SimpleNamespace(
                    content_type="image/png",
                    file_name=storage_path.rsplit("/", 1)[-1],
                    storage_path=storage_path,
                )
                response = views.support_attachment_file_response(
                    attachment, Path("/path/need/not/exist")
                )
                header = response["X-Accel-Redirect"]
                self.assertTrue(header.startswith(expected_prefix), header)
                self.assertTrue(header.isascii())
                self.assertNotIn("=?utf-8?", header)
                self.assertEqual(
                    unquote(header),
                    "/_protected_support_media/" + storage_path.lstrip("/"),
                )

    def test_svg_and_spoofed_png_are_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        message = SimpleNamespace(id=7)
        db_session = mock.Mock()
        with tempfile.TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root,
            SUPPORT_ATTACHMENT_MAX_BYTES=1024 * 1024,
        ):
            result = views.attach_support_attachments(
                db_session,
                message,
                [
                    SimpleUploadedFile(
                        "payload.svg",
                        b'<svg onload="alert(1)"></svg>',
                        content_type="image/svg+xml",
                    ),
                    SimpleUploadedFile(
                        "payload.png",
                        b"<html><script>alert(1)</script>",
                        content_type="image/png",
                    ),
                ],
            )

        self.assertEqual(result, [])
        db_session.add.assert_not_called()

    def test_valid_png_signature_is_saved(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        import io
        from PIL import Image
        image = io.BytesIO()
        Image.new("RGB", (2, 2)).save(image, format="PNG")
        message = SimpleNamespace(id=8)
        db_session = mock.Mock()
        with tempfile.TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root,
            SUPPORT_ATTACHMENT_MAX_BYTES=1024 * 1024,
        ):
            result = views.attach_support_attachments(
                db_session,
                message,
                [
                    SimpleUploadedFile(
                        "screen.png",
                        image.getvalue(),
                        content_type="image/png",
                    )
                ],
            )
            stored_files = list(Path(media_root).rglob("*.png"))

        self.assertEqual(len(result), 1)
        self.assertEqual(len(stored_files), 1)

    def test_legacy_svg_is_forced_to_download_with_nosniff(self):
        with tempfile.TemporaryDirectory() as media_root:
            path = Path(media_root) / "legacy.svg"
            path.write_text('<svg onload="alert(1)"></svg>')
            attachment = SimpleNamespace(
                content_type="image/svg+xml", file_name="legacy.svg"
            )

            response = views.support_attachment_file_response(attachment, path)

            self.assertEqual(response["Content-Type"], "application/octet-stream")
            self.assertIn("attachment", response["Content-Disposition"])
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            response.close()


class FrontendSecurityAndOfflineTests(SimpleTestCase):
    def test_acquisition_table_escapes_data_cells_but_allows_explicit_markup(self):
        template = template_source("engine/templates/admin_dashboard.html")
        block = template[
            template.index("const acqHtml =") : template.index("async function acqFetch")
        ]
        script = """
const escapeHtml = (value) => String(value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/\"/g, '&quot;').replace(/'/g, '&#039;');
""" + block + """
const rendered = acqTable(['<header>'], [['<img src=x onerror=alert(1)>', acqHtml('<b>ok</b>')]]);
if (rendered.includes('<img')) process.exit(1);
if (!rendered.includes('&lt;img')) process.exit(2);
if (!rendered.includes('<b>ok</b>')) process.exit(3);
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script)
            script_path = handle.name
        try:
            completed = subprocess.run(
                ["node", script_path], capture_output=True, text=True, timeout=10
            )
        finally:
            Path(script_path).unlink(missing_ok=True)

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_service_worker_never_caches_authenticated_login_response(self):
        worker = template_source("engine/templates/pwa/sw.js")

        self.assertNotIn("'/login/'", worker)
        self.assertIn("/static/pwa/offline.html", worker)
        self.assertIn("cacheName.startsWith(CACHE_PREFIX)", worker)
        self.assertTrue(Path("engine/static/pwa/offline.html").is_file())

    def test_short_viewport_onboarding_allows_art_to_shrink(self):
        template = template_source("engine/templates/login.html")

        self.assertIn("@media (max-height: 700px)", template)
        self.assertIn("max-height: min(42vh, 100%);", template)
        self.assertIn("pointer-events: none;", template)

    def test_heavy_editor_is_loaded_only_when_requested(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertNotIn(
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror',
            template,
        )
        self.assertIn("function ensureCodeMirrorAssets()", template)
        self.assertIn("if (tabId === 'stats'", template)
        self.assertNotIn("if (statsForm) loadStats();", template)

    def test_payment_sheets_use_accessible_dialog_lifecycle(self):
        template = template_source("engine/templates/dashboard.html")

        for title_id in (
            "payments-history-title",
            "autopay-title",
            "no-autopay-title",
        ):
            self.assertIn(f'aria-labelledby="{title_id}"', template)
        self.assertIn("function openLegacyDialog(sheet, content)", template)
        open_dialog_source = template[
            template.index("function openLegacyDialog(sheet, content)"):
            template.index("function closeLegacyDialog(sheet)")
        ]
        self.assertIn('const background = document.getElementById("app-container");', open_dialog_source)
        self.assertIn("if (background) background.setAttribute('inert', '');", open_dialog_source)
        self.assertIn("if (event.key === 'Escape')", template)


class SQLAlchemyBackendLifecycleTests(SimpleTestCase):
    def test_get_user_closes_session_when_query_fails(self):
        from engine.auth_backend import SQLAlchemyBackend

        db_session = mock.Mock()
        db_session.query.side_effect = RuntimeError("database unavailable")
        with mock.patch(
            "engine.auth_backend.session_factory", return_value=db_session
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                SQLAlchemyBackend().get_user(12)

        db_session.close.assert_called_once_with()

    def test_authenticate_closes_session_when_query_fails(self):
        from engine.auth_backend import SQLAlchemyBackend

        db_session = mock.Mock()
        db_session.query.side_effect = RuntimeError("database unavailable")
        with mock.patch(
            "engine.auth_backend.session_factory", return_value=db_session
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                SQLAlchemyBackend().authenticate(object(), user_id=12)

        db_session.close.assert_called_once_with()


class TelegramBindLinkTests(SimpleTestCase):
    class SessionDict(dict):
        modified = False

    class Query:
        def __init__(self, result=None):
            self.result = result

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return self.result

    class DbSession:
        def __init__(self, result=None):
            self.result = result
            self.added = []

        def query(self, *args, **kwargs):
            return TelegramBindLinkTests.Query(self.result)

        def add(self, value):
            self.added.append(value)

    def test_new_link_uses_random_152_bit_token_and_stores_only_hash(self):
        from common.models.db import TelegramLoginToken
        from engine.views import get_or_create_telegram_bind_link
        from engine.views import hash_telegram_bind_token
        from engine.views import hash_telegram_login_token

        request = SimpleNamespace(session=self.SessionDict())
        db_session = self.DbSession()
        link = get_or_create_telegram_bind_link(
            request,
            db_session,
            SimpleNamespace(id=71),
            "test_bot",
        )
        raw_token = link.rsplit("_", 1)[-1]

        self.assertRegex(raw_token, r"^[0-9a-f]{38}$")
        self.assertEqual(len(db_session.added), 1)
        self.assertIsInstance(db_session.added[0], TelegramLoginToken)
        self.assertEqual(
            db_session.added[0].token_hash,
            hash_telegram_bind_token(raw_token),
        )
        # Пространство хэшей входа (/login/telegram/<token>/) не пересекается
        # с bind-токенами: непогашенный bind-токен не станет ссылкой входа.
        self.assertNotEqual(
            db_session.added[0].token_hash,
            hash_telegram_login_token(raw_token),
        )
        self.assertNotEqual(db_session.added[0].token_hash, raw_token)
        self.assertNotIn(raw_token, repr(db_session.added[0].__dict__))

    def test_fresh_unused_session_link_is_reused_without_new_row(self):
        from engine.views import TELEGRAM_BIND_SESSION_CREATED_KEY
        from engine.views import TELEGRAM_BIND_SESSION_TOKEN_KEY
        from engine.views import get_or_create_telegram_bind_link

        raw_token = "a" * 38
        request = SimpleNamespace(
            session=self.SessionDict(
                {
                    TELEGRAM_BIND_SESSION_TOKEN_KEY: raw_token,
                    TELEGRAM_BIND_SESSION_CREATED_KEY: time.time(),
                }
            )
        )
        db_session = self.DbSession(result=SimpleNamespace(id=1))

        link = get_or_create_telegram_bind_link(
            request,
            db_session,
            SimpleNamespace(id=72),
            "test_bot",
        )

        self.assertEqual(link, f"https://t.me/test_bot?start=bind_72_{raw_token}")
        self.assertEqual(db_session.added, [])

    def test_bind_link_write_failure_is_not_a_dashboard_error(self):
        from sqlalchemy.exc import OperationalError

        db_session = mock.Mock()
        db_session.commit.side_effect = OperationalError(
            "INSERT", {}, Exception("cannot execute INSERT in a read-only transaction")
        )
        request = SimpleNamespace(session=self.SessionDict())

        with mock.patch.object(
            views, "session_factory", return_value=db_session
        ), self.assertLogs(level="ERROR") as logs:
            link = views.issue_dashboard_telegram_bind_link(
                request, SimpleNamespace(id=71), "test_bot"
            )

        self.assertIsNone(link)
        db_session.rollback.assert_called_once_with()
        db_session.close.assert_called_once_with()
        self.assertNotIn(views.TELEGRAM_BIND_SESSION_TOKEN_KEY, request.session)
        self.assertNotIn(views.TELEGRAM_BIND_SESSION_CREATED_KEY, request.session)
        self.assertIn("telegram bind link was not issued", "\n".join(logs.output))


class RequestTimingMiddlewareTests(SimpleTestCase):
    @override_settings(REQUEST_TIMING_LOG_ALL=True)
    def test_logs_route_pattern_without_secret_path_and_sets_request_id(self):
        from django.http import HttpResponse
        from engine.request_timing_middleware import RequestTimingMiddleware

        request = RequestFactory().get("/login/magic/super-secret-token/")
        request.resolver_match = SimpleNamespace(route="login/magic/<uuid:token>/")
        middleware = RequestTimingMiddleware(lambda _request: HttpResponse("ok"))

        with self.assertLogs("engine.request_timing", level="INFO") as logs:
            response = middleware(request)

        self.assertRegex(response["X-Request-ID"], r"^[0-9a-f]{32}$")
        self.assertIn("login/magic/<uuid:token>/", logs.output[0])
        self.assertNotIn("super-secret-token", logs.output[0])

    def test_server_timing_is_not_exposed_by_default(self):
        """OPS-4: время SQL в ответе magic-link выдавало, есть ли email в базе."""
        from django.http import HttpResponse
        from engine.request_timing_middleware import RequestTimingMiddleware

        self.assertFalse(settings.REQUEST_TIMING_EXPOSE_SERVER_TIMING)
        middleware = RequestTimingMiddleware(lambda _request: HttpResponse("ok"))

        response = middleware(RequestFactory().post("/login/send-link/"))

        self.assertNotIn("Server-Timing", response)
        self.assertRegex(response["X-Request-ID"], r"^[0-9a-f]{32}$")

    @override_settings(REQUEST_TIMING_EXPOSE_SERVER_TIMING=True)
    def test_server_timing_can_be_enabled_explicitly(self):
        from django.http import HttpResponse
        from engine.request_timing_middleware import RequestTimingMiddleware

        middleware = RequestTimingMiddleware(lambda _request: HttpResponse("ok"))

        response = middleware(RequestFactory().get("/"))

        self.assertRegex(response["Server-Timing"], r"^sql;dur=\d+\.\d$")
        self.assertRegex(response["X-Request-ID"], r"^[0-9a-f]{32}$")


class SupportTicketPaginationTests(SimpleTestCase):
    def payload(self):
        return {
            "status_filter": "open",
            "open_count": 3,
            "closed_count": 2,
            "tickets": [],
            "ticket_payloads": [
                {
                    "id": 15,
                    "updated_at_iso": "2026-09-10T12:00:00",
                    "subject": "Test",
                }
            ],
            "has_more": True,
            "next_updated_at": "2026-09-10T11:00:00",
            "next_id": 10,
        }

    def test_json_endpoint_forwards_bounded_keyset_cursor(self):
        from engine import views

        request = RequestFactory().get(
            "/support-admin/tickets.json",
            {
                "status": "closed",
                "limit": "75",
                "before_updated_at": "2026-09-10T11:00:00",
                "before_id": "10",
            },
        )
        with mock.patch.object(views, "require_support_admin", return_value=None), mock.patch.object(
            views, "load_support_admin_tickets", return_value=self.payload()
        ) as loader:
            response = views.support_admin_tickets_json(request)

        self.assertEqual(response.status_code, 200)
        loader.assert_called_once_with(
            "closed",
            limit=75,
            before_updated_at="2026-09-10T11:00:00",
            before_id="10",
        )
        body = json.loads(response.content)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_cursor"]["id"], 10)

    def test_matching_etag_returns_empty_304(self):
        from engine import views

        with mock.patch.object(views, "require_support_admin", return_value=None), mock.patch.object(
            views, "load_support_admin_tickets", return_value=self.payload()
        ):
            first = views.support_admin_tickets_json(
                RequestFactory().get("/support-admin/tickets.json")
            )
            second = views.support_admin_tickets_json(
                RequestFactory().get(
                    "/support-admin/tickets.json",
                    HTTP_IF_NONE_MATCH=first["ETag"],
                )
            )

        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.content, b"")
        self.assertEqual(second["ETag"], first["ETag"])

    def test_invalid_cursor_is_rejected(self):
        from engine import views

        request = RequestFactory().get(
            "/support-admin/tickets.json",
            {"before_updated_at": "not-a-date", "before_id": "12"},
        )
        with mock.patch.object(views, "require_support_admin", return_value=None):
            response = views.support_admin_tickets_json(request)

        self.assertEqual(response.status_code, 400)

    def test_admin_template_has_bounded_load_more_and_poll_timeout(self):
        template = template_source("engine/templates/admin_dashboard.html")

        self.assertIn('id="tickets-load-more"', template)
        self.assertIn("before_updated_at", template)
        self.assertTrue("ticketsCursorHistory" in template)
        self.assertFalse("TICKETS_MAX_RENDERED" in template)
        self.assertIn("window.setTimeout(() => controller.abort(), 10000)", template)


from common.models.db import PromoCode as _PromoCode  # noqa: E402
from common.models.db import PromoCodeUse as _PromoCodeUse  # noqa: E402
from common.models.db import User as _User  # noqa: E402
from common.models.db import UserDiscount as _UserDiscount  # noqa: E402
from common.rwms_client import RwmsUnavailableError as _RwmsUnavailableError  # noqa: E402


class CabinetPromoActivateTests(SimpleTestCase):
    """Активация промокода из кабинета: правила бота (окно дат, лимит,
    однократность, «только первая покупка»), дни — в БД и RWMS одной
    транзакцией, скидка — в user_discounts."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def _promo(self, **overrides):
        base = dict(
            id=7, code="SUMMER", is_active=True, valid_from=None, valid_until=None,
            max_uses=0, used_count=0, first_purchase_only=False,
            promo_type="days", value=5,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def _session(self, promo, db_user, used=None, discount=None):
        recorded = {"added": [], "committed": False, "rolled_back": False}

        class FakeQuery:
            def __init__(self, model):
                self.model = model

            def filter(self, *args, **kwargs):
                return self

            def with_for_update(self):
                return self

            def first(self):
                if self.model is _PromoCode:
                    return promo
                if self.model is _User:
                    return db_user
                if self.model is _PromoCodeUse:
                    return used
                if self.model is _UserDiscount:
                    return discount
                return None

        class FakeSession:
            def query(self, model):
                return FakeQuery(model)

            def add(self, obj):
                recorded["added"].append(obj)

            def commit(self):
                recorded["committed"] = True

            def rollback(self):
                recorded["rolled_back"] = True

            def close(self):
                recorded["closed"] = True

        return FakeSession(), recorded

    def _post(self, code, user_id=42):
        request = RequestFactory().post("/api/cabinet/promo/activate/", {"code": code})
        request.user = SimpleNamespace(is_authenticated=True, id=user_id, username="u42")
        return request

    def _activate(self, request, session, subscription="sub", update_result="ok", has_payment=False):
        from engine import views
        import proto.rwmanager_pb2 as proto

        sub = None
        if subscription == "sub":
            sub = proto.UserResponse(uuid="rw-1", username="u42")
        if subscription == "unavailable":
            strict = mock.Mock(side_effect=_RwmsUnavailableError("u42", "UNAVAILABLE"))
        else:
            strict = mock.Mock(return_value=sub)
        update = mock.Mock(return_value=None if update_result is None else proto.UserResponse(uuid="rw-1"))
        with mock.patch("engine.views.session_factory", return_value=session), \
                mock.patch.object(views.rwms_client, "get_user_by_username_strict", strict), \
                mock.patch.object(views.rwms_client, "update_user", update), \
                mock.patch("engine.views._cabinet_has_any_payment", return_value=has_payment):
            response = views.cabinet_promo_activate(request)
        return response, update

    def test_days_promo_extends_db_and_panel_in_one_transaction(self):
        promo = self._promo()
        expire = datetime.utcnow() + timedelta(days=3)
        db_user = SimpleNamespace(id=42, expire_at=expire)
        session, recorded = self._session(promo, db_user)

        response, update = self._activate(self._post(" summer "), session)

        self.assertEqual(response.status_code, 200, response.content)
        payload = json.loads(response.content)
        self.assertEqual((payload["status"], payload["promo_type"], payload["value"]), ("ok", "days", 5))
        # Дни стакуются к действующему сроку, а не к «сейчас».
        self.assertEqual(db_user.expire_at, expire + timedelta(days=5))
        self.assertEqual(promo.used_count, 1)
        self.assertTrue(recorded["committed"])
        self.assertTrue(any(isinstance(obj, _PromoCodeUse) for obj in recorded["added"]))
        (update_request,), _ = update.call_args
        self.assertEqual(update_request.uuid, "rw-1")
        self.assertEqual(update_request.expire_at.ToDatetime(), db_user.expire_at)

    def test_days_promo_on_expired_user_counts_from_now(self):
        promo = self._promo(value=2)
        db_user = SimpleNamespace(id=42, expire_at=datetime.utcnow() - timedelta(days=30))
        session, _ = self._session(promo, db_user)

        response, _ = self._activate(self._post("SUMMER"), session)

        self.assertEqual(response.status_code, 200)
        self.assertGreater(db_user.expire_at, datetime.utcnow() + timedelta(days=1, hours=23))

    def test_days_promo_rolls_back_when_panel_update_fails(self):
        promo = self._promo()
        db_user = SimpleNamespace(id=42, expire_at=None)
        session, recorded = self._session(promo, db_user)

        response, _ = self._activate(self._post("SUMMER"), session, update_result=None)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["reason"], "rwms_unavailable")
        self.assertTrue(recorded["rolled_back"])
        self.assertFalse(recorded["committed"])

    def test_days_promo_without_panel_subscription_is_rejected(self):
        promo = self._promo()
        session, recorded = self._session(promo, SimpleNamespace(id=42, expire_at=None))

        response, _ = self._activate(self._post("SUMMER"), session, subscription=None)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)["reason"], "subscription_missing")
        self.assertTrue(recorded["rolled_back"])

    def test_days_promo_when_rwms_unavailable_does_not_burn_activation(self):
        promo = self._promo()
        session, recorded = self._session(promo, SimpleNamespace(id=42, expire_at=None))

        response, _ = self._activate(self._post("SUMMER"), session, subscription="unavailable")

        self.assertEqual(response.status_code, 503)
        self.assertTrue(recorded["rolled_back"])

    def test_discount_promo_creates_user_discount_with_default_ttl(self):
        promo = self._promo(promo_type="discount", value=15)
        session, recorded = self._session(promo, SimpleNamespace(id=42, expire_at=None))

        response, update = self._activate(self._post("SUMMER"), session)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual((payload["promo_type"], payload["value"]), ("discount", 15))
        discounts = [obj for obj in recorded["added"] if isinstance(obj, _UserDiscount)]
        self.assertEqual(len(discounts), 1)
        self.assertEqual(discounts[0].percent, 15)
        self.assertEqual(discounts[0].source_promo_id, 7)
        self.assertGreater(discounts[0].valid_until, datetime.utcnow() + timedelta(hours=71))
        update.assert_not_called()
        self.assertTrue(recorded["committed"])

    def test_discount_promo_reactivation_reopens_window(self):
        promo = self._promo(promo_type="discount", value=20, valid_until=datetime.utcnow() + timedelta(days=2))
        existing = SimpleNamespace(percent=5, valid_until=None, source_promo_id=1, created_at=datetime(2020, 1, 1))
        session, recorded = self._session(promo, SimpleNamespace(id=42, expire_at=None), discount=existing)

        response, _ = self._activate(self._post("SUMMER"), session)

        self.assertEqual(response.status_code, 200)
        self.assertEqual((existing.percent, existing.source_promo_id, existing.valid_until), (20, 7, promo.valid_until))
        self.assertGreater(existing.created_at, datetime(2020, 1, 2))
        self.assertFalse(any(isinstance(obj, _UserDiscount) for obj in recorded["added"]))

    def test_validation_reasons(self):
        cases = (
            (dict(is_active=False), "not_found"),
            (dict(valid_from=datetime.utcnow() + timedelta(days=1)), "expired"),
            (dict(valid_until=datetime.utcnow() - timedelta(days=1)), "expired"),
            (dict(max_uses=3, used_count=3), "exhausted"),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                session, recorded = self._session(self._promo(**overrides), SimpleNamespace(id=42, expire_at=None))
                response, _ = self._activate(self._post("SUMMER"), session)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.content)["reason"], reason)
                self.assertFalse(recorded["committed"])

    def test_unknown_code_and_empty_code(self):
        session, _ = self._session(None, SimpleNamespace(id=42, expire_at=None))
        response, _ = self._activate(self._post("NOPE"), session)
        self.assertEqual(json.loads(response.content)["reason"], "not_found")
        response, _ = self._activate(self._post("   "), session)
        self.assertEqual(json.loads(response.content)["reason"], "not_found")

    def test_already_used_and_first_purchase_only(self):
        session, _ = self._session(self._promo(), SimpleNamespace(id=42, expire_at=None), used=SimpleNamespace(id=1))
        response, _ = self._activate(self._post("SUMMER"), session)
        self.assertEqual(json.loads(response.content)["reason"], "already_used")

        session, _ = self._session(self._promo(first_purchase_only=True), SimpleNamespace(id=42, expire_at=None))
        response, _ = self._activate(self._post("SUMMER"), session, has_payment=True)
        self.assertEqual(json.loads(response.content)["reason"], "not_first")

    def test_rate_limit_and_method_guard(self):
        from engine import views

        session, _ = self._session(None, SimpleNamespace(id=42, expire_at=None))
        for _ in range(views.CABINET_PROMO_RATE_LIMIT):
            self._activate(self._post("NOPE", user_id=77), session)
        response, _ = self._activate(self._post("NOPE", user_id=77), session)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(json.loads(response.content)["reason"], "rate_limited")

        request = RequestFactory().get("/api/cabinet/promo/activate/")
        request.user = SimpleNamespace(is_authenticated=True, id=42)
        self.assertEqual(views.cabinet_promo_activate(request).status_code, 403)


class CabinetSubscriptionReissueTests(SimpleTestCase):
    """Перевыпуск подписки владельцем: RWMS Revoke_UserSubscription по uuid
    собственной подписки, лимит попыток, 503 при недоступности."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def _post(self, user_id=42):
        request = RequestFactory().post("/api/cabinet/subscription/reissue/")
        request.user = SimpleNamespace(is_authenticated=True, id=user_id, username="u42")
        return request

    def test_reissue_returns_new_subscription_url(self):
        from engine import views
        import proto.rwmanager_pb2 as proto

        current = proto.UserResponse(uuid="rw-1", short_uuid="old", username="u42")
        updated = proto.UserResponse(uuid="rw-1", short_uuid="new", subscription_url="https://s/new")
        with mock.patch.object(views.rwms_client, "get_user_by_username_strict", return_value=current), \
                mock.patch.object(views.rwms_client, "revoke_user_subscription", return_value=updated) as revoke:
            response = views.cabinet_subscription_reissue(self._post())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["subscription_url"], "https://s/new")
        revoke.assert_called_once_with("rw-1")

    def test_reissue_unavailable_and_not_found(self):
        from engine import views
        import proto.rwmanager_pb2 as proto

        with mock.patch.object(views.rwms_client, "get_user_by_username_strict",
                               side_effect=_RwmsUnavailableError("u42", "UNAVAILABLE")):
            self.assertEqual(views.cabinet_subscription_reissue(self._post()).status_code, 503)
        with mock.patch.object(views.rwms_client, "get_user_by_username_strict", return_value=None):
            self.assertEqual(views.cabinet_subscription_reissue(self._post()).status_code, 404)
        current = proto.UserResponse(uuid="rw-1", username="u42")
        with mock.patch.object(views.rwms_client, "get_user_by_username_strict", return_value=current), \
                mock.patch.object(views.rwms_client, "revoke_user_subscription",
                                  side_effect=_RwmsUnavailableError("rw-1", "UNIMPLEMENTED")):
            # Старый RWMS без RPC — «временно недоступно», а не 500.
            self.assertEqual(views.cabinet_subscription_reissue(self._post()).status_code, 503)

    def test_reissue_rate_limit_and_method_guard(self):
        from engine import views
        import proto.rwmanager_pb2 as proto

        current = proto.UserResponse(uuid="rw-1", username="u42")
        with mock.patch.object(views.rwms_client, "get_user_by_username_strict", return_value=current), \
                mock.patch.object(views.rwms_client, "revoke_user_subscription", return_value=current):
            for _ in range(views.CABINET_REISSUE_RATE_LIMIT):
                self.assertEqual(views.cabinet_subscription_reissue(self._post(user_id=9)).status_code, 200)
            response = views.cabinet_subscription_reissue(self._post(user_id=9))
        self.assertEqual(response.status_code, 429)

        request = RequestFactory().get("/api/cabinet/subscription/reissue/")
        request.user = SimpleNamespace(is_authenticated=True, id=42)
        self.assertEqual(views.cabinet_subscription_reissue(request).status_code, 403)


class MobileCabinetTemplateTests(SimpleTestCase):
    """Мобильный кабинет по образцу приложения: экраны, эндпоинты, устройства
    с User-Agent, промокоды, перевыпуск ключа; без докупки трафика."""

    def setUp(self):
        self.template = template_source("engine/templates/dashboard.html")
        self.include = Path("engine/templates/includes/cabinet_mobile.html").read_text()
        self.script = Path("engine/static/js/cabinet-mobile.js").read_text()
        self.css = Path("engine/static/css/cabinet-mobile.css").read_text()

    def test_screens_and_wiring(self):
        for page in ("home", "account", "login", "payment", "referral", "promo", "buy", "install", "devices"):
            self.assertIn(f'data-cm-page="{page}"', self.include, page)
        for needle in (
            'id="cm-root"',
            "{% url 'cabinet_promo_activate' %}",
            "'/api/cabinet/subscription/reissue/'",
            "'/api/cabinet/devices/'",
            "'/api/cabinet/devices/delete/'",
            'id="cm-reissue-sheet"',
            "Старый ключ перестанет работать на всех устройствах.",
            'data-cm-sheet="cm-qr-sheet"',
            "data-payment-form data-context=\"dashboard_renew\"",
            'name="tariff_id" value="{{ card.id }}" data-price="{{ card.price }}"',
            "Выгода {{ card.savings }}%",
            "{{ payment_method_label }}",
            "Вы сэкономили {{ cabinet_discount.percent }}%",
            'data-cm-go="promo">У меня есть промокод',
            "{{ telegram_channel_url }}",
            "{{ tg_bot_url }}",
            'href="{% url \'logout\' %}"',
            'data-mi3-copy="{{ referral_link }}"',
            'data-mi3-copy="{{ site_referral_link }}"',
            "+{{ join_referrer_bonus_days }} дн.",
            "{{ referral_bonus_days }} дней пробного периода",
            'id="cm-autopay-toggle"',
            "onclick=\"openAutopaySheet()\"",
            "{{ recurrent_info.tariff }} · {{ recurrent_info.amount }} {{ recurrent_info.currency }}",
            "onclick=\"openEmailBindSheet()\"",
            "{{ tg_bind_link }}",
        ):
            self.assertIn(needle, self.template, needle)
        self.assertIn("{% static 'js/cabinet-mobile.js' %}", self.template)
        self.assertIn("{% static 'css/cabinet-mobile.css' %}?v=6", self.template)
        # Старая мобильная навигация и шторки убраны вместе со скриптом.
        for gone in ('<nav class="nav-mobile"', 'id="mi3-connect-sheet"', 'id="mi3-devices-sheet"', "cabinet-sheets.js", "mi3OpenDevices", "mi3OpenConnect"):
            self.assertNotIn(gone, self.template, gone)
        self.assertFalse(Path("engine/static/js/cabinet-sheets.js").exists())

    def test_devices_show_user_agent_preview(self):
        # Вопрос «что за устройство добавилось» закрывает строка User-Agent из панели.
        self.assertIn("'<div class=\"cm-device-field\"><span>User-Agent</span><code class=\"cm-ua\">' + esc(d.user_agent || 'не передан приложением')", self.script)
        self.assertIn("function deviceOs(d)", self.script)
        self.assertIn("data-cm-hwid=", self.script)
        self.assertIn("Точно удалить?", self.script)

    def test_promo_and_reissue_flows(self):
        self.assertIn("payload.promo_type === 'days'", self.script)
        self.assertIn("window.location.reload()", self.script)
        self.assertIn("state.discount = payload.value;", self.script)
        self.assertIn("'X-CSRFToken': csrfToken()", self.script)
        self.assertIn("window.showTariffs = function ()", self.script)
        # Копирование: «Скопировано!» зелёным в самом элементе, без тостов.
        self.assertIn("function flashCopied(el, ok)", self.script)
        self.assertIn("label.innerHTML = ok ? 'Скопировано!' : 'Не удалось скопировать';", self.script)
        self.assertNotIn("toast(ok ? 'Ссылка скопирована'", self.script)
        self.assertIn('<span data-cm-copy-label>Скопировать ссылку</span>', self.include)
        self.assertIn(".cm .is-copied", self.css)
        # Для Apple при рекомендованном INCY альтернатива — Happ, а не второй INCY.
        self.assertIn("alternative: { name: 'Happ'", self.script)
        self.assertIn('data-cm-happ-url="{{ happ_subscription_url }}"', self.include)
        # Отключение автопродления только через подтверждение, как в боте.
        self.assertIn("autopayToggle.checked = true;", self.script)
        self.assertIn("window.openAutopaySheet()", self.script)

    def test_mobile_breakpoint_hides_paywall_and_legacy_nav(self):
        self.assertIn("body.dashboard-v2 .nav-mobile { display: none !important; }", self.css)
        self.assertIn("body.dashboard-v2 .plans-only-view { display: none !important; }", self.css)
        self.assertIn("body.dashboard-v2 #interface-wrapper.hidden { display: block !important; }", self.css)
        self.assertIn("body.tg-webapp #interface-wrapper.hidden { display: block !important; }", self.css)
        self.assertIn("--cm-accent: #ffc700", self.css)
        # Отступы списка устройств: подпись не липнет к списку, раскрытая часть — к названию.
        self.assertIn("#cm-devices-note { margin: 0 2px 16px; }", self.css)
        self.assertIn(".cm-device-body { padding: 16px 16px 18px 54px;", self.css)
        # Шторки вынесены на body: fixed внутри кабинета обрезался углами контейнера.
        self.assertIn("sheetLayer.className = 'cm cm-sheet-layer';", self.script)
        self.assertIn("document.body.appendChild(sheetLayer);", self.script)
        self.assertIn(".cm-sheet-layer { position: static;", self.css)
        # Кружок способа оплаты не растягивается правилом `.cm-row > span`.
        self.assertIn(".cm-row > .cm-method-mark, .cm-method-mark { width: 40px; height: 40px; flex: 0 0 40px;", self.css)

    def test_dashboard_context_for_mobile_cabinet(self):
        import inspect

        from engine import views

        src = inspect.getsource(views.dashboard)
        for key in ('"recurrent_info"', '"cabinet_discount"', '"cabinet_tariffs"', '"referral_bonus_days"', '"telegram_channel_url"', '"tg_bot_url"', '"payment_method_label"'):
            self.assertIn(key, src, key)
        cards = views.cabinet_tariff_cards(views.ACTUAL_TARIFFS)
        by_id = {card["id"]: card for card in cards}
        self.assertEqual(by_id["month"]["per_month"], views.ACTUAL_TARIFFS[0].price)
        self.assertEqual(by_id["month"]["savings"], 0)
        self.assertEqual(by_id["threemonths"]["months"], 3)
        self.assertGreater(by_id["year"]["savings"], by_id["threemonths"]["savings"])
        self.assertEqual(by_id["year"]["title"], "12 месяцев")

