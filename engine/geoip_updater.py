"""Автоматическое обновление DB-IP City Lite из фонового Django-воркера.

DB-IP публикует ежемесячную City Lite MMDB без аккаунта и download key.
Загрузчик проверяет полученную MMDB и только затем атомарно заменяет рабочий
файл. Старая база остаётся доступной при любой сетевой ошибке или битом архиве.
"""

from __future__ import annotations

import fcntl
import gzip
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import maxminddb
from django.conf import settings

log = logging.getLogger("infra.geoip-updater")

DOWNLOAD_URL_TEMPLATE = (
    "https://download.db-ip.com/free/"
    "dbip-city-lite-{year:04d}-{month:02d}.mmdb.gz"
)
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_DATABASE_BYTES = 512 * 1024 * 1024
RETRY_SECONDS = 60 * 60


def _database_path() -> Path | None:
    raw_path = str(getattr(settings, "GEOIP_CITY_DB_PATH", "") or "").strip()
    return Path(raw_path).expanduser() if raw_path else None


def _interval_seconds() -> int:
    try:
        hours = int(getattr(settings, "GEOIPUPDATE_INTERVAL_HOURS", 168))
    except (TypeError, ValueError):
        hours = 168
    return max(1, min(hours, 24 * 30)) * 60 * 60


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _touch(path: Path, timestamp: float) -> None:
    path.touch(exist_ok=True)
    os.utime(path, (timestamp, timestamp))


def _is_due(database_path: Path, now: float) -> bool:
    checked_path = database_path.with_suffix(database_path.suffix + ".checked")
    last_success = max(
        (value for value in (_mtime(database_path), _mtime(checked_path)) if value),
        default=None,
    )
    if last_success is not None and now - last_success < _interval_seconds():
        return False

    attempt_path = database_path.with_suffix(database_path.suffix + ".attempt")
    last_attempt = _mtime(attempt_path)
    if last_attempt is not None and now - last_attempt < RETRY_SECONDS:
        return False
    return True


def _release_months(now: datetime) -> tuple[tuple[int, int], tuple[int, int]]:
    """Текущий и предыдущий месяц: новый релиз иногда появляется не 1-го числа."""

    current = (now.year, now.month)
    previous = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return current, previous


def download_url(year: int, month: int) -> str:
    return DOWNLOAD_URL_TEMPLATE.format(year=year, month=month)


def _download_archive(
    client: httpx.Client,
    destination: Path,
    now: datetime,
) -> str:
    """Скачивает свежий релиз, откатываясь на предыдущий месяц при 404."""

    attempted_urls: list[str] = []
    for year, month in _release_months(now):
        url = download_url(year, month)
        attempted_urls.append(url)
        written = 0
        with client.stream("GET", url) as response:
            if response.status_code == 404:
                continue
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise ValueError("DB-IP City Lite archive exceeds the safety limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if written == 0:
            raise ValueError("DB-IP returned an empty City Lite archive")
        return url
    raise FileNotFoundError(
        "DB-IP City Lite release not found: " + ", ".join(attempted_urls)
    )


def _extract_database(archive_path: Path, destination: Path) -> None:
    written = 0
    with gzip.open(archive_path, mode="rb") as source, destination.open("wb") as output:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_DATABASE_BYTES:
                raise ValueError("DB-IP City Lite database exceeds the safety limit")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if written == 0:
        raise ValueError("DB-IP City Lite archive contains an empty database")


def _validate_database(path: Path) -> None:
    reader = maxminddb.open_database(str(path))
    try:
        reader.metadata()
    finally:
        reader.close()


def _replace_database(database_path: Path, now: datetime) -> str:
    archive_name = None
    database_name = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".dbip-city-lite-",
            suffix=".mmdb.gz",
            dir=database_path.parent,
            delete=False,
        ) as archive_file:
            archive_name = archive_file.name
        with tempfile.NamedTemporaryFile(
            prefix=".dbip-city-lite-",
            suffix=".mmdb",
            dir=database_path.parent,
            delete=False,
        ) as database_file:
            database_name = database_file.name

        timeout = httpx.Timeout(connect=20.0, read=180.0, write=30.0, pool=20.0)
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            source_url = _download_archive(client, Path(archive_name), now)
        _extract_database(Path(archive_name), Path(database_name))
        _validate_database(Path(database_name))
        os.chmod(database_name, 0o644)
        os.replace(database_name, database_path)
        database_name = None
        return source_url
    finally:
        for temporary_name in (archive_name, database_name):
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except FileNotFoundError:
                    pass


def update_if_due(now: float | None = None) -> str:
    """Обновляет City-MMDB при необходимости.

    Функция вызывается лидером ``infra_worker`` на каждом тике. Persistent
    marker ограничивает успешные загрузки одним разом в 168 часов, а ошибки —
    одной попыткой в час даже после перезапуска Gunicorn.
    """

    database_path = _database_path()
    if database_path is None:
        return "disabled"

    now = time.time() if now is None else float(now)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if not _is_due(database_path, now):
        return "current"

    lock_path = database_path.with_suffix(database_path.suffix + ".lock")
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "locked"
        if not _is_due(database_path, now):
            return "current"

        attempt_path = database_path.with_suffix(database_path.suffix + ".attempt")
        checked_path = database_path.with_suffix(database_path.suffix + ".checked")
        _touch(attempt_path, now)
        try:
            source_url = _replace_database(
                database_path,
                datetime.fromtimestamp(now, tz=timezone.utc),
            )
        except Exception:
            log.exception("DB-IP City Lite update failed; keeping the previous database")
            return "error"

        _touch(checked_path, now)
        # Другие gunicorn-процессы увидят новый mtime при следующем lookup;
        # лидер сразу закрывает старый reader и освобождает его mmap.
        from engine import geoip_lookup

        geoip_lookup.reset_caches()
        log.info("DB-IP City Lite database updated from %s: %s", source_url, database_path)
        return "updated"


def reset_state_for_tests() -> None:
    """Сохранён для совместимости существующих тестовых teardown-хуков."""
