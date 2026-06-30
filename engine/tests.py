import hashlib
import hmac
import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory
from django.test import SimpleTestCase
from django.test import override_settings

from common.models.settings import BOT_APPLE_RECOMMENDED_APP_SETTING
from common.models.settings import BOT_TARIFF_PRICE_MONTH_SETTING
from common.models.settings import BOT_TARIFF_PRICE_YEAR_SETTING
from common.models.db import CustomConfigTemplate
from common.models.db import MagicToken
from common.models.db import User
from engine.payments import create_wata_payment_sync
from engine.payments import create_yk_payment_sync
from engine.payments import fetch_wata_transaction_status
from engine.views import active_wata_status_for_token
from engine.views import admin_runtime_setting_payload
from engine.views import admin_runtime_setting_type
from engine.views import admin_validate_runtime_setting
from engine.views import admin_stats_row_bucket_key
from engine.views import auth_by_telegram_widget
from engine.views import create_site_user
from engine.views import custom_config_template_payload
from engine.views import form_bool_enabled
from engine.views import get_runtime_actual_tariffs
from engine.views import get_telegram_auth_bot
from engine.views import get_telegram_web_login_start_code
from engine.views import cancel_autopay
from engine.views import pay
from engine.views import payment_session_url_key
from engine.views import payment_retry
from engine.views import render_login
from engine.views import should_create_trial_for_channel
from engine.views import should_send_payment_login_email
from engine.views import site_trial_registration_enabled
from engine.views import support_admin_api_config_templates
from engine.views import verify_telegram_widget_auth
from web_app.settings import telegram_web_login_start_codes


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
        self.assertIn("setup-cta-btn dashboard-action-btn !w-full sm:!w-auto", template)
        self.assertNotIn("setup-cta-btn !w-full !py-5 !mb-0 !mr-0", template)
        self.assertNotIn("setup-link-btn.setup-secondary-btn", template)
        self.assertNotIn("setup-cta-btn.setup-secondary-btn", template)
        self.assertNotIn('${link.text}</a>`).join(\' или \');', template)
        self.assertNotIn('id="tab-subscription"', template)
        self.assertNotIn("showTab('subscription')", template)

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

        registration_bonus = '+{{ join_referrer_bonus_days|default:"3" }} дня за регистрацию'
        self.assertIn("Приглашения и бонусы", template)
        self.assertIn(registration_bonus, template)
        self.assertIn("когда друг создаст аккаунт", template)
        self.assertIn("md:grid-cols-3", template)
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


class WataPaymentFlowTests(SimpleTestCase):
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

        self.assertIn("Открыть форму оплаты", template)
        self.assertNotIn("Проверить статус вручную", template)
        self.assertNotIn("manual-status-action", template)
        self.assertIn('target="_blank" rel="noopener"', template)

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

        class FakeSession:
            def get(self, model, template_id):
                return template

            def query(self, model):
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


class AdminSalesSeriesTests(SimpleTestCase):
    def test_bucket_key_matches_selected_granularity(self):
        value = datetime(2026, 5, 20, 12, 0)

        self.assertEqual(admin_stats_row_bucket_key(value, "day"), "2026-05-20")
        self.assertEqual(admin_stats_row_bucket_key(value, "week"), "2026-05-18")
        self.assertEqual(admin_stats_row_bucket_key(value, "month"), "2026-05-01")


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
        class FakeSession:
            def __init__(self):
                self.added = []

            def get(self, model, key):
                return None

            def add(self, obj):
                self.added.append(obj)
                if isinstance(obj, User):
                    obj.id = 1

            def flush(self):
                return None

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
        class FakeRwUser(SimpleNamespace):
            def HasField(self, field_name):
                return False

        class FakeSession:
            def __init__(self):
                self.added = []

            def get(self, model, key):
                return None

            def add(self, obj):
                self.added.append(obj)
                if isinstance(obj, User):
                    obj.id = 2

            def flush(self):
                return None

        session = FakeSession()
        request = SimpleNamespace()
        rw_user = FakeRwUser(username="rw-user")

        with mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": 42, "ymid": None},
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
        with mock.patch(
            "engine.views.fetch_wata_transaction_status"
        ) as fetch:
            self.assertIsNone(active_wata_status_for_token(token))
        fetch.assert_not_called()


class WebsiteDockerRuntimeTests(SimpleTestCase):
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


class SettingsTabTemplateTests(SimpleTestCase):
    def test_settings_tab_present_with_autopay_and_faq(self):
        template = Path("engine/templates/dashboard.html").read_text()

        self.assertIn('data-tab="settings"', template)
        self.assertIn('id="tab-settings"', template)
        self.assertIn("Отключить автопродление", template)
        self.assertIn("openAutopaySheet()", template)
        self.assertIn("confirmCancelAutopay", template)
        self.assertIn("{% url 'cancel_autopay' %}", template)
        self.assertIn("toggleFaq", template)
        self.assertIn("Как подключить ваш VPN?", template)

    def test_autopay_button_always_clickable_with_nothing_to_cancel_sheet(self):
        template = Path("engine/templates/dashboard.html").read_text()

        # Кнопка отключения видна всегда и кликабельна, без обёртки {% if has_recurrent %}.
        self.assertIn('onclick="onAutopayButtonClick()"', template)
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
