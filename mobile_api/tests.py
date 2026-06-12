from datetime import datetime, timedelta

from django.test import SimpleTestCase
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from common.models.db import Base, MobileAccessToken, MobileAuthCode, User
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
