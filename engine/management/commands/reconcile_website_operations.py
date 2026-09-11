"""Retry durable operations. Run one service; PostgreSQL elects its leader."""
import logging
import signal
import threading
import time
from django.conf import settings
from django.core.management.base import BaseCommand
from sqlalchemy import func, literal, select
from sqlalchemy.exc import ProgrammingError
from engine.checkout_attempts import pending_payment_ids, reconcile_payment_attempt
from engine.email_change import pending_email_users, sync_email_change
from engine.management.commands.check_common_schema import common_schema_problems

logger = logging.getLogger(__name__)

LEADER_LOCK_KEY = 731104281
# Не чаще: у Wata GET /links — 1 запрос в 30 секунд на клиента.
CYCLE_SECONDS = 31
# После этого платёжная часть не берёт новые попытки; email-часть — после
# CYCLE_BUDGET_SECONDS (но хотя бы одного пользователя за цикл обрабатывает).
PAYMENT_BUDGET_SECONDS = 20
CYCLE_BUDGET_SECONDS = 30
EXPIRED_PAYMENT_BATCH = 20
YOOKASSA_PAYMENT_BATCH = 20
WATA_PAYMENT_BATCH = 1
EMAIL_BATCH = 10
SCHEMA_RECHECK_SECONDS = 600
SCHEMA_ERROR_LOG_SECONDS = 600
SLEEP_STEP_SECONDS = 1.0


class LeaderLock:
    """Session-level pg_try_advisory_lock на выделенном AUTOCOMMIT-соединении.

    Соединение простаивает ВНЕ транзакции (xmin не пиннится — инцидент
    2026-09-02, не рвётся по idle_in_transaction_session_timeout). Каждый цикл
    лидер проверяет соединение SELECT 1; при ошибке лидерство сбрасывается, а
    переподключение и перевыборы происходят на следующем цикле.
    """

    def __init__(self, engine, key=LEADER_LOCK_KEY):
        self.engine = engine
        self.key = key
        self.conn = None
        self.held = False

    def ensure(self):
        try:
            if self.conn is None:
                self.conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
                self.held = False
            if self.held:
                self.conn.execute(select(literal(1))).scalar()
                return True
            self.held = bool(self.conn.execute(select(func.pg_try_advisory_lock(self.key))).scalar())
            if self.held:
                logger.info("website reconciliation: leadership acquired")
            return self.held
        except Exception:
            logger.warning("website reconciliation: leader connection failed, will re-elect", exc_info=True)
            self.release()
            return False

    def release(self):
        conn, self.conn = self.conn, None
        self.held = False
        if conn is None:
            return
        # invalidate закрывает DBAPI-соединение, а не возвращает его в пул:
        # иначе session-level lock остался бы висеть на пуловом соединении.
        try:
            conn.invalidate()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


