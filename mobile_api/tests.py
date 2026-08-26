import json
from datetime import datetime, timedelta
from unittest import mock

from django.test import SimpleTestCase
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from common.models.db import (
    Base,
    MobileAccessToken,
    MobileAuthCode,
    EmailLoginCode,
    User,
    UserBlock,
)
from mobile_api import auth


# Tests only: the models use BigInteger+Sequence PKs (PostgreSQL). On SQLite a
# BIGINT PK does not autoincrement — only "INTEGER PRIMARY KEY" does — so compile
# BigInteger as INTEGER here to get autoincrementing ids in the in-memory DB.
@compiles(BigInteger, "sqlite")
def _compile_bigint_as_integer_on_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


class _Req:
    """Minimal stand-in for a Django request (only ``.META`` is used by auth)."""

    def __init__(self, token=None):
        self.META = {}
        if token is not None:
            self.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"


class MobileAuthHelpersTests(SimpleTestCase):
    def setUp(self):
        # One shared in-memory SQLite DB across all sessions (StaticPool).
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(
            self.engine,
            tables=[
                User.__table__,
                MobileAuthCode.__table__,
                MobileAccessToken.__table__,
                UserBlock.__table__,
            ],
        )
        self.Session = sessionmaker(bind=self.engine)
        session = self.Session()
        session.add(User(id=1, username="u1", telegram_id=1001))
        session.commit()
        session.close()

    def test_hashes_are_deterministic_and_distinct(self):
        self.assertEqual(auth.hash_auth_code("abc"), auth.hash_auth_code("abc"))
        self.assertNotEqual(auth.hash_auth_code("abc"), auth.hash_auth_code("abd"))
        # Code-hash and token-hash namespaces must not collide.
        self.assertNotEqual(auth.hash_auth_code("abc"), auth.hash_access_token("abc"))

    def test_register_then_exchange_roundtrip(self):
        session = self.Session()
        auth.register_auth_code(session, user_id=1, code="code-1")
        session.commit()
        user, token = auth.exchange_code(session, "code-1")
        session.commit()
        self.assertIsNotNone(user)
        self.assertEqual(user.id, 1)
        self.assertTrue(token)
        self.assertEqual(session.query(MobileAccessToken).count(), 1)
        session.close()

    def test_code_is_one_time(self):
        session = self.Session()
        auth.register_auth_code(session, user_id=1, code="code-2")
        session.commit()
        user1, _ = auth.exchange_code(session, "code-2")
        session.commit()
        user2, token2 = auth.exchange_code(session, "code-2")
        session.commit()
        self.assertIsNotNone(user1)
        self.assertIsNone(user2)  # already used
        self.assertIsNone(token2)
        session.close()

    def test_expired_code_rejected(self):
        session = self.Session()
        past = datetime.utcnow() - timedelta(minutes=1)
        session.add(
            MobileAuthCode(
                user_id=1, code_hash=auth.hash_auth_code("old"), expires_at=past
            )
        )
        session.commit()
        user, token = auth.exchange_code(session, "old")
        self.assertIsNone(user)
        self.assertIsNone(token)
        session.close()

    def test_unknown_code_rejected(self):
        session = self.Session()
        user, token = auth.exchange_code(session, "does-not-exist")
        self.assertIsNone(user)
        self.assertIsNone(token)
        session.close()

    def test_is_user_blocked_fails_open_on_db_error(self):
        """Ошибка проверки блокировки (например, таблицы user_blocks ещё нет)
        не должна ронять кабинет/оплату/мобильный API — считаем незаблокированным."""
        from engine.user_block import is_user_blocked

        class BrokenSession:
            def query(self, model):
                raise RuntimeError("no such table: user_blocks")

        self.assertFalse(is_user_blocked(BrokenSession(), 1))

    def test_exchange_code_rejected_for_blocked_user(self):
        session = self.Session()
        session.add(UserBlock(user_id=1, reason="test block"))
        auth.register_auth_code(session, user_id=1, code="code-blocked")
        session.commit()

        user, token = auth.exchange_code(session, "code-blocked")

        self.assertIsNone(user)
        self.assertIsNone(token)
        self.assertEqual(session.query(MobileAccessToken).count(), 0)
        session.close()

    def test_authenticate_rejected_for_blocked_user(self):
        session = self.Session()
        auth.register_auth_code(session, user_id=1, code="code-pre-block")
        session.commit()
        _user, raw = auth.exchange_code(session, "code-pre-block")
        session.commit()
        self.assertIsNotNone(raw)

        # Блокировка после выдачи токена: существующий токен перестаёт работать
        session.add(UserBlock(user_id=1, reason="test block"))
        session.commit()

        user, row = auth.authenticate(session, _Req(raw))

        self.assertIsNone(user)
        self.assertIsNone(row)
        session.close()

    def test_authenticate_valid_revoked_unknown_missing(self):
        session = self.Session()
        auth.register_auth_code(session, user_id=1, code="c")
        session.commit()
        _user, raw = auth.exchange_code(session, "c")
        session.commit()

        # valid token -> resolves to the user, updates last_seen_at
        user, row = auth.authenticate(session, _Req(raw))
        session.commit()
        self.assertIsNotNone(user)
        self.assertEqual(user.id, 1)
        self.assertIsNotNone(row.last_seen_at)

        # unknown token
        u2, _ = auth.authenticate(session, _Req("garbage"))
        self.assertIsNone(u2)

        # missing header
        u3, _ = auth.authenticate(session, _Req())
        self.assertIsNone(u3)

        # revoked token
        row.revoked_at = datetime.utcnow()
        session.commit()
        u4, r4 = auth.authenticate(session, _Req(raw))
        self.assertIsNone(u4)
        self.assertIsNone(r4)
        session.close()


