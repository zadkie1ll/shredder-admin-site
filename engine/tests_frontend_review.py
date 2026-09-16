"""Фронтенд-гарды по ревью аудита 2026-09-10 (группа L3).

Только шаблоны и статика: рендер через Django template loader с фиктивным
контекстом и собственным минимальным URLconf — без БД, сети и импорта
engine.views. Поведение JS проверяют node-тесты
engine/tests_js/frontend_review.test.cjs.
"""
import re
import subprocess
import sys
from pathlib import Path

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings
from django.urls import path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "engine" / "templates"
STATIC = ROOT / "engine" / "static"


def _stub_view(request, *args, **kwargs):
    return HttpResponse("")


# Шаблонам нужны только имена маршрутов; реальные имена сверяет
# test_template_route_names_exist_in_project_urls.
urlpatterns = [
    path("dashboard/", _stub_view, name="dashboard"),
    path("login/", _stub_view, name="login"),
    path("login/google/", _stub_view, name="google_login"),
    path("login/yandex/", _stub_view, name="yandex_login"),
]

NEUTRAL_ROLES = ("vps", "vps_direct_sale")
# Словарь запретов для нейтральных VPS-доменов: README «Лендинги и платный flow»
# плюс формулировки, которые были в онбординге (белые списки, реклама, банки).
VPS_FORBIDDEN_WORDS = (
    "vpn",
    "обход",
    "блокир",
    "цензур",
    "шифрован",
    "приватн",
    "любые сайты",
    "роутер",
    "белый список",
    "белых спис",
    "youtube",
    "реклам",
    "госуслуг",
    "банк",
)

SLIDE_RE = re.compile(r'class="cabinet-onboarding-slide(?: is-active)?"')
INLINE_SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S)
LEGACY_UNSAFE_SYNTAX_RE = re.compile(r"\?\.(?!\d)|\?\?|\bglobalThis\b")


def inline_scripts(template_name):
    source = (TEMPLATES / template_name).read_text()
    return [
        match.group("body")
        for match in INLINE_SCRIPT_RE.finditer(source)
        if "src=" not in match.group("attrs")
    ]


def static_path(url):
    prefix = "/static/"
    assert url.startswith(prefix), url
    return STATIC / url[len(prefix):]


@override_settings(ROOT_URLCONF="engine.tests_frontend_review")
class LoginOnboardingNeutralCopyTests(SimpleTestCase):
    """F4 / AUTH-08: онбординг /login/ на нейтральных VPS-доменах."""

    def render_login(self, site_role):
        return render_to_string(
            "login.html",
            {
                "site_role": site_role,
                "login_onboarding_enabled": site_role != "cabinet",
                "tracking_params": {},
                "telegram_bot_login_enabled": False,
                "google_oauth_enabled": False,
                "yandex_oauth_enabled": False,
                "pwa_mirror_source_url": "",
                "telegram_auth_url": "https://cab.example.com/auth/telegram/",
                "users_count": 0,
            },
        )

    @staticmethod
    def onboarding_block(html):
        start = html.index('id="loginOnboarding"')
        return html[start:html.index('id="app-container"', start)]

    @staticmethod
    def onboarding_image_urls(html):
        # Картинки слайдов и фоновые ассеты страницы (внешние пиксели метрики не в счёт).
        urls = re.findall(r'\bsrc="(/static/img/onboarding/[^"]+)"', html)
        for srcset in re.findall(r'\bsrcset="([^"]+)"', html):
            urls.extend(item.strip().split()[0] for item in srcset.split(","))
        urls.extend(re.findall(r'url\("(/static/img/onboarding/[^"]+)"\)', html))
        return urls

    def test_neutral_roles_show_five_strictly_neutral_slides(self):
        for role in NEUTRAL_ROLES:
            with self.subTest(role=role):
                block = self.onboarding_block(self.render_login(role))
                self.assertEqual(len(SLIDE_RE.findall(block)), 5)
                self.assertEqual(block.count("cabinet-onboarding-slide is-active"), 1)
                lowered = block.lower()
                for word in VPS_FORBIDDEN_WORDS:
                    self.assertNotIn(word, lowered, f"{role}: {word}")
                for expected in (
                    "Безлимитный трафик",
                    "до 10 Гбит/с",
                    "локацию",
                    "до 15 устройств",
                    "поддержка",
                ):
                    self.assertIn(expected, block)

    def test_neutral_roles_login_page_has_no_forbidden_wording_or_asset_names(self):
        for role in NEUTRAL_ROLES:
            with self.subTest(role=role):
                html = self.render_login(role)
                lowered = html.lower()
                for word in VPS_FORBIDDEN_WORDS:
                    self.assertNotIn(word, lowered, f"{role}: {word}")
                for name in ("no-ads", "open-internet", "family-vpn", "smart-switch", "always-on"):
                    self.assertNotIn(name, html, f"{role}: {name}")

    def test_onboarding_images_exist_for_every_role(self):
        for role in ("vpn",) + NEUTRAL_ROLES:
            with self.subTest(role=role):
                urls = self.onboarding_image_urls(self.render_login(role))
                self.assertGreaterEqual(len(urls), 17)
                for url in urls:
                    self.assertTrue(static_path(url).is_file(), url)

    def test_vpn_role_keeps_direct_onboarding(self):
        block = self.onboarding_block(self.render_login("vpn"))

        self.assertEqual(len(SLIDE_RE.findall(block)), 5)
        for expected in (
            "VPN для всей семьи и ваших устройств",
            "Автоматическое переключение",
            "Белый список — не проблема",
            "YouTube без рекламы",
            "Работает, когда другие VPN нет",
            "img/onboarding/no-ads-480.webp",
        ):
            self.assertIn(expected, block)

    def test_cabinet_role_has_no_onboarding(self):
        self.assertNotIn('id="loginOnboarding"', self.render_login("cabinet"))

    def test_magic_link_form_fallback_does_not_put_email_into_url(self):
        # Если JS не выполнился, нативная отправка не должна быть GET с email в URL.
        html = self.render_login("cabinet")
        self.assertIn('<form id="magicLinkForm" method="post"', html)


