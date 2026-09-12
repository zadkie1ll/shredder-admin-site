"""Regression checks without database connections or provider requests."""
import hashlib
import io
import json
import signal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit, parse_qs
import httpx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, RequestFactory, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, ProgrammingError, OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from yookassa.domain.exceptions import (
    BadRequestError, ForbiddenError, InternalServerError, NotFoundError, TooManyRequestsError, UnauthorizedError,
)
from engine import views, payments, email_change, checkout_attempts
from engine.management.commands import check_common_schema, reconcile_website_operations as reconcile_command
from common.models.db import (
    PurchaseLoginToken, WataInvoice, WataTransaction, WebsiteEmailChange, WebsitePaymentAttempt, YkPayment,
    TelegramLoginToken, User,
)
from common.rwms_client import RwmsUnavailableError


@compiles(JSONB, "sqlite")
def _jsonb_as_json_for_sqlite(type_, compiler, **kw):
    # Tests only: JSONB — тип PostgreSQL; SQLite in-memory хранит его как JSON.
    return "JSON"


class Query:
    def __init__(self, value=None):
        self.value = value
    def filter(self, *args): return self
    def filter_by(self, **kwargs): return self
    def with_for_update(self, **kwargs): return self
    def order_by(self, *args): return self
    def first(self): return self.value
    def one(self): return self.value


class Session:
    def __init__(self, value=None):
        self.value = value
        self.commit = mock.Mock()
        self.close = mock.Mock()
        self.added = []
    def query(self, *args): return Query(self.value)
    def add(self, item):
        self.value = item
        self.added.append(item)


class SessionDict(dict):
    modified = False


class TelegramPayloadRegressionTests(SimpleTestCase):
    def test_full_payload_fits_and_round_trips_old_bot_parser(self):
        for user_id in [1, 71, 2**63 - 1]:
            with self.subTest(user_id=user_id):
                request = SimpleNamespace(session=SessionDict())
                session = Session()
                link = views.get_or_create_telegram_bind_link(request, session, SimpleNamespace(id=user_id), 'test_bot')
                payload = parse_qs(urlsplit(link).query)['start'][0]
                self.assertLessEqual(len(payload), 64)
                # Actual legacy delimiter contract: first split '-' then '_'.
                owner, token = payload.split('-')[0][len('bind_'):].split('_')
                self.assertEqual(owner, str(user_id))
                self.assertEqual(len(token), 38)
                self.assertEqual(session.added[0].token_hash, views.hash_telegram_bind_token(token))
                self.assertNotEqual(session.added[0].token_hash, views.hash_telegram_login_token(token))

    def test_overlong_session_token_is_rotated(self):
        request = SimpleNamespace(session=SessionDict({views.TELEGRAM_BIND_SESSION_TOKEN_KEY: 'a'*64, views.TELEGRAM_BIND_SESSION_CREATED_KEY: datetime.now().timestamp()}))
        session = Session(SimpleNamespace(id=1))
        link = views.get_or_create_telegram_bind_link(request, session, SimpleNamespace(id=71), 'test_bot')
        self.assertNotIn('a'*64, link)
        self.assertEqual(len(session.added), 1)


class TelegramBindTokenSeparationTests(SimpleTestCase):
    """TG-BIND-1/2: bind-токен не открывает вход; ссылка из сессии живёт в показе 5 минут."""

    def setUp(self):
        import itertools
        from sqlalchemy import event
        self.Session = _sqlite_sessionmaker(self, User, TelegramLoginToken)
        ids = itertools.count(1)

        def assign_id(mapper, connection, target):
            # BigInteger PK в SQLite не автоинкрементный (в PostgreSQL — sequence).
            if target.id is None:
                target.id = next(ids)

        event.listen(TelegramLoginToken, 'before_insert', assign_id)
        self.addCleanup(event.remove, TelegramLoginToken, 'before_insert', assign_id)
        with self.Session() as session:
            session.add(User(id=71, username='user-71', email='site@example.test'))
            session.commit()

    def issue_link(self, request):
        with self.Session() as session:
            link = views.get_or_create_telegram_bind_link(request, session, SimpleNamespace(id=71), 'test_bot')
            session.commit()
        return link

    def token_count(self):
        with self.Session() as session:
            return session.query(TelegramLoginToken).count()

    def bind_telegram(self):
        with self.Session() as session:
            session.get(User, 71).telegram_id = 777
            session.commit()

    def open_login_link(self, raw_token):
        from django.http import HttpResponse
        request = RequestFactory().get(f'/login/telegram/{raw_token}/')
        request.session = SessionDict()
        with mock.patch.object(views, 'session_factory', self.Session), \
                mock.patch.object(views, 'render_login', return_value=HttpResponse('login', status=401)), \
                mock.patch.object(views, 'add_event_log_once'), \
                mock.patch.object(views, 'authorize_user_session') as authorize:
            response = views.auth_by_telegram_link(request, raw_token)
        return response, authorize

    def test_unconsumed_bind_token_never_opens_telegram_login(self):
        raw_token = self.issue_link(SimpleNamespace(session=SessionDict())).rsplit('_', 1)[-1]
        # Бот привязал аккаунт по другой ссылке; этот bind-токен так и остался непогашенным.
        self.bind_telegram()
        response, authorize = self.open_login_link(raw_token)
        self.assertEqual(response.status_code, 401)
        authorize.assert_not_called()

    def test_telegram_login_token_still_opens_login(self):
        raw_token = 'b' * 38
        with self.Session() as session:
            session.add(TelegramLoginToken(user_id=71, token_hash=views.hash_telegram_login_token(raw_token)))
            session.commit()
        self.bind_telegram()
        response, authorize = self.open_login_link(raw_token)
        self.assertEqual(response.status_code, 302)
        authorize.assert_called_once()

    def test_bind_hash_matches_bot_contract(self):
        from django.conf import settings
        token = 'c' * 38
        self.assertEqual(
            views.hash_telegram_bind_token(token),
            hashlib.sha256(f'telegram-bind:{token}:{settings.SECRET_KEY}'.encode()).hexdigest(),
        )
        self.assertNotEqual(views.hash_telegram_bind_token(token), views.hash_telegram_login_token(token))

    def test_session_link_is_reused_only_within_five_minutes(self):
        self.assertEqual(views.TELEGRAM_BIND_TOKEN_REUSE_SECONDS, 5 * 60)
        self.assertLess(views.TELEGRAM_BIND_TOKEN_REUSE_SECONDS + 10 * 60, views.TELEGRAM_BIND_TOKEN_MAX_AGE_SECONDS + 1)
        for age_minutes, reused in ((4, True), (6, False)):
            with self.subTest(age_minutes=age_minutes):
                request = SimpleNamespace(session=SessionDict())
                first = self.issue_link(request)
                with self.Session() as session:
                    row = session.query(TelegramLoginToken).order_by(TelegramLoginToken.id.desc()).first()
                    row.created_at = datetime.utcnow() - timedelta(minutes=age_minutes)
                    session.commit()
                before = self.token_count()
                second = self.issue_link(request)
                self.assertEqual(second == first, reused)
                self.assertEqual(self.token_count() - before, 0 if reused else 1)


class SupportEtagRegressionTests(SimpleTestCase):
    def test_any_representation_change_invalidates_etag(self):
        payload = {'status_filter': 'open', 'open_count': 2, 'closed_count': 0,
                   'ticket_payloads': [{'id': 2, 'updated_at_iso': 'fixed'}, {'id': 1, 'email': 'old@example.test'}],
                   'has_more': True, 'next_updated_at': 'fixed', 'next_id': 1}
        with mock.patch.object(views, 'require_support_admin', return_value=None), mock.patch.object(views, 'load_support_admin_tickets', return_value=payload):
            previous = views.support_admin_tickets_json(RequestFactory().get('/tickets'))
            for change in [lambda: payload['ticket_payloads'][1].update(email='new@example.test'), lambda: payload.update(next_id=0), lambda: payload.update(has_more=False)]:
                change()
                response = views.support_admin_tickets_json(RequestFactory().get('/tickets', HTTP_IF_NONE_MATCH=previous['ETag']))
                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(response['ETag'], previous['ETag'])
                previous = response
            unchanged = views.support_admin_tickets_json(RequestFactory().get('/tickets', HTTP_IF_NONE_MATCH=previous['ETag']))
            self.assertEqual(unchanged.status_code, 304)
            self.assertEqual(unchanged['Cache-Control'], 'private, no-cache')

    def _tickets_payload(self):
        return {'status_filter': 'open', 'open_count': 1, 'closed_count': 0,
                'ticket_payloads': [{'id': 1, 'subject': 'x' * 400}],
                'has_more': False, 'next_updated_at': '', 'next_id': None}

    def test_weak_and_listed_etags_match(self):
        with mock.patch.object(views, 'require_support_admin', return_value=None), \
                mock.patch.object(views, 'load_support_admin_tickets', return_value=self._tickets_payload()):
            etag = views.support_admin_tickets_json(RequestFactory().get('/tickets'))['ETag']
            for header in [f'W/{etag}', f'"other", W/{etag}', f'W/"other",{etag}', '*']:
                with self.subTest(header=header):
                    response = views.support_admin_tickets_json(RequestFactory().get('/tickets', HTTP_IF_NONE_MATCH=header))
                    self.assertEqual(response.status_code, 304)
                    self.assertEqual(response['ETag'], etag)
            for header in ['W/"other"', '"other", W/"another"', 'garbage', '']:
                with self.subTest(header=header):
                    response = views.support_admin_tickets_json(RequestFactory().get('/tickets', HTTP_IF_NONE_MATCH=header))
                    self.assertEqual(response.status_code, 200)

    def test_gzip_weak_etag_round_trip_returns_304(self):
        # GZipMiddleware (есть в MIDDLEWARE) делает ETag слабым; JS шлёт его обратно как есть.
        from django.middleware.gzip import GZipMiddleware
        with mock.patch.object(views, 'require_support_admin', return_value=None), \
                mock.patch.object(views, 'load_support_admin_tickets', return_value=self._tickets_payload()):
            middleware = GZipMiddleware(views.support_admin_tickets_json)
            first = middleware(RequestFactory().get('/tickets', HTTP_ACCEPT_ENCODING='gzip'))
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first['Content-Encoding'], 'gzip')
            self.assertTrue(first['ETag'].startswith('W/"'))
            second = middleware(RequestFactory().get('/tickets', HTTP_ACCEPT_ENCODING='gzip', HTTP_IF_NONE_MATCH=first['ETag']))
            self.assertEqual(second.status_code, 304)


class AttachmentDecodeRegressionTests(SimpleTestCase):
    def test_valid_images_and_file_position(self):
        for format_, mime in [('PNG','image/png'), ('JPEG','image/jpeg'), ('GIF','image/gif'), ('WEBP','image/webp')]:
            with self.subTest(format=format_):
                image = io.BytesIO()
                Image.new('RGB', (10, 10), 'red').save(image, format=format_)
                upload = SimpleUploadedFile('image', image.getvalue(), content_type=mime)
                self.assertTrue(views.support_attachment_signature_matches(upload, mime))
                self.assertEqual(upload.tell(), 0)

    def test_header_only_and_wrong_mime_rejected(self):
        fake = SimpleUploadedFile('fake.png', b'\x89PNG\r\n\x1a\nnot-an-image')
        self.assertFalse(views.support_attachment_signature_matches(fake, 'image/png'))
        image = io.BytesIO()
        Image.new('RGB', (2, 2)).save(image, format='PNG')
        self.assertFalse(views.support_attachment_signature_matches(SimpleUploadedFile('x', image.getvalue()), 'image/jpeg'))

    def test_large_dimensions_are_rejected_before_decode(self):
        picture = mock.MagicMock(format='PNG', size=(10000, 10000))
        picture.__enter__.return_value = picture
        with mock.patch.object(views.Image, 'open', return_value=picture):
            self.assertFalse(views.support_attachment_signature_matches(io.BytesIO(b'header'), 'image/png'))
        picture.load.assert_not_called()

    # Реальные GIF из фаззинга Pillow 12: один подменённый байт после блока
    # изображения роняет n_frames/seek исключениями вне прежнего except (было 500).
    CORRUPTED_GIFS = {
        'IndexError': (
            '4749463839610400040080000000000000000021ff0b4e45545343415045322e3003010000'
            '0021f90400030000002c00000000040004000008090001081c48b02080800021'
        ),
        'struct.error': (
            '4749463839610400040080000000000000000021ff0b4e45545343415045322e3003010000'
            '0021f90400030000002c00000000040004000008090001081c48b0208080002c'
        ),
    }

    def test_corrupted_gif_is_rejected_instead_of_crashing(self):
        for label, data in self.CORRUPTED_GIFS.items():
            with self.subTest(label=label):
                upload = SimpleUploadedFile('broken.gif', bytes.fromhex(data), content_type='image/gif')
                with self.assertLogs(level='WARNING') as logs:
                    self.assertFalse(views.support_attachment_signature_matches(upload, 'image/gif'))
                self.assertEqual(upload.tell(), 0)
                self.assertIn('image decoder failed', '\n'.join(logs.output))

    def test_unexpected_decoder_errors_are_rejected(self):
        import struct
        for error in (IndexError('index out of range'), struct.error('unpack requires a buffer'), KeyError('x')):
            with self.subTest(error=type(error).__name__):
                picture = mock.MagicMock(format='GIF', size=(4, 4))
                picture.__enter__.return_value = picture
                type(picture).n_frames = mock.PropertyMock(side_effect=error)
                with mock.patch.object(views.Image, 'open', return_value=picture), self.assertLogs(level='WARNING'):
                    self.assertFalse(views.support_attachment_signature_matches(io.BytesIO(b'GIF89a'), 'image/gif'))


class EmailChangeRegressionTests(SimpleTestCase):
    def test_latest_nonce_only_and_single_use(self):
        user = SimpleNamespace(id=71, email='old@example.test')
        session = Session()
        first = email_change.issue_email_change(session, user, 'first@example.test')
        second = email_change.issue_email_change(session, user, 'second@example.test')
        self.assertFalse(email_change.consume_email_change(session, user, {'nonce': first, 'email':'first@example.test'}, 900))
        self.assertTrue(email_change.consume_email_change(session, user, {'nonce': second, 'email':'second@example.test'}, 900))
        self.assertTrue(session.value.sync_pending)
        self.assertFalse(email_change.consume_email_change(session, user, {'nonce': second, 'email':'second@example.test'}, 900))

    def test_expired_and_legacy_links_fail_closed(self):
        user = SimpleNamespace(id=71, email='old@example.test')
        session = Session()
        nonce = email_change.issue_email_change(session, user, 'new@example.test')
        session.value.issued_at -= timedelta(minutes=16)
        self.assertFalse(email_change.consume_email_change(session, user, {'nonce':nonce, 'email':'new@example.test'}, 900))
        self.assertFalse(email_change.consume_email_change(session, user, {'email':'new@example.test'}, 900))

    def test_rwms_failure_keeps_durable_retry_then_syncs_latest_email(self):
        Session_ = _sqlite_sessionmaker(self, User, WebsiteEmailChange)
        session = Session_()
        now = datetime.utcnow()
        session.add(User(id=71, username='existing-uuid', email='latest@example.test'))
        session.add(WebsiteEmailChange(user_id=71, token_hash='0' * 64, requested_email='latest@example.test',
                                       previous_email='', issued_at=now, used_at=now, sync_pending=True,
                                       attempts=0, next_attempt_at=now))
        session.commit()
        session.close()
        rwms = SimpleNamespace(get_user_by_username_strict=mock.Mock(side_effect=RuntimeError('offline')), update_user=mock.Mock())
        self.assertFalse(email_change.sync_email_change(Session_, rwms, 71, SimpleNamespace))
        check = Session_()
        change = check.get(WebsiteEmailChange, 71)
        self.assertTrue(change.sync_pending)
        self.assertEqual(change.attempts, 1)
        self.assertGreater(change.next_attempt_at, datetime.utcnow())
        change.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        check.commit()
        check.close()
        rwms.get_user_by_username_strict.side_effect = None
        rwms.get_user_by_username_strict.return_value = SimpleNamespace(uuid='existing-uuid')
        rwms.update_user.return_value = object()
        self.assertTrue(email_change.sync_email_change(Session_, rwms, 71, SimpleNamespace))
        sent = rwms.update_user.call_args.args[0]
        self.assertEqual((sent.uuid, sent.email), ('existing-uuid', 'latest@example.test'))
        check = Session_()
        self.assertFalse(check.get(WebsiteEmailChange, 71).sync_pending)
        check.close()


