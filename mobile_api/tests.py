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
    """Stand-in for proto.UserResponse (HasField/expire_at/subscription_url)."""

    def __init__(self, expire_at=None, subscription_url="https://sub.example/abc"):
        self._expire_at = _FakeExpire(expire_at) if expire_at is not None else None
        self.subscription_url = subscription_url

    @property
    def expire_at(self):
        return self._expire_at

    def HasField(self, name):  # noqa: N802 - mirrors protobuf API
        if name == "expire_at":
            return self._expire_at is not None
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
        self._seed_code("new@example.com", "123456")
        expire = datetime.utcnow() + timedelta(days=7)
        with mock.patch(
            "mobile_api.provisioning.create_user",
            return_value=_FakeRwUser(expire_at=expire),
        ) as create_user, mock.patch("mobile_api.provisioning.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = "deadbeefdeadbeefdeadbeefdeadbeef"
            # Fresh username => get_user_by_username returns None (safety check).
            client = self.rwms_client.return_value
            client.get_user_by_username.return_value = None
            resp = self._post({"email": "New@Example.com", "code": "123456"})

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["access_token"])
        self.assertEqual(data["subscription_url"], "https://sub.example/abc")
        # RWMS create_user was called exactly once for the brand-new user.
        self.assertEqual(create_user.call_count, 1)
        _, kwargs = create_user.call_args
        self.assertEqual(kwargs["trial_period_days"], 7)
        self.assertEqual(kwargs["email"], "new@example.com")
        # Local user row created and a token issued.
        session = self.Session()
        user = session.query(User).filter(User.email == "new@example.com").one()
        self.assertEqual(data["user"]["id"], user.id)
        self.assertEqual(session.query(MobileAccessToken).count(), 1)
        session.close()

    def test_verify_existing_user_does_not_provision(self):
        session = self.Session()
        session.add(User(id=42, username="existing", email="old@example.com"))
        session.commit()
        session.close()
        self._seed_code("old@example.com", "123456")
        with mock.patch("mobile_api.provisioning.create_user") as create_user:
            resp = self._post({"email": "old@example.com", "code": "123456"})

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["user"]["id"], 42)
        # CRITICAL: existing user must never touch RWMS create/recreate.
        create_user.assert_not_called()
        self.rwms_client.return_value.get_user_by_username.assert_not_called()
        session = self.Session()
        self.assertEqual(session.query(MobileAccessToken).count(), 1)
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