class LegacyBrowserSyntaxGuardTests(SimpleTestCase):
    """F3: вход, оплата и статус оплаты разбираются браузерами уровня ES2019."""

    templates = (
        "login.html",
        "index_vpn.html",
        "index_vps.html",
        "index_vps_direct_sale.html",
        "payment_status.html",
        "dashboard.html",
        "support_admin_ticket_detail.html",
    )
    static_scripts = ("mi-network.js", "scripts/support_admin_ticket_detail-1.js", "js/cabinet-mobile.js")

    def sources(self):
        for name in self.templates:
            for index, body in enumerate(inline_scripts(name)):
                yield f"{name}#script{index}", body
        for name in self.static_scripts:
            yield name, (STATIC / name).read_text()

    def test_no_optional_chaining_nullish_or_global_this(self):
        for label, code in self.sources():
            with self.subTest(source=label):
                match = LEGACY_UNSAFE_SYNTAX_RE.search(code)
                context = code[max(0, match.start() - 80):match.end() + 20] if match else ""
                self.assertIsNone(match, f"{label}: {context!r}")

    def test_abort_controller_is_created_only_after_feature_check(self):
        found = 0
        for label, code in self.sources():
            for match in re.finditer(r"new AbortController\(\)", code):
                found += 1
                with self.subTest(source=label, offset=match.start()):
                    preceding = code[max(0, match.start() - 160):match.start()]
                    self.assertIn("typeof AbortController === 'function'", preceding)
        self.assertGreater(found, 0)

    def test_login_email_focus_has_no_optional_chaining(self):
        script = "\n".join(inline_scripts("login.html"))
        self.assertIn("if (emailField) emailField.focus({preventScroll: true});", script)
        self.assertNotIn("getElementById('email')?.", script)


class PaymentLaunchErrorHandlingTemplateTests(SimpleTestCase):
    """F7: запуск оплаты не показывает SyntaxError при не-JSON ответе."""

    def test_payment_handlers_check_content_type_before_parsing_json(self):
        for name in ("index_vpn.html", "index_vps.html", "index_vps_direct_sale.html", "dashboard.html"):
            with self.subTest(template=name):
                template = (TEMPLATES / name).read_text()
                self.assertNotIn("const payload = await response.json();", template)
                self.assertIn("contentType.indexOf('application/json') !== -1", template)
                self.assertIn(
                    "'Не удалось открыть оплату. Обновите страницу и попробуйте ещё раз'",
                    template,
                )
                self.assertIn("(payload && payload.message) || paymentLaunchFallbackMessage", template)
                self.assertIn("if (payload.confirmation_required) {", template)


class SupportTicketTemplateTests(SimpleTestCase):
    """SUP-04 и F2: страница тикета и отправка вложений."""

    def test_server_rendered_ticket_messages_are_known_to_refresh(self):
        template = (TEMPLATES / "support_admin_ticket_detail.html").read_text()
        script = (STATIC / "scripts" / "support_admin_ticket_detail-1.js").read_text()

        self.assertIn('data-message-id="{{ message.id }}"', template)
        self.assertIn("<div data-empty-messages ", template)
        self.assertIn("querySelectorAll('[data-message-id]')", script)
        self.assertIn("querySelector('[data-empty-messages]')", script)

    def test_support_uploads_use_long_timeout_and_polling_stays_short(self):
        sources = {
            "admin": (STATIC / "scripts" / "support_admin_ticket_detail-1.js").read_text(),
            "dashboard": (TEMPLATES / "dashboard.html").read_text(),
        }
        for label, source in sources.items():
            with self.subTest(source=label):
                start = source.index("MiNetwork.requestJSON(form.action, {")
                call = source[start:source.index(");", start) + 2]
                self.assertIn("method: 'POST'", call)
                self.assertTrue(call.endswith("}, 180000);"), call)
                poll_start = source.index("MiNetwork.requestJSON(form.dataset.messagesUrl, {")
                poll = source[poll_start:source.index(");", poll_start) + 2]
                self.assertNotIn("180000", poll)