class _EmailFixtureMixin:
    """SQLite in-memory: users + website_email_changes."""

    def setUp(self):
        self.Session = _sqlite_sessionmaker(self, User, WebsiteEmailChange)

    def reset_db(self):
        self.Session = _sqlite_sessionmaker(self, User, WebsiteEmailChange)

    def add_user(self, user_id=71, email='old@example.test', username='user-71'):
        session = self.Session()
        session.add(User(id=user_id, username=username, email=email))
        session.commit()
        session.close()

    def add_pending_change(self, user_id=71, email='new@example.test', **values):
        now = datetime.utcnow()
        row = dict(user_id=user_id, token_hash='0' * 64, requested_email=email, previous_email='old@example.test',
                   issued_at=now - timedelta(minutes=1), used_at=now, sync_pending=True, attempts=0,
                   next_attempt_at=now)
        row.update(values)
        session = self.Session()
        session.add(WebsiteEmailChange(**row))
        session.commit()
        session.close()

    def change(self, user_id=71):
        session = self.Session()
        try:
            return session.get(WebsiteEmailChange, user_id)
        finally:
            session.close()

    def stored_email(self, user_id=71):
        session = self.Session()
        try:
            return session.get(User, user_id).email
        finally:
            session.close()

    def set_email(self, email, user_id=71):
        session = self.Session()
        session.get(User, user_id).email = email
        session.commit()
        session.close()

    @staticmethod
    def panel(**overrides):
        client = SimpleNamespace(
            get_user_by_username_strict=mock.Mock(return_value=SimpleNamespace(uuid='panel-uuid')),
            update_user=mock.Mock(return_value=object()),
        )
        for name, value in overrides.items():
            setattr(client, name, value)
        return client


