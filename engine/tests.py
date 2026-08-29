import hashlib
import hmac
import json
import time
from contextlib import ExitStack
from datetime import date
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.test import RequestFactory
from django.test import SimpleTestCase
from django.test import override_settings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from common.models.db import Base
from common.models.db import WataInvoice
from common.models.db import WataTransaction
from common.models.db import YkPayment

from common.models.settings import BOT_APPLE_RECOMMENDED_APP_SETTING
from common.models.settings import BOT_TARIFF_PRICE_ONEDAY_SETTING
from common.models.settings import BOT_TARIFF_PRICE_THREEDAYS_SETTING
from common.models.settings import BOT_TARIFF_PRICE_MONTH_SETTING
from common.models.settings import BOT_TARIFF_PRICE_THREEMONTHS_SETTING
from common.models.settings import BOT_TARIFF_PRICE_YEAR_SETTING
from common.models.db import ClientUaRule
from common.models.db import CensorCheck
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
from engine.views import _acq_pushes
from engine.views import _acq_renew45
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
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn("Для подключения", template)
        self.assertIn("Подключиться в 1 клик!", template)
        self.assertIn("min-width: min(100%, 420px);", template)
        self.assertIn("align-self: flex-start;", template)
        self.assertIn("align-self: stretch;", template)
        self.assertIn('data-tab="setup"><i class="fas fa-bolt"></i> Установка</button>', template)
        self.assertIn("<span>Установка</span>", template)
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
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

        # Актуальная рекомендация на скачивание Happ (iOS/macOS) — только RU App Store.
        self.assertIn("https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6788279553", template)
        self.assertNotIn("id6746188973", template)
        # Кнопка/ссылка «для других регионов» (US App Store) убрана.
        self.assertNotIn("id6504287215", template)
        self.assertNotIn("других регионов", template)

    def test_dashboard_customer_copy_uses_respectful_tone(self):
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn("document.documentElement.classList.add('standalone-pwa')", template)
        self.assertIn("document.documentElement.classList.add('ios-device')", template)
        self.assertIn("html.standalone-pwa.ios-device #app-container", template)
        self.assertIn("html.standalone-pwa.ios-device body.dashboard-v2 #tariff-selection-view", template)
        self.assertIn("padding-top: calc(18px + env(safe-area-inset-top)) !important;", template)
        self.assertIn("padding-top: calc(20px + env(safe-area-inset-top)) !important;", template)
        self.assertIn("height: 72px;", template)
        self.assertIn("padding-bottom: 88px !important;", template)

    def test_mobile_renewal_cta_is_short(self):
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertNotIn("Оплатить и получить доступ", template)