@override_settings(ROOT_URLCONF="engine.tests_frontend_review")
class PaymentStatusTemplateTests(SimpleTestCase):
    """F6 и PAY-09: страница статуса оплаты."""

    def render_status(self, status, login_url="", message="", terminal=False):
        return render_to_string(
            "payment_status.html",
            {
                "initial_status": status,
                "initial_message": message,
                "initial_terminal": terminal,
                "login_url": login_url,
                "payment_url": "",
                "status_api_url": "/payment/status/token/json/",
                "support_telegram_url": "https://t.me/support",
            },
        )

    @staticmethod
    def tag(html, element_id):
        match = re.search(r'<(?:a|p|button)\b[^>]*\bid="%s"[^>]*>' % re.escape(element_id), html)
        assert match, element_id
        return match.group(0)

    @staticmethod
    def is_hidden(tag):
        return re.search(r"\shidden(?=[\s>])", tag) is not None

    def test_hidden_attribute_beats_button_display(self):
        css = (STATIC / "styles" / "payment_status-1.css").read_text()
        template = (TEMPLATES / "payment_status.html").read_text()

        self.assertIn("[hidden] { display: none !important; }", css)
        self.assertIn('.login-action[href=""] { display: none !important; }', css)
        self.assertIn('<button id="poll-retry" type="button" class="btn btn-support" hidden>', template)

    def test_success_without_login_url_links_to_login_page(self):
        html = self.render_status("succeeded")

        login_action = self.tag(html, "login-action")
        login_page_action = self.tag(html, "login-page-action")
        self.assertTrue(self.is_hidden(login_action))
        self.assertFalse(self.is_hidden(login_page_action))
        self.assertIn('href="/login/"', login_page_action)
        self.assertFalse(self.is_hidden(self.tag(html, "success-login-copy")))
        self.assertTrue(self.is_hidden(self.tag(html, "success-redirect-copy")))
        self.assertIn("Оплата прошла. Войдите в личный кабинет по email или вернитесь в Telegram", html)

    def test_success_with_login_url_keeps_cabinet_button(self):
        html = self.render_status("succeeded", login_url="/dashboard/")

        login_action = self.tag(html, "login-action")
        self.assertFalse(self.is_hidden(login_action))
        self.assertIn('href="/dashboard/"', login_action)
        self.assertTrue(self.is_hidden(self.tag(html, "login-page-action")))
        self.assertFalse(self.is_hidden(self.tag(html, "success-redirect-copy")))
        self.assertTrue(self.is_hidden(self.tag(html, "success-login-copy")))

    def test_success_without_session_shows_server_message(self):
        # R03: после анонимной покупки ссылка входа ушла письмом — текст приходит
        # из payment_status_payload (как failed-copy), email на странице не выводится.
        message = (
            "Оплата прошла. Ссылка для входа в личный кабинет отправлена на email, "
            "указанный при оплате. Если письма нет, войдите по email на странице входа."
        )
        html = self.render_status("succeeded", message=message)

        copy = html[html.index('id="success-login-copy"'):]
        copy = copy[:copy.index("</p>")]
        self.assertIn(message, copy)
        self.assertNotIn("Войдите в личный кабинет по email", copy)
        self.assertFalse(self.is_hidden(self.tag(html, "success-login-copy")))
        self.assertFalse(self.is_hidden(self.tag(html, "login-page-action")))

    def test_success_script_takes_message_only_without_login_url(self):
        template = (TEMPLATES / "payment_status.html").read_text()
        script = template[template.index("function applyStatus"):]
        script = script[:script.index("if (status === 'failed')")]
        # Текст заменяется только в ветке без login_url и только непустым message.
        self.assertIn(
            "} else if (payload.message) {\n"
            "                    // Как failed-copy: текст без сессии владельца приходит из\n"
            "                    // сервера (например, «ссылка для входа отправлена на email»).\n"
            "                    successLoginCopy.textContent = payload.message;",
            script,
        )

    @staticmethod
    def orb_wrap(html):
        match = re.search(r'<div\b[^>]*\bid="orb-wrap"[^>]*>', html)
        assert match, "orb-wrap"
        return match.group(0)

    def test_terminal_legacy_status_is_neutral_and_offers_login(self):
        # FE-FINAL-02: legacy-ссылка без строки или старше 7 дней — без
        # «ожидаем подтверждение», индикатора и опроса; ведём на вход.
        message = "Статус оплаты и подписку можно посмотреть в личном кабинете после входа"
        html = self.render_status("pending", message=message, terminal=True)

        self.assertIn('data-status="info"', html)
        self.assertIn(f'<h1 id="status-title">{message}</h1>', html)
        self.assertTrue(self.is_hidden(self.orb_wrap(html)))
        self.assertFalse(self.is_hidden(self.tag(html, "login-page-action")))
        for element_id in ("login-action", "payment-action", "cabinet-action"):
            self.assertTrue(self.is_hidden(self.tag(html, element_id)), element_id)
        self.assertIn("terminal:  'true' === 'true',", html)

    def test_ordinary_pending_render_is_unchanged_without_terminal(self):
        html = self.render_status("pending", message="Ждем подтверждения платежа")

        self.assertIn('data-status="pending"', html)
        self.assertIn('<h1 id="status-title">Проверяем платеж</h1>', html)
        self.assertFalse(self.is_hidden(self.orb_wrap(html)))
        self.assertTrue(self.is_hidden(self.tag(html, "login-page-action")))
        self.assertFalse(self.is_hidden(self.tag(html, "cabinet-action")))
        self.assertIn("terminal:  '' === 'true',", html)

    def test_pending_and_failed_hide_both_login_buttons(self):
        for status in ("pending", "failed"):
            with self.subTest(status=status):
                html = self.render_status(status)
                self.assertTrue(self.is_hidden(self.tag(html, "login-action")))
                self.assertTrue(self.is_hidden(self.tag(html, "login-page-action")))

    def test_template_route_names_exist_in_project_urls(self):
        urls = (ROOT / "engine" / "urls.py").read_text()
        self.assertIn('name="login"', urls)
        self.assertIn('name="dashboard"', urls)


