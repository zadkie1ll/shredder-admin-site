import hashlib
import hmac
import json
import time
from datetime import date
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory
from django.test import SimpleTestCase
from django.test import override_settings

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
from engine.views import create_site_user
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

    def test_payment_search_is_rendered_before_payment_history(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        payment_panel = template[template.index('<section id="panel-payment-info"'):]
        search_position = payment_panel.index('<form id="payment-info-form"')
        history_position = payment_panel.index('<section class="card payments-board">')
        result_position = payment_panel.index('<div id="payment-info-result">')

        self.assertLess(search_position, history_position)
        self.assertLess(history_position, result_position)

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

        self.assertIn('id="acq-csv-form"', template)
        self.assertIn("action', 'import_csv'", template.replace('"', "'"))
        self.assertIn('id="acq-ads-daily-chart"', template)
        self.assertIn('id="acq-ads-cost-chart"', template)
        self.assertIn('id="acq-ads-group"', template)
        self.assertIn('data-help="ads_daily"', template)
        self.assertIn("'Подключения', 'Цена подключения', 'Продажи', 'Цена продажи'", template)
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
        self.assertLess(template.index('monkey_island_admin_theme_v1'), template.index('<script src="https://cdn.tailwindcss.com'))
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
        self.assertNotIn('data-top-payments-url=', template)
        self.assertNotIn("function loadRecurrents", template)
        self.assertNotIn("function loadTopPayments", template)
        # Сетка рефералки не должна использовать фиксированную минимальную ширину колонок,
        # из-за которой контент вылезал за экран.
        self.assertNotIn(".system-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px", template)
        self.assertIn(".system-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", template)

    def test_referral_block_management_is_part_of_found_client(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()
        css = Path("engine/static/css/admin_dashboard.css").read_text()

        self.assertIn("function clientReferralControlHtml", template)
        self.assertIn("function clientSubscriptionManageHtml(refResult)", template)
        self.assertIn("${clientReferralControlHtml(refResult)}", template)
        self.assertIn("${clientSubscriptionManageHtml(refPayload?.result)}", template)
        self.assertIn("Управление клиентом", template)
        self.assertIn('data-client-referral-block-action="block"', template)
        self.assertIn('data-client-referral-block-action="unblock"', template)
        self.assertIn("function clientReferralBlockAction", template)
        self.assertIn("formData.set('q', clientCardState.q)", template)
        self.assertIn("await renderClientCard();", template)
        self.assertNotIn("await renderClientCard('referrals')", template)
        self.assertNotIn("const controlHtml = clientReferralControlHtml(refResult);", template)
        self.assertIn("Заблокировать рефералку", template)
        self.assertIn("Разблокировать рефералку", template)
        self.assertIn(".client-ref-control", css)
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
    def test_admin_dashboard_has_node_traffic_tab(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('data-tab="node-traffic"', template)
        self.assertIn('id="panel-node-traffic"', template)
        self.assertIn('id="node-traffic-form"', template)
        self.assertIn("data-traffic-nodes-url", template)
        self.assertIn("data-node-traffic-url", template)
        # вкладка доступна только полному админу
        admin_only_block = template.split('{% if support_admin_is_full_admin %}')
        self.assertTrue(
            any('data-tab="node-traffic"' in part.split("{% endif %}")[0] for part in admin_only_block[1:])
        )

    def test_node_traffic_loads_today_report_on_first_tab_open(self):
        template = Path("engine/templates/admin_dashboard.html").read_text()

        self.assertIn('<option value="1" selected>Сегодня (UTC)</option>', template)
        self.assertIn('<option value="24">Вчера + сегодня</option>', template)
        self.assertIn("let nodeTrafficInitialReportLoaded = false;", template)
        self.assertIn("if (!nodeTrafficInitialReportLoaded && form)", template)
        self.assertIn("nodeTrafficInitialReportLoaded = true;", template)
        self.assertIn("loadNodeTrafficForForm(form);", template)
        self.assertIn("function loadNodeTraffic(event)", template)


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

        # запрос к rwms ушёл с началом в полночь — суточный бакет 9 июля
        # (created_at = 09.07 00:00) не будет отброшен фильтром панели
        self.assertEqual(captured["start"], datetime(2026, 7, 9, 0, 0))
        self.assertEqual(report["start"], "2026-07-09 00:00")


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
        self.assertIn(
            "r.invoice_clicks ? (100 * r.payments / r.invoice_clicks).toFixed(1) + '%' : '—'",
            template,
        )
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
        self.assertNotIn("Сохранить параметры", self.template)

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
