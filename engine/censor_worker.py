"""Фоновый воркер «Замеров ТСПУ»: крутится внутри процесса сайта.

Стартует из web_app/wsgi.py (то есть только в gunicorn, не при migrate/
collectstatic/manage-командах). Раз в CENSOR_WORKER_INTERVAL секунд запускает
просроченные по расписанию проверки, собирает результаты pending-прогонов и
шлёт Telegram-алерты — без крона и без отдельного контейнера.

У gunicorn несколько воркеров, поэтому работает ровно один «лидер»: он держит
Postgres advisory-lock на выделенном соединении. Остальные воркеры лок не
получают и просто ждут; если лидер умрёт, его соединение закроется, лок
освободится и лидерство подхватит другой воркер.
"""

import logging
import random
import threading
import time

from sqlalchemy import text

from database import engine
from database import session_factory

# Произвольный уникальный ключ advisory-lock (чтобы не пересечься с чужими).
_ADVISORY_LOCK_KEY = 4820257001
_DEFAULT_INTERVAL = 60

_started = False
_start_lock = threading.Lock()


def _run_maintenance(fallback_key: str) -> None:
    from engine import ripe_atlas

    db = session_factory()
    try:
        ripe_atlas.finalize_pending_runs(db, fallback_key)
        ripe_atlas.schedule_due_checks(db, fallback_key)
    finally:
        db.close()


def _simple_loop(interval: int, fallback_key: str) -> None:
    """Цикл без выбора лидера — для не-Postgres (dev на SQLite).

    Advisory-lock есть только в Postgres. В деве процесс один, поэтому просто
    крутим обслуживание без блокировки.
    """
    logging.info("censor worker: non-postgres backend, running without leader lock")
    while True:
        try:
            _run_maintenance(fallback_key)
        except Exception:
            logging.exception("censor worker: maintenance iteration failed")
        time.sleep(interval)


def _leader_loop(interval: int, fallback_key: str) -> None:
    if engine.dialect.name != "postgresql":
        _simple_loop(interval, fallback_key)
        return

    failures = 0
    while True:
        # AUTOCOMMIT: не держим открытую транзакцию; session-level advisory-lock
        # висит на соединении, пока оно живо.
        conn = None
        try:
            conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            failures = 0
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            ).scalar()
            if not got:
                # Лидер уже есть в другом воркере — ждём и пробуем снова.
                time.sleep(interval)
                continue

            logging.info("censor worker: acquired leadership, running maintenance loop")
            while True:
                # Проверяем, что соединение с локом живо; если нет — теряем
                # лидерство и уходим переизбираться.
                conn.execute(text("SELECT 1"))
                try:
                    _run_maintenance(fallback_key)
                except Exception:
                    logging.exception("censor worker: maintenance iteration failed")
                time.sleep(interval)
        except Exception:
            logging.exception("censor worker: leader loop error, will re-elect")
            failures += 1
            delay = min(max(1, interval), 2 ** min(failures - 1, 6))
            time.sleep(delay + random.uniform(0, min(1, delay * 0.1)))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def start() -> None:
    """Запускает фоновый поток один раз на процесс. Безопасно вызывать повторно."""
    global _started
    from django.conf import settings

    if not getattr(settings, "CENSOR_WORKER_ENABLED", True):
        return

    with _start_lock:
        if _started:
            return
        _started = True

    interval = int(getattr(settings, "CENSOR_WORKER_INTERVAL", _DEFAULT_INTERVAL))
    fallback_key = getattr(settings, "RIPE_ATLAS_API_KEY", "")

    thread = threading.Thread(
        target=_leader_loop,
        args=(interval, fallback_key),
        name="censor-worker",
        daemon=True,
    )
    thread.start()
    logging.info("censor worker: background thread started (interval=%ss)", interval)