class DashboardPaymentEmailCopyTests(SimpleTestCase):
    """PAY-04 (тексты): email при оплате — для чека, вход подтверждается письмом."""

    def test_payment_email_field_mentions_receipt_and_confirmation_letter(self):
        template = (TEMPLATES / "dashboard.html").read_text()

        self.assertNotIn("Email для чека и входа", template)
        self.assertEqual(template.count('placeholder="Email для чека"'), 3)
        # Письмо подтверждения уходит не всегда (у аккаунта с email его нет),
        # поэтому подсказка не обещает письмо, а объясняет, зачем подтверждать.
        self.assertNotIn("На этот адрес придёт письмо для подтверждения входа по email", template)
        self.assertEqual(
            template.count(
                "Адрес для чека. Чтобы входить на сайт по email, подтвердите его по ссылке из письма."
            ),
            3,
        )
        for hint_id in ("plans-email-hint", "renew-email-hint", "renew-card-email-hint-{{ tariff.db_tariff_id }}"):
            self.assertIn(f'aria-describedby="{hint_id}"', template)
            self.assertIn(f'id="{hint_id}"', template)


class LandingPurchaseCopyTests(SimpleTestCase):
    """R03: прямая покупка с лендинга — кабинет после оплаты сам не открывается
    (сессия анониму не выдаётся), ссылка для входа приходит письмом."""

    LANDINGS = ("index_vpn.html", "index_vps.html", "index_vps_direct_sale.html")
    BROKEN_PROMISES = (
        "сразу откроется личный кабинет",
        "откроется личный кабинет",
        "откроем личный кабинет",
        "кабинет с ключами откроется сразу",
        "сразу откройте личный кабинет",
    )

    def test_landing_copy_promises_login_link_by_email(self):
        for name in self.LANDINGS:
            with self.subTest(template=name):
                template = (TEMPLATES / name).read_text()
                for phrase in self.BROKEN_PROMISES:
                    self.assertNotIn(phrase, template)
                self.assertIn(
                    "После оплаты ссылка для входа в личный кабинет придёт на email.",
                    template,
                )
                # Прямая покупка сохранена: форма просит постоянную ссылку входа
                # письмом и по-прежнему понимает старый ответ confirmation_required.
                self.assertIn('name="login_link_kind" value="purchase_permanent"', template)
                self.assertIn("if (payload.confirmation_required) {", template)


class CabinetTemplateGuardScriptTests(SimpleTestCase):
    """TR-07: лёгкий скрипт template guards снова запускается без БД."""

    def test_run_cabinet_template_guards_script_passes(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "run_cabinet_template_guards.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-4000:])