class AdminDashboardTemplateTests(SimpleTestCase):
    def test_large_loading_state_uses_payment_status_orb_spinner(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn("'apple_recommended_app'", template)
        self.assertIn(
            "'technical_work_enabled', 'apple_recommended_app'",
            template,
        )

    def test_admin_dashboard_loads_black_gold_control_skin(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn(
            "#panel-censor-checks .censor-check-form { grid-template-columns: minmax(0, 1fr); }",
            template,
        )
        self.assertIn("#panel-censor-checks .censor-check-row > span::before", template)
        self.assertIn("#panel-censor-checks .censor-key-row > span::before", template)
        self.assertIn('data-label="Последний замер"', template)
        self.assertIn('class="censor-row-actions" data-label="Действия"', template)

    def test_censor_checks_offer_safe_bulk_settings_form(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('id="acq-new-revenue-help"', template)
        self.assertIn("Как считаются «Новые покупатели» и «Выручка новых»", template)
        self.assertIn("самый ранний успешный платёж за всю доступную историю Wata и YooKassa", template)
        self.assertIn("Это не LTV пришедших за период", template)
        self.assertIn("не хранится как зафиксированный снимок", template)
        self.assertIn("Это сопоставление недельных итогов, а не строгая когортная конверсия", template)

    def test_winback_table_shows_conversion_segments(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

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

    def test_trials_window_selector_in_template(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('id="acq-trials-window"', template)
        self.assertIn('<option value="10" selected>', template)


class AcquisitionRenew45Tests(SimpleTestCase):
    @mock.patch("engine.views._acq_rows")
    def test_pct_and_maturity_flags(self, rows_mock):
        current_month = date.today().replace(day=1)
        rows_mock.return_value = [
            {"month": date(2020, 1, 1), "payers": 4, "renewed": 3},
            {"month": current_month, "payers": 10, "renewed": 1},
        ]

        result = _acq_renew45(object(), 12)

        old, cur = result["months"]
        self.assertEqual(old["renew_pct"], 75.0)
        # Давно закрытое 45-дневное окно — процент финальный.
        self.assertTrue(old["mature"])
        # У текущего месяца окно не закрыто — процент занижен.
        self.assertFalse(cur["mature"])
        self.assertEqual(cur["renew_pct"], 10.0)

    @mock.patch("engine.views._acq_rows")
    def test_zero_cohort_does_not_divide_by_zero(self, rows_mock):
        rows_mock.return_value = [{"month": date(2020, 1, 1), "payers": 0, "renewed": 0}]

        result = _acq_renew45(object(), 12)

        self.assertEqual(result["months"][0]["renew_pct"], 0.0)

    def test_renew45_section_registered(self):
        from engine.views import ACQ_SECTIONS

        self.assertIn("renew45", ACQ_SECTIONS)

    def test_template_has_renew45_chart_and_month_switcher(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('id="acq-renew45-chart"', template)
        self.assertIn("Отвал базы: % продливших в течение 45 дней", template)
        self.assertIn('data-help="renew45"', template)
        self.assertIn("окно 45 дней ещё не закрыто", template)
        # Переключатель месяцев и итоги за выбранный период.
        self.assertIn('id="acq-newrep-month"', template)
        self.assertIn("За период <b>${fullDay(rows[0].day)}", template)


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


class AcquisitionAdsTemplateTests(SimpleTestCase):
    def test_ads_tab_has_csv_import_and_daily_analytics(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/offer.html").read_text()

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

        template = Path("engine/templates/offer.html").read_text()
        # 2.2 не дублирует таблицу ценой, а говорит, где какие тарифы доступны.
        self.assertIn("доступны для оплаты в Telegram-боте Сервиса", template)
        self.assertNotIn("также доступен тариф «Подписка на 1 день» стоимостью", template)

    def test_offer_brand_follows_site_role(self):
        # Шапка оферты обязана подстраиваться под тип домена: на VPS-доменах
        # "MONKEY ISLAND VPS", на VPN/кабинетных — "MONKEY ISLAND VPN".
        # Регресс: бренд был захардкожен как VPS и светился на VPN-доменах.
        template = Path("engine/templates/offer.html").read_text()

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

        template = Path("engine/templates/terms.html").read_text()
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
        template = Path("engine/templates/privacy.html").read_text()
        self.assertIn("Дата вступления в силу: 04.08.2026", template)
        self.assertNotIn("03.05.2026", template)

    def test_runtime_offer_tariffs_use_database_prices(self):
        class FakeSession:
            def get(self, model, key):
                values = {
                    BOT_TARIFF_PRICE_THREEDAYS_SETTING: "11",
                    BOT_TARIFF_PRICE_ONEDAY_SETTING: "19",
                    BOT_TARIFF_PRICE_MONTH_SETTING: "299",
                    BOT_TARIFF_PRICE_THREEMONTHS_SETTING: "609",
                    BOT_TARIFF_PRICE_YEAR_SETTING: "1809",
                }
                value = values.get(key)
                return SimpleNamespace(value=value) if value is not None else None

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
        vpn_template = Path("engine/templates/index_vpn.html").read_text()
        self.assertIn(".mi-logo-mark", vpn_template)

        vps_template = Path("engine/templates/index_vps.html").read_text()
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
        template = Path("engine/templates/index_vps.html").read_text()

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
        template = Path("engine/templates/index_vpn.html").read_text()

        self.assertIn('{% if client_ip %}', template)
        self.assertIn("ip-topbar", template)
        self.assertIn("Ваш IP:", template)
        self.assertIn("Вы не защищены!", template)
        self.assertIn("{{ client_ip_country }}", template)

    def test_direct_sale_landing_sells_immediately_after_hero(self):
        # Смысл direct-sale-лендинга — сразу продавать: блок тарифов идёт
        # первым после hero, до всех остальных секций.
        template = Path("engine/templates/index_vps_direct_sale.html").read_text()

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
        template = Path("engine/templates/payment_status.html").read_text()

        self.assertIn("Продолжить оплату", template)
        self.assertNotIn("Проверить статус вручную", template)
        self.assertNotIn("manual-status-action", template)
        self.assertIn('target="_blank" rel="noopener"', template)

    def test_payment_status_can_return_to_cabinet_without_canceling_payment(self):
        template = Path("engine/templates/payment_status.html").read_text()

        self.assertIn('id="cabinet-action" href="{% url \'dashboard\' %}"', template)
        self.assertIn("Вернуться в кабинет", template)
        self.assertIn("window.location.replace(cabinetUrl)", template)
        self.assertIn("tg.BackButton.onClick(returnToCabinet)", template)
        self.assertIn("tg.BackButton.show()", template)
        self.assertIn("tg.BackButton.hide()", template)
        # Возврат не вызывает API отмены и не останавливает polling платежа.
        self.assertNotIn("cancelPayment", template)
        self.assertIn("window.setTimeout(pollStatus, 2500)", template)

    def test_payment_status_matches_mobile_dashboard_typography_and_buttons(self):
        template = Path("engine/templates/payment_status.html").read_text()

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
        request.user = SimpleNamespace(is_authenticated=False, id=None)
        request.session = SessionDict()

        with (
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
        login_token = SimpleNamespace(payment_gateway=None, payment_reference=None)

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return user

        class FakeSession:
            def query(self, model):
                return FakeQuery()

            def add(self, obj):
                if isinstance(obj, MagicToken):
                    obj.token = "magic-token"

            def flush(self):
                return None

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
        request.user = SimpleNamespace(is_authenticated=False, id=None)
        request.session = SessionDict()

        created_payment = SimpleNamespace(
            confirmation_url="https://wata.example/pay",
            reference="order-1",
            payload={"id": "invoice-1"},
        )

        with (
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.is_user_blocked", return_value=False),
            mock.patch("engine.views.get_runtime_actual_tariffs", return_value=[tariff]),
            mock.patch("engine.views.get_registration_context", return_value={"traffic_source": None, "ymid": None}),
            mock.patch("engine.views.sync_existing_user_tracking"),
            mock.patch("engine.views.create_purchase_login_token", return_value="raw-token"),
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
                "/payment/status/raw-token/"
            )
        )
        self.assertEqual(login_token.payment_gateway, "wata")
        self.assertEqual(login_token.payment_reference, "order-1")
        self.assertEqual(
            request.session[payment_session_url_key("raw-token")],
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
        login_token = SimpleNamespace(payment_gateway=None, payment_reference=None)

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return user

        class FakeSession:
            def query(self, model):
                return FakeQuery()

            def add(self, obj):
                if isinstance(obj, MagicToken):
                    obj.token = "magic-token"

            def flush(self):
                return None

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
        request.user = SimpleNamespace(is_authenticated=False, id=None)
        request.session = SessionDict()

        created_payment = SimpleNamespace(
            confirmation_url="https://yookassa.example/pay",
            reference="yk-payment-1",
        )

        with (
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.is_user_blocked", return_value=False),
            mock.patch("engine.views.get_runtime_actual_tariffs", return_value=[tariff]),
            mock.patch(
                "engine.views.get_registration_context",
                return_value={"traffic_source": None, "ymid": None},
            ),
            mock.patch("engine.views.sync_existing_user_tracking"),
            mock.patch("engine.views.create_purchase_login_token", return_value="raw-token"),
            mock.patch("engine.views.get_purchase_login_token", return_value=login_token),
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
                "/payment/status/raw-token/"
            )
        )
        self.assertEqual(login_token.payment_gateway, "yookassa")
        self.assertEqual(login_token.payment_reference, "yk-payment-1")

    def test_wata_payment_retry_redirects_to_hosted_invoice_url(self):
        request = RequestFactory().get("/pay/retry/token/")
        invoice = SimpleNamespace(url="https://wata.example/pay", order_id="order-1")

        class FakeSession:
            def close(self):
                return None

        with (
            mock.patch("engine.views.session_factory", return_value=FakeSession()),
            mock.patch("engine.views.get_purchase_login_token", return_value=object()),
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn('<meta name="color-scheme" content="dark light">', template)
        # Тема должна выставляться до загрузки стилей, иначе при светлой
        # теме будет вспышка тёмного фона. Привязываться к конкретному
        # CDN нельзя — состав подключаемых стилей меняется.
        self.assertLess(
            template.index('monkey_island_admin_theme_v1'),
            template.index('<link href="https://cdnjs.cloudflare.com'),
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('id="subpanel-overview"', template)
        self.assertIn('id="subpanel-cohort"', template)
        self.assertIn('data-subtab="overview"', template)
        self.assertIn('data-subtab="cohort"', template)
        self.assertIn("function showSubtab", template)
        self.assertIn("setupSubtabs('panel-stats')", template)

    def test_cohort_form_groups_period_and_cohort_ranges(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        for slug in ("sys-tariffs", "sys-winback", "sys-payment", "sys-referral", "sys-alerts", "sys-general"):
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn("invited_referrals", template)
        self.assertIn("invited_referrals_active", template)
        self.assertIn("invited_referrals_paid", template)
        self.assertIn("Привела рефералов", template)

    def test_cohort_all_time_preset_starts_at_business_start_not_2020(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        # «Всё время» в когорте отсчитывается от старта бизнеса (апрель 2025),
        # а не от условного 2020 года, по которому нет данных.
        self.assertIn("COHORT_BUSINESS_START = new Date(2025, 3, 1)", template)
        self.assertNotIn("start = new Date(2020, 0, 1);\n                end = todayOnly;", template)

    def test_cohort_tooltip_shows_month_name_for_monthly_granularity(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn("function cohortBucketLabel", template)
        self.assertIn("cohortBucketLabel(row.label, series.granularity)", template)

    def test_cohort_panel_has_metrics_legend(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('class="metrics-legend"', template)
        self.assertIn("Как читать показатели", template)
        # Определения ключевых показателей присутствуют.
        for term in ("ARPU", "ARPPU", "Размер когорты", "Привела рефералов"):
            self.assertIn(term, template)

    def test_existing_stats_form_is_untouched(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        # Старый блок аналитики и его контракт остаются на месте.
        self.assertIn('id="stats-form"', template)
        self.assertIn('name="sales_mode"', template)
        self.assertIn("function loadStats", template)
        self.assertIn('<option value="auto" selected>Авто</option>', template)
        self.assertIn('<option value="week">По неделям</option>', template)

    def test_stats_period_presets_are_grouped_and_include_calendar_ranges(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        find_rwms.assert_called_once_with(email="new@example.com", telegram_id=None)
        create_rwms_user.assert_not_called()
        add_event_log.assert_not_called()

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

    def test_runtime_actual_tariffs_use_database_prices(self):
        class FakeSession:
            def get(self, model, key):
                values = {
                    BOT_TARIFF_PRICE_MONTH_SETTING: "199",
                    BOT_TARIFF_PRICE_YEAR_SETTING: "bad-value",
                }
                value = values.get(key)
                return SimpleNamespace(value=value) if value is not None else None

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

        with mock.patch("engine.payments.httpx.Client", return_value=FakeClient()):
            created_payment = create_wata_payment_sync(
                wata_host="https://wata.example",
                wata_token="token",
                tariff=tariff,
                success_redirect_url="https://example.com/login/purchase/token/",
                fail_redirect_url="https://example.com/",
            )

        self.assertEqual(created_payment.confirmation_url, "https://wata.example/pay")
        self.assertEqual(created_payment.reference, "order-1")
        self.assertEqual(captured["url"], "https://wata.example/links")
        self.assertEqual(
            captured["json"]["successRedirectUrl"],
            "https://example.com/login/purchase/token/",
        )
        self.assertEqual(captured["json"]["failRedirectUrl"], "https://example.com/")


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

            def begin(self):
                return outer._FakeBegin()

            def query(self, *args, **kwargs):
                return FakeQuery()

            def add(self, obj):
                if isinstance(obj, MagicToken):
                    obj.token = "magic-token"

            def close(self):
                self.closed = True

        return FakeSession()

    def _post(self, email):
        request = RequestFactory().post("/magic/", {"email": email})
        request.session = {}
        return request

    def test_magic_link_asks_to_retry_when_registration_unavailable(self):
        session = self._fake_session(None)

        with mock.patch(
            "engine.views.session_factory", return_value=session
        ), mock.patch(
            "engine.views.create_site_user",
            side_effect=SiteRegistrationUnavailable("rwms unavailable"),
        ), mock.patch(
            "engine.views.send_magic_link_email"
        ) as send_email:
            response = send_magic_link(self._post("new@example.com"))

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], SITE_REGISTRATION_RETRY_MESSAGE)
        self.assertIn("попробуйте позже", payload["message"])
        send_email.assert_not_called()
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

    @override_settings(PAYMENT_GATEWAY="wata")
    def test_pay_asks_to_retry_instead_of_creating_orphan(self):
        """Деньги: если аккаунт под оплату подготовить нельзя, форма оплаты не
        открывается и в панели ничего не создаётся."""
        tariff = SimpleNamespace(price=100, db_tariff_id="month", description="1 месяц")

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
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
            ),
            mock.patch("engine.views.create_wata_payment_sync") as create_invoice,
        ):
            response = pay(request)

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], SITE_REGISTRATION_RETRY_MESSAGE)
        create_invoice.assert_not_called()


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
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn('data-tab="settings"', template)
        self.assertIn('id="tab-settings"', template)
        # Пользовательское название и иконка соответствуют содержимому раздела;
        # внутренний settings-id сохраняется для обратной совместимости ссылок.
        self.assertIn('<i class="far fa-user"></i><span>Профиль</span>', template)
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
        template = Path("engine/templates/dashboard.html").read_text()

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

        template = Path("engine/templates/dashboard.html").read_text()
        dashboard_source = inspect.getsource(views.dashboard)

        self.assertIn("@media (min-width: 1025px)", template)
        self.assertIn("font-family: 'Golos Text'", template)
        self.assertIn(".sidebar-desktop .nav-btn.active::before", template)
        self.assertIn("background: rgba(255, 255, 255, 0.065);", template)
        self.assertIn("background: #ffc700;", template)
        self.assertIn("border-radius: 8px !important;", template)
        self.assertIn('class="desktop-referral-strip"', template)
        self.assertIn("Пригласите друга — получите до {{ max_referral_bonus_days }} дней", template)
        self.assertIn('class="desktop-referral-btn" onclick="toggleRefSheet()"', template)
        self.assertNotIn('id="ref-pill"', template)
        self.assertIn('"max_referral_bonus_days": (', dashboard_source)
        self.assertIn("join_referrer_bonus_days", dashboard_source)
        self.assertIn("traffic_referrer_bonus_days", dashboard_source)
        self.assertIn("purchase_referrer_bonus_days", dashboard_source)

    def test_desktop_home_uses_inline_devices_without_summary_or_detail_panel(self):
        template = Path("engine/templates/dashboard.html").read_text()
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
        self.assertIn("desktop-subscription-card", desktop_home)
        self.assertIn("desktop-devices-panel", desktop_home)
        self.assertIn('id="desktop-devices-list"', desktop_home)
        self.assertIn('data-desktop-device-filter="all"', desktop_home)
        self.assertIn('onclick="quickAccessInstall()"', desktop_home)
        self.assertIn('class="desktop-home-actions"', desktop_home)
        self.assertIn('onclick="quickAccessInstall()" class="desktop-secondary-action"', desktop_home)
        self.assertIn("Быстрый доступ", desktop_home)
        self.assertIn('onclick="showTab(\'setup\')" class="desktop-primary-action"', desktop_home)
        self.assertIn('onclick="showTab(\'setup\')" class="desktop-secondary-action"', desktop_home)
        self.assertIn("Подключить устройство", desktop_home)
        self.assertIn('onclick="openPaymentsHistorySheet()"', desktop_home)
        self.assertIn("История платежей", desktop_home)
        self.assertIn("fas fa-receipt", desktop_home)

    def test_mobile_home_uses_white_monkey_face_without_tint(self):
        template = Path("engine/templates/dashboard.html").read_text()
        logo_styles = template[
            template.index(".tg-mini-brand-mark img {"):
            template.index("}", template.index(".tg-mini-brand-mark img {")) + 1
        ]

        self.assertIn("{% static 'icons/logo-face-white.png' %}", template)
        self.assertIn("filter: none;", logo_styles)
        self.assertNotIn("sepia", logo_styles)

    def test_desktop_home_uses_full_width_compact_surface_palette(self):
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn("function renderDesktopDevices(data)", template)
        self.assertIn("function bindDeviceDeleteButtons(root)", template)
        self.assertIn("data-desktop-device-status", template)
        self.assertIn("data-mi3-hwid", template)
        self.assertIn("this.classList.contains('is-armed')", template)
        self.assertIn("/api/cabinet/devices/delete/", template)
        self.assertIn("updateHomeDevices(payload)", template)
        self.assertIn("desktopDeviceFilter === 'online'", template)

    def test_mobile_home_uses_compact_state_driven_layout(self):
        template = Path("engine/templates/dashboard.html").read_text()

        # Компактная главная всегда есть в DOM: на сайте её включает mobile
        # breakpoint, а Mini App использует её независимо от ширины.
        self.assertIn('<div class="tg-mini-home">', template)
        self.assertIn("body.tg-webapp .tg-mini-home", template)
        self.assertIn("{% if not tg_webapp_mode %}", template)
        self.assertIn('<div class="standard-dashboard-home">', template)
        self.assertIn('class="mi3-card" aria-label="Статус подписки"', template)
        self.assertIn('До {{ user.expire_at|date:"j E Y" }}', template)
        self.assertIn('class="tg-mini-primary"', template)
        self.assertIn("Подключить VPN", template)
        self.assertIn("Продлить подписку", template)
        self.assertIn("Купить подписку", template)
        self.assertIn('class="tg-mini-action-list"', template)
        # На главном экране вместо «Автопродление» — «История платежей»
        self.assertIn('onclick="openPaymentsHistorySheet()" class="tg-mini-action-row"', template)

    def test_mobile_home_has_restrained_visual_hierarchy(self):
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn("@media (max-width: 1024px)", template)
        self.assertIn(".tg-mini-home {\n                display: block;", template)
        self.assertIn(".standard-dashboard-home {\n                display: none;", template)
        self.assertIn("body.dashboard-v2 .main-content", template)
        self.assertIn("body.dashboard-v2 .nav-mobile", template)

    def test_desktop_referral_dialog_matches_approved_concept(self):
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn('id="ref-desktop-content" class="ref-desktop-dialog"', template)
        self.assertIn('role="dialog" aria-modal="true"', template)
        self.assertIn('aria-labelledby="ref-desktop-title"', template)
        self.assertIn("Отправьте ссылку другу — бонусы начислятся автоматически", template)
        self.assertIn("Как получить до {{ max_referral_bonus_days|default:\"40\" }} дней", template)
        self.assertIn("Друг подключился", template)
        self.assertIn("Использовал 100 МБ", template)
        self.assertIn("Оплатил подписку", template)
        self.assertIn('aria-label="Статистика приглашений"', template)
        self.assertIn('id="ref-desktop-telegram-link"', template)
        self.assertIn('id="ref-desktop-site-link"', template)
        self.assertIn("Рекомендуем", template)
        self.assertIn('onclick="shareTelegramReferral()">Отправить</button>', template)
        self.assertIn("Бонусы начисляются автоматически после выполнения условий", template)
        self.assertIn("body.dashboard-v2 #ref-sheet #ref-content", template)
        self.assertIn("body.dashboard-v2 .ref-desktop-dialog.is-open", template)

    def test_mobile_referral_bottom_sheet_is_preserved(self):
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

        # В HTML-атрибуте работает автоэскейпинг Django; |escapejs здесь
        # портил ссылку литеральными =-последовательностями.
        self.assertIn('data-mi3-copy="{{ referral_link }}"', template)
        self.assertIn('data-mi3-copy="{{ site_referral_link }}"', template)
        self.assertIn("document.querySelectorAll('[data-mi3-copy]')", template)
        self.assertIn("navigator.clipboard.writeText(value)", template)
        # Fallback для webview без clipboard API
        self.assertIn("document.execCommand('copy')", template)

    def test_connect_sheet_has_back_button(self):
        """Шторка подключения: назад | прогресс шагов | закрыть, заголовок
        отдельной строкой (кнопки не смещают его). На шаге 1 «Назад»
        невидима, но держит место."""
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn('id="mi3-connect-back"', template)
        self.assertIn('onclick="mi3ConnectBack()"', template)
        self.assertIn("window.mi3ConnectBack = function ()", template)
        self.assertIn('class="mi3-sheet-nav"', template)
        self.assertIn('id="mi3-connect-bars"', template)
        self.assertIn("back.classList.toggle('is-ghosted', step === 1)", template)
        self.assertIn("bar.classList.toggle('is-on', i < step)", template)
        # Заголовок вне flex-строки с кнопками
        self.assertIn(
            '<div class="mi3-sheet-title" id="mi3-connect-title">Что подключаем?</div>\n        <div id="mi3-connect-body"></div>',
            template,
        )

    def test_quick_access_waits_for_deferred_install_prompt(self):
        """Первый клик по «Быстрому доступу» не должен сваливаться в
        инструкцию, если beforeinstallprompt ещё не успел прийти."""
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn("function waitForInstallPrompt(", template)
        self.assertIn("const installPrompt = await waitForInstallPrompt(1500);", template)
        self.assertIn("installPromptWaiters.splice(0).forEach((resolve) => resolve(event));", template)

    def test_faq_back_returns_to_origin_tab(self):
        """«Назад» из FAQ возвращает на вкладку, с которой FAQ открыли
        (например, «Поддержка»), а не всегда в «Профиль»."""
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn("let faqReturnTab = 'settings';", template)
        self.assertIn("faqReturnTab = activeTab ? activeTab.id.replace('tab-', '') : 'settings';", template)
        self.assertIn("function backFromSettingsFaq()", template)
        self.assertIn("showTab(faqReturnTab || 'settings');", template)
        self.assertIn('onclick="backFromSettingsFaq()"', template)

    def test_referral_dialog_supports_escape_focus_and_safe_telegram_share(self):
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn('aria-hidden="true"', template)
        self.assertIn("let refSheetTrigger = null;", template)
        self.assertIn("appContainer?.setAttribute('inert', '');", template)
        self.assertIn("appContainer?.removeAttribute('inert');", template)
        self.assertIn("document.getElementById('ref-desktop-close')?.focus();", template)
        self.assertIn("event.key === 'Escape'", template)
        self.assertIn("function shareTelegramReferral()", template)
        self.assertIn("https://t.me/share/url?url=${encodeURIComponent(input.value)}", template)
        self.assertIn("telegramWebApp.openTelegramLink(shareUrl)", template)
        self.assertIn("window.open(shareUrl, '_blank', 'noopener,noreferrer')", template)

    def test_desktop_referral_icons_stay_centered_and_links_have_no_input_bars(self):
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn("grid-template-columns: 50px minmax(0, 1fr);", template)
        self.assertIn("min-height: 96px;", template)
        self.assertNotIn("min-height: 136px;", template)
        self.assertIn("position: absolute;\n                top: 60px;\n                right: 28px;", template)
        self.assertIn("right: 28px;\n                min-width: 150px;", template)
        self.assertNotIn("margin-top: 48px;", template)
        self.assertIn("text-align: center;", template)
        self.assertIn("position: absolute;\n                top: 24px;\n                right: 28px;", template)

    def test_referral_terms_use_dedicated_responsive_dialog(self):
        template = Path("engine/templates/dashboard.html").read_text()
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
        self.assertIn("Друг использовал 100 МБ трафика", template)
        self.assertIn("Друг оплатил подписку от 1 месяца", template)
        self.assertNotIn('id="referral-terms-content"', template)
        self.assertNotIn("openQuickAccessModal", open_terms_source)
        self.assertIn("modal.classList.remove('hidden')", open_terms_source)

    def test_referral_terms_match_desktop_and_mobile_concept(self):
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn("function referralTermsIsOpen()", template)
        self.assertIn("modal?.classList.contains('is-open')", template)
        self.assertIn("event.key === 'Escape' && referralTermsIsOpen()", template)
        self.assertIn("document.getElementById('referral-terms-close')?.focus();", template)
        self.assertIn("appContainer?.setAttribute('inert', '');", template)
        self.assertIn("appContainer?.removeAttribute('inert');", template)
        self.assertIn("if (referralTermsOpen || setupActive) back.show();", template)
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
        self.assertIn('data-config-pins-url', template)
        self.assertIn('id="config-pins-form"', template)
        self.assertIn("submitConfigPinsSearch", template)
        self.assertIn("saveConfigPins", template)
        self.assertIn("data-config-pins-save", template)
        self.assertIn("data-config-pins-clear", template)
        self.assertIn("Персональные конфиги для пользователя", template)


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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
        # Скроллится вся карточка (колесо/жест над шапкой тоже прокручивает к кнопке),
        # шапка закреплена сверху и непрозрачна.
        card_rule = template.split(".modal-card.config-template-modal-card {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-y: auto", card_rule)
        self.assertIn(".config-template-modal-card .modal-header { position: sticky", template)
        self.assertIn(".config-template-modal-card .modal-body { overflow: visible", template)


class ConfigTemplatesAdminUiTests(SimpleTestCase):
    def test_json_editor_selection_is_visible(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()
        # Выделение текста в CodeMirror перекрывает блеклый фон темы material-darker,
        # иначе выделенный фрагмент почти не отличим от фона редактора.
        self.assertIn(".cm-s-material-darker div.CodeMirror-selected", template)
        self.assertIn(".cm-s-material-darker.CodeMirror-focused div.CodeMirror-selected", template)
        self.assertIn(".cm-s-material-darker .CodeMirror-line::selection", template)

    def test_config_modal_does_not_close_on_backdrop_or_escape(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
            def get_node_users_usage(self, request):
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        # «Трафик нод» — теперь вкладка раздела «Инфраструктура»
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('data-tab="infrastructure"', template)
        self.assertIn('data-subtab="inf-traffic"', template)
        self.assertIn('id="subpanel-inf-traffic"', template)
        self.assertIn('id="panel-node-traffic"', template)
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn("function positionDatePopover(field)", template)
        self.assertIn("popover.style.setProperty('--date-popover-shift', `${Math.round(shift)}px`);", template)
        self.assertIn("transform: translateX(var(--date-popover-shift, 0px));", template)
        self.assertIn("transform: translateX(calc(-50% + var(--date-popover-shift, 0px)));", template)
        self.assertIn("positionDatePopover(field);\n            popover.querySelector('[data-date-prev]')", template)
        self.assertIn("document.querySelectorAll('[data-date-picker].open').forEach(positionDatePopover);", template)

    def test_node_traffic_loads_today_report_on_first_tab_open(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('<option value="1" selected>Сегодня (с 03:00 МСК)</option>', template)
        self.assertIn('<option value="24">Вчера + сегодня</option>', template)
        self.assertIn("let nodeTrafficInitialReportLoaded = false;", template)
        self.assertIn("if (!nodeTrafficInitialReportLoaded && form)", template)
        self.assertIn("nodeTrafficInitialReportLoaded = true;", template)
        self.assertIn("loadNodeTrafficForForm(form);", template)
        self.assertIn("function loadNodeTraffic(event)", template)


class NodeProvisionTemplateTests(SimpleTestCase):
    def setUp(self):
        self.template = Path("engine/templates/admin_dashboard.html").read_text()

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
            def get_node_users_usage(self, request):
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('data-subtab="acq-journey"', template)
        self.assertIn('id="subpanel-acq-journey"', template)
        self.assertIn('id="acq-timing-table"', template)
        self.assertIn('id="acq-ladder-table"', template)
        self.assertIn('id="acq-tariff-paths-table"', template)
        self.assertIn("'acq-journey': loadJourney", template)
        self.assertIn("acqFetch('trial_timing'", template)
        self.assertIn("acqFetch('renewal_ladder'", template)
        self.assertIn("acqFetch('tariff_paths'", template)


class AcquisitionFunnelTemplateTests(SimpleTestCase):
    def test_funnel_table_has_invoice_to_payment_percent(self):
        # В таблице воронки рядом с «Подписка→покупатель» есть колонка
        # «Инвойс→Оплата»: все оплаты недели ÷ инвойсы той же недели.
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn("'Инвойс→Оплата'", template)
        self.assertIn("funnelPct(r.payments, r.invoice_clicks)", template)
        # Метрика описана в легенде метрик и в help-модалке воронки.
        self.assertIn("Инвойс→Оплата в воронке", template)
        self.assertIn(
            "<b>Инвойс→Оплата</b> = все оплаты недели ÷ инвойсы той же недели.",
            template,
        )


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
        self.template = Path("engine/templates/admin_dashboard.html").read_text()
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
        self.template = Path("engine/templates/admin_dashboard.html").read_text()
        self.css = Path("engine/static/css/admin_dashboard.css").read_text()

    def test_axis_labels_are_compact_and_carry_units(self):
        self.assertIn("function adminChartAxisLabel(value, fmt = 'raw')", self.template)
        self.assertIn("млн", self.template)
        self.assertIn("тыс", self.template)
        self.assertIn("if (fmt === 'pct') return `${Math.round(value)}%`;", self.template)
        # Обе оси обоих графиков используют общий форматтер.
        self.assertEqual(self.template.count("adminChartAxisLabel("), 5)

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
        self.template = Path("engine/templates/admin_dashboard.html").read_text()
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

        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()
        self.assertIn("Подкл.→100 МБ", template)


class SetupWizardTests(SimpleTestCase):
    """Мастер подключения в кабинете и Mini App."""

    def test_wizard_resets_to_start_after_finish(self):
        template = Path("engine/templates/dashboard.html").read_text()

        # Повторный вход в мастер после «Завершить» (шаг done) начинается с
        # первого шага — без сброса клиент видел бы последний экран.
        self.assertIn("newSetupStep === 'done'", template)
        showtab_src = template.split("function showTab(tabId)", 1)[1].split(
            "function ", 1
        )[0]
        self.assertIn("newSetupStep = 'start'", showtab_src)
        self.assertIn("renderNewSetupWizard()", showtab_src)

    def test_wizard_honors_apple_recommended_app(self):
        template = Path("engine/templates/dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        # Нативный инпут скрыт внутри стилизованной кнопки-label
        self.assertIn('id="acq-csv-file-label"', template)
        self.assertIn('id="acq-csv-filename"', template)
        self.assertIn('id="acq-csv-file-input"', template)
        # Имя выбранного файла показывается на кнопке
        self.assertIn("nameEl.textContent = file ? file.name", template)
        # Скрытый инпут больше не полагается на браузерный required
        self.assertIn("сначала выберите CSV-файл", template)

    def test_csv_form_has_explicit_account_selector(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('id="acq-csv-account"', template)
        # Селекторы синхронизированы в обе стороны
        self.assertIn("csvSel.value = currentAdAccount()", template)
        # Статус импорта называет аккаунт
        self.assertIn("импортировано в «${account}»", template)
        self.assertIn("загрузка в аккаунт «${account}»", template)


class AdminAcqDatePickerTests(SimpleTestCase):
    """Календарь периода в «Привлечении»: кастомный пикер как в «Аналитике»
    вместо нативных input[type=date], плюс запрет «конец раньше начала»."""

    def test_newrep_period_uses_custom_date_picker(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        # Нативных date-инпутов у графика «новые vs повторные» больше нет —
        # только hidden внутри date-field.
        self.assertNotIn('type="date" id="acq-newrep-start"', template)
        self.assertNotIn('type="date" id="acq-newrep-end"', template)
        self.assertIn('<input type="hidden" id="acq-newrep-start">', template)
        self.assertIn('<input type="hidden" id="acq-newrep-end">', template)
        # Программная установка дат идёт через setDateFieldValue (лейблы)
        self.assertIn("function setAcqDateField", template)
        self.assertIn("setAcqDateField('acq-newrep-start'", template)

    def test_date_range_cannot_invert(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        # Дни вне диапазона выключены: у конца min — это начало, у начала
        # max — это конец. Подключено и в «Привлечении», и в «Аналитике».
        self.assertIn("function resolveRangeBound", template)
        self.assertIn("range-disabled", template)
        self.assertIn('data-range-min-from="#acq-newrep-start"', template)
        self.assertIn('data-range-max-from="#acq-newrep-end"', template)
        self.assertIn('data-range-min-from=\'[name="start"]\'', template)
        self.assertIn('data-range-min-from=\'[name="cohort_start"]\'', template)

    def test_month_selection_survives_programmatic_date_set(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        # setDateFieldValue шлёт input-событие; подстановка границ месяца
        # не должна сбрасывать сам селектор месяца.
        self.assertIn("settingMonthBounds = true", template)
        self.assertIn("if (!settingMonthBounds) monthSel.value = ''", template)
        self.assertIn("input.dispatchEvent(new Event('input'", template)


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
        template = Path("engine/templates/dashboard.html").read_text()

        # Главный экран mini app: вместо «Автопродление» — «История платежей»
        self.assertNotIn('tg-mini-action-label">Автопродление', template)
        self.assertIn('tg-mini-action-label">История платежей', template)
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()
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
        template = Path("engine/templates/admin_dashboard.html").read_text()
        self.assertNotIn("Сегодня (UTC)", template)
        self.assertNotIn("} UTC<", template)


class AdminPaymentJournalTests(SimpleTestCase):
    """Единый современный журнал операций для двух списков платежей."""

    def test_client_and_global_payment_lists_use_expandable_journal(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn(
            ".broadcast-primary-fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(250px, .88fr); gap: 12px; align-items: start; }",
            template,
        )
        self.assertIn("#broadcast-segment-hint:empty { display: none; }", template)

    def test_zero_broadcast_funnel_metrics_do_not_repeat_zero_percent(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

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

        template = Path("engine/templates/admin_dashboard.html").read_text()
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

        template = Path("engine/templates/admin_dashboard.html").read_text()
        self.assertIn('id="broadcasts-archive-toggle"', template)
        self.assertIn("data-broadcast-archive=", template)
        self.assertIn("data-broadcast-unarchive=", template)
        self.assertIn("broadcastsArchiveView", template)
        self.assertIn("?archived=1", template)
        self.assertIn("Архив пуст.", template)

    def test_broadcast_history_polls_without_flicker(self):
        """Автообновление истории рассылок (раз в 5с при running) не должно
        подменять список спиннером — страница «моргала» на каждом тике."""
        template = Path("engine/templates/admin_dashboard.html").read_text()

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

        template = Path("engine/templates/admin_dashboard.html").read_text()
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

        template = Path("engine/templates/admin_dashboard.html").read_text()
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

        template = Path("engine/templates/admin_dashboard.html").read_text()
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

        template = Path("engine/templates/admin_dashboard.html").read_text()
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

        template = Path("engine/templates/admin_dashboard.html").read_text()
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

        template = Path("engine/templates/admin_dashboard.html").read_text()
        self.assertIn('id="broadcast-disable-preview"', template)
        self.assertIn("checked", template)
        self.assertIn("body.append('disable_preview', '1')", template)

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('data-tab="broadcasts"', template)
        self.assertIn('data-tab="bulk-actions"', template)
        self.assertIn('id="bulk-preview"', template)
        self.assertIn("dry-run", template)
        self.assertIn('data-broadcast-stop=', template)


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

        src = inspect.getsource(views.site_apply_first_purchase_discount)
        self.assertIn("has_paid", src)
        self.assertIn("valid_until", src)
        # Цена не может уйти ниже 1 ₽ и скидка fail-open при ошибках.
        self.assertIn("max(1,", src)
        self.assertIn("except Exception", src)

    def test_yk_payment_carries_promo_metadata(self):
        import inspect

        from engine import payments

        src = inspect.getsource(payments.create_yk_payment_sync)
        self.assertIn('"promo": promo', src)

    def test_template_has_promocodes_tab(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('data-tab="promocodes"', template)
        self.assertIn('id="promo-create"', template)
        self.assertIn('id="batch-create"', template)
        self.assertIn("start=promo_", Path("engine/views.py").read_text())

    def test_promocode_editor_explains_effects_and_audiences(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn("Один код — одна активация на человека", template)
        self.assertIn("Скидка на следующую оплату", template)
        self.assertIn("Без даты она доступна 72 часа", template)
        self.assertIn('data-promo-type-picker="promo"', template)
        self.assertIn('data-promo-type-picker="batch"', template)
        self.assertIn('id="promo-first-only"', template)
        self.assertIn('id="batch-first-only"', template)
        self.assertIn('id="batch-valid-until"', template)
        self.assertIn("renderPromoCodes", template)
        self.assertIn("renderPromoBatches", template)
        self.assertNotIn('<select id="promo-type"', template)

    def test_promocode_icons_and_supporting_copy_are_readable(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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
        self.assertIn("t.payment_time > uses.created_at", src)
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
        template = Path("engine/templates/admin_dashboard.html").read_text()

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

        template = Path("engine/templates/admin_dashboard.html").read_text()
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

    def _dashboard_context(self, rwms_patch_kwargs, time_left=None):
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

            def commit(self):
                return None

            def rollback(self):
                return None

            def close(self):
                return None

        class SessionDict(dict):
            modified = False

        request = RequestFactory().get("/dashboard/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            id=42,
            username="user-42",
            telegram_id=100500,
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

        sub = SimpleNamespace(subscription_url="https://sub.example/u")
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
                    return_value=None,
                ), mock.patch(
                    "engine.views.create_user", return_value=None
                ), mock.patch(
                    "engine.views.find_rwms_user_by_identity",
                    return_value=self._panel_record(),
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

    def test_magic_link_sends_ownership_conflict_to_support(self):
        request = RequestFactory().post("/magic/", {"email": "reused@example.com"})
        request.session = {}

        with mock.patch(
            "engine.views.session_factory", return_value=self._magic_link_session()
        ), mock.patch(
            "engine.views.create_site_user",
            side_effect=SiteRegistrationOwnershipConflict("row belongs to other"),
        ), mock.patch(
            "engine.views.send_magic_link_email"
        ) as send_email:
            response = send_magic_link(request)

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], SITE_REGISTRATION_SUPPORT_MESSAGE)
        send_email.assert_not_called()

    @override_settings(PAYMENT_GATEWAY="wata")
    def test_pay_sends_ownership_conflict_to_support(self):
        tariff = SimpleNamespace(price=100, db_tariff_id="month", description="1 месяц")

        class SessionDict(dict):
            modified = False

        class FakeQuery:
            def filter(self, *args, **kwargs):
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
    """CRITICAL: purchase-токен выпускается ДО оплаты, и его сырое значение
    возвращается инициатору платежа в payment_status_url (pay() отдаёт JSON с
    этим URL). Раньше auth_by_purchase_link логинил по нему БЕЗ проверки
    оплаты — значит, любой, кто ввёл ЧУЖОЙ email в форму оплаты, получал
    рабочий вход в чужой аккаунт, ничего не заплатив. Вход разрешён только по
    подтверждённой оплате этого токена."""

    def _run_auth(self, payment_status):
        token_row = SimpleNamespace(
            user_id=42,
            token_hash=views.hash_purchase_login_token("raw-token"),
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
            result = views.auth_by_purchase_link(mock.Mock(), "raw-token")

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


class InfraServersDashboardTemplateTests(SimpleTestCase):
    """Регрессии нового UX «Инфраструктура → Серверы»."""

    def setUp(self):
        self.template = Path("engine/templates/admin_dashboard.html").read_text()

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

    def test_server_cards_are_primary_and_table_view_remains_available(self):
        for marker in (
            'data-infra-view="cards"',
            'data-infra-view="table"',
            'class="infra-server-grid"',
            'class="infra-server-card${cardTone}"',
            'class="infra-server-card-traffic"',
            'class="infra-server-card-stats"',
            'class="infra-server-card-open"',
            "function infraServerPresentation(server)",
            "function renderInfraServerCards(servers)",
            "function renderInfraServerTable(servers)",
            "let infraServersView = 'cards';",
            "infraServersView === 'table'",
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

    def test_server_view_preference_and_card_interactions_are_preserved(self):
        for marker in (
            "const INFRA_SERVERS_VIEW_STORAGE_KEY",
            "localStorage.getItem(INFRA_SERVERS_VIEW_STORAGE_KEY)",
            "localStorage.setItem(INFRA_SERVERS_VIEW_STORAGE_KEY, nextView)",
            "button.setAttribute('aria-pressed', active ? 'true' : 'false')",
            "event.target !== serverElement",
            "focus({preventScroll: true})",
            'data-infra-menu="${server.id}"',
            'data-infra-open-detail="${server.id}"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

        for css_marker in (
            ".infra-server-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));",
            ".infra-server-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }",
            ".infra-server-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }",
            ".infra-server-card:hover",
            'html[data-admin-theme="light"] .infra-server-card',
            ".infra-server-grid { grid-template-columns: 1fr; padding: 10px; }",
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
            ".infra-server-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; padding: 16px; }",
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
            "function infraConfirmPendingDomain(container, pendingRow, domain, server)",
            "function infraWireDomainDeleteButton(button, server)",
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
            ".infra-domain-manage-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 420px)); align-content: start; align-items: start; gap: 10px; }",
            ".infra-domain-manage-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 50px;",
            ".infra-detail-management-panel#infra-detail-domains { display: block; min-height: 0; }",
            'id="infra-add-domain-form" class="infra-form infra-domain-add-form"',
            ".infra-domain-add-form { grid-template-columns: minmax(260px, 520px) auto;",
            ".infra-domain-add-form .infra-form-submit { width: auto; min-width: 180px;",
            ".infra-domain-manage-list { grid-template-columns: 1fr; }",
            ".infra-domain-add-form { grid-template-columns: 1fr; max-width: none; }",
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
        for marker in (
            'html[data-admin-theme="light"] .infra-table tbody tr:not(.is-selected):hover',
            "background: rgba(181,139,0,.075); box-shadow: inset 3px 0 0 rgba(154,113,0,.42);",
            'html[data-admin-theme="light"] .infra-table tbody tr.infra-row-offline:not(.is-selected):hover',
            "background: rgba(211,65,78,.09); box-shadow: inset 3px 0 0 rgba(197,54,69,.46);",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.template)

    def test_server_list_uses_readable_typography(self):
        for marker in (
            "#subpanel-inf-servers { font-family: 'Manrope'",
            "#infra-server-detail, .infra-server-screen-head { font-family: 'Inter'",
            "font-optical-sizing: auto; font-synthesis: none;",
            "#subpanel-inf-servers .infra-server-card-name { font-size: 19px; font-weight: 800;",
            "#subpanel-inf-servers .infra-server-card-host { font-size: 13px; font-weight: 600; }",
            ".infra-detail-stat-label { font-size: 12px; font-weight: 700; }",
            ".infra-detail-stat-value { font-size: clamp(25px, 1.7vw, 34px); font-weight: 800; }",
            "#infra-server-detail .infra-detail-card h4 { font-size: 17px; line-height: 1.2; font-weight: 700;",
            "#infra-server-detail .infra-kv { font-size: 13.5px; line-height: 1.4; }",
            '#infra-server-detail .infra-kv code { font-family: "SFMono-Regular"',
            'class="infra-machine-id"',
            "#infra-server-detail .infra-control { font-size: 12.5px; font-weight: 600; }",
            ".infra-table-heading h3 { margin: 0; color: var(--text-main); font-size: 16px;",
            ".infra-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }",
            "font-size: 13.5px; line-height: 1.25; font-weight: 850;",
            ".infra-server-host { color: rgba(255,255,255,.67); font-size: 11.5px;",
            'html[data-admin-theme="light"] .infra-server-host { color: rgba(34,39,47,.68); }',
            ".infra-table td, .infra-table th { padding: 10px 8px; }",
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