def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            MobileAuthCode.__table__,
            MobileAccessToken.__table__,
            EmailLoginCode.__table__,
            UserBlock.__table__,
        ],
    )
    return engine


class _FakeExpire:
    """Stand-in for a protobuf Timestamp (only ``ToDatetime`` is used)."""

    def __init__(self, dt):
        self._dt = dt

    def ToDatetime(self):  # noqa: N802 - mirrors protobuf API
        return self._dt


class _FakeRwUser:
    """Stand-in for proto.UserResponse (HasField/expire_at/email/subscription_url).

    ``email`` по умолчанию НЕ выставлен — это легитимная «наша» подписка без
    проставленной почты; тесты владельца передают его явно."""

    def __init__(
        self,
        expire_at=None,
        subscription_url="https://sub.example/abc",
        email=None,
        username="rw-user",
    ):
        self._expire_at = _FakeExpire(expire_at) if expire_at is not None else None
        self.subscription_url = subscription_url
        self.email = email or ""
        self.username = username

    @property
    def expire_at(self):
        return self._expire_at

    def HasField(self, name):  # noqa: N802 - mirrors protobuf API
        if name == "expire_at":
            return self._expire_at is not None
        if name == "email":
            return bool(self.email)
        return False


class EmailLoginCodeHelpersTests(SimpleTestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)

    def test_register_then_verify_roundtrip(self):
        session = self.Session()
        auth.register_email_code(session, "user@example.com", "123456")
        session.commit()
        ok, row = auth.verify_email_code(session, "user@example.com", "123456")
        session.commit()
        self.assertTrue(ok)
        self.assertIsNotNone(row.used_at)
        session.close()

    def test_email_codes_store_only_hash(self):
        session = self.Session()
        auth.register_email_code(session, "a@b.com", "654321")
        session.commit()
        row = session.query(EmailLoginCode).one()
        self.assertNotEqual(row.code_hash, "654321")
        self.assertEqual(row.code_hash, auth.hash_email_code("654321"))
        session.close()

    def test_wrong_code_increments_attempts(self):
        session = self.Session()
        auth.register_email_code(session, "a@b.com", "111111")
        session.commit()
        ok, row = auth.verify_email_code(session, "a@b.com", "999999")
        session.commit()
        self.assertFalse(ok)
        self.assertEqual(row.attempts, 1)
        # The right code still works after a wrong attempt (under the cap).
        ok2, _ = auth.verify_email_code(session, "a@b.com", "111111")
        self.assertTrue(ok2)
        session.close()

    def test_attempts_limit_locks_code(self):
        session = self.Session()
        auth.register_email_code(session, "a@b.com", "222222")
        session.commit()
        for _ in range(auth.EMAIL_CODE_MAX_ATTEMPTS):
            auth.verify_email_code(session, "a@b.com", "000000")
            session.commit()
        # Even the correct code is rejected once attempts hit the cap.
        ok, row = auth.verify_email_code(session, "a@b.com", "222222")
        self.assertFalse(ok)
        self.assertEqual(row.attempts, auth.EMAIL_CODE_MAX_ATTEMPTS)
        self.assertIsNone(row.used_at)
        session.close()

    def test_expired_code_rejected(self):
        session = self.Session()
        past = datetime.utcnow() - timedelta(minutes=1)
        session.add(
            EmailLoginCode(
                email="a@b.com",
                code_hash=auth.hash_email_code("333333"),
                created_at=datetime.utcnow() - timedelta(minutes=11),
                expires_at=past,
            )
        )
        session.commit()
        ok, row = auth.verify_email_code(session, "a@b.com", "333333")
        self.assertFalse(ok)
        self.assertIsNone(row)
        session.close()

    def test_used_code_cannot_be_reused(self):
        session = self.Session()
        auth.register_email_code(session, "a@b.com", "444444")
        session.commit()
        ok1, _ = auth.verify_email_code(session, "a@b.com", "444444")
        session.commit()
        ok2, row2 = auth.verify_email_code(session, "a@b.com", "444444")
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertIsNone(row2)  # no other unused code exists
        session.close()

    def test_verify_picks_newest_code(self):
        session = self.Session()
        now = datetime.utcnow()
        # Older code first, then a newer one — only the newest should verify.
        auth.register_email_code(
            session, "a@b.com", "555555", now=now - timedelta(minutes=2)
        )
        auth.register_email_code(session, "a@b.com", "666666", now=now)
        session.commit()
        ok_old, _ = auth.verify_email_code(session, "a@b.com", "555555")
        self.assertFalse(ok_old)  # newest row checked, attempts++ on it
        ok_new, _ = auth.verify_email_code(session, "a@b.com", "666666")
        self.assertTrue(ok_new)
        session.close()