class Command(BaseCommand):
    help = "Recover pending website email changes and payment attempts (requires common migration)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="One cycle without waiting for the interval; schedule no more often than every 31 seconds",
        )

    def handle(self, *args, **options):
        import database
        from engine.views import session_factory, rwms_client, proto

        self.stop_requested = False
        self._schema_ok = False
        self._schema_checked_at = None
        self._schema_error_logged_at = None
        previous_handlers = self._install_signal_handlers()
        leader = LeaderLock(database.engine)
        try:
            while not self.stop_requested:
                started = time.monotonic()
                if leader.ensure() and self._schema_ready(started):
                    self.run_cycle(started, session_factory, rwms_client, proto)
                if options["once"]:
                    break
                self._sleep_until(started + CYCLE_SECONDS)
            if self.stop_requested:
                logger.info("website reconciliation: stop requested, exiting")
        finally:
            leader.release()
            self._restore_signal_handlers(previous_handlers)

    # --- сигналы и ожидание -------------------------------------------------

    def _request_stop(self, signum, frame):
        # Только флаг: цикл завершит текущий шаг и выйдет.
        self.stop_requested = True

    def _install_signal_handlers(self):
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._request_stop)
        return previous

    @staticmethod
    def _restore_signal_handlers(previous):
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (TypeError, ValueError):
                pass

    def _sleep_until(self, deadline):
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(SLEEP_STEP_SECONDS, remaining))

    # --- схема common -------------------------------------------------------

    def _schema_ready(self, now):
        """Без таблиц common воркер не работает, но и не падает.

        Готовая схема перепроверяется раз в SCHEMA_RECHECK_SECONDS; пока схемы
        нет (или проверка не удалась) — каждый цикл, чтобы подхватить
        миграцию сразу. ERROR пишется не чаще раза в SCHEMA_ERROR_LOG_SECONDS.
        """
        if (self._schema_ok and self._schema_checked_at is not None
                and now - self._schema_checked_at < SCHEMA_RECHECK_SECONDS):
            return True
        try:
            problems = common_schema_problems()
        except Exception as exc:
            problems = [f"проверка схемы не удалась: {type(exc).__name__}"]
        self._schema_checked_at = now
        self._schema_ok = not problems
        if problems:
            if (self._schema_error_logged_at is None
                    or now - self._schema_error_logged_at >= SCHEMA_ERROR_LOG_SECONDS):
                self._schema_error_logged_at = now
                logger.error(
                    "website reconciliation paused: схема common не готова (%s); "
                    "примените alembic-миграцию common",
                    "; ".join(problems),
                )
        elif self._schema_error_logged_at is not None:
            self._schema_error_logged_at = None
            logger.info("website reconciliation resumed: схема common на месте")
        return self._schema_ok

    # --- цикл ---------------------------------------------------------------

    def run_cycle(self, started, session_factory, rwms_client, proto):
        # Платёжная и email-часть изолированы: сбой одной не блокирует другую.
        try:
            self.reconcile_payments(started, session_factory)
        except Exception as exc:
            self._part_failed("payment", exc)
        if self.stop_requested:
            return
        try:
            self.reconcile_emails(started, session_factory, rwms_client, proto)
        except Exception as exc:
            self._part_failed("email", exc)

    def _part_failed(self, part, exc):
        if isinstance(exc, ProgrammingError):
            self._schema_ok = False  # перепроверить схему на следующем цикле
        logger.error("website %s reconciliation failed", part, exc_info=exc)

    def reconcile_payments(self, started, session_factory):
        query = session_factory()
        try:
            # Просроченные уходят в review без сети; YooKassa — пачкой (повтор
            # с тем же ключом идемпотентности); Wata — не больше ОДНОЙ за цикл.
            expired = pending_payment_ids(query, limit=EXPIRED_PAYMENT_BATCH, recoverable=False)
            wata = pending_payment_ids(query, limit=WATA_PAYMENT_BATCH, gateway="wata", recoverable=True)
            yookassa = pending_payment_ids(query, limit=YOOKASSA_PAYMENT_BATCH, gateway="yookassa", recoverable=True)
        finally:
            query.close()
        for attempt_id in [*expired, *wata, *yookassa]:
            if self.stop_requested or time.monotonic() - started >= PAYMENT_BUDGET_SECONDS:
                break
            try:
                reconcile_payment_attempt(session_factory, settings, attempt_id)
            except Exception:
                logger.exception("website payment reconciliation failed attempt_id=%s", attempt_id)

    def reconcile_emails(self, started, session_factory, rwms_client, proto):
        query = session_factory()
        try:
            user_ids = pending_email_users(query, limit=EMAIL_BATCH)
        finally:
            query.close()
        processed = 0
        for user_id in user_ids:
            if self.stop_requested:
                break
            if processed and time.monotonic() - started >= CYCLE_BUDGET_SECONDS:
                break
            processed += 1
            try:
                sync_email_change(session_factory, rwms_client, user_id, proto.UpdateUserRequest)
            except Exception:
                logger.exception("website email reconciliation failed user_id=%s", user_id)
