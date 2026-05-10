import hashlib
import hmac
import time

from django.test import RequestFactory
from django.test import SimpleTestCase
from django.test import override_settings

from engine.views import auth_by_telegram_widget
from engine.views import get_telegram_auth_bot
from engine.views import render_login
from engine.views import verify_telegram_widget_auth


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
    def test_telegram_return_to_points_to_login_page(self):
        request = self.factory.get(
            "/login/",
            HTTP_HOST="monkey-island-vpn.com",
            secure=True,
        )
        request.session = {}

        response = render_login(request)
        content = response.content.decode()

        self.assertIn(
            "return_to=https%3A%2F%2Fmonkey-island-vpn.com%2Flogin%2F", content
        )
