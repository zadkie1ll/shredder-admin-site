import hashlib
import hmac
import json
import time
from datetime import date
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
from engine.views import admin_clamp_cohort_window
from engine.views import admin_runtime_setting_payload
from engine.views import admin_runtime_setting_type
from engine.views import admin_validate_runtime_setting
from engine.views import admin_stats_row_bucket_key
from engine.views import support_admin_api_cohort_stats
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
from engine.views import config_pins_payload
from engine.views import remove_config_template_id_from_pins
from engine.views import support_admin_api_config_pins
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
    def test_public_landing_headers_use_animated_brand_logo(self):
        animated_logo = "icons/monkey-island-logo-animated.gif"
        self.assertTrue(Path(f"engine/static/{animated_logo}").is_file())

        for template_name in (
            "engine/templates/index_vpn.html",
            "engine/templates/index_vps.html",
            "engine/templates/index_vps_direct_sale.html",
        ):
            with self.subTest(template=template_name):
                template = Path(template_name).read_text()
                self.assertIn(animated_logo, template)

        for template_name in (
            "engine/templates/index_vpn.html",
            "engine/templates/index_vps.html",
        ):
            with self.subTest(template=template_name):
                template = Path(template_name).read_text()
                self.assertIn(".landing-brand-mark", template)
                self.assertIn("@media (max-width: 380px)", template)

    def test_public_landing_hero_art_has_lightweight_animation(self):
        for template_name in (
            "engine/templates/index_vpn.html",
            "engine/templates/index_vps.html",
            "engine/templates/index_vps_direct_sale.html",
        ):
            with self.subTest(template=template_name):
                template = Path(template_name).read_text()
                self.assertIn("hero-art-scene", template)
                self.assertIn("island-art-float", template)
                self.assertIn("prefers-reduced-motion: reduce", template)

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
                "engine.views.require_support_admin_role", return_value=None
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
                "engine.views.require_support_admin_role", return_value=None
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


class AdminCohortDashboardTemplateTests(SimpleTestCase):
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
        # Когортный график переиспользует существующий построитель серии (с опцией
        # плотности подписей), не дублируя отрисовку.
        self.assertIn("drawSalesSeriesChart(chart, series, {maxBarCountLabels", template)

    def test_analytics_has_overview_and_cohort_subtabs(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('id="subpanel-overview"', template)
        self.assertIn('id="subpanel-cohort"', template)
        self.assertIn('data-subtab="overview"', template)
        self.assertIn('data-subtab="cohort"', template)
        self.assertIn("function showSubtab", template)
        self.assertIn("setupSubtabs('panel-stats')", template)

    def test_system_tab_grouped_into_subtabs(self):
        # Вкладка «Система» организована подвкладками (как «Аналитика»),
        # вместо жёсткой двухколоночной сетки, вылезавшей за экран.
        template = Path("engine/templates/admin_dashboard.html").read_text()

        for slug in ("sys-tariffs", "sys-winback", "sys-payment", "sys-referral", "sys-alerts", "sys-general", "sys-operations"):
            self.assertIn(f'data-subtab="{slug}"', template)
            self.assertIn(f'id="subpanel-{slug}"', template)
        # Runtime-настройки раскладываются по контейнерам групп.
        for group in ("tariffs", "winback", "payment", "referral", "alerts", "general", "other"):
            self.assertIn(f'data-settings-group="{group}"', template)
        self.assertIn("setupSubtabs('panel-system')", template)
        # Блоки рефералки (антифрод и блокировка) живут в подвкладке «Рефералка».
        self.assertIn('id="referral-antifraud-form"', template)
        self.assertIn('id="referral-block-form"', template)
        self.assertIn('id="load-recurrents"', template)
        self.assertIn('id="load-top-payments"', template)
        # Сетка рефералки не должна использовать фиксированную минимальную ширину колонок,
        # из-за которой контент вылезал за экран.
        self.assertNotIn(".system-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px", template)
        self.assertIn(".system-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", template)

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
        self.assertIn("cohortBucketLabel(row.label, granularity)", template)

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