class EmailSyncWithoutUserLockTests(_EmailFixtureMixin, SimpleTestCase):
    """EMAIL-02/03/04, F4: RPC без блокировок, закрытие синка CAS-ом."""

    def test_rpc_runs_outside_any_transaction_and_closes_sync(self):
        self.add_user(email='New@Example.test')
        self.add_pending_change()
        opened = []

        def factory():
            session = self.Session()
            opened.append(session)
            return session

        outside_transaction = []

        def lookup(username):
            outside_transaction.append(bool(opened) and not any(s.in_transaction() for s in opened))
            return SimpleNamespace(uuid='panel-uuid')

        def update(request):
            outside_transaction.append(not any(s.in_transaction() for s in opened))
            return object()

        rwms = self.panel(get_user_by_username_strict=mock.Mock(side_effect=lookup),
                          update_user=mock.Mock(side_effect=update))
        self.assertTrue(email_change.sync_email_change(factory, rwms, 71, SimpleNamespace))
        self.assertEqual(outside_transaction, [True, True])
        rwms.get_user_by_username_strict.assert_called_once_with('user-71')
        sent = rwms.update_user.call_args.args[0]
        self.assertEqual((sent.uuid, sent.email), ('panel-uuid', 'new@example.test'))
        change = self.change()
        self.assertFalse(change.sync_pending)
        self.assertIsNone(change.next_attempt_at)

    def test_users_row_is_never_locked_and_challenge_lock_does_not_wait(self):
        from sqlalchemy.orm import Query as OrmQuery
        self.add_user(email='new@example.test')
        self.add_pending_change()
        real_with_for_update = OrmQuery.with_for_update
        locks = []

        def spy(query, *args, **kwargs):
            locks.append((query.column_descriptions[0]['entity'], kwargs))
            return real_with_for_update(query, *args, **kwargs)

        with mock.patch.object(OrmQuery, 'with_for_update', spy):
            self.assertTrue(email_change.sync_email_change(self.Session, self.panel(), 71, SimpleNamespace))
        self.assertTrue(locks)
        self.assertNotIn(User, [entity for entity, _ in locks])
        self.assertTrue(all(entity is WebsiteEmailChange and kwargs.get('skip_locked') for entity, kwargs in locks))

    def test_email_changed_during_rpc_keeps_sync_pending_and_resends_latest(self):
        self.add_user(email='new@example.test')
        self.add_pending_change()

        def update(request):
            self.set_email('newer@example.test')  # другая транзакция успела раньше CAS
            return object()

        rwms = self.panel(update_user=mock.Mock(side_effect=update))
        started = datetime.utcnow()
        self.assertFalse(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
        change = self.change()
        self.assertTrue(change.sync_pending)
        self.assertGreaterEqual(change.next_attempt_at, started)
        self.assertLessEqual(change.next_attempt_at, datetime.utcnow())
        self.assertEqual(email_change.pending_email_users(self.Session()), [71])

        rwms.update_user.side_effect = None
        self.assertTrue(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
        self.assertEqual(rwms.update_user.call_args.args[0].email, 'newer@example.test')
        self.assertFalse(self.change().sync_pending)

    def test_challenge_reissued_or_reconfirmed_during_rpc_is_not_closed(self):
        def reissue(session):
            email_change.issue_email_change(session, session.get(User, 71), 'other@example.test')

        def reconfirm(session):
            row = session.get(WebsiteEmailChange, 71)
            row.used_at = datetime.utcnow() + timedelta(seconds=1)
            row.attempts = 0

        for label, mutate in (('reissued', reissue), ('reconfirmed', reconfirm)):
            with self.subTest(label=label):
                self.reset_db()
                self.add_user(email='new@example.test')
                self.add_pending_change(attempts=2)

                def update(request, mutate=mutate):
                    session = self.Session()
                    mutate(session)
                    session.commit()
                    session.close()
                    return object()

                rwms = self.panel(update_user=mock.Mock(side_effect=update))
                self.assertFalse(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
                change = self.change()
                self.assertTrue(change.sync_pending)
                self.assertLessEqual(change.next_attempt_at, datetime.utcnow())

    def test_rpc_failure_postpones_with_backoff(self):
        self.add_user(email='new@example.test')
        self.add_pending_change()
        rwms = self.panel(get_user_by_username_strict=mock.Mock(side_effect=RuntimeError('panel down')))
        with self.assertLogs('engine.email_change', level='WARNING') as logs:
            self.assertFalse(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
        change = self.change()
        self.assertTrue(change.sync_pending)
        self.assertEqual(change.attempts, 1)
        self.assertGreater(change.next_attempt_at, datetime.utcnow() + timedelta(seconds=50))
        rwms.update_user.assert_not_called()
        self.assertIn('postponed user_id=71 attempt=1', '\n'.join(logs.output))

    def test_unacknowledged_update_is_retried(self):
        self.add_user(email='new@example.test')
        self.add_pending_change()
        rwms = self.panel(update_user=mock.Mock(return_value=None))
        self.assertFalse(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
        self.assertEqual(self.change().attempts, 1)
        self.assertTrue(self.change().sync_pending)

    def test_rpc_failure_does_not_postpone_a_newer_confirmation(self):
        self.add_user(email='new@example.test')
        self.add_pending_change(attempts=3)

        def failing_lookup(username):
            session = self.Session()
            row = session.get(WebsiteEmailChange, 71)
            now = datetime.utcnow()
            row.used_at, row.attempts, row.next_attempt_at = now + timedelta(seconds=1), 0, now
            session.commit()
            session.close()
            raise RuntimeError('panel down')

        rwms = self.panel(get_user_by_username_strict=mock.Mock(side_effect=failing_lookup))
        self.assertFalse(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
        change = self.change()
        self.assertTrue(change.sync_pending)
        self.assertEqual(change.attempts, 0)
        self.assertLessEqual(change.next_attempt_at, datetime.utcnow())

    def test_unusable_panel_email_closes_sync_without_rpc(self):
        for email in (None, '', 'milenapanowa@yandex'):
            with self.subTest(email=email):
                self.reset_db()
                self.add_user(email=email)
                self.add_pending_change()
                rwms = self.panel()
                with self.assertLogs('engine.email_change', level='WARNING') as logs:
                    self.assertTrue(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
                rwms.get_user_by_username_strict.assert_not_called()
                rwms.update_user.assert_not_called()
                change = self.change()
                self.assertFalse(change.sync_pending)
                self.assertIsNone(change.next_attempt_at)
                self.assertIn('closed without RPC', '\n'.join(logs.output))

    def test_not_due_sync_does_nothing(self):
        self.add_user(email='new@example.test')
        self.add_pending_change(next_attempt_at=datetime.utcnow() + timedelta(minutes=5))
        rwms = self.panel()
        self.assertFalse(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
        rwms.get_user_by_username_strict.assert_not_called()

    def test_account_without_panel_subscription_closes_sync(self):
        self.add_user(email='new@example.test')
        self.add_pending_change()
        rwms = self.panel(get_user_by_username_strict=mock.Mock(return_value=None))
        self.assertTrue(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
        rwms.update_user.assert_not_called()
        self.assertFalse(self.change().sync_pending)

    def test_challenge_row_held_by_another_transaction_is_left_pending(self):
        # SKIP LOCKED не вернул строку (её держит подтверждение или merge бота): не ждём и не пишем.
        self.add_user(email='new@example.test')
        self.add_pending_change()
        for rwms in (self.panel(), self.panel(get_user_by_username_strict=mock.Mock(side_effect=RuntimeError('down')))):
            with self.subTest(rwms=rwms), mock.patch.object(email_change, '_locked_change', return_value=None):
                self.assertFalse(email_change.sync_email_change(self.Session, rwms, 71, SimpleNamespace))
            change = self.change()
            self.assertTrue(change.sync_pending)
            self.assertEqual(change.attempts, 0)


class EmailConfirmationReplayTests(_EmailFixtureMixin, SimpleTestCase):
    """EMAIL-07: повторный переход по применённой ссылке — успех, а не ошибка."""

    def request(self, user=None):
        request = RequestFactory().get('/confirm-email/token/')
        request.session = SessionDict()
        request.user = user or SimpleNamespace(is_authenticated=False, id=None)
        return request

    def issue(self, new_email='new@example.test', nonce=None):
        session = self.Session()
        user = session.get(User, 71)
        issued_nonce = email_change.issue_email_change(session, user, new_email)
        token = views.build_email_confirmation_token(71, new_email, user.email, nonce or issued_nonce)
        session.commit()
        session.close()
        return token

    def confirm(self, token, request=None):
        request = request or self.request()
        with mock.patch.object(views, 'session_factory', self.Session), \
                mock.patch.object(views, 'sync_email_change') as sync:
            response = views.confirm_email(request, token)
        return request, response, sync

    def test_repeated_click_after_link_scanner_shows_success_and_authorizes(self):
        self.add_user()
        token = self.issue()
        scanner, first, sync = self.confirm(token)
        self.assertEqual(first.url, reverse('dashboard'))
        self.assertNotIn('email_bind_modal', scanner.session)
        sync.assert_called_once()
        self.assertEqual(self.stored_email(), 'new@example.test')

        with self.assertLogs(level='INFO') as logs:
            person, second, sync_again = self.confirm(token)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(second.url, reverse('dashboard'))
        self.assertNotIn('email_bind_modal', person.session)
        self.assertEqual(person.session[views.SESSION_KEY], '71')
        sync_again.assert_not_called()
        self.assertEqual(self.stored_email(), 'new@example.test')
        self.assertIn('repeated confirmation of an already applied email change', '\n'.join(logs.output))

    def test_repeated_click_by_logged_in_owner_keeps_session(self):
        self.add_user()
        token = self.issue()
        self.confirm(token)
        owner = SimpleNamespace(is_authenticated=True, id=71, email='old@example.test')
        request, response, _ = self.confirm(token, self.request(owner))
        self.assertEqual(response.url, reverse('dashboard'))
        self.assertEqual(owner.email, 'new@example.test')
        self.assertNotIn(views.SESSION_KEY, request.session)
        self.assertNotIn('email_bind_modal', request.session)

    def test_replay_is_rejected_when_the_change_is_not_current(self):
        def used_long_ago():
            session = self.Session()
            session.get(WebsiteEmailChange, 71).used_at = datetime.utcnow() - timedelta(minutes=16)
            session.commit()
            session.close()

        cases = (
            ('used_at_older_than_ttl', used_long_ago),
            ('email_changed_again', lambda: self.set_email('other@example.test')),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                self.reset_db()
                self.add_user()
                token = self.issue()
                self.confirm(token)
                mutate()
                request, response, sync = self.confirm(token)
                self.assertEqual(response.url, reverse('login'))
                self.assertIn('устарела', request.session['email_bind_modal']['error'])
                self.assertNotIn(views.SESSION_KEY, request.session)
                sync.assert_not_called()

    def test_foreign_nonce_never_counts_as_applied(self):
        self.add_user()
        token = self.issue()
        self.confirm(token)
        forged = views.build_email_confirmation_token(71, 'new@example.test', 'old@example.test', 'guessed-nonce')
        request, response, _ = self.confirm(forged)
        self.assertEqual(response.url, reverse('login'))
        self.assertIn('error', request.session['email_bind_modal'])
        self.assertNotIn(views.SESSION_KEY, request.session)


class PaymentEmailConfirmationTests(_EmailFixtureMixin, SimpleTestCase):
    """PAY-04/PAY-01: email из оплаты привязывается только штатным подтверждением."""

    def setUp(self):
        super().setUp()
        # Лимит писем подтверждения (ZONE-EMAIL-01) живёт в кэше: изоляция
        # тестов и фиксированные часы, чтобы окно не сменилось посреди теста.
        cache.clear()
        self.addCleanup(cache.clear)
        self.clock = 7_200_010.0
        patcher = mock.patch.object(rate_limit, '_now', side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, email='receipt@example.test', send_side_effect=None, user_id=71):
        request = RequestFactory().post('/pay/')
        request.session = SessionDict()
        request.user = SimpleNamespace(is_authenticated=True, id=user_id)
        with mock.patch.object(views, 'session_factory', self.Session), \
                mock.patch.object(views, 'get_current_base_url', return_value='https://site.test'), \
                mock.patch.object(views, 'send_email_confirmation_email', side_effect=send_side_effect) as send:
            result = views.send_payment_email_confirmation(request, user_id, email)
        return result, send

    def test_account_without_email_gets_confirmation_and_email_is_attached_only_by_link(self):
        self.add_user(email=None)
        result, send = self.send()
        self.assertTrue(result)
        send.assert_called_once()
        address, link = send.call_args.args
        self.assertEqual(address, 'receipt@example.test')
        self.assertTrue(link.startswith('https://site.test/'))
        self.assertIsNone(self.stored_email())
        change = self.change()
        self.assertEqual((change.requested_email, change.previous_email), ('receipt@example.test', ''))
        self.assertIsNone(change.used_at)
        self.assertFalse(change.sync_pending)

        request = RequestFactory().get('/confirm-email/token/')
        request.session = SessionDict()
        request.user = SimpleNamespace(is_authenticated=False, id=None)
        token = link.rstrip('/').rsplit('/', 1)[-1]
        with mock.patch.object(views, 'session_factory', self.Session), mock.patch.object(views, 'sync_email_change'):
            response = views.confirm_email(request, token)
        self.assertEqual(response.url, reverse('dashboard'))
        self.assertEqual(self.stored_email(), 'receipt@example.test')
        self.assertTrue(self.change().sync_pending)

    def test_email_of_another_account_is_not_offered(self):
        self.add_user(email=None)
        self.add_user(user_id=72, email='receipt@example.test', username='user-72')
        result, send = self.send()
        self.assertFalse(result)
        send.assert_not_called()
        self.assertIsNone(self.change())
        self.assertIsNone(self.stored_email())

    def test_account_with_email_is_untouched(self):
        self.add_user(email='own@example.test')
        result, send = self.send()
        self.assertFalse(result)
        send.assert_not_called()
        self.assertIsNone(self.change())
        self.assertEqual(self.stored_email(), 'own@example.test')

    def test_recent_link_for_the_same_address_is_not_reissued(self):
        self.add_user(email=None)
        self.assertTrue(self.send()[0])
        first_hash = self.change().token_hash
        result, send = self.send()
        self.assertFalse(result)
        send.assert_not_called()
        self.assertEqual(self.change().token_hash, first_hash)
        # Другой адрес — это новая операция, ссылка перевыпускается.
        result, send = self.send(email='other@example.test')
        self.assertTrue(result)
        self.assertNotEqual(self.change().token_hash, first_hash)

    def test_mail_failure_is_logged_and_never_raises(self):
        self.add_user(email=None)
        with self.assertLogs(level='ERROR') as logs:
            result, send = self.send(send_side_effect=RuntimeError('smtp down'))
        self.assertFalse(result)
        send.assert_called_once()
        self.assertIn('payment email confirmation was not sent', '\n'.join(logs.output))
        self.assertIsNotNone(self.change())
        self.assertIsNone(self.stored_email())

    def test_database_failure_is_logged_and_never_raises(self):
        broken = mock.Mock()
        broken.query.side_effect = OperationalError('SELECT', {}, Exception('connection lost'))
        request = RequestFactory().post('/pay/')
        with mock.patch.object(views, 'session_factory', return_value=broken), \
                mock.patch.object(views, 'send_email_confirmation_email') as send, \
                self.assertLogs(level='ERROR'):
            self.assertFalse(views.send_payment_email_confirmation(request, 71, 'receipt@example.test'))
        send.assert_not_called()
        broken.rollback.assert_called_once_with()
        broken.close.assert_called_once_with()

    def test_confirmations_are_limited_per_account_regardless_of_address(self):
        # ZONE-EMAIL-01: не больше 3 писем подтверждения на аккаунт в час.
        self.assertEqual((views.PAYMENT_EMAIL_CONFIRMATION_RATE_LIMIT,
                          views.PAYMENT_EMAIL_CONFIRMATION_RATE_WINDOW_SECONDS), (3, 3600))
        self.add_user(email=None)
        for i in range(3):
            result, send = self.send(email=f'receipt{i}@example.test')
            self.assertTrue(result)
            send.assert_called_once()
        with self.assertLogs(level='WARNING') as logs:
            result, send = self.send(email='receipt9@example.test')
        self.assertFalse(result)
        send.assert_not_called()
        output = '\n'.join(logs.output)
        self.assertIn('rate limit reached', output)
        self.assertNotIn('receipt9@example.test', output)
        # Ссылка не перевыпущена: действует последняя отправленная.
        self.assertEqual(self.change().requested_email, 'receipt2@example.test')
        self.assertIsNone(self.stored_email())
        # Другой аккаунт лимит не делит.
        self.add_user(user_id=72, email=None, username='user-72')
        self.assertTrue(self.send(email='other@example.test', user_id=72)[0])
        # Следующее окно снова пропускает.
        self.clock += 3600
        self.assertTrue(self.send(email='receipt9@example.test')[0])

    def test_skipped_confirmations_do_not_consume_the_limit(self):
        self.add_user(email=None)
        self.assertTrue(self.send()[0])
        for _ in range(3):
            # Та же ссылка ещё действует: пропуск до выпуска, лимит не расходуется.
            self.assertFalse(self.send()[0])
        self.assertTrue(self.send(email='second@example.test')[0])
        self.assertTrue(self.send(email='third@example.test')[0])


from django.contrib.auth import SESSION_KEY  # noqa: E402
from django.core.cache import cache  # noqa: E402
from common.models.db import MagicToken  # noqa: E402
from engine import rate_limit  # noqa: E402


class _CheckoutRows(Query):
    """Строки одной модели фейковой сессии: filter/first — последняя
    добавленная, filter_by — точное совпадение полей (как в finish_attempt)."""

    def __init__(self, rows):
        super().__init__(rows[-1] if rows else None)
        self.rows = rows

    def filter_by(self, **kwargs):
        matching = [row for row in self.rows
                    if all(getattr(row, key, None) == value for key, value in kwargs.items())]
        return Query(matching[-1] if matching else None)


class _AnonymousCheckoutSession(Session):
    next_user_id = 91

    def __init__(self, existing_user=None):
        super().__init__()
        self.existing_user = existing_user
        self.commit = mock.Mock()
        self.rollback = mock.Mock()
        # flush остаётся Mock (тесты считают вызовы), но с побочным эффектом
        # настоящей сессии: именно здесь применяются питоновские column
        # defaults. Пока дубль ставил MagicToken.token в add(), код без
        # flush() проходил тесты и слал /login/magic/None/ (инцидент
        # 2026-09-12).
        self.flush = mock.Mock(side_effect=self._apply_insert_defaults)

    def rows(self, model):
        return [item for item in self.added if isinstance(item, model)]

    def query(self, model, *args):
        if model is User:
            return Query(self.existing_user)
        if model in (PurchaseLoginToken, WebsitePaymentAttempt):
            return _CheckoutRows(self.rows(model))
        return Query(None)

    def _apply_insert_defaults(self):
        """Как SQLAlchemy на INSERT: проставляем то, что даёт БД/дефолт."""
        for item in self.added:
            if isinstance(item, MagicToken) and item.token is None:
                item.token = 'magic-short-token'
            if isinstance(item, User) and item.id is None:
                item.id = self.next_user_id

    def add(self, item):
        # Никаких дефолтов здесь: их применяет flush(), как настоящая сессия.
        self.added.append(item)


@override_settings(
    YOOKASSA_SHOP_ID='shop', YOOKASSA_SECRET_KEY='secret', WATA_HOST='https://wata.test', WATA_TOKEN='test',
    PAYMENT_ANON_IP_RATE_LIMIT=120, PAYMENT_ANON_EMAIL_RATE_LIMIT=10, PAYMENT_ANON_RATE_WINDOW_SECONDS=900,
    PAYMENT_ANON_GLOBAL_RATE_LIMIT=600, PAYMENT_ANON_GLOBAL_RATE_WINDOW_SECONDS=60,
)
class AnonymousDirectCheckoutTests(SimpleTestCase):
    """R03 (решение владельца): прямая покупка с лендинга без входа.

    email → сразу счёт; аккаунт по email создаётся до оплаты, но без пробной
    подписки в панели (B12); браузеру — только pstatus_-ссылка статуса, ссылка
    входа — только письмом (B01); сессия не авторизуется; лимиты по IP, email
    и общему объёму — до обращения к БД.
    """

    TARIFF = SimpleNamespace(price=199, db_tariff_id='month', description='month')

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.clock = 2_000_010.0
        patcher = mock.patch.object(rate_limit, '_now', side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_request(self, email, login_link_kind='purchase_permanent', launch_json=True, ip='198.51.100.20', user=None):
        data = {'email': email, 'tariff_id': 'month'}
        if login_link_kind:
            data['login_link_kind'] = login_link_kind
        headers = {'HTTP_X_PAYMENT_LAUNCH': 'new-tab', 'HTTP_ACCEPT': 'application/json'} if launch_json else {}
        request = RequestFactory().post('/pay/', data, REMOTE_ADDR=ip, **headers)
        request.user = user or SimpleNamespace(is_authenticated=False, id=None)
        request.session = SessionDict()
        return request

    def run_pay(self, email='buyer@example.test', existing_user=None, blocked=False, previous_attempt=None,
                gateway='yookassa', real_registration=False, provider_error=None, find_side_effect=None,
                send_email_error=None, session=None, **request_kwargs):
        from contextlib import ExitStack
        session = session if session is not None else _AnonymousCheckoutSession(existing_user)
        request = self.make_request(email, **request_kwargs)
        created_user = SimpleNamespace(id=81, email=email.strip().lower(), username='site-buyer', telegram_id=None)

        def provider(**kwargs):
            key = kwargs['idempotency_key']
            payload = {'orderId': key, 'amount': 199.0, 'currency': 'RUB'}
            kwargs['before_send'](payload)
            if provider_error is not None:
                # Неизвестный исход: prepared-попытка уже закоммичена.
                raise provider_error
            reference = key if gateway == 'wata' else 'yk-payment-1'
            return payments.CreatedPayment(f'https://{gateway}.test/pay/abc', reference, payload)

        with ExitStack() as stack:
            def patch(name, **kwargs):
                return stack.enter_context(mock.patch.object(views, name, **kwargs))

            stack.enter_context(override_settings(PAYMENT_GATEWAY=gateway))
            patch('session_factory', return_value=session)
            patch('get_runtime_actual_tariffs', return_value=[self.TARIFF])
            patch('is_user_blocked', return_value=blocked)
            find = patch('find_reusable_attempt', return_value=previous_attempt, side_effect=find_side_effect)
            patch('site_apply_first_purchase_discount', return_value=(self.TARIFF, False))
            patch('get_registration_context', return_value={'referrer': None, 'traffic_source': None, 'ymid': None})
            sync_tracking = patch('sync_existing_user_tracking')
            create_site_user = None if real_registration else patch('create_site_user', return_value=created_user)
            provider_mock = patch('create_wata_payment_sync' if gateway == 'wata' else 'create_yk_payment_sync',
                                  side_effect=provider)
            patch('add_event_log')
            send_email = patch('send_magic_link_email', side_effect=send_email_error)
            confirmation = patch('send_payment_email_confirmation')
            authorize = patch('authorize_user_session')
            patch('messages')
            stack.enter_context(mock.patch.object(checkout_attempts, 'save_wata_invoice'))
            response = views.pay(request)
        return SimpleNamespace(response=response, request=request, session=session, find=find,
                               create_site_user=create_site_user, provider=provider_mock, send_email=send_email,
                               sync_tracking=sync_tracking, confirmation=confirmation, authorize=authorize)

    def post_light(self, email, ip, launch_json=True, user=None):
        """Запрос до первого обращения к БД и отказ «тариф не найден» сразу после него."""
        request = self.make_request(email, launch_json=launch_json, ip=ip, user=user)
        with mock.patch.object(views, 'session_factory', side_effect=lambda: _AnonymousCheckoutSession()) as factory, \
                mock.patch.object(views, 'get_runtime_actual_tariffs', return_value=[]):
            response = views.pay(request)
        return response, factory.call_count

    def test_new_email_gets_invoice_and_login_link_only_by_email(self):
        for gateway in ('yookassa', 'wata'):
            with self.subTest(gateway=gateway):
                result = self.run_pay(gateway=gateway)
                content = result.response.content.decode()
                body = json.loads(content)
                self.assertEqual(result.response.status_code, 200)
                self.assertEqual(body['status'], 'ok')
                self.assertEqual(body['payment_url'], f'https://{gateway}.test/pay/abc')
                self.assertIn('/payment/status/pstatus_', body['payment_status_url'])
                self.assertNotIn('confirmation_required', body)
                # Аккаунт по email создаётся до оплаты, но пробная подписка — нет.
                result.create_site_user.assert_called_once_with(
                    result.session, 'buyer@example.test', result.request, allow_trial=False)
                attempt, = result.session.rows(WebsitePaymentAttempt)
                self.assertEqual((attempt.user_id, attempt.state, attempt.gateway), (81, 'ready', gateway))
                raw_status = body['payment_status_url'].rstrip('/').rsplit('/', 1)[-1]
                self.assertEqual(attempt.status_token_hash, views.hash_purchase_status_token(raw_status))
                # Постоянная ссылка входа — только письмом на введённый адрес.
                result.send_email.assert_called_once()
                address, link = result.send_email.call_args.args[:2]
                self.assertEqual(address, 'buyer@example.test')
                self.assertIn('/login/purchase/plogin_', link)
                raw_login = link.rstrip('/').rsplit('/', 1)[-1]
                self.assertEqual(attempt.login_token_hash, views.hash_purchase_login_token(raw_login))
                self.assertNotIn(raw_login, content)
                self.assertNotIn('plogin_', content)
                # Сессия не авторизована, pending-тариф старого потока не ставится.
                result.authorize.assert_not_called()
                self.assertNotIn(SESSION_KEY, result.request.session)
                self.assertNotIn('pending_checkout_tariff_id', result.request.session)
                self.assertEqual(result.request.session[views.payment_session_url_key(raw_status)], body['payment_url'])
                result.confirmation.assert_not_called()

    def test_plain_form_post_redirects_to_provider(self):
        result = self.run_pay(launch_json=False)
        self.assertEqual(result.response.status_code, 302)
        self.assertEqual(result.response['Location'], 'https://yookassa.test/pay/abc')
        self.assertNotIn(SESSION_KEY, result.request.session)
        result.send_email.assert_called_once()

    def test_existing_email_bills_that_account_without_signing_in(self):
        owner = SimpleNamespace(id=42, email='owner@example.test', username='owner-42', telegram_id=None)
        result = self.run_pay(email=' Owner@Example.test ', existing_user=owner)
        content = result.response.content.decode()
        body = json.loads(content)
        self.assertEqual(result.response.status_code, 200)
        result.create_site_user.assert_not_called()
        result.sync_tracking.assert_called_once()
        attempt, = result.session.rows(WebsitePaymentAttempt)
        self.assertEqual(attempt.user_id, 42)
        self.assertEqual(result.provider.call_args.kwargs['username'], 'owner-42')
        self.assertIn('/payment/status/pstatus_', body['payment_status_url'])
        address, link = result.send_email.call_args.args[:2]
        self.assertEqual(address, 'owner@example.test')
        self.assertIn('/login/purchase/plogin_', link)
        self.assertNotIn(link.rstrip('/').rsplit('/', 1)[-1], content)
        result.authorize.assert_not_called()
        self.assertNotIn(SESSION_KEY, result.request.session)

    @override_settings(SITE_TRIAL_REGISTRATION_ENABLED=True, SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=False)
    def test_enabled_site_trial_is_not_provisioned_before_email_confirmation(self):
        rwms = mock.Mock()
        rwms.get_user_by_username_strict.return_value = None
        with mock.patch.object(views, 'rwms_client', rwms), \
                mock.patch.object(views, 'should_create_trial_for_channel', return_value=True), \
                mock.patch.object(views, 'lock_registration_email') as lock_email, \
                mock.patch.object(views, 'lock_registration_telegram_id'), \
                mock.patch.object(views, 'add_user_to_traffic_progress'), \
                mock.patch.object(views, 'create_user') as add_user:
            result = self.run_pay(email='trial@example.test', real_registration=True)
        self.assertEqual(result.response.status_code, 200)
        add_user.assert_not_called()
        rwms.add_user.assert_not_called()
        rwms.get_user_by_username_strict.assert_called_once()
        lock_email.assert_called_once()
        new_user, = result.session.rows(User)
        self.assertEqual((new_user.email, new_user.expire_at), ('trial@example.test', None))
        attempt, = result.session.rows(WebsitePaymentAttempt)
        self.assertEqual(attempt.user_id, new_user.id)
        self.assertNotIn(SESSION_KEY, result.request.session)

    def test_malformed_new_email_is_rejected_without_writes(self):
        with self.assertLogs(level='WARNING'):
            result = self.run_pay(email='broken@@example')
        self.assertEqual(result.response.status_code, 400)
        self.assertIn('опечатка', json.loads(result.response.content)['message'])
        result.create_site_user.assert_not_called()
        result.provider.assert_not_called()
        result.send_email.assert_not_called()
        self.assertEqual(result.session.added, [])
        result.session.commit.assert_not_called()

    def test_blocked_account_is_refused_without_invoice(self):
        owner = SimpleNamespace(id=42, email='blocked@example.test', username='blocked-42', telegram_id=None)
        with self.assertLogs(level='WARNING'):
            result = self.run_pay(email='blocked@example.test', existing_user=owner, blocked=True)
        self.assertEqual(result.response.status_code, 403)
        self.assertEqual(json.loads(result.response.content),
                         {'status': 'error', 'message': views.ACCOUNT_BLOCKED_MESSAGE})
        result.provider.assert_not_called()
        result.send_email.assert_not_called()
        self.assertEqual(result.session.rows(WebsitePaymentAttempt), [])

    def test_repeat_in_reuse_window_returns_previous_attempt_without_new_link(self):
        owner = SimpleNamespace(id=42, email='owner@example.test', username='owner-42', telegram_id=None)
        previous = SimpleNamespace(status_url='http://testserver/payment/status/pstatus_previous/',
                                   confirmation_url='https://yookassa.test/pay/previous')
        result = self.run_pay(email='owner@example.test', existing_user=owner, previous_attempt=previous)
        body = json.loads(result.response.content)
        self.assertEqual((body['payment_url'], body['payment_status_url']),
                         (previous.confirmation_url, previous.status_url))
        self.assertEqual(result.find.call_args.args[1], 42)
        result.provider.assert_not_called()
        result.send_email.assert_not_called()
        result.session.rollback.assert_called_once_with()
        self.assertNotIn('plogin_', result.response.content.decode())
        self.assertNotIn(SESSION_KEY, result.request.session)

    def test_without_permanent_kind_short_magic_link_goes_only_to_email(self):
        result = self.run_pay(login_link_kind=None)
        attempt, = result.session.rows(WebsitePaymentAttempt)
        self.assertIsNone(attempt.login_token_hash)
        address, link = result.send_email.call_args.args[:2]
        self.assertEqual(address, 'buyer@example.test')
        self.assertTrue(link.endswith('/login/magic/magic-short-token/'))
        self.assertNotIn('magic-short-token', result.response.content.decode())
        self.assertNotIn(SESSION_KEY, result.request.session)

    @override_settings(SITE_TRIAL_REGISTRATION_ENABLED=False, SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=False)
    def test_rwms_outage_does_not_block_anonymous_purchase_for_new_email(self):
        # FINAL-PAY-01: как в HEAD — аккаунт без подписки и счёт; подписку сведёт payment по username.
        rwms = mock.Mock()
        rwms.get_user_by_username_strict.side_effect = RwmsUnavailableError('site-user', None, 'deadline exceeded')
        with mock.patch.object(views, 'rwms_client', rwms), \
                mock.patch.object(views, 'lock_registration_email'), \
                mock.patch.object(views, 'lock_registration_telegram_id'), \
                mock.patch.object(views, 'add_user_to_traffic_progress'), \
                mock.patch.object(views, 'create_user') as add_user, \
                self.assertLogs(level='ERROR') as logs:
            result = self.run_pay(email='outage@example.test', real_registration=True)
        body = json.loads(result.response.content)
        self.assertEqual((result.response.status_code, body['status']), (200, 'ok'))
        self.assertEqual(body['payment_url'], 'https://yookassa.test/pay/abc')
        add_user.assert_not_called()
        rwms.add_user.assert_not_called()
        rwms.get_all_users.assert_not_called()
        new_user, = result.session.rows(User)
        self.assertEqual((new_user.email, new_user.expire_at), ('outage@example.test', None))
        self.assertEqual(new_user.username, views.site_registration_username('outage@example.test'))
        attempt, = result.session.rows(WebsitePaymentAttempt)
        self.assertEqual(attempt.user_id, new_user.id)
        self.assertEqual(result.provider.call_args.kwargs['username'], new_user.username)
        self.assertTrue(any('ALERT: RWMS unavailable during anonymous purchase' in line for line in logs.output))
        self.assertNotIn(SESSION_KEY, result.request.session)

    def test_unknown_outcome_still_emails_committed_permanent_login_link(self):
        # FINAL-PAY-02: таймаут провайдера после commit prepared-попытки с plogin_-токеном.
        success = self.run_pay()
        for gateway in ('yookassa', 'wata'):
            with self.subTest(gateway=gateway):
                with self.assertLogs(level='WARNING') as logs:
                    result = self.run_pay(gateway=gateway, provider_error=httpx.ReadTimeout('timed out'))
                content = result.response.content.decode()
                body = json.loads(content)
                self.assertEqual((result.response.status_code, body['status']), (200, 'ok'))
                self.assertIn('/payment/status/pstatus_', body['payment_url'])
                self.assertEqual(body['payment_url'], body['payment_status_url'])
                attempt, = result.session.rows(WebsitePaymentAttempt)
                self.assertEqual(attempt.state, 'prepared')
                result.session.rollback.assert_called()
                self.assertTrue(any('checkout result unknown' in line for line in logs.output))
                result.send_email.assert_called_once()
                address, link = result.send_email.call_args.args[:2]
                self.assertEqual(address, 'buyer@example.test')
                self.assertIn('/login/purchase/plogin_', link)
                raw_login = link.rstrip('/').rsplit('/', 1)[-1]
                self.assertEqual(attempt.login_token_hash, views.hash_purchase_login_token(raw_login))
                self.assertNotIn('plogin_', content)
                # Тот же шаблон письма, что после успешного создания платежа.
                self.assertEqual(result.send_email.call_args.kwargs, success.send_email.call_args.kwargs)
                self.assertEqual(result.send_email.call_args.kwargs['subject'], 'Ссылка доступа VPN Monkey Island')
                result.authorize.assert_not_called()
                self.assertNotIn(SESSION_KEY, result.request.session)

    def test_unknown_outcome_login_email_failure_is_only_logged(self):
        with self.assertLogs(level='ERROR') as logs:
            result = self.run_pay(provider_error=httpx.ReadTimeout('timed out'),
                                  send_email_error=RuntimeError('smtp down'))
        self.assertEqual((result.response.status_code, json.loads(result.response.content)['status']), (200, 'ok'))
        result.send_email.assert_called_once()
        self.assertTrue(any('failed to send payment login email after unknown checkout outcome' in line
                            for line in logs.output))

    def test_unknown_outcome_without_committed_login_token_sends_nothing(self):
        owner = SimpleNamespace(id=42, email='owner@example.test', username='owner-42', telegram_id=None)
        cases = [
            ('short magic kind', dict(login_link_kind=None)),
            ('signed-in owner', dict(email='owner@example.test', existing_user=owner,
                                     user=SimpleNamespace(is_authenticated=True, id=42))),
        ]
        for label, kwargs in cases:
            with self.subTest(label):
                with self.assertLogs(level='WARNING'):
                    result = self.run_pay(provider_error=httpx.ReadTimeout('timed out'), **kwargs)
                self.assertEqual(json.loads(result.response.content)['status'], 'ok')
                attempt, = result.session.rows(WebsitePaymentAttempt)
                self.assertIsNone(attempt.login_token_hash)
                result.send_email.assert_not_called()

    def test_existing_email_row_is_locked_before_attempt_lookup(self):
        # FINAL-PAY-03: анонимная ветка берёт users FOR UPDATE до find_reusable_attempt.
        owner = SimpleNamespace(id=42, email='owner@example.test', username='owner-42', telegram_id=None)
        events = []

        class LockRecordingSession(_AnonymousCheckoutSession):
            def query(self, model, *args):
                query = super().query(model, *args)
                if model is User:
                    locked = query.with_for_update

                    def with_for_update(**kwargs):
                        events.append('users FOR UPDATE')
                        return locked(**kwargs)
                    query.with_for_update = with_for_update
                return query

        def find(session, user_id, fingerprint, username=None):
            events.append('find_reusable_attempt')
            return None

        result = self.run_pay(email='owner@example.test', session=LockRecordingSession(owner), find_side_effect=find)
        self.assertEqual(result.response.status_code, 200)
        self.assertEqual(events, ['users FOR UPDATE', 'find_reusable_attempt'])
        attempt, = result.session.rows(WebsitePaymentAttempt)
        self.assertEqual(attempt.user_id, 42)

    def test_yookassa_metadata_email_is_account_email_not_receipt_email(self):
        # XSVC-01: metadata.email — users.email; email чека в metadata не попадает.
        tg_user = SimpleNamespace(id=71, email=None, username='tg-71', telegram_id=777)
        result = self.run_pay(email='receipt@example.test', existing_user=tg_user,
                              user=SimpleNamespace(is_authenticated=True, id=71))
        kwargs = result.provider.call_args.kwargs
        self.assertEqual((kwargs['email'], kwargs['account_email'], kwargs['username'], kwargs['telegram_id']),
                         ('receipt@example.test', None, 'tg-71', 777))
        owner = SimpleNamespace(id=42, email='owner@example.test', username='owner-42', telegram_id=None)
        result = self.run_pay(email='owner@example.test', existing_user=owner)
        self.assertEqual(result.provider.call_args.kwargs['account_email'], 'owner@example.test')
        self.assertEqual(result.find.call_args.kwargs['username'], 'owner-42')

    def merged_attempt_finder(self, gateway, fingerprint):
        """SQLite: попытка проигравшего (user 71) после rebind бота на выжившего (user 72)."""
        sqlite = _sqlite_sessionmaker(self, WebsitePaymentAttempt, YkPayment, WataInvoice, WataTransaction)()
        self.addCleanup(sqlite.close)
        sqlite.add(WebsitePaymentAttempt(
            id='loser-attempt', user_id=71, fingerprint=fingerprint, gateway=gateway, tariff_id='month', state='ready',
            request_payload=({'metadata': {'username': 'site-loser', 'telegram_id': 0}} if gateway == 'yookassa'
                             else {'orderId': 'loser-attempt', 'amount': 199.0, 'currency': 'RUB'}),
            status_token_hash='status-loser', status_url='http://testserver/payment/status/pstatus_loser/',
            provider_reference='loser-attempt' if gateway == 'wata' else 'yk-loser',
            confirmation_url=f'https://{gateway}.test/pay/loser',
            created_at=datetime.utcnow() - timedelta(minutes=3), attempts=0,
        ))
        sqlite.commit()
        # Merge бота (handlers/menu.py): попытки проигравшего переходят выжившему как есть.
        sqlite.query(WebsitePaymentAttempt).filter_by(user_id=71).update({'user_id': 72})
        sqlite.commit()
        real_find = checkout_attempts.find_reusable_attempt
        return lambda session, user_id, fp, username=None: real_find(sqlite, user_id, fp, username=username)

    def test_survivor_after_merge_gets_new_yookassa_payment_but_reuses_wata_link(self):
        # XSVC-01/ZONE-BIND-01: ссылка ЮKassa проигравшего выжившему не отдаётся.
        survivor = SimpleNamespace(id=72, email='merged@example.test', username='tg-survivor', telegram_id=777)
        fingerprint = checkout_attempts.fingerprint_for(72, 'yookassa', 'month', 'merged@example.test',
                                                        price=199, promo=False)
        with self.assertLogs(checkout_attempts.logger, 'WARNING'):
            result = self.run_pay(email='merged@example.test', existing_user=survivor,
                                  find_side_effect=self.merged_attempt_finder('yookassa', fingerprint))
        body = json.loads(result.response.content)
        self.assertEqual(body['payment_url'], 'https://yookassa.test/pay/abc')
        result.provider.assert_called_once()
        kwargs = result.provider.call_args.kwargs
        self.assertEqual((kwargs['username'], kwargs['telegram_id'], kwargs['account_email']),
                         ('tg-survivor', 777, 'merged@example.test'))
        attempt, = result.session.rows(WebsitePaymentAttempt)
        self.assertNotEqual(attempt.id, 'loser-attempt')
        self.assertEqual(kwargs['idempotency_key'], attempt.id)
        self.assertEqual(result.find.call_args.kwargs['username'], 'tg-survivor')

        wata_fingerprint = checkout_attempts.fingerprint_for(72, 'wata', 'month', 'merged@example.test',
                                                             price=199, promo=False)
        result = self.run_pay(email='merged@example.test', existing_user=survivor, gateway='wata',
                              find_side_effect=self.merged_attempt_finder('wata', wata_fingerprint))
        body = json.loads(result.response.content)
        self.assertEqual(body['payment_url'], 'https://wata.test/pay/loser')
        result.provider.assert_not_called()
        self.assertEqual(result.session.rows(WebsitePaymentAttempt), [])

    @override_settings(PAYMENT_ANON_IP_RATE_LIMIT=2)
    def test_ip_limit_answers_429_before_database(self):
        for i in range(2):
            response, calls = self.post_light(f'buyer{i}@example.test', '198.51.100.7')
            self.assertEqual((response.status_code, calls), (400, 1))
            self.assertEqual(json.loads(response.content),
                             {'status': 'error', 'message': 'Выбранный тариф не найден'})
        with self.assertLogs(level='WARNING'):
            response, calls = self.post_light('late@example.test', '198.51.100.7')
        self.assertEqual((response.status_code, calls, response['Retry-After']), (429, 0, '900'))
        self.assertEqual(json.loads(response.content),
                         {'status': 'error', 'message': views.PAYMENT_RATE_LIMITED_MESSAGE})
        self.assertEqual(views.PAYMENT_RATE_LIMITED_MESSAGE,
                         'Слишком много попыток оплаты. Подождите несколько минут и попробуйте снова.')
        # Форма без JS получает тот же отказ текстом.
        with self.assertLogs(level='WARNING'):
            plain, calls = self.post_light('late@example.test', '198.51.100.7', launch_json=False)
        self.assertEqual((plain.status_code, calls, plain['Retry-After']), (429, 0, '900'))
        self.assertEqual(plain.content.decode(), views.PAYMENT_RATE_LIMITED_MESSAGE)
        # Соседний IP не задет.
        self.assertEqual(self.post_light('other@example.test', '198.51.100.8')[0].status_code, 400)

    @override_settings(PAYMENT_ANON_EMAIL_RATE_LIMIT=2)
    def test_email_limit_spans_ips_and_other_addresses_still_pass(self):
        for i in range(2):
            self.assertEqual(self.post_light('victim@example.test', f'198.51.100.{i + 1}')[0].status_code, 400)
        with self.assertLogs(level='WARNING') as logs:
            response, calls = self.post_light('victim@example.test', '198.51.100.50')
        self.assertEqual((response.status_code, calls, response['Retry-After']), (429, 0, '900'))
        self.assertNotIn('victim', '\n'.join(logs.output))
        self.assertEqual(self.post_light('someone@example.test', '198.51.100.50')[0].status_code, 400)

    @override_settings(PAYMENT_ANON_GLOBAL_RATE_LIMIT=3, PAYMENT_ANON_GLOBAL_RATE_WINDOW_SECONDS=60)
    def test_global_limit_uses_its_own_window(self):
        for i in range(3):
            self.assertEqual(self.post_light(f'user{i}@example.test', f'198.51.100.{i + 1}')[0].status_code, 400)
        with self.assertLogs(level='WARNING'):
            response, calls = self.post_light('late@example.test', '198.51.100.99')
        self.assertEqual((response.status_code, calls, response['Retry-After']), (429, 0, '60'))

    @override_settings(PAYMENT_ANON_EMAIL_RATE_LIMIT=1, PAYMENT_ANON_GLOBAL_RATE_LIMIT=1)
    def test_malformed_addresses_do_not_consume_email_or_global_buckets(self):
        for i in range(3):
            self.assertEqual(self.post_light('broken@@example', f'198.51.100.{i + 1}')[0].status_code, 400)
        self.assertEqual(self.post_light('real@example.test', '198.51.100.9')[0].status_code, 400)

    @override_settings(PAYMENT_ANON_IP_RATE_LIMIT=1, PAYMENT_ANON_EMAIL_RATE_LIMIT=1, PAYMENT_ANON_GLOBAL_RATE_LIMIT=1)
    def test_signed_in_payment_is_not_rate_limited(self):
        user = SimpleNamespace(is_authenticated=True, id=71)
        for _ in range(3):
            response, calls = self.post_light('owner@example.test', '198.51.100.7', user=user)
            self.assertEqual((response.status_code, calls), (400, 1))


class CheckoutRegressionTests(SimpleTestCase):
    def test_wata_mismatched_response_does_not_bind_tokens_or_invoice(self):
        attempt = WebsitePaymentAttempt(id='expected', gateway='wata', state='prepared')
        for reference, order_id in [('other', 'expected'), ('expected', 'other')]:
            with self.subTest(reference=reference, order_id=order_id):
                session = Session(attempt)
                created = payments.CreatedPayment('https://pay.test', reference, {'orderId':order_id})
                with mock.patch.object(checkout_attempts, 'save_wata_invoice') as save:
                    with self.assertRaises(ValueError):
                        checkout_attempts.finish_attempt(session, attempt, created)
                save.assert_not_called()
                self.assertEqual(attempt.state, 'prepared')

    def test_persistence_failure_prevents_provider_call(self):
        tariff = SimpleNamespace(price=199, description='one month', db_tariff_id='Month1')
        with mock.patch.object(payments.TimeoutPayment, 'create') as create:
            with self.assertRaises(RuntimeError):
                payments.create_yk_payment_sync('shop','secret',tariff,'owner',None,'https://site.test', idempotency_key='fixed', before_send=mock.Mock(side_effect=RuntimeError('commit failed')))
        create.assert_not_called()

    def test_provider_uses_exact_persisted_key_and_payload(self):
        tariff = SimpleNamespace(price=199, description='one month', db_tariff_id='Month1')
        saved = []
        def create(body, key):
            self.assertEqual(saved, [body])
            self.assertEqual(key, 'stable-key')
            return SimpleNamespace(id='payment-id', confirmation=SimpleNamespace(confirmation_url='https://pay.test'))
        with mock.patch.object(payments.TimeoutPayment, 'create', side_effect=create):
            payments.create_yk_payment_sync('shop','secret',tariff,'owner',None,'https://site.test', idempotency_key='stable-key', before_send=saved.append)

    def test_expired_unknown_attempt_never_reposts(self):
        attempt = WebsitePaymentAttempt(id='attempt', state='prepared', gateway='yookassa', created_at=datetime.utcnow()-timedelta(hours=1), next_attempt_at=datetime.utcnow()-timedelta(seconds=1))
        session = Session(attempt)
        with mock.patch.object(checkout_attempts, 'recover_prepared_payment') as recover:
            self.assertFalse(checkout_attempts.reconcile_payment_attempt(lambda:session, SimpleNamespace(), 'attempt'))
        recover.assert_not_called()
        self.assertEqual(attempt.state, 'review')

    def test_wata_recovery_never_posts_when_link_missing(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get.return_value.json.return_value = {'items': []}
        config = SimpleNamespace(WATA_HOST='https://wata.test', WATA_TOKEN='secret')
        with mock.patch.object(payments.httpx, 'Client', return_value=client):
            result = payments.recover_prepared_payment('wata', {'amount':199.0,'currency':'RUB'}, 'known-order', config)
        self.assertIsNone(result)
        client.post.assert_not_called()
        self.assertEqual(client.get.call_args.kwargs['params']['orderId'], 'known-order')


class CheckoutViewOrderTests(SimpleTestCase):
    @override_settings(PAYMENT_GATEWAY='wata', WATA_HOST='https://wata.test', WATA_TOKEN='test')
    def test_verified_checkout_commits_mapping_before_provider_and_completes(self):
        from contextlib import ExitStack
        from common.models.db import User, PurchaseLoginToken, WataInvoice
        events = []
        user = SimpleNamespace(id=71, email='owner@example.test', username='existing-user', telegram_id=None)
        token = SimpleNamespace(token_hash='status-hash', payment_gateway=None, payment_reference=None)
        tariff = SimpleNamespace(price=199, db_tariff_id='month', description='month')
        class FakeSession(Session):
            def __init__(self):
                super().__init__()
                self.commit = lambda: events.append('commit')
                self.rollback = mock.Mock()
                self.flush = mock.Mock()
            def query(self, model):
                if model is User: return Query(user)
                if model is WebsitePaymentAttempt: return Query(self.value)
                if model is PurchaseLoginToken: return Query(token)
                if model is WataInvoice: return Query(None)
                raise AssertionError(f'unexpected model {model}')
        session = FakeSession()
        def provider(**kwargs):
            payload = {'orderId':kwargs['idempotency_key'], 'amount':199.0, 'currency':'RUB'}
            kwargs['before_send'](payload)
            self.assertEqual(events, ['commit'])
            self.assertEqual(session.value.request_payload, payload)
            self.assertEqual(token.payment_reference, kwargs['idempotency_key'])
            events.append('provider')
            return payments.CreatedPayment('https://wata.test/pay', kwargs['idempotency_key'], payload)
        request = RequestFactory().post('/pay', {'email':user.email, 'tariff_id':'month', 'login_link_kind':'purchase_permanent'}, HTTP_X_PAYMENT_LAUNCH='new-tab', HTTP_ACCEPT='application/json')
        request.user = SimpleNamespace(is_authenticated=True, id=user.id, email=user.email)
        request.session = SessionDict()
        with ExitStack() as stack:
            for name, value in [('session_factory',session),('get_runtime_actual_tariffs',[tariff]),('is_user_blocked',False),('create_purchase_status_token','pstatus_test'),('get_purchase_status_token',token),('find_reusable_attempt',None),('site_apply_first_purchase_discount',(tariff,False)),('should_send_payment_login_email',False)]:
                stack.enter_context(mock.patch.object(views,name,return_value=value))
            stack.enter_context(mock.patch.object(views,'create_wata_payment_sync',side_effect=provider))
            stack.enter_context(mock.patch.object(views,'add_event_log'))
            save = stack.enter_context(mock.patch.object(checkout_attempts,'save_wata_invoice'))
            response = views.pay(request)
        self.assertEqual(response.status_code,200)
        self.assertEqual(json.loads(response.content)['payment_url'],'https://wata.test/pay')
        self.assertEqual(events, ['commit','provider','commit'])
        self.assertEqual(session.value.state,'ready')
        self.assertEqual(session.value.user_id,user.id)
        save.assert_called_once()


class PerformanceRegressionTests(SimpleTestCase):
    def test_report_deadline_does_not_queue_all_500_requests(self):
        import time
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from engine.node_traffic import _bounded_fetch
        release = threading.Event()
        calls = []
        def fetch(item):
            calls.append(item)
            release.wait(1)
            return item
        pool = ThreadPoolExecutor(max_workers=3)
        started = time.monotonic()
        try:
            results = _bounded_fetch(pool, list(range(500)), fetch, 3, started + 0.02)
            self.assertEqual(results, [])
            self.assertLessEqual(len(calls), 3)
            self.assertLess(time.monotonic()-started, 0.5)
        finally:
            release.set()
            pool.shutdown(wait=True, cancel_futures=True)

    def test_aggregation_batches_without_per_bucket_selects(self):
        from engine.infra import _upsert_agg_batch
        from sqlalchemy.dialects import postgresql
        session = mock.Mock()
        session.get_bind.return_value.dialect.name = 'postgresql'
        rows = [{'server_id':1, 'bucket_seconds':60, 'bucket_start':datetime(2026,1,1)+timedelta(minutes=i), 'rx_bytes':i, 'sample_count':1} for i in range(1200)]
        _upsert_agg_batch(session,rows)
        self.assertEqual(session.execute.call_count,3)
        session.query.assert_not_called()
        sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn('ON CONFLICT (server_id, bucket_seconds, bucket_start) DO UPDATE',sql)

    def test_concurrent_node_summary_computes_once(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        from engine import infra
        barrier = threading.Barrier(3)
        now = datetime.utcnow()
        def build(*args):
            time.sleep(0.02)
            return {'total':42}
        def request(_):
            barrier.wait()
            return infra.who_connects_payload(object(),'test-single-flight-node',now)
        with infra._who_connects_lock:
            infra._who_connects_cache.clear()
        with mock.patch.object(infra,'_who_connects_payload_uncached',side_effect=build) as calculate, ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(request,range(3)))
        self.assertEqual(results,[{'total':42}]*3)
        calculate.assert_called_once()

    def test_report_cache_reuses_result_but_auth_is_always_checked(self):
        from engine.report_cache import report_response
        from django.http import JsonResponse, HttpResponse
        from django.core.cache import cache
        cache.clear()
        build = mock.Mock(return_value=JsonResponse({'status':'ok','result':42}))
        first = report_response('test', ['2026-01-01'],build)
        second = report_response('test', ['2026-01-01'],build)
        self.assertEqual(first.content,second.content)
        self.assertEqual(second['X-Report-Cache'],'hit')
        build.assert_called_once()
        with mock.patch.object(views,'require_support_admin_any',return_value=HttpResponse(status=403)), mock.patch.object(views,'report_response') as cached:
            response=views.support_admin_api_stats(RequestFactory().get('/stats'))
        self.assertEqual(response.status_code,403)
        cached.assert_not_called()
        cache.clear()

    def test_worker_budget_defers_work_without_starving_next_step(self):
        from engine import infra_worker as worker
        clock=[100.0]
        ran=[]
        def step(name, fn):
            ran.append(name)
            clock[0]+=40
            return True
        with override_settings(INFRA_WORKER_INTERVAL=1, INFRA_MAINTENANCE_BUDGET_SECONDS=30), mock.patch.object(worker,'_maintenance_due',{}), mock.patch.object(worker,'_infra_tables_ready',return_value=True), mock.patch('engine.geoip_updater.update_if_due'), mock.patch.object(worker.time,'monotonic',side_effect=lambda:clock[0]), mock.patch.object(worker,'_run_step',side_effect=step):
            worker.run_maintenance()
            worker.run_maintenance()
        self.assertEqual(ran[:2], ['offline','aggregate'])


def _sqlite_sessionmaker(test, *models):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in models:
        model.__table__.create(engine)
    test.addCleanup(engine.dispose)
    return sessionmaker(bind=engine)


class _AttemptFixtureMixin:
    """SQLite in-memory: website_payment_attempts + таблицы исхода провайдера."""

    def setUp(self):
        self.Session = _sqlite_sessionmaker(self, WebsitePaymentAttempt, YkPayment, WataInvoice, WataTransaction)
        self.session = self.Session()
        self.addCleanup(self.session.close)

    def add_attempt(self, attempt_id, state, age, gateway='wata', fingerprint='fp', user_id=71, **extra):
        now = datetime.utcnow()
        values = dict(
            id=attempt_id, user_id=user_id, fingerprint=fingerprint, gateway=gateway, tariff_id='month', state=state,
            request_payload={'orderId': attempt_id, 'amount': 199.0, 'currency': 'RUB'},
            status_token_hash=f'status-{attempt_id}', status_url=f'https://site.test/s/{attempt_id}',
            created_at=now - age, attempts=0,
        )
        if state == 'ready':
            values.update(provider_reference=attempt_id if gateway == 'wata' else f'yk-{attempt_id}',
                          confirmation_url=f'https://pay.test/{attempt_id}')
        if state == 'prepared':
            values['next_attempt_at'] = now - timedelta(seconds=1)
        values.update(extra)
        self.session.add(WebsitePaymentAttempt(**values))
        self.session.commit()

    def add_invoice(self, row_id, order_id, expires_in):
        now = datetime.utcnow()
        self.session.add(WataInvoice(
            id=row_id, user_id=71, invoice_id=f'inv-{order_id}', amount=199, currency='RUB', status='Opened',
            url='https://wata.test/pay', terminal_name='t', terminal_public_id='tp', creation_time=now,
            order_id=order_id, expiration_datetime=now + expires_in, tariff_id='month',
        ))
        self.session.commit()

    def add_wata_transaction(self, row_id, order_id, status):
        self.session.add(WataTransaction(
            id=row_id, transaction_id=f'tx-{row_id}', transaction_type='Payment', terminal_public_id='tp',
            transaction_status=status, terminal_name='t', amount=199, currency='RUB', order_id=order_id,
            order_description='month', commission=0, payment_time=datetime.utcnow(),
        ))
        self.session.commit()

    def add_yk_payment(self, row_id, payment_id, status):
        self.session.add(YkPayment(
            id=row_id, user_id=71, amount=199, currency='RUB', status=status, created_at=datetime.utcnow(),
            payment_id=payment_id, subscription_period='month',
        ))
        self.session.commit()

    def reuse(self, fingerprint='fp', user_id=71):
        found = checkout_attempts.find_reusable_attempt(self.session, user_id, fingerprint)
        return None if found is None else found.id

    def fetch(self, attempt_id):
        with self.Session() as session:
            return session.query(WebsitePaymentAttempt).filter_by(id=attempt_id).one()


class CheckoutReuseWindowTests(_AttemptFixtureMixin, SimpleTestCase):
    """PAY-01/PAY-02/PAY-03/F1/OPS-5: блокировка нового заказа ограничена окнами."""

    def test_fingerprint_is_backward_compatible_and_includes_price_and_promo(self):
        legacy = hashlib.sha256(json.dumps(['wata', 'month', 'a@example.test'], separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(checkout_attempts.fingerprint_for(1, 'wata', 'month', 'a@example.test'), legacy)
        priced = checkout_attempts.fingerprint_for(1, 'wata', 'month', 'a@example.test', price=199, promo=False)
        self.assertNotEqual(priced, legacy)
        self.assertNotEqual(priced, checkout_attempts.fingerprint_for(1, 'wata', 'month', 'a@example.test', price=99, promo=False))
        self.assertNotEqual(priced, checkout_attempts.fingerprint_for(1, 'wata', 'month', 'a@example.test', price=199, promo=True))
        self.assertEqual(priced, checkout_attempts.fingerprint_for(2, 'wata', 'month', 'a@example.test', price=199, promo=False))

    def test_prepared_is_reused_only_within_recovery_window(self):
        self.add_attempt('young', 'prepared', timedelta(minutes=19), fingerprint='young')
        self.add_attempt('old', 'prepared', timedelta(minutes=21), fingerprint='old')
        self.assertEqual(self.reuse('young'), 'young')
        self.assertIsNone(self.reuse('old'))

    def test_review_and_failed_never_block_new_checkout(self):
        self.add_attempt('review', 'review', timedelta(minutes=16), next_attempt_at=None)
        self.add_attempt('failed', 'failed', timedelta(minutes=1), next_attempt_at=None)
        self.assertIsNone(self.reuse())
        self.add_attempt('review-30d', 'review', timedelta(days=30), fingerprint='month-old', next_attempt_at=None)
        self.assertIsNone(self.reuse('month-old'))

    def test_ready_yookassa_is_reused_only_while_payment_is_open(self):
        for row_id, status, reused in [(1, None, True), (2, 'pending', True), (3, 'canceled', False), (4, 'succeeded', False)]:
            with self.subTest(status=status):
                fingerprint = f'yk-{row_id}'
                self.add_attempt(f'r{row_id}', 'ready', timedelta(minutes=5), gateway='yookassa', fingerprint=fingerprint)
                if status:
                    self.add_yk_payment(row_id, f'yk-r{row_id}', status)
                self.assertEqual(self.reuse(fingerprint), f'r{row_id}' if reused else None)

    def test_ready_wata_paid_or_expiring_link_is_not_reused(self):
        cases = [
            ('open', timedelta(minutes=10), None, True),
            ('declined', timedelta(minutes=10), 'Declined', True),
            ('pending-tx', timedelta(minutes=10), 'Pending', True),
            ('paid', timedelta(minutes=10), 'Paid', False),
            ('expiring', timedelta(minutes=1), None, False),
        ]
        for index, (name, expires_in, tx_status, reused) in enumerate(cases, start=1):
            with self.subTest(case=name):
                self.add_attempt(name, 'ready', timedelta(minutes=3), fingerprint=name)
                self.add_invoice(index, name, expires_in)
                if tx_status:
                    self.add_wata_transaction(index, name, tx_status)
                self.assertEqual(self.reuse(name), name if reused else None)

    def test_ready_wata_without_invoice_uses_link_lifetime_from_creation(self):
        self.add_attempt('fresh', 'ready', timedelta(minutes=5), fingerprint='fresh')
        self.add_attempt('late', 'ready', timedelta(minutes=14), fingerprint='late')
        self.assertEqual(self.reuse('fresh'), 'fresh')
        self.assertIsNone(self.reuse('late'))

    def test_ready_older_than_window_is_not_reused(self):
        self.add_attempt('stale', 'ready', timedelta(minutes=16), gateway='yookassa')
        self.assertIsNone(self.reuse())

    def test_prepared_wins_over_newer_terminal_ready_attempt(self):
        self.add_attempt('unknown', 'prepared', timedelta(minutes=10))
        self.add_attempt('paid', 'ready', timedelta(minutes=1))
        self.add_wata_transaction(1, 'paid', 'Paid')
        self.assertEqual(self.reuse(), 'unknown')

    def test_other_owner_or_fingerprint_is_ignored(self):
        self.add_attempt('mine', 'prepared', timedelta(minutes=1))
        self.assertIsNone(self.reuse(user_id=72))
        self.assertIsNone(self.reuse('other'))

    def test_link_expiry_margin_handles_naive_and_aware_datetimes(self):
        margin = checkout_attempts.WATA_LINK_MIN_REMAINING
        self.assertTrue(checkout_attempts._expires_within(datetime.now(timezone.utc) + timedelta(minutes=1), margin))
        self.assertFalse(checkout_attempts._expires_within(datetime.now(timezone.utc) + timedelta(minutes=10), margin))
        self.assertTrue(checkout_attempts._expires_within(datetime.utcnow() + timedelta(minutes=1), margin))
        self.assertFalse(checkout_attempts._expires_within(datetime.utcnow() + timedelta(minutes=10), margin))


class CheckoutAttemptStateTests(_AttemptFixtureMixin, SimpleTestCase):
    """PAY-02/PAY-07/PAY-08/EMAIL-06: failed/review и изоляция ошибок воркера."""

    def test_mark_attempt_failed_changes_only_prepared_attempts(self):
        self.add_attempt('prepared', 'prepared', timedelta(minutes=1))
        self.add_attempt('ready', 'ready', timedelta(minutes=1), fingerprint='other')
        self.assertTrue(checkout_attempts.mark_attempt_failed(self.session, 'prepared'))
        self.assertFalse(checkout_attempts.mark_attempt_failed(self.session, 'ready'))
        self.assertEqual((self.fetch('prepared').state, self.fetch('prepared').next_attempt_at), ('failed', None))
        self.assertEqual(self.fetch('ready').state, 'ready')

    def test_pending_payment_ids_filters_gateway_and_recovery_window(self):
        self.add_attempt('wata-5', 'prepared', timedelta(minutes=5), fingerprint='a')
        self.add_attempt('yk-5', 'prepared', timedelta(minutes=5), gateway='yookassa', fingerprint='b')
        self.add_attempt('yk-16', 'prepared', timedelta(minutes=16), gateway='yookassa', fingerprint='c')
        self.add_attempt('wata-later', 'prepared', timedelta(minutes=1), fingerprint='d',
                         next_attempt_at=datetime.utcnow() + timedelta(minutes=1))
        self.add_attempt('ready', 'ready', timedelta(minutes=1), fingerprint='e', next_attempt_at=datetime.utcnow() - timedelta(minutes=1))
        pending = checkout_attempts.pending_payment_ids
        self.assertEqual(sorted(pending(self.session, limit=10)), ['wata-5', 'yk-16', 'yk-5'])
        self.assertEqual(len(pending(self.session)), 1)
        self.assertEqual(pending(self.session, limit=10, gateway='wata', recoverable=True), ['wata-5'])
        self.assertEqual(pending(self.session, limit=10, gateway='yookassa', recoverable=True), ['yk-5'])
        self.assertEqual(pending(self.session, limit=10, recoverable=False), ['yk-16'])

    def test_yookassa_replay_rejection_marks_attempt_failed(self):
        self.add_attempt('yk', 'prepared', timedelta(minutes=5), gateway='yookassa')
        with mock.patch.object(checkout_attempts, 'recover_prepared_payment',
                               side_effect=payments.ProviderRejected('YooKassa rejected payment creation: UnauthorizedError')), \
                self.assertLogs(checkout_attempts.logger, 'ERROR') as logs:
            self.assertFalse(checkout_attempts.reconcile_payment_attempt(self.Session, SimpleNamespace(), 'yk'))
        attempt = self.fetch('yk')
        self.assertEqual((attempt.state, attempt.next_attempt_at), ('failed', None))
        self.assertTrue(any('rejected' in line for line in logs.output))

    def test_provider_error_postpones_with_traceback(self):
        self.add_attempt('wata', 'prepared', timedelta(minutes=5))
        error = httpx.HTTPStatusError('unauthorized', request=httpx.Request('GET', 'https://wata.test/links'),
                                      response=httpx.Response(401))
        before = datetime.utcnow()
        with mock.patch.object(checkout_attempts, 'recover_prepared_payment', side_effect=error), \
                self.assertLogs(checkout_attempts.logger, 'WARNING') as logs:
            self.assertFalse(checkout_attempts.reconcile_payment_attempt(self.Session, SimpleNamespace(), 'wata'))
        attempt = self.fetch('wata')
        self.assertEqual((attempt.state, attempt.attempts), ('prepared', 1))
        self.assertGreater(attempt.next_attempt_at, before + timedelta(seconds=50))
        self.assertTrue(any(record.exc_info for record in logs.records))

    def test_database_error_in_finish_rolls_back_and_postpones(self):
        self.add_attempt('wata', 'prepared', timedelta(minutes=5))
        created = payments.CreatedPayment('https://wata.test/pay', 'wata', {'orderId': 'wata'})
        before = datetime.utcnow()
        with mock.patch.object(checkout_attempts, 'recover_prepared_payment', return_value=created), \
                mock.patch.object(checkout_attempts, 'finish_attempt',
                                  side_effect=IntegrityError('INSERT wata_invoices', {}, Exception('duplicate'))), \
                self.assertLogs(checkout_attempts.logger, 'WARNING'):
            self.assertFalse(checkout_attempts.reconcile_payment_attempt(self.Session, SimpleNamespace(), 'wata'))
        attempt = self.fetch('wata')
        self.assertEqual((attempt.state, attempt.attempts, attempt.confirmation_url), ('prepared', 1, None))
        self.assertGreater(attempt.next_attempt_at, before + timedelta(seconds=50))

    def test_commit_failure_does_not_escape_and_still_postpones(self):
        self.add_attempt('wata', 'prepared', timedelta(minutes=5))
        session = self.Session()
        real_commit = session.commit
        commits = []
        def flaky_commit():
            commits.append(1)
            if len(commits) == 1:
                raise OperationalError('COMMIT', {}, Exception('server closed the connection'))
            return real_commit()
        session.commit = flaky_commit
        with mock.patch.object(checkout_attempts, 'recover_prepared_payment', return_value=None), \
                self.assertLogs(checkout_attempts.logger, 'WARNING'):
            self.assertFalse(checkout_attempts.reconcile_payment_attempt(lambda: session, SimpleNamespace(), 'wata'))
        self.assertEqual(len(commits), 2)
        attempt = self.fetch('wata')
        self.assertEqual((attempt.state, attempt.attempts), ('prepared', 1))

    def test_broken_database_is_logged_not_raised(self):
        broken = mock.Mock()
        broken.query.side_effect = OperationalError('SELECT', {}, Exception('connection refused'))
        with self.assertLogs(checkout_attempts.logger, 'WARNING') as logs:
            self.assertFalse(checkout_attempts.reconcile_payment_attempt(lambda: broken, SimpleNamespace(), 'any'))
        broken.close.assert_called_once()
        self.assertTrue(any('could not be postponed' in line for line in logs.output))

    def test_wata_link_absent_postpones_and_counts_attempt(self):
        self.add_attempt('wata', 'prepared', timedelta(minutes=5))
        with mock.patch.object(checkout_attempts, 'recover_prepared_payment', return_value=None) as recover:
            self.assertFalse(checkout_attempts.reconcile_payment_attempt(self.Session, SimpleNamespace(), 'wata'))
        recover.assert_called_once()
        attempt = self.fetch('wata')
        self.assertEqual((attempt.state, attempt.attempts), ('prepared', 1))
        self.assertGreater(attempt.next_attempt_at, datetime.utcnow())

    def test_wata_review_alerts_payment_without_invoice_without_mutations(self):
        self.add_attempt('lost', 'prepared', timedelta(minutes=16))
        self.add_wata_transaction(1, 'lost', 'Paid')
        with mock.patch.object(checkout_attempts, 'recover_prepared_payment') as recover, \
                self.assertLogs(checkout_attempts.logger, 'ERROR') as logs:
            self.assertFalse(checkout_attempts.reconcile_payment_attempt(self.Session, SimpleNamespace(), 'lost'))
        recover.assert_not_called()
        self.assertEqual(self.fetch('lost').state, 'review')
        self.assertTrue(any('ALERT: wata order paid without website invoice' in line for line in logs.output))
        with self.Session() as session:
            self.assertEqual(session.query(WataInvoice).count(), 0)
            self.assertEqual(session.query(WataTransaction).one().transaction_status, 'Paid')

    def test_wata_review_without_payment_has_no_alert(self):
        self.add_attempt('unpaid', 'prepared', timedelta(minutes=16))
        with self.assertLogs(checkout_attempts.logger, 'ERROR') as logs:
            checkout_attempts.reconcile_payment_attempt(self.Session, SimpleNamespace(), 'unpaid')
        self.assertEqual(self.fetch('unpaid').state, 'review')
        self.assertFalse(any('ALERT' in line for line in logs.output))


class CheckoutMergeOwnershipTests(_AttemptFixtureMixin, SimpleTestCase):
    """XSVC-01/ZONE-BIND-01: после merge выживший не получает ссылку ЮKassa проигравшего."""

    def add_yk(self, attempt_id, state, username, age=timedelta(minutes=3), user_id=71, fingerprint='fp'):
        self.add_attempt(attempt_id, state, age, gateway='yookassa', fingerprint=fingerprint, user_id=user_id,
                         request_payload={'amount': {'value': 199, 'currency': 'RUB'},
                                          'metadata': {'username': username, 'telegram_id': 0}})

    def merge(self, old=71, new=72):
        # Как rebind_payment_attempts бота: только user_id, состояние не меняется.
        self.session.query(WebsitePaymentAttempt).filter_by(user_id=old).update({'user_id': new})
        self.session.commit()

    def reuse_for(self, username, user_id=72, fingerprint='fp'):
        found = checkout_attempts.find_reusable_attempt(self.session, user_id, fingerprint, username=username)
        return None if found is None else found.id

    def test_loser_yookassa_attempt_is_not_reused_by_survivor(self):
        for state in ('ready', 'prepared'):
            with self.subTest(state=state):
                fingerprint = f'fp-{state}'
                self.add_yk(f'loser-{state}', state, 'site-loser', fingerprint=fingerprint)
                self.merge()
                with self.assertLogs(checkout_attempts.logger, 'WARNING') as logs:
                    self.assertIsNone(self.reuse_for('tg-survivor', fingerprint=fingerprint))
                output = '\n'.join(logs.output)
                self.assertIn('another username', output)
                self.assertNotIn('site-loser', output)
                # Проверка только по metadata.username; без username — прежнее поведение.
                self.assertEqual(self.reuse_for('site-loser', fingerprint=fingerprint), f'loser-{state}')
                self.assertEqual(self.reuse_for(None, fingerprint=fingerprint), f'loser-{state}')

    def test_own_older_attempt_is_found_behind_a_foreign_newer_one(self):
        self.add_yk('own-older', 'ready', 'tg-survivor', age=timedelta(minutes=6), user_id=72)
        self.add_yk('loser-newer', 'ready', 'site-loser', age=timedelta(minutes=2))
        self.merge()
        with self.assertLogs(checkout_attempts.logger, 'WARNING'):
            self.assertEqual(self.reuse_for('tg-survivor'), 'own-older')

    def test_yookassa_payload_without_metadata_is_not_reused_for_a_named_user(self):
        self.add_attempt('yk-no-metadata', 'ready', timedelta(minutes=3), gateway='yookassa', user_id=72)
        with self.assertLogs(checkout_attempts.logger, 'WARNING'):
            self.assertIsNone(self.reuse_for('tg-survivor'))

    def test_wata_attempt_moved_by_merge_is_still_reused(self):
        # Владелец заказа Wata — wata_invoices.user_id, merge его переносит.
        self.add_attempt('wata-loser', 'ready', timedelta(minutes=3), gateway='wata')
        self.add_attempt('wata-prepared', 'prepared', timedelta(minutes=3), gateway='wata', fingerprint='fp-prepared')
        self.merge()
        self.assertEqual(self.reuse_for('tg-survivor'), 'wata-loser')
        self.assertEqual(self.reuse_for('tg-survivor', fingerprint='fp-prepared'), 'wata-prepared')


class YooKassaMetadataEmailTests(SimpleTestCase):
    """XSVC-01: metadata.email — email аккаунта, только если он задан."""

    TARIFF = SimpleNamespace(price=199, description='one month', db_tariff_id='month')

    def setUp(self):
        from yookassa import Configuration
        saved = (Configuration.account_id, Configuration.secret_key)
        self.addCleanup(lambda: (setattr(Configuration, 'account_id', saved[0]), setattr(Configuration, 'secret_key', saved[1])))

    def payload(self, **kwargs):
        saved = []
        created = SimpleNamespace(id='payment-id', confirmation=SimpleNamespace(confirmation_url='https://pay.test'))
        with mock.patch.object(payments.TimeoutPayment, 'create', return_value=created):
            payments.create_yk_payment_sync('shop', 'secret', self.TARIFF, 'owner', 0, 'https://site.test',
                                            idempotency_key='key', before_send=saved.append, **kwargs)
        return saved[0]

    def test_account_email_goes_to_metadata_and_receipt_keeps_receipt_email(self):
        body = self.payload(email='receipt@example.test', account_email='owner@example.test')
        self.assertEqual(body['metadata']['email'], 'owner@example.test')
        self.assertEqual(body['receipt']['customer']['email'], 'receipt@example.test')
        self.assertEqual(body['metadata']['username'], 'owner')

    def test_without_account_email_metadata_has_no_email_key(self):
        for kwargs in ({}, {'account_email': None}, {'account_email': ''}):
            with self.subTest(kwargs=kwargs):
                body = self.payload(email='receipt@example.test', **kwargs)
                self.assertNotIn('email', body['metadata'])
                self.assertEqual(body['receipt']['customer']['email'], 'receipt@example.test')


class ProviderRejectedClassificationTests(SimpleTestCase):
    """PAY-02: однозначный отказ провайдера отличается от неизвестного исхода."""

    TARIFF = SimpleNamespace(price=199, description='one month', db_tariff_id='Month1')

    def setUp(self):
        from yookassa import Configuration
        saved = (Configuration.account_id, Configuration.secret_key)
        self.addCleanup(lambda: (setattr(Configuration, 'account_id', saved[0]), setattr(Configuration, 'secret_key', saved[1])))

    def create_yk(self, before_send, shop_id='shop', email=None):
        return payments.create_yk_payment_sync(shop_id, 'secret', self.TARIFF, 'owner', None, 'https://site.test',
                                               email=email, idempotency_key='key', before_send=before_send)

    def test_yookassa_local_validation_runs_before_commit(self):
        from yookassa.configuration import ConfigurationError
        for kwargs, error in [({'email': 'name@yandex'}, ValueError), ({'shop_id': None}, ConfigurationError)]:
            with self.subTest(kwargs=kwargs):
                before_send = mock.Mock()
                with mock.patch.object(payments.TimeoutPayment, 'create') as create:
                    with self.assertRaises(error):
                        self.create_yk(before_send, **kwargs)
                before_send.assert_not_called()
                create.assert_not_called()

    def test_yookassa_http_rejections_become_provider_rejected(self):
        for error_class in (BadRequestError, UnauthorizedError, ForbiddenError, NotFoundError):
            with self.subTest(error=error_class.__name__):
                before_send = mock.Mock()
                with mock.patch.object(payments.TimeoutPayment, 'create',
                                       side_effect=error_class({'type': 'error', 'code': 'invalid_request'})):
                    with self.assertRaises(payments.ProviderRejected) as raised:
                        self.create_yk(before_send, email='owner@example.com')
                before_send.assert_called_once()
                self.assertIsInstance(raised.exception.__cause__, error_class)

    def test_yookassa_unknown_outcomes_are_not_rejected(self):
        outcomes = [
            InternalServerError({'type': 'error', 'code': 'internal_server_error'}),
            TooManyRequestsError({'type': 'error', 'code': 'too_many_requests'}),
            AttributeError("'NoneType' object has no attribute 'status_code'"),  # таймаут внутри SDK
            ValueError('Expecting value: line 1 column 1'),  # HTML от прокси после отправки
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                with mock.patch.object(payments.TimeoutPayment, 'create', side_effect=outcome):
                    with self.assertRaises(type(outcome)) as raised:
                        self.create_yk(mock.Mock())
                self.assertNotIsInstance(raised.exception, payments.ProviderRejected)

    def run_wata(self, response=None, error=None):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        if error is not None:
            client.post.side_effect = error
        else:
            client.post.return_value = response
        before_send = mock.Mock()
        with mock.patch.object(payments.httpx, 'Client', return_value=client):
            try:
                return payments.create_wata_payment_sync('https://wata.test', 'token', self.TARIFF,
                                                         idempotency_key='order', before_send=before_send)
            finally:
                before_send.assert_called_once()

    def test_wata_rejection_status_codes(self):
        request = httpx.Request('POST', 'https://wata.test/links')
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                with self.assertRaises(payments.ProviderRejected) as raised:
                    self.run_wata(httpx.Response(code, request=request, content=b'{}'))
                self.assertIsInstance(raised.exception.__cause__, httpx.HTTPStatusError)
        for code in (408, 409, 429, 500, 502, 503):
            with self.subTest(code=code):
                with self.assertRaises(httpx.HTTPStatusError) as raised:
                    self.run_wata(httpx.Response(code, request=request, content=b'{}'))
                self.assertNotIsInstance(raised.exception, payments.ProviderRejected)
        with self.assertRaises(httpx.ReadTimeout):
            self.run_wata(error=httpx.ReadTimeout('timed out', request=request))

    def test_yookassa_recovery_replay_rejection_is_classified(self):
        config = SimpleNamespace(YOOKASSA_SHOP_ID='shop', YOOKASSA_SECRET_KEY='secret')
        with mock.patch.object(payments.TimeoutPayment, 'create', side_effect=UnauthorizedError({'type': 'error'})):
            with self.assertRaises(payments.ProviderRejected):
                payments.recover_prepared_payment('yookassa', {'amount': {'value': 199, 'currency': 'RUB'}}, 'key', config)


class CheckoutProviderOutcomeViewTests(SimpleTestCase):
    """PAY-02: pay() различает отказ провайдера, неизвестный исход и локальную ошибку."""

    def run_pay(self, provider_effect, gateway='wata', before_send=True, discount=False, previous_attempt=None):
        from contextlib import ExitStack
        from common.models.db import User
        user = SimpleNamespace(id=71, email='owner@example.test', username='existing-user', telegram_id=None)
        token = SimpleNamespace(token_hash='status-hash', payment_gateway=None, payment_reference=None)
        tariff = SimpleNamespace(price=199, db_tariff_id='month', description='month')
        class FakeSession(Session):
            def __init__(self):
                super().__init__()
                self.rollback = mock.Mock()
                self.flush = mock.Mock()
            def query(self, model):
                return Query(user if model is User else None)
        session = FakeSession()
        seen = {}
        def provider(**kwargs):
            seen['attempt_id'] = kwargs['idempotency_key']
            if before_send:
                kwargs['before_send']({'orderId': kwargs['idempotency_key'], 'amount': 199.0, 'currency': 'RUB'})
            raise provider_effect
        request = RequestFactory().post('/pay', {'email': user.email, 'tariff_id': 'month'},
                                        HTTP_X_PAYMENT_LAUNCH='new-tab', HTTP_ACCEPT='application/json')
        request.user = SimpleNamespace(is_authenticated=True, id=user.id, email=user.email)
        request.session = SessionDict()
        with ExitStack() as stack:
            stack.enter_context(override_settings(PAYMENT_GATEWAY=gateway, WATA_HOST='https://wata.test', WATA_TOKEN='test',
                                                  YOOKASSA_SHOP_ID='shop', YOOKASSA_SECRET_KEY='secret'))
            for name, value in [('session_factory', session), ('get_runtime_actual_tariffs', [tariff]), ('is_user_blocked', False),
                                ('create_purchase_status_token', 'pstatus_test'), ('get_purchase_status_token', token),
                                ('site_apply_first_purchase_discount', (tariff, discount)),
                                ('should_send_payment_login_email', False)]:
                stack.enter_context(mock.patch.object(views, name, return_value=value))
            find = stack.enter_context(mock.patch.object(views, 'find_reusable_attempt', return_value=previous_attempt))
            provider_name = 'create_wata_payment_sync' if gateway == 'wata' else 'create_yk_payment_sync'
            stack.enter_context(mock.patch.object(views, provider_name, side_effect=provider))
            mark = stack.enter_context(mock.patch.object(views, 'mark_attempt_failed', return_value=True))
            stack.enter_context(mock.patch.object(views, 'messages'))
            logs = stack.enter_context(self.assertLogs(level='INFO'))
            response = views.pay(request)
        return SimpleNamespace(response=response, mark=mark, seen=seen, logs=logs, session=session, find=find, user=user)

    def test_provider_rejection_marks_attempt_failed_and_returns_error(self):
        for gateway in ('wata', 'yookassa'):
            with self.subTest(gateway=gateway):
                result = self.run_pay(payments.ProviderRejected('rejected: HTTP 401'), gateway=gateway)
                self.assertEqual(result.response.status_code, 502)
                body = json.loads(result.response.content)
                self.assertEqual(body['status'], 'error')
                self.assertIn('Не удалось открыть форму оплаты', body['message'])
                result.mark.assert_called_once_with(result.session, result.seen['attempt_id'])
                rejected = [r for r in result.logs.records if 'provider rejected' in r.getMessage()]
                self.assertEqual(len(rejected), 1)
                self.assertEqual(rejected[0].levelname, 'ERROR')
                self.assertIsNotNone(rejected[0].exc_info)

    def test_unknown_outcome_keeps_recovery_and_logs_traceback(self):
        result = self.run_pay(httpx.ReadTimeout('timed out'))
        body = json.loads(result.response.content)
        self.assertEqual((result.response.status_code, body['status']), (200, 'ok'))
        self.assertTrue(body['payment_url'].endswith('/payment/status/pstatus_test/'))
        result.mark.assert_not_called()
        unknown = [r for r in result.logs.records if 'checkout result unknown' in r.getMessage()]
        self.assertEqual(len(unknown), 1)
        self.assertIsNotNone(unknown[0].exc_info)

    def test_local_error_before_commit_is_plain_pay_error(self):
        result = self.run_pay(ValueError('Invalid email value type'), gateway='yookassa', before_send=False)
        self.assertEqual(result.response.status_code, 502)
        result.mark.assert_not_called()
        result.session.commit.assert_not_called()
        self.assertTrue(any(r.getMessage() == 'Pay error' and r.exc_info for r in result.logs.records))

    def test_fingerprint_includes_price_and_promo(self):
        previous = SimpleNamespace(status_url='https://site.test/payment/status/pstatus_old/', confirmation_url=None)
        for discount in (False, True):
            with self.subTest(discount=discount):
                result = self.run_pay(AssertionError('provider must not be called'), discount=discount, previous_attempt=previous)
                self.assertEqual(json.loads(result.response.content)['payment_status_url'], previous.status_url)
                fingerprint = result.find.call_args.args[2]
                self.assertEqual(fingerprint, checkout_attempts.fingerprint_for(
                    71, 'wata', 'month', result.user.email, price=199, promo=discount))
                self.assertNotEqual(fingerprint, checkout_attempts.fingerprint_for(71, 'wata', 'month', result.user.email))


class PaymentStatusAttemptStateTests(SimpleTestCase):
    """PAY-01/PAY-02/OPS-2: страница статуса по состоянию попытки."""

    def payload(self, attempt, status=('pending', 'Ждем подтверждения платежа'), query_error=None, user=None):
        token = SimpleNamespace(token_hash='status-hash', user_id=71, payment_gateway='wata', payment_reference='order')
        class FakeSession(Session):
            def __init__(self):
                super().__init__()
                self.rollback = mock.Mock()
            def query(self, model):
                if model is WebsitePaymentAttempt and query_error is not None:
                    raise query_error
                return Query(attempt)
        session = FakeSession()
        request = RequestFactory().get('/payment/status/pstatus_x/json/')
        request.session = SessionDict()
        request.user = user or SimpleNamespace(is_authenticated=False)
        with mock.patch.object(views, 'session_factory', return_value=session), \
                mock.patch.object(views, 'get_purchase_status_token', return_value=token), \
                mock.patch.object(views, 'get_purchase_payment_status', return_value=status), \
                mock.patch.object(views, 'get_purchase_wata_invoice', return_value=None):
            return views.payment_status_payload(request, 'pstatus_x'), session

    def test_review_is_a_clear_failed_state_without_payment_link(self):
        payload, _ = self.payload(SimpleNamespace(state='review', confirmation_url=None))
        self.assertEqual((payload['status'], payload['payment_url']), ('failed', ''))
        self.assertEqual(payload['message'], views.CHECKOUT_REVIEW_STATUS_MESSAGE)
        self.assertNotIn('Не создавайте повторный платёж', payload['message'])

    def test_failed_attempt_says_payment_was_not_created(self):
        payload, _ = self.payload(SimpleNamespace(state='failed', confirmation_url=None))
        self.assertEqual((payload['status'], payload['message']), ('failed', 'Платёж не создан, деньги не списаны, попробуйте ещё раз'))

    def test_prepared_attempt_is_pending_preparation(self):
        payload, _ = self.payload(SimpleNamespace(state='prepared', confirmation_url=None))
        self.assertEqual((payload['status'], payload['message']), ('pending', 'Подготавливаем платёж'))

    def test_ready_attempt_keeps_provider_link(self):
        payload, _ = self.payload(SimpleNamespace(state='ready', confirmation_url='https://pay.test/1'))
        self.assertEqual((payload['status'], payload['payment_url']), ('pending', 'https://pay.test/1'))

    def test_provider_outcome_wins_over_attempt_state(self):
        payload, _ = self.payload(SimpleNamespace(state='review', confirmation_url=None), status=('succeeded', 'Платеж прошел успешно'))
        self.assertEqual(payload['status'], 'succeeded')

    def test_success_without_session_after_emailed_login_link_says_link_was_sent(self):
        # R03: после анонимной покупки ссылка входа ушла письмом; адрес не раскрывается.
        attempt = SimpleNamespace(state='ready', confirmation_url='https://pay.test/1', login_token_hash='login-hash')
        payload, _ = self.payload(attempt, status=('succeeded', 'Платеж прошел успешно'))
        self.assertEqual(payload, {'status': 'succeeded', 'message': views.PAYMENT_SUCCEEDED_LOGIN_LINK_SENT_MESSAGE,
                                   'login_url': '', 'payment_url': ''})
        self.assertEqual(views.PAYMENT_SUCCEEDED_LOGIN_LINK_SENT_MESSAGE,
                         'Оплата прошла. Ссылка для входа в личный кабинет отправлена на email, указанный при оплате. '
                         'Если письма нет, войдите по email на странице входа.')
        self.assertNotIn('@', payload['message'])

    def test_success_without_session_and_without_emailed_link_asks_to_sign_in(self):
        for attempt in (SimpleNamespace(state='ready', confirmation_url=None, login_token_hash=None), None):
            with self.subTest(attempt=attempt):
                payload, _ = self.payload(attempt, status=('succeeded', 'Платеж прошел успешно'))
                self.assertEqual((payload['message'], payload['login_url']),
                                 (views.PAYMENT_SUCCEEDED_SIGN_IN_MESSAGE, ''))

    def test_owner_session_keeps_cabinet_redirect_and_generic_message(self):
        attempt = SimpleNamespace(state='ready', confirmation_url=None, login_token_hash='login-hash')
        payload, _ = self.payload(attempt, status=('succeeded', 'Платеж прошел успешно'),
                                  user=SimpleNamespace(is_authenticated=True, id=71))
        self.assertEqual((payload['message'], payload['login_url']), ('Платеж прошел успешно', reverse('dashboard')))

    def test_other_signed_in_user_gets_no_login_url(self):
        attempt = SimpleNamespace(state='ready', confirmation_url=None, login_token_hash='login-hash')
        payload, _ = self.payload(attempt, status=('succeeded', 'Платеж прошел успешно'),
                                  user=SimpleNamespace(is_authenticated=True, id=99))
        self.assertEqual((payload['message'], payload['login_url']),
                         (views.PAYMENT_SUCCEEDED_LOGIN_LINK_SENT_MESSAGE, ''))

    def test_missing_attempts_table_does_not_break_status_page(self):
        error = ProgrammingError('SELECT website_payment_attempts', {}, Exception('relation does not exist'))
        with self.assertLogs(level='ERROR') as logs:
            payload, session = self.payload(None, query_error=error)
        session.rollback.assert_called_once()
        self.assertEqual(payload['status'], 'pending')
        self.assertTrue(any('website_payment_attempts' in line for line in logs.output))


class LegacyPurchaseTokenTests(SimpleTestCase):
    """PAY-05/AUTH-04/TR-02: старые токены без префикса — только статус, никогда вход."""

    def test_legacy_lookup_uses_head_hash_formula_and_seven_day_window(self):
        from django.conf import settings
        token = 'legacy-fresh'
        self.assertEqual(views.hash_purchase_login_token(token),
                         hashlib.sha256(f'purchase-login:{token}:{settings.SECRET_KEY}'.encode()).hexdigest())
        Session_ = _sqlite_sessionmaker(self, PurchaseLoginToken)
        now = datetime.utcnow()
        with Session_() as session:
            for row_id, raw, age, revoked in [(1, 'legacy-fresh', timedelta(days=3), None),
                                              (2, 'legacy-old', timedelta(days=8), None),
                                              (3, 'legacy-revoked', timedelta(days=1), now)]:
                session.add(PurchaseLoginToken(id=row_id, user_id=71, token_hash=views.hash_purchase_login_token(raw),
                                               created_at=now - age, revoked_at=revoked))
            session.commit()
            self.assertEqual(views.get_legacy_purchase_status_token(session, 'legacy-fresh').id, 1)
            self.assertIsNone(views.get_legacy_purchase_status_token(session, 'legacy-old'))
            self.assertIsNone(views.get_legacy_purchase_status_token(session, 'legacy-revoked'))
        prefixed = mock.Mock()
        for raw in ('pstatus_x', 'plogin_x'):
            self.assertIsNone(views.get_legacy_purchase_status_token(prefixed, raw))
        prefixed.query.assert_not_called()

    def status(self, token, legacy_row, payment_status=('succeeded', 'Платеж прошел успешно'), allow_active_check=False):
        session = mock.Mock()
        request = RequestFactory().get(f'/payment/status/{token}/json/')
        request.session = SessionDict({views.payment_session_url_key(token): 'https://pay.test/session'})
        request.user = SimpleNamespace(is_authenticated=True, id=71)
        with mock.patch.object(views, 'session_factory', return_value=session), \
                mock.patch.object(views, 'get_purchase_status_token', return_value=None), \
                mock.patch.object(views, 'get_legacy_purchase_status_token', return_value=legacy_row) as legacy, \
                mock.patch.object(views, 'get_purchase_payment_status', return_value=payment_status), \
                mock.patch.object(views, 'active_wata_status_for_token') as active:
            payload = views.payment_status_payload(request, token, allow_active_check=allow_active_check)
        return payload, legacy, active

    def test_fresh_legacy_status_link_shows_only_payment_status(self):
        row = SimpleNamespace(user_id=71, token_hash='h', payment_gateway='wata', payment_reference='order')
        # Успех по legacy-ссылке никогда не авторизует: текст ведёт на вход (R03).
        for payment_status, message in [(('succeeded', 'Платеж прошел успешно'), views.PAYMENT_SUCCEEDED_SIGN_IN_MESSAGE),
                                        (('pending', 'Ждем подтверждения платежа'), 'Ждем подтверждения платежа')]:
            with self.subTest(status=payment_status[0]):
                payload, legacy, active = self.status('legacy-token', row, payment_status, allow_active_check=True)
                self.assertEqual(payload, {'status': payment_status[0], 'message': message, 'login_url': '', 'payment_url': ''})
                legacy.assert_called_once()
                active.assert_not_called()

    def test_unknown_or_stale_legacy_link_is_neutral_not_failed(self):
        payload, _, _ = self.status('legacy-token', None)
        self.assertEqual(payload['status'], 'pending')
        self.assertEqual(payload['message'], views.LEGACY_PURCHASE_STATUS_NEUTRAL_MESSAGE)
        self.assertEqual((payload['login_url'], payload['payment_url']), ('', ''))
        # FE-FINAL-02: исход не узнать — страница прекращает опрос.
        self.assertIs(payload['terminal'], True)

    def test_status_page_passes_terminal_flag_to_first_render(self):
        request = RequestFactory().get('/payment/status/legacy-token/')
        terminal = {'status': 'pending', 'message': views.LEGACY_PURCHASE_STATUS_NEUTRAL_MESSAGE,
                    'login_url': '', 'payment_url': '', 'terminal': True}
        ordinary = {'status': 'pending', 'message': 'Ждем подтверждения платежа', 'login_url': '', 'payment_url': ''}
        for payload, expected in ((terminal, True), (ordinary, False)):
            with self.subTest(expected=expected):
                with mock.patch.object(views, 'payment_status_payload', return_value=payload), \
                        mock.patch.object(views, 'render', return_value='rendered') as render:
                    self.assertEqual(views.payment_status(request, 'legacy-token'), 'rendered')
                context = render.call_args.args[2]
                self.assertIs(context['initial_terminal'], expected)
                self.assertEqual((context['initial_status'], context['initial_message']),
                                 (payload['status'], payload['message']))

    def test_unknown_prefixed_status_link_still_fails(self):
        payload, legacy, _ = self.status('pstatus_unknown', None)
        self.assertEqual((payload['status'], payload['message']), ('failed', 'Ссылка проверки платежа истекла или неверна'))
        self.assertNotIn('terminal', payload)
        legacy.assert_not_called()

    def test_legacy_token_never_opens_retry(self):
        session = mock.Mock()
        with mock.patch.object(views, 'session_factory', return_value=session), \
                mock.patch.object(views, 'get_purchase_wata_invoice') as invoice:
            response = views.payment_retry(RequestFactory().get('/payment/retry/legacy-token/'), 'legacy-token')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/payment/status/legacy-token/')
        invoice.assert_not_called()
        session.query.assert_not_called()

    def test_legacy_login_link_never_authorizes_even_when_paid(self):
        with mock.patch.object(views, 'session_factory') as factory, \
                mock.patch.object(views, 'get_legacy_purchase_status_token', return_value=SimpleNamespace(user_id=71)), \
                mock.patch.object(views, 'get_purchase_payment_status', return_value=('succeeded', 'ok')), \
                mock.patch.object(views, 'authorize_user_session') as authorize, \
                mock.patch.object(views, 'render_login', side_effect=lambda request, ctx: ('login', ctx)):
            result = views.auth_by_purchase_link(mock.Mock(), 'legacy-token')
        self.assertEqual(result, ('login', {'error': views.LEGACY_PURCHASE_LOGIN_MESSAGE}))
        authorize.assert_not_called()
        factory.assert_not_called()


class _FakeInspector:
    def __init__(self, tables):
        self.tables = tables
    def has_table(self, name, schema=None):
        return name in self.tables
    def get_columns(self, name, schema=None):
        return [{'name': column} for column in self.tables[name]]


def _full_schema():
    return {model.__tablename__: {column.name for column in model.__table__.columns}
            for model in (WebsitePaymentAttempt, WebsiteEmailChange)}


class CommonSchemaCheckTests(SimpleTestCase):
    """OPS-2/PAY-06: read-only проверка миграции common на моках inspect."""

    def test_complete_schema_passes(self):
        out = io.StringIO()
        with mock.patch.object(check_common_schema, 'sa_inspect', return_value=_FakeInspector(_full_schema())) as inspect_:
            self.assertEqual(check_common_schema.common_schema_problems(bind=object()), [])
            call_command('check_common_schema', stdout=out)
        self.assertIn('OK', out.getvalue())
        self.assertEqual(inspect_.call_count, 2)

    def test_missing_table_fails_with_clear_message(self):
        tables = _full_schema()
        del tables['website_email_changes']
        with mock.patch.object(check_common_schema, 'sa_inspect', return_value=_FakeInspector(tables)):
            with self.assertRaises(CommandError) as raised:
                call_command('check_common_schema')
        self.assertIn('нет таблицы website_email_changes', str(raised.exception))
        self.assertIn('alembic-миграцию common', str(raised.exception))
        # Порядок релиза: миграция -> боты -> сайт; payment в релизе не
        # обновляется и таблицы website_* не читает (PAY-05 откачен).
        self.assertIn('до выката website и бота', str(raised.exception))
        self.assertNotIn('payment', str(raised.exception))

    def test_partial_migration_reports_missing_columns(self):
        tables = _full_schema()
        tables['website_payment_attempts'] -= {'attempts', 'next_attempt_at'}
        with mock.patch.object(check_common_schema, 'sa_inspect', return_value=_FakeInspector(tables)):
            problems = check_common_schema.common_schema_problems(bind=object())
        self.assertEqual(problems, ['в таблице website_payment_attempts нет колонок: attempts, next_attempt_at'])

    def test_exit_code_is_nonzero_and_database_errors_are_reported(self):
        tables = _full_schema()
        del tables['website_payment_attempts']
        with mock.patch.object(check_common_schema, 'sa_inspect', return_value=_FakeInspector(tables)), \
                mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as exited:
                check_common_schema.Command().run_from_argv(['manage.py', 'check_common_schema'])
        self.assertEqual(exited.exception.code, 1)
        self.assertIn('нет таблицы website_payment_attempts', stderr.getvalue())
        with mock.patch.object(check_common_schema, 'sa_inspect',
                               side_effect=OperationalError('connect', {}, Exception('connection refused'))):
            with self.assertRaises(CommandError) as raised:
                call_command('check_common_schema')
        self.assertIn('Не удалось проверить схему common', str(raised.exception))


class ReconcileWorkerTests(SimpleTestCase):
    """OPS-5/OPS-6/OPS-7/EMAIL-05/EMAIL-06/PAY-07: лидерство, изоляция, сигналы."""

    def command(self):
        command = reconcile_command.Command()
        command.stop_requested = False
        command._schema_ok = False
        command._schema_checked_at = None
        command._schema_error_logged_at = None
        return command

    @staticmethod
    def connection(*scalars):
        conn = mock.MagicMock()
        conn.execution_options.return_value = conn
        conn.execute.return_value.scalar.side_effect = list(scalars)
        return conn

    def test_leader_uses_session_lock_on_autocommit_connection_and_pings(self):
        conn = self.connection(True, 1)
        engine = mock.Mock()
        engine.connect.return_value = conn
        lock = reconcile_command.LeaderLock(engine)
        self.assertTrue(lock.ensure())
        self.assertTrue(lock.ensure())
        engine.connect.assert_called_once()
        conn.execution_options.assert_called_once_with(isolation_level='AUTOCOMMIT')
        first, second = [call.args[0] for call in conn.execute.call_args_list]
        self.assertIn('pg_try_advisory_lock', str(first))
        self.assertNotIn('xact', str(first))
        self.assertEqual(list(first.compile().params.values()), [731104281])
        self.assertNotIn('advisory', str(second))
        conn.close.assert_not_called()

    def test_connection_error_resets_leadership_and_reconnects(self):
        broken = self.connection(True, OperationalError('SELECT 1', {}, Exception('terminating connection')))
        fresh = self.connection(True)
        engine = mock.Mock()
        engine.connect.side_effect = [broken, fresh]
        lock = reconcile_command.LeaderLock(engine)
        self.assertTrue(lock.ensure())
        with self.assertLogs(reconcile_command.logger, 'WARNING'):
            self.assertFalse(lock.ensure())
        broken.invalidate.assert_called_once()
        broken.close.assert_called_once()
        self.assertEqual((lock.conn, lock.held), (None, False))
        self.assertTrue(lock.ensure())
        self.assertIs(lock.conn, fresh)

    def test_release_swallows_close_errors_and_not_elected_keeps_connection(self):
        conn = self.connection(False, False)
        conn.invalidate.side_effect = RuntimeError('gone')
        conn.close.side_effect = RuntimeError('gone')
        engine = mock.Mock()
        engine.connect.return_value = conn
        lock = reconcile_command.LeaderLock(engine)
        self.assertFalse(lock.ensure())
        self.assertFalse(lock.ensure())
        engine.connect.assert_called_once()
        lock.release()
        self.assertIsNone(lock.conn)

    def test_schema_gate_pauses_without_traceback_spam(self):
        command = self.command()
        with mock.patch.object(reconcile_command, 'common_schema_problems', return_value=['нет таблицы website_payment_attempts']), \
                self.assertLogs(reconcile_command.logger, 'ERROR') as logs:
            self.assertFalse(command._schema_ready(1000.0))
            self.assertFalse(command._schema_ready(1031.0))
            self.assertFalse(command._schema_ready(1601.0))
        errors = [record for record in logs.records if record.levelname == 'ERROR']
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(record.exc_info is None for record in errors))
        with mock.patch.object(reconcile_command, 'common_schema_problems', side_effect=OperationalError('x', {}, Exception('down'))):
            self.assertFalse(command._schema_ready(1700.0))
        with mock.patch.object(reconcile_command, 'common_schema_problems', return_value=[]) as check:
            self.assertTrue(command._schema_ready(2000.0))
            self.assertTrue(command._schema_ready(2500.0))
            self.assertEqual(check.call_count, 1)
            self.assertTrue(command._schema_ready(2601.0))
            self.assertEqual(check.call_count, 2)

    def run_cycle(self, command, pending=None, reconcile=None, emails=(), sync=None, clock=0.0):
        rwms = object()
        patches = [
            mock.patch.object(reconcile_command, 'pending_payment_ids', side_effect=pending),
            mock.patch.object(reconcile_command, 'reconcile_payment_attempt', side_effect=reconcile),
            mock.patch.object(reconcile_command, 'pending_email_users', return_value=list(emails)),
            mock.patch.object(reconcile_command, 'sync_email_change', side_effect=sync),
            mock.patch.object(reconcile_command.time, 'monotonic', side_effect=clock if callable(clock) else (lambda: clock)),
        ]
        from contextlib import ExitStack
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch) for patch in patches]
            command.run_cycle(0.0, mock.Mock, rwms, SimpleNamespace(UpdateUserRequest=object))
        return mocks

    def test_cycle_isolates_failures_and_limits_wata_to_one_attempt(self):
        selections = {(20, None, False): ['expired'], (1, 'wata', True): ['wata-1'], (20, 'yookassa', True): ['yk-1', 'yk-2']}
        def pending(session, limit=1, gateway=None, recoverable=None):
            return selections[(limit, gateway, recoverable)]
        reconciled = []
        def reconcile(factory, settings, attempt_id):
            reconciled.append(attempt_id)
            if attempt_id == 'yk-1':
                raise RuntimeError('boom')
            return False
        command = self.command()
        with self.assertLogs(reconcile_command.logger, 'ERROR'):
            mocks = self.run_cycle(command, pending, reconcile, emails=[7, 8], sync=[RuntimeError('rwms down'), True])
        self.assertEqual(reconciled, ['expired', 'wata-1', 'yk-1', 'yk-2'])
        self.assertEqual(sorted(call.kwargs.get('limit') for call in mocks[0].call_args_list), [1, 20, 20])
        self.assertEqual(mocks[3].call_count, 2)

    def test_payment_selection_failure_does_not_block_emails(self):
        command = self.command()
        command._schema_ok = True
        error = ProgrammingError('SELECT', {}, Exception('relation "website_payment_attempts" does not exist'))
        with self.assertLogs(reconcile_command.logger, 'ERROR') as logs:
            mocks = self.run_cycle(command, pending=error, emails=[7], sync=[True])
        mocks[3].assert_called_once()
        self.assertFalse(command._schema_ok)
        self.assertTrue(any(record.exc_info for record in logs.records))

    def test_payment_budget_stops_starting_new_attempts(self):
        now = [0.0]
        def reconcile(factory, settings, attempt_id):
            now[0] += 12
        def pending(session, limit=1, gateway=None, recoverable=None):
            return ['yk-1', 'yk-2', 'yk-3'] if gateway == 'yookassa' else []
        mocks = self.run_cycle(self.command(), pending, reconcile, clock=lambda: now[0])
        self.assertEqual(mocks[1].call_count, 2)

    def test_email_part_processes_at_least_one_user_after_budget(self):
        mocks = self.run_cycle(self.command(), pending=lambda *a, **k: [], emails=[7, 8], sync=[True, True], clock=45.0)
        self.assertEqual(mocks[3].call_count, 1)

    def run_handle(self, cycle, *args):
        leader = mock.Mock()
        leader.ensure.return_value = True
        handler_before = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(reconcile_command, 'LeaderLock', return_value=leader), \
                mock.patch.object(reconcile_command.Command, '_schema_ready', return_value=True), \
                mock.patch.object(reconcile_command.Command, 'run_cycle', autospec=True, side_effect=cycle) as run_cycle, \
                mock.patch.object(reconcile_command.time, 'sleep') as sleep:
            call_command('reconcile_website_operations', *args)
        self.assertEqual(signal.getsignal(signal.SIGTERM), handler_before)
        leader.release.assert_called_once()
        return run_cycle, sleep

    def test_once_runs_single_cycle_without_waiting(self):
        run_cycle, sleep = self.run_handle(lambda *args: None, '--once')
        run_cycle.assert_called_once()
        sleep.assert_not_called()

    def test_sigterm_stops_after_current_step(self):
        def cycle(command, *args):
            handler = signal.getsignal(signal.SIGTERM)
            self.assertEqual(handler, command._request_stop)
            handler(signal.SIGTERM, None)
        run_cycle, sleep = self.run_handle(cycle)
        run_cycle.assert_called_once()
        sleep.assert_not_called()

    def test_sleep_wakes_up_in_small_steps_on_stop(self):
        command = self.command()
        def sleep(seconds):
            self.assertLessEqual(seconds, reconcile_command.SLEEP_STEP_SECONDS)
            command.stop_requested = True
        with mock.patch.object(reconcile_command.time, 'monotonic', return_value=0.0), \
                mock.patch.object(reconcile_command.time, 'sleep', side_effect=sleep) as sleeper:
            command._sleep_until(reconcile_command.CYCLE_SECONDS)
        sleeper.assert_called_once()


# Импорт модуля, а не классов: иначе загрузчик тестов подхватил бы чужие
# тест-классы второй раз.
from engine import node_provisioning  # noqa: E402
from engine import tests_node_provisioning as node_provisioning_tests  # noqa: E402
from common.models.db import NodeProvisionStatus  # noqa: E402

_PRIVATE_PROXIES = ["127.0.0.1/32", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


class NodeBootstrapClaimIpGuardTests(node_provisioning_tests.NodeProvisioningDbTestCase):
    """INFRA-02: нода не создаётся с адресом прокси или приватной сети."""

    @override_settings(TRUSTED_PROXY_NETWORKS=_PRIVATE_PROXIES + ["45.67.89.10/32"])
    def test_claim_rejects_empty_private_or_proxy_ip_before_rwms(self):
        provision_request, _token = self.make_request()
        rwms = node_provisioning_tests.make_rwms_mock()
        for ip in (
            None, "", "not-an-ip", "127.0.0.1", "10.1.2.3", "172.18.0.4", "192.168.1.5",
            "169.254.10.1", "0.0.0.0", "::1", "fe80::1", "::ffff:10.0.0.1", "45.67.89.10",
        ):
            with self.subTest(ip=ip):
                with self.assertLogs("engine", level="ERROR"), \
                        self.assertRaises(node_provisioning.ProvisionError) as ctx:
                    node_provisioning.claim_request(self.session, provision_request, ip, rwms)
                self.assertEqual(ctx.exception.http_status, 409)

        rwms.get_node_secret.assert_not_called()
        rwms.create_node.assert_not_called()
        self.assertEqual(provision_request.status, NodeProvisionStatus.CREATED)
        self.assertIsNone(provision_request.claimed_ip)
        self.assertIsNone(provision_request.remnawave_node_uuid)

    def test_claim_passes_bulk_timeout_to_create_node(self):
        provision_request, _token = self.make_request()
        rwms = node_provisioning_tests.make_rwms_mock()

        with override_settings(RWMS_BULK_RPC_TIMEOUT_SECONDS=42.0):
            node_provisioning.claim_request(self.session, provision_request, "1.2.3.4", rwms)

        self.assertEqual(rwms.create_node.call_args.kwargs["timeout"], 42.0)
        self.assertEqual(provision_request.claimed_ip, "1.2.3.4")

    def post_claim(self, token, rwms, **meta):
        request = RequestFactory().post(
            "/node-bootstrap/claim/",
            HTTP_HOST="panel.test",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            **meta,
        )
        with mock.patch("engine.views.session_factory", side_effect=self.Session), \
                mock.patch("engine.views.rwms_client", rwms):
            return views.node_bootstrap_claim(request)

    @override_settings(
        NODE_BOOTSTRAP_DOMAINS=["panel.test"],
        TRUSTED_PROXY_NETWORKS=_PRIVATE_PROXIES + ["203.0.113.10/32"],
    )
    def test_claim_through_public_edge_registers_real_node_address(self):
        _provision_request, token = self.make_request()
        rwms = node_provisioning_tests.make_rwms_mock()
        rwms.create_node.return_value.address = "45.10.20.30"

        response = self.post_claim(
            token, rwms, REMOTE_ADDR="172.18.0.4", HTTP_X_FORWARDED_FOR="45.10.20.30, 203.0.113.10",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(rwms.create_node.call_args.args[0].address, "45.10.20.30")

    @override_settings(
        NODE_BOOTSTRAP_DOMAINS=["panel.test"],
        TRUSTED_PROXY_NETWORKS=_PRIVATE_PROXIES + ["45.67.89.10/32"],
    )
    def test_claim_view_answers_409_json_when_ip_is_a_proxy(self):
        _provision_request, token = self.make_request()
        rwms = node_provisioning_tests.make_rwms_mock()

        with self.assertLogs("engine", level="ERROR"):
            response = self.post_claim(token, rwms, REMOTE_ADDR="45.67.89.10")

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "error")
        self.assertIn("TRUSTED_PROXY_NETWORKS", payload["message"])
        rwms.get_node_secret.assert_not_called()
        rwms.create_node.assert_not_called()
