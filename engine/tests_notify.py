# Маршрутизация алертов сайта (ТСПУ, монитор серверов) по темам форума —
# те же ключи system_settings, что у бота: admin_alert_chat_id и
# admin_alert_topics. Пустой chat_id — прежний env-список TELEGRAM_ALERT_CHAT_ID.

import json
from unittest import mock

import httpx
from django.test import SimpleTestCase, override_settings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from common.models.db import Base, SystemSetting
from common.models.settings import ADMIN_ALERT_CHAT_ID_SETTING
from common.models.settings import ADMIN_ALERT_TOPICS_SETTING
from engine import notify

GROUP = -1004221070028
# Настоящий класс клиента: в тестах notify.httpx.Client подменяется, а фейку
# нужен именно оригинал, иначе он вызывает сам себя.
RealClient = httpx.Client


class _Telegram:
    """Фейковый Bot API: журнал payload'ов и настраиваемые ответы."""

    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder or (lambda payload: httpx.Response(200))

    def handler(self, request):
        payload = json.loads(request.content)
        self.calls.append(payload)
        response = self.responder(payload)
        response.request = request
        return response

    def client(self):
        return RealClient(transport=httpx.MockTransport(self.handler))


def _patched(telegram):
    # Новый клиент на каждый вызов: `with httpx.Client()` закрывает его,
    # а один алерт может уйти в несколько запросов (тема -> лента -> env).
    return mock.patch.object(notify.httpx, "Client", side_effect=lambda **kw: telegram.client())


class ParseTests(SimpleTestCase):
    def test_chat_id(self):
        self.assertIsNone(notify.parse_admin_alert_chat_id(None))
        self.assertIsNone(notify.parse_admin_alert_chat_id("  "))
        self.assertIsNone(notify.parse_admin_alert_chat_id("abc"))
        self.assertEqual(notify.parse_admin_alert_chat_id(" -100123 "), -100123)

    def test_topics_skip_broken_chunks(self):
        parsed = notify.parse_admin_alert_topics(
            "ipguard:11, Health:15,traffic:x,reports,:4,geo:0,,"
        )
        self.assertEqual(parsed, {"ipguard": 11, "health": 15})
        self.assertEqual(notify.parse_admin_alert_topics(None), {})


class RouteFromDbTests(SimpleTestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine, tables=[SystemSetting.__table__])
        self.session = sessionmaker(bind=engine)()
        # LIFO: сначала закрыть сессию, потом движок.
        self.addCleanup(engine.dispose)
        self.addCleanup(self.session.close)

    def _set(self, key, value):
        self.session.add(SystemSetting(key=key, value=value))
        self.session.commit()

    def test_empty_settings_mean_env_route(self):
        route = notify.admin_alert_route(self.session)
        self.assertFalse(route.to_group)

    def test_group_with_health_topic(self):
        self._set(ADMIN_ALERT_CHAT_ID_SETTING, str(GROUP))
        self._set(ADMIN_ALERT_TOPICS_SETTING, "ipguard:11,health:15")
        route = notify.admin_alert_route(self.session)
        self.assertEqual(route, notify.AdminAlertRoute(chat_id=GROUP, thread_id=15))

    def test_group_without_own_topic_goes_to_general(self):
        self._set(ADMIN_ALERT_CHAT_ID_SETTING, str(GROUP))
        self._set(ADMIN_ALERT_TOPICS_SETTING, "ipguard:11")
        route = notify.admin_alert_route(self.session)
        self.assertEqual(route, notify.AdminAlertRoute(chat_id=GROUP, thread_id=None))

    def test_load_route_survives_db_error(self):
        with mock.patch("database.session_factory", side_effect=RuntimeError("db down")):
            self.assertEqual(notify.load_admin_alert_route(), notify.AdminAlertRoute())


@override_settings(TELEGRAM_ALERT_BOT_TOKEN="t0k", TELEGRAM_ALERT_CHAT_ID="1,2")
class SendTests(SimpleTestCase):
    def test_env_recipients_when_no_group_configured(self):
        telegram = _Telegram()
        with _patched(telegram):
            ok = notify.send_admin_telegram_alert(
                "hi", route=notify.AdminAlertRoute()
            )
        self.assertTrue(ok)
        self.assertEqual([c["chat_id"] for c in telegram.calls], ["1", "2"])
        self.assertTrue(all("message_thread_id" not in c for c in telegram.calls))

    def test_group_topic_gets_single_message(self):
        telegram = _Telegram()
        with _patched(telegram):
            ok = notify.send_admin_telegram_alert(
                "<b>x</b>", route=notify.AdminAlertRoute(chat_id=GROUP, thread_id=15)
            )
        self.assertTrue(ok)
        self.assertEqual(len(telegram.calls), 1)
        call = telegram.calls[0]
        self.assertEqual(call["chat_id"], GROUP)
        self.assertEqual(call["message_thread_id"], 15)
        self.assertEqual(call["parse_mode"], "HTML")
        self.assertEqual(call["text"], "<b>x</b>")

    def test_closed_topic_retries_in_general(self):
        def responder(payload):
            if "message_thread_id" in payload:
                return httpx.Response(400, text='{"description":"TOPIC_CLOSED"}')
            return httpx.Response(200)

        telegram = _Telegram(responder)
        with _patched(telegram):
            ok = notify.send_admin_telegram_alert(
                "x", route=notify.AdminAlertRoute(chat_id=GROUP, thread_id=15)
            )
        self.assertTrue(ok)
        self.assertEqual([c["chat_id"] for c in telegram.calls], [GROUP, GROUP])
        self.assertNotIn("message_thread_id", telegram.calls[1])

    def test_unreachable_group_falls_back_to_env(self):
        def responder(payload):
            if payload["chat_id"] == GROUP:
                return httpx.Response(403, text='{"description":"kicked"}')
            return httpx.Response(200)

        telegram = _Telegram(responder)
        with _patched(telegram):
            ok = notify.send_admin_telegram_alert(
                "x", route=notify.AdminAlertRoute(chat_id=GROUP, thread_id=15)
            )
        self.assertTrue(ok)
        # тема -> общая лента -> env-получатели
        self.assertEqual(
            [c["chat_id"] for c in telegram.calls], [GROUP, GROUP, "1", "2"]
        )

    def test_network_error_is_not_raised(self):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        client = RealClient(transport=httpx.MockTransport(handler))
        with mock.patch.object(notify.httpx, "Client", return_value=client):
            ok = notify.send_admin_telegram_alert(
                "x", route=notify.AdminAlertRoute(chat_id=GROUP, thread_id=15)
            )
        self.assertFalse(ok)

    def test_route_is_read_from_db_by_default(self):
        telegram = _Telegram()
        with _patched(telegram), mock.patch.object(
            notify,
            "load_admin_alert_route",
            return_value=notify.AdminAlertRoute(chat_id=GROUP, thread_id=15),
        ) as loader:
            notify.send_admin_telegram_alert("x")
        loader.assert_called_once_with(notify.ADMIN_ALERT_HEALTH)
        self.assertEqual(telegram.calls[0]["message_thread_id"], 15)

    @override_settings(TELEGRAM_ALERT_BOT_TOKEN="")
    def test_no_token_means_disabled(self):
        telegram = _Telegram()
        with _patched(telegram):
            self.assertFalse(notify.send_admin_telegram_alert("x"))
        self.assertEqual(telegram.calls, [])