class MobileEmailRequestViewTests(SimpleTestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        # Route every view session through the shared in-memory DB.
        self._sf_patch = mock.patch(
            "mobile_api.views.session_factory", side_effect=self.Session
        )
        self._sf_patch.start()
        # Stub the synchronous email send (same transport as magic-link) so no
        # network is touched. The view lazy-imports it from engine.views.
        self._send_patch = mock.patch("engine.views.send_login_code_email")
        self.send_email = self._send_patch.start()

    def tearDown(self):
        self._sf_patch.stop()
        self._send_patch.stop()

    def _post(self, body):
        from mobile_api import views

        request = mock.Mock()
        request.method = "POST"
        request.body = json.dumps(body).encode()
        return views.auth_email_request(request)

    def test_request_happy_path_sends_email(self):
        resp = self._post({"email": "New@Example.com"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["ok"])
        self.assertEqual(data["ttl_seconds"], 600)
        # One hashed code stored for the lowercased email; publisher called once.
        session = self.Session()
        rows = session.query(EmailLoginCode).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].email, "new@example.com")
        session.close()
        self.assertEqual(self.send_email.call_count, 1)
        args = self.send_email.call_args.args
        self.assertEqual(args[0], "new@example.com")
        self.assertRegex(args[1], r"^\d{6}$")  # zero-padded 6-digit code

    def test_invalid_email_rejected(self):
        resp = self._post({"email": "not-an-email"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(json.loads(resp.content)["error"], "invalid_email")
        self.send_email.assert_not_called()

    def test_resend_too_soon_rate_limited(self):
        first = self._post({"email": "a@b.com"})
        self.assertEqual(first.status_code, 200)
        second = self._post({"email": "a@b.com"})
        self.assertEqual(second.status_code, 429)
        self.assertEqual(json.loads(second.content)["error"], "rate_limited")
        self.assertEqual(self.send_email.call_count, 1)

    def test_hourly_limit_rate_limited(self):
        session = self.Session()
        now = datetime.utcnow()
        # Five codes spaced inside the last hour but older than the 60s window.
        for i in range(5):
            session.add(
                EmailLoginCode(
                    email="a@b.com",
                    code_hash=auth.hash_email_code(f"{i:06d}"),
                    created_at=now - timedelta(minutes=5 * (i + 1)),
                    expires_at=now + timedelta(minutes=10),
                )
            )
        session.commit()
        session.close()
        resp = self._post({"email": "a@b.com"})
        self.assertEqual(resp.status_code, 429)
        self.send_email.assert_not_called()


class MobileEmailVerifyViewTests(SimpleTestCase):
    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        self._sf_patch = mock.patch(
            "mobile_api.views.session_factory", side_effect=self.Session
        )
        self._sf_patch.start()
        # RWMS lookups (subscription_url for the response) and the trial client.
        self._getrw_patch = mock.patch(
            "mobile_api.views.get_rwms_user", return_value=_FakeRwUser()
        )
        self.get_rwms_user = self._getrw_patch.start()
        self._client_patch = mock.patch(
            "mobile_api.views.rwms_client", return_value=mock.Mock()
        )
        self.rwms_client = self._client_patch.start()

    def tearDown(self):
        self._sf_patch.stop()
        self._getrw_patch.stop()
        self._client_patch.stop()

    def _seed_code(self, email, code):
        session = self.Session()
        auth.register_email_code(session, email, code)
        session.commit()
        session.close()

    def _post(self, body):
        from mobile_api import views

        request = mock.Mock()
        request.method = "POST"
        request.body = json.dumps(body).encode()
        return views.auth_email_verify(request)

    def test_verify_new_user_provisions_trial(self):
        from mobile_api.provisioning import deterministic_username

        self._seed_code("new@example.com", "123456")
        expire = datetime.utcnow() + timedelta(days=7)
        with mock.patch(
            "mobile_api.provisioning.create_user",
            return_value=_FakeRwUser(expire_at=expire),
        ) as create_user:
            # Fresh username => strict lookup returns None, i.e. a confirmed
            # NOT_FOUND (safety check).
            client = self.rwms_client.return_value
            client.get_user_by_username_strict.return_value = None
            resp = self._post({"email": "New@Example.com", "code": "123456"})

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["access_token"])
        self.assertEqual(data["subscription_url"], "https://sub.example/abc")
        # RWMS create_user was called exactly once for the brand-new user, with
        # the DETERMINISTIC (adoptable-after-crash) username for this email.
        self.assertEqual(create_user.call_count, 1)
        _, kwargs = create_user.call_args
        self.assertEqual(kwargs["trial_period_days"], 7)
        self.assertEqual(kwargs["email"], "new@example.com")
        self.assertEqual(
            kwargs["username"], deterministic_username("new@example.com")
        )
        # Local user row created and a token issued.
        session = self.Session()
        user = session.query(User).filter(User.email == "new@example.com").one()
        self.assertEqual(user.username, deterministic_username("new@example.com"))
        self.assertEqual(data["user"]["id"], user.id)
        self.assertEqual(session.query(MobileAccessToken).count(), 1)
        session.close()

    def test_verify_existing_user_does_not_provision(self):
        # Legacy random username (the old uuid4().hex site generator): existing
        # users keep it forever — the deterministic scheme applies only to NEW
        # registrations, no path may recompute an existing username.
        legacy_username = "deadbeefdeadbeefdeadbeefdeadbeef"
        session = self.Session()
        session.add(User(id=42, username=legacy_username, email="old@example.com"))
        session.commit()
        session.close()
        self._seed_code("old@example.com", "123456")
        with mock.patch("mobile_api.provisioning.create_user") as create_user:
            resp = self._post({"email": "old@example.com", "code": "123456"})

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["user"]["id"], 42)
        self.assertEqual(data["user"]["username"], legacy_username)
        # CRITICAL: existing user must never touch RWMS create/recreate.
        create_user.assert_not_called()
        self.rwms_client.return_value.get_user_by_username_strict.assert_not_called()
        session = self.Session()
        self.assertEqual(session.query(MobileAccessToken).count(), 1)
        # The legacy username is untouched after login.
        self.assertEqual(
            session.query(User).filter(User.id == 42).one().username,
            legacy_username,
        )
        session.close()

    def test_verify_wrong_code_rejected(self):
        self._seed_code("a@b.com", "123456")
        resp = self._post({"email": "a@b.com", "code": "000000"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(json.loads(resp.content)["error"], "invalid_or_expired_code")
        # Attempt counter persisted.
        session = self.Session()
        self.assertEqual(session.query(EmailLoginCode).one().attempts, 1)
        session.close()

    def test_verify_expired_code_rejected(self):
        session = self.Session()
        past = datetime.utcnow() - timedelta(minutes=1)
        session.add(
            EmailLoginCode(
                email="a@b.com",
                code_hash=auth.hash_email_code("123456"),
                created_at=datetime.utcnow() - timedelta(minutes=11),
                expires_at=past,
            )
        )
        session.commit()
        session.close()
        resp = self._post({"email": "a@b.com", "code": "123456"})
        self.assertEqual(resp.status_code, 401)

    def test_verify_attempts_exhausted_rejected(self):
        session = self.Session()
        session.add(
            EmailLoginCode(
                email="a@b.com",
                code_hash=auth.hash_email_code("123456"),
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(minutes=10),
                attempts=auth.EMAIL_CODE_MAX_ATTEMPTS,
            )
        )
        session.commit()
        session.close()
        # Even the correct code is rejected once the cap is reached.
        resp = self._post({"email": "a@b.com", "code": "123456"})
        self.assertEqual(resp.status_code, 401)


class ExpireFieldsTests(SimpleTestCase):
    """days_left must round UP like the cabinet (engine/views.py), not truncate:
    on the last paid day (23h remaining) the app card must not read «истекла»."""

    def _days_left(self, expire_at):
        from mobile_api import views

        _iso, days_left = views._expire_fields(_FakeRwUser(expire_at=expire_at))
        return days_left

    def test_23h_remaining_is_one_day(self):
        self.assertEqual(
            self._days_left(datetime.utcnow() + timedelta(hours=23)), 1
        )

    def test_25h_remaining_is_two_days(self):
        self.assertEqual(
            self._days_left(datetime.utcnow() + timedelta(hours=25)), 2
        )

    def test_expired_is_zero(self):
        self.assertEqual(
            self._days_left(datetime.utcnow() - timedelta(hours=1)), 0
        )

    def test_no_rw_user_is_none(self):
        from mobile_api import views

        self.assertEqual(views._expire_fields(None), (None, None))


class MobileExchangeRateLimitTests(SimpleTestCase):
    """Per-IP throttle on POST auth/exchange (PLAN §3.2: rate limit against
    device-code brute force). 429 is treated as transient by the app's poller."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        self._sf_patch = mock.patch(
            "mobile_api.views.session_factory", side_effect=self.Session
        )
        self._sf_patch.start()

    def tearDown(self):
        self._sf_patch.stop()

    def _post(self, ip="10.0.0.1", forwarded=None):
        from mobile_api import views

        request = mock.Mock()
        request.method = "POST"
        request.body = json.dumps({"code": "deadbeef"}).encode()
        request.META = {"REMOTE_ADDR": ip}
        if forwarded is not None:
            request.META["HTTP_X_FORWARDED_FOR"] = forwarded
        return views.auth_exchange(request)

    def test_within_limit_unknown_code_is_401(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(
            json.loads(resp.content)["error"], "invalid_or_expired_code"
        )

    def test_over_limit_is_429(self):
        from mobile_api import views

        with mock.patch.object(views, "EXCHANGE_RATE_LIMIT", 3):
            for _ in range(3):
                self.assertEqual(self._post().status_code, 401)
            resp = self._post()
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(json.loads(resp.content)["error"], "rate_limited")

    def test_limit_is_per_ip(self):
        from mobile_api import views

        with mock.patch.object(views, "EXCHANGE_RATE_LIMIT", 3):
            for _ in range(4):
                self._post(ip="10.0.0.1")
            # A different client is not affected by the exhausted bucket.
            resp = self._post(ip="10.0.0.2")
        self.assertEqual(resp.status_code, 401)

    def test_forwarded_header_wins_over_remote_addr(self):
        from mobile_api import views

        with mock.patch.object(views, "EXCHANGE_RATE_LIMIT", 3):
            for _ in range(4):
                self._post(ip="127.0.0.1", forwarded="203.0.113.7")
            resp = self._post(ip="127.0.0.1", forwarded="203.0.113.7")
            self.assertEqual(resp.status_code, 429)
            # Same proxy, different original client → separate bucket.
            other = self._post(ip="127.0.0.1", forwarded="203.0.113.8")
        self.assertEqual(other.status_code, 401)


class MobileMeRwmsPolicyTests(SimpleTestCase):
    """GET /api/mobile/me и политика «БД — истина по времени, панель — истина
    по существованию ключа»: недоступность RWMS/панели — «временно недоступно»
    (503), а НЕ null-поля, которые приложение показало бы как «нет подписки»;
    достоверный NOT_FOUND — прежнее поведение (null-поля)."""

    def setUp(self):
        from types import SimpleNamespace

        self._sf_patch = mock.patch(
            "mobile_api.views.session_factory", return_value=mock.MagicMock()
        )
        self._sf_patch.start()
        self.user = SimpleNamespace(id=7, username="u7", telegram_id=None)
        self._auth_patch = mock.patch(
            "mobile_api.views.authenticate", return_value=(self.user, mock.Mock())
        )
        self._auth_patch.start()

    def tearDown(self):
        self._sf_patch.stop()
        self._auth_patch.stop()

    def _get(self):
        from mobile_api import views

        request = mock.Mock()
        request.method = "GET"
        request.META = {"HTTP_AUTHORIZATION": "Bearer token"}
        return views.me(request)

    def test_rwms_unavailable_returns_503_not_no_subscription(self):
        from common.rwms_client import RwmsUnavailableError

        with mock.patch(
            "mobile_api.views.get_rwms_user",
            side_effect=RwmsUnavailableError("u7", None, "panel down"),
        ):
            resp = self._get()
        self.assertEqual(resp.status_code, 503)
        data = json.loads(resp.content)
        self.assertEqual(data["error"], "temporarily_unavailable")
        # Ответ не должен маскироваться под «нет подписки».
        self.assertNotIn("subscription_url", data)
        self.assertNotIn("days_left", data)

    def test_confirmed_not_found_keeps_null_subscription_payload(self):
        with mock.patch("mobile_api.views.get_rwms_user", return_value=None):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIsNone(data["subscription_url"])
        self.assertIsNone(data["days_left"])
        self.assertIsNone(data["status"])

    def test_active_subscription_payload(self):
        expire = datetime.utcnow() + timedelta(days=3)
        with mock.patch(
            "mobile_api.views.get_rwms_user",
            return_value=_FakeRwUser(expire_at=expire),
        ):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["subscription_url"], "https://sub.example/abc")
        self.assertEqual(data["days_left"], 3)


class MobileAuthRwmsDegradationTests(SimpleTestCase):
    """Успешный вход не валится из-за блипа панели: токен выдаётся,
    subscription_url == null, приложение добирает его позже через /me."""

    def test_exchange_succeeds_with_null_url_when_rwms_unavailable(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from common.rwms_client import RwmsUnavailableError
        from mobile_api import views

        cache.clear()
        user = SimpleNamespace(id=1, username="u1", telegram_id=None)
        request = mock.Mock()
        request.method = "POST"
        request.body = json.dumps({"code": "abc"}).encode()
        request.META = {"REMOTE_ADDR": "10.0.0.9"}
        with (
            mock.patch(
                "mobile_api.views.session_factory", return_value=mock.MagicMock()
            ),
            mock.patch(
                "mobile_api.views.exchange_code", return_value=(user, "tok")
            ),
            mock.patch(
                "mobile_api.views.get_rwms_user",
                side_effect=RwmsUnavailableError("u1", None, "down"),
            ),
        ):
            resp = views.auth_exchange(request)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["access_token"], "tok")
        self.assertIsNone(data["subscription_url"])


class ProvisioningRwmsUnavailableTests(SimpleTestCase):
    """SAFETY: недоступность RWMS не считается ни «username свободен», ни
    «подписка существует» — при RwmsUnavailableError провижининг триала
    прерывается, AddUser не зовётся, adoption не выполняется."""

    def test_provision_trial_user_aborts_when_rwms_unavailable(self):
        from common.rwms_client import RwmsUnavailableError
        from mobile_api import provisioning

        client = mock.Mock()
        client.get_user_by_username_strict.side_effect = RwmsUnavailableError(
            "candidate", None, "down"
        )
        db_session = mock.Mock()
        with mock.patch("mobile_api.provisioning.create_user") as create_user:
            result = provisioning.provision_trial_user(
                db_session, client, "down@example.com"
            )
        self.assertIsNone(result)
        create_user.assert_not_called()
        client.add_user.assert_not_called()
        db_session.add.assert_not_called()


class DeterministicUsernameTests(SimpleTestCase):
    """Username мобильной email-регистрации — детерминированная функция от
    нормализованного email: повтор провижининга после крэша получает ТО ЖЕ имя
    и может принять уже созданную в панели подписку (см. adoption-тесты)."""

    def test_same_email_maps_to_same_username(self):
        from mobile_api.provisioning import deterministic_username

        self.assertEqual(
            deterministic_username("user@example.com"),
            deterministic_username("user@example.com"),
        )

    def test_normalization_case_and_whitespace(self):
        from mobile_api.provisioning import deterministic_username

        self.assertEqual(
            deterministic_username("User@Example.COM "),
            deterministic_username("user@example.com"),
        )

    def test_different_emails_map_to_different_usernames(self):
        from mobile_api.provisioning import deterministic_username

        self.assertNotEqual(
            deterministic_username("a@example.com"),
            deterministic_username("b@example.com"),
        )

    def test_format_is_panel_compatible_and_collision_free(self):
        import re

        from mobile_api.provisioning import deterministic_username

        username = deterministic_username("user@example.com")
        # Панель Remnawave: ^[a-zA-Z0-9_-]+$, 3..36 символов.
        self.assertRegex(username, r"^[a-zA-Z0-9_-]+$")
        self.assertLessEqual(len(username), 36)
        self.assertGreaterEqual(len(username), 3)
        # 'm' + hex: не совпадает ни с чисто цифровыми именами бота
        # (str(telegram_id)), ни с uuid4().hex сайта ('m' — не hex-символ),
        # ни с legacy 'mi_' фолбэком (второй символ 'i' — не hex).
        self.assertTrue(username.startswith("m"))
        self.assertEqual(len(username), 32)
        self.assertIsNotNone(re.fullmatch(r"m[0-9a-f]{31}", username))


class ProvisioningAdoptionTests(SimpleTestCase):
    """Закрытие crash-окна после AddUser: панель создала подписку, процесс упал
    до commit — строки users нет, advisory-лок отпущен. Повторная верификация
    выводит ТО ЖЕ детерминированное имя, strict-чтение находит подписку, и она
    ПРИНИМАЕТСЯ: строка users создаётся, панель не трогается (никакого второго
    AddUser) — орфан невозможен."""

    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)

    def test_retry_adopts_existing_subscription_without_second_adduser(self):
        from mobile_api import provisioning
        from mobile_api.provisioning import deterministic_username

        email = "crashed@example.com"
        expire = datetime.utcnow() + timedelta(days=7)
        client = mock.Mock()
        # «Панель создала, БД нет»: strict-чтение по детерминированному имени
        # возвращает уже существующую подписку.
        client.get_user_by_username_strict.return_value = _FakeRwUser(
            expire_at=expire,
            email=email,
            username=deterministic_username(email),
        )
        session = self.Session()
        with mock.patch("mobile_api.provisioning.create_user") as create_user:
            user = provisioning.provision_trial_user(session, client, email)
        session.commit()

        self.assertIsNotNone(user)
        self.assertEqual(user.email, email)
        self.assertEqual(user.username, deterministic_username(email))
        # Строгое чтение шло по детерминированному имени.
        client.get_user_by_username_strict.assert_called_once_with(
            deterministic_username(email)
        )
        # КРИТИЧНО: панель не тронута — ни второго AddUser, ни recreate.
        create_user.assert_not_called()
        client.add_user.assert_not_called()
        # expire_at перенят у существующей подписки.
        self.assertEqual(user.expire_at, expire)
        check = self.Session()
        self.assertEqual(check.query(User).filter(User.email == email).count(), 1)
        check.close()
        session.close()

    def test_confirmed_not_found_still_creates_via_adduser(self):
        from mobile_api import provisioning
        from mobile_api.provisioning import deterministic_username

        email = "fresh@example.com"
        expire = datetime.utcnow() + timedelta(days=7)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = None  # NOT_FOUND
        session = self.Session()
        with mock.patch(
            "mobile_api.provisioning.create_user",
            return_value=_FakeRwUser(expire_at=expire),
        ) as create_user:
            user = provisioning.provision_trial_user(session, client, email)
        session.commit()

        self.assertIsNotNone(user)
        self.assertEqual(user.username, deterministic_username(email))
        self.assertEqual(create_user.call_count, 1)
        self.assertEqual(
            create_user.call_args.kwargs["username"], deterministic_username(email)
        )
        session.close()

    def test_view_level_retry_after_crash_adopts_and_logs_in(self):
        """Сценарий crash-окна через сам verify-эндпоинт: пользователь снова
        запрашивает код и верифицируется — вход успешен, подписка принята,
        AddUser не вызывается."""
        from mobile_api.provisioning import deterministic_username

        email = "crashed2@example.com"
        expire = datetime.utcnow() + timedelta(days=7)
        session = self.Session()
        auth.register_email_code(session, email, "123456")
        session.commit()
        session.close()

        with (
            mock.patch(
                "mobile_api.views.session_factory", side_effect=self.Session
            ),
            mock.patch(
                "mobile_api.views.get_rwms_user", return_value=_FakeRwUser()
            ),
            mock.patch("mobile_api.views.rwms_client") as rwms_client,
            mock.patch("mobile_api.provisioning.create_user") as create_user,
        ):
            client = rwms_client.return_value
            client.get_user_by_username_strict.return_value = _FakeRwUser(
                expire_at=expire,
                email=email,
                username=deterministic_username(email),
            )
            from mobile_api import views

            request = mock.Mock()
            request.method = "POST"
            request.body = json.dumps({"email": email, "code": "123456"}).encode()
            resp = views.auth_email_verify(request)

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["access_token"])
        self.assertEqual(data["user"]["username"], deterministic_username(email))
        create_user.assert_not_called()
        client.add_user.assert_not_called()
        session = self.Session()
        user = session.query(User).filter(User.email == email).one()
        self.assertEqual(user.username, deterministic_username(email))
        self.assertEqual(session.query(MobileAccessToken).count(), 1)
        session.close()


class LockEmailTests(SimpleTestCase):
    """``lock_email`` — сериализация конкурентных verify по email через
    transaction-scoped advisory lock Postgres (авто-освобождение на
    commit/rollback). На не-Postgres бэкендах (тестовый SQLite) — no-op."""

    def test_postgres_dialect_takes_pg_advisory_xact_lock(self):
        session = mock.Mock()
        session.get_bind.return_value.dialect.name = "postgresql"
        auth.lock_email(session, "user@example.com")
        self.assertEqual(session.execute.call_count, 1)
        clause, params = session.execute.call_args.args
        # Транзакционный (xact) лок по хэшу email: держится до конца
        # транзакции провижининга и снимается сам даже при откате.
        self.assertIn("pg_advisory_xact_lock(hashtext(:email))", str(clause))
        self.assertEqual(params, {"email": "user@example.com"})

    def test_non_postgres_dialect_is_noop(self):
        session = mock.Mock()
        session.get_bind.return_value.dialect.name = "sqlite"
        auth.lock_email(session, "user@example.com")
        session.execute.assert_not_called()


class MobileEmailVerifyConcurrencyTests(SimpleTestCase):
    """Регрессия на «конкурентная email-регистрация создаёт orphan-подписку в
    RWMS»: verify сериализуется per-email advisory-локом, взятым В НАЧАЛЕ
    транзакции — до проверки кода, до проверки существования пользователя и до
    ЛЮБЫХ обращений в RWMS. Победитель провижинит (ровно один AddUser),
    проигравший после ожидания видит уже созданного пользователя (или
    потреблённый код) и в RWMS не ходит — орфан невозможен."""

    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)
        self._sf_patch = mock.patch(
            "mobile_api.views.session_factory", side_effect=self.Session
        )
        self._sf_patch.start()
        self._getrw_patch = mock.patch(
            "mobile_api.views.get_rwms_user", return_value=_FakeRwUser()
        )
        self._getrw_patch.start()
        self._client_patch = mock.patch(
            "mobile_api.views.rwms_client", return_value=mock.Mock()
        )
        self.rwms_client = self._client_patch.start()

    def tearDown(self):
        self._sf_patch.stop()
        self._getrw_patch.stop()
        self._client_patch.stop()

    def _seed_code(self, email, code):
        session = self.Session()
        auth.register_email_code(session, email, code)
        session.commit()
        session.close()

    def _post(self, body):
        from mobile_api import views

        request = mock.Mock()
        request.method = "POST"
        request.body = json.dumps(body).encode()
        return views.auth_email_verify(request)

    def test_lock_taken_before_code_check_and_any_rwms_call(self):
        """Порядок внутри verify: lock -> проверка кода -> strict-проверка
        username в RWMS -> AddUser. Лок первым — иначе окно гонки открыто."""
        self._seed_code("order@example.com", "123456")
        order = []
        real_verify = auth.verify_email_code

        def tracking_verify(*args, **kwargs):
            order.append("verify")
            return real_verify(*args, **kwargs)

        expire = datetime.utcnow() + timedelta(days=7)

        def tracking_create(*args, **kwargs):
            order.append("rwms_create")
            return _FakeRwUser(expire_at=expire)

        client = self.rwms_client.return_value
        # None == подтверждённый NOT_FOUND (username свободен).
        client.get_user_by_username_strict.side_effect = (
            lambda *a, **k: order.append("rwms_check")
        )
        with (
            mock.patch(
                "mobile_api.views.lock_email",
                side_effect=lambda db_session, email: order.append("lock"),
            ),
            mock.patch(
                "mobile_api.views.verify_email_code", side_effect=tracking_verify
            ),
            mock.patch(
                "mobile_api.provisioning.create_user", side_effect=tracking_create
            ),
        ):
            resp = self._post({"email": "order@example.com", "code": "123456"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(order[0], "lock")
        self.assertLess(order.index("lock"), order.index("verify"))
        self.assertLess(order.index("verify"), order.index("rwms_check"))
        self.assertLess(order.index("rwms_check"), order.index("rwms_create"))

    def test_concurrent_same_code_single_adduser_no_orphan(self):
        """Двойная отправка одного кода (retry/двойной тап): под локом победитель
        провижинит и коммитит, проигравший «просыпается», видит потреблённый код
        и получает 401, НЕ дойдя до RWMS. Ровно один AddUser, одна строка users —
        орфан-подписки нет."""
        email = "race@example.com"
        self._seed_code(email, "123456")
        expire = datetime.utcnow() + timedelta(days=7)
        state = {}

        def fake_lock(db_session, addr):
            # Эмуляция pg_advisory_xact_lock на SQLite: первый пришедший сюда —
            # ПРОИГРАВШИЙ; победитель успел взять лок раньше и полностью
            # завершается (включая commit), пока проигравший «ждёт».
            if "winner" not in state:
                state["winner"] = "running"
                state["winner"] = self._post({"email": email, "code": "123456"})

        client = self.rwms_client.return_value
        client.get_user_by_username_strict.return_value = None
        with (
            mock.patch("mobile_api.views.lock_email", side_effect=fake_lock),
            mock.patch(
                "mobile_api.provisioning.create_user",
                return_value=_FakeRwUser(expire_at=expire),
            ) as create_user,
        ):
            loser_resp = self._post({"email": email, "code": "123456"})

        winner_resp = state["winner"]
        self.assertEqual(winner_resp.status_code, 200)
        # Код одноразовый: проигравший после ожидания получает 401…
        self.assertEqual(loser_resp.status_code, 401)
        # …но КРИТИЧНО: в RWMS ровно один AddUser и одна strict-проверка —
        # проигравший в панель не ходил, орфана нет.
        self.assertEqual(create_user.call_count, 1)
        self.assertEqual(client.get_user_by_username_strict.call_count, 1)
        session = self.Session()
        self.assertEqual(
            session.query(User).filter(User.email == email).count(), 1
        )
        self.assertEqual(session.query(MobileAccessToken).count(), 1)
        session.close()

    def test_concurrent_two_codes_loser_reuses_winner_user_without_rwms(self):
        """Если проигравший всё же проходит проверку кода (у него другой ещё
        активный код), он под локом перечитывает существование пользователя,
        видит созданного победителем и НЕ ходит в RWMS: оба запроса получают
        ОДНОГО пользователя, AddUser ровно один."""
        email = "race2@example.com"
        now = datetime.utcnow()
        session = self.Session()
        session.add(
            EmailLoginCode(
                email=email,
                code_hash=auth.hash_email_code("111111"),
                created_at=now - timedelta(seconds=5),
                expires_at=now + timedelta(minutes=10),
            )
        )
        session.add(
            EmailLoginCode(
                email=email,
                code_hash=auth.hash_email_code("222222"),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            )
        )
        session.commit()
        session.close()

        expire = datetime.utcnow() + timedelta(days=7)
        state = {}

        def fake_lock(db_session, addr):
            if "winner" not in state:
                state["winner"] = "running"
                # Победитель верифицирует новейший код и провижинит триал.
                state["winner"] = self._post({"email": email, "code": "222222"})

        client = self.rwms_client.return_value
        client.get_user_by_username_strict.return_value = None
        with (
            mock.patch("mobile_api.views.lock_email", side_effect=fake_lock),
            mock.patch(
                "mobile_api.provisioning.create_user",
                return_value=_FakeRwUser(expire_at=expire),
            ) as create_user,
        ):
            loser_resp = self._post({"email": email, "code": "111111"})

        winner_resp = state["winner"]
        self.assertEqual(winner_resp.status_code, 200)
        self.assertEqual(loser_resp.status_code, 200)
        winner_user = json.loads(winner_resp.content)["user"]
        loser_user = json.loads(loser_resp.content)["user"]
        # Оба запроса получают одного и того же пользователя.
        self.assertEqual(winner_user["id"], loser_user["id"])
        # Ровно один AddUser и одна strict-проверка (только у победителя).
        self.assertEqual(create_user.call_count, 1)
        self.assertEqual(client.get_user_by_username_strict.call_count, 1)
        session = self.Session()
        self.assertEqual(
            session.query(User).filter(User.email == email).count(), 1
        )
        # Оба успешных verify выдали по токену — на одного пользователя.
        self.assertEqual(session.query(MobileAccessToken).count(), 2)
        session.close()

    def test_rwms_unavailable_still_aborts_provisioning(self):
        """Политика прошлых раундов не ослаблена: недоступность панели при
        strict-проверке username прерывает провижининг (401), AddUser не
        зовётся, строка users не создаётся."""
        from common.rwms_client import RwmsUnavailableError

        email = "down@example.com"
        self._seed_code(email, "123456")
        client = self.rwms_client.return_value
        client.get_user_by_username_strict.side_effect = RwmsUnavailableError(
            "candidate", None, "panel down"
        )
        with mock.patch("mobile_api.provisioning.create_user") as create_user:
            resp = self._post({"email": email, "code": "123456"})

        self.assertEqual(resp.status_code, 401)
        create_user.assert_not_called()
        session = self.Session()
        self.assertEqual(session.query(User).count(), 0)
        self.assertEqual(session.query(MobileAccessToken).count(), 0)
        session.close()


class ProvisioningOwnershipGuardTests(SimpleTestCase):
    """P2: adoption проверяет ВЛАДЕЛЬЦА найденной подписки.

    Совпадение имени само по себе не доказывает, что подписка наша: помимо
    (маловероятной) хеш-коллизии бывают записи, созданные руками, импортом или
    ошибочным прошлым кодом. Раньше любая подписка с вычисленным username
    принималась безусловно — то есть чужой доступ мог быть выдан текущему
    email. Теперь только точное непустое совпадение разрешает adoption;
    отсутствующий или чужой email → ALERT, остановка, панель не тронута."""

    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)

    def test_shared_generator_is_the_engine_one(self):
        """Генератор имени — ОДИН на mobile_api и сайт (не копия)."""
        from engine.rwms_helpers import deterministic_username as engine_name
        from mobile_api.provisioning import deterministic_username as mobile_name

        self.assertIs(mobile_name, engine_name)

    def test_foreign_email_blocks_adoption_with_alert(self):
        from mobile_api import provisioning
        from mobile_api.provisioning import (
            RwmsSubscriptionOwnershipError,
            deterministic_username,
        )

        email = "victim@example.com"
        username = deterministic_username(email)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeRwUser(
            expire_at=datetime.utcnow() + timedelta(days=7),
            email="stranger@example.com",
            username=username,
        )
        session = self.Session()

        with self.assertLogs(level="CRITICAL") as captured_logs:
            with self.assertRaises(RwmsSubscriptionOwnershipError):
                with mock.patch(
                    "mobile_api.provisioning.create_user"
                ) as create_user:
                    provisioning.provision_trial_user(session, client, email)

        self.assertTrue(any("ALERT:" in line for line in captured_logs.output))
        self.assertTrue(
            any("stranger@example.com" in line for line in captured_logs.output)
        )
        # Панель не тронута и локальной строки не появилось.
        create_user.assert_not_called()
        client.add_user.assert_not_called()
        client.update_user.assert_not_called()
        session.rollback()
        self.assertEqual(session.query(User).count(), 0)
        session.close()

    def test_matching_email_allows_adoption(self):
        from mobile_api import provisioning
        from mobile_api.provisioning import deterministic_username

        email = "owner@example.com"
        username = deterministic_username(email)
        expire = datetime.utcnow() + timedelta(days=7)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeRwUser(
            expire_at=expire,
            email="  Owner@Example.COM  ",  # регистр/пробелы не мешают
            username=username,
        )
        session = self.Session()

        with mock.patch("mobile_api.provisioning.create_user") as create_user:
            user = provisioning.provision_trial_user(session, client, email)
        session.commit()

        self.assertIsNotNone(user)
        self.assertEqual(user.username, username)
        create_user.assert_not_called()
        client.add_user.assert_not_called()
        session.close()

    def test_panel_record_without_email_is_not_adoptable(self):
        from mobile_api import provisioning
        from mobile_api.provisioning import deterministic_username

        email = "noemail@example.com"
        username = deterministic_username(email)
        client = mock.Mock()
        client.get_user_by_username_strict.return_value = _FakeRwUser(
            expire_at=datetime.utcnow() + timedelta(days=7),
            username=username,
        )
        session = self.Session()

        with mock.patch("mobile_api.provisioning.create_user") as create_user:
            with self.assertLogs(level="CRITICAL"):
                with self.assertRaises(
                    provisioning.RwmsSubscriptionOwnershipError
                ):
                    provisioning.provision_trial_user(session, client, email)

        create_user.assert_not_called()
        self.assertEqual(session.query(User).count(), 0)
        session.rollback()
        session.close()


class MobileVerifyOwnershipConflictViewTests(SimpleTestCase):
    """Вьюха отдаёт понятную ошибку, а не чужой доступ и не «неверный код»."""

    def setUp(self):
        self.engine = _make_engine()
        self.Session = sessionmaker(bind=self.engine)

    def test_verify_returns_503_with_message_on_ownership_conflict(self):
        from mobile_api import views
        from mobile_api.provisioning import deterministic_username

        email = "conflict@example.com"
        session = self.Session()
        auth.register_email_code(session, email, "123456")
        session.commit()
        session.close()

        with (
            mock.patch("mobile_api.views.session_factory", side_effect=self.Session),
            mock.patch("mobile_api.views.get_rwms_user", return_value=_FakeRwUser()),
            mock.patch("mobile_api.views.rwms_client") as rwms_client,
            mock.patch("mobile_api.provisioning.create_user") as create_user,
        ):
            client = rwms_client.return_value
            client.get_user_by_username_strict.return_value = _FakeRwUser(
                expire_at=datetime.utcnow() + timedelta(days=7),
                email="someone-else@example.com",
                username=deterministic_username(email),
            )
            request = mock.Mock()
            request.method = "POST"
            request.body = json.dumps({"email": email, "code": "123456"}).encode()
            with self.assertLogs(level="CRITICAL"):
                resp = views.auth_email_verify(request)

        self.assertEqual(resp.status_code, 503)
        payload = json.loads(resp.content)
        self.assertEqual(payload["error"], "temporarily_unavailable")
        self.assertIn("Попробуйте позже", payload["message"])
        create_user.assert_not_called()
        client.add_user.assert_not_called()
        session = self.Session()
        self.assertEqual(session.query(User).count(), 0)
        self.assertEqual(session.query(MobileAccessToken).count(), 0)
        session.close()
