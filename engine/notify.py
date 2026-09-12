"""Отправка админ-алертов в Telegram напрямую через Bot API.

Используется вкладкой «Замеры ТСПУ» (бан/восстановление нод) и монитором
серверов (`infra_worker`: OFFLINE/ONLINE, лимиты ноды, автозамена IP).
Сайт не подключён к Redis-очереди бота, поэтому шлём напрямую тем же токеном,
что и авторизационный бот.

Куда шлём — по тем же ключам `system_settings`, что и бот
(`utils/admin_alerts.py` в боте): `admin_alert_chat_id` — супергруппа с
темами, `admin_alert_topics` — `вид:thread_id` через запятую. Все алерты сайта
это «здоровье инфраструктуры», поэтому идут в тему вида `health`. Деградация
как у бота, чтобы алерт не потерялся: тема закрыта/удалена — повтор в общую
ленту группы; группа недоступна или ключ пуст — получатели из env
`TELEGRAM_ALERT_CHAT_ID` (id через запятую), как было до маршрутизации.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from django.conf import settings

from common.models.db import SystemSetting
from common.models.settings import ADMIN_ALERT_CHAT_ID_SETTING
from common.models.settings import ADMIN_ALERT_TOPICS_SETTING

TELEGRAM_API = "https://api.telegram.org"

# Вид алертов сайта в терминах admin_alert_topics бота. Ключ уже в настройке
# пользователя — переименовывать нельзя.
ADMIN_ALERT_HEALTH = "health"


@dataclass(frozen=True)
class AdminAlertRoute:
    """Куда слать алерт. chat_id=None — прежнее поведение (env-список)."""

    chat_id: Optional[int] = None
    thread_id: Optional[int] = None

    @property
    def to_group(self) -> bool:
        return self.chat_id is not None


def alert_chat_ids() -> list[str]:
    raw = settings.TELEGRAM_ALERT_CHAT_ID or ""
    return [chat_id.strip() for chat_id in raw.split(",") if chat_id.strip()]


def parse_admin_alert_chat_id(raw: Optional[str]) -> Optional[int]:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        logging.error("invalid admin alert chat id %r", value)
        return None


def parse_admin_alert_topics(raw: Optional[str]) -> dict[str, int]:
    """`ipguard:12,health:15` -> {"ipguard": 12, "health": 15}.

    Битый кусок пропускается с warning — остальные виды продолжают работать,
    один в один с парсером бота.
    """
    result: dict[str, int] = {}
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        kind, sep, value = chunk.partition(":")
        kind = kind.strip().lower()
        if not sep or not kind:
            logging.warning("invalid admin alert topic %r", chunk)
            continue
        try:
            thread_id = int(value.strip())
        except ValueError:
            logging.warning("invalid admin alert topic %r", chunk)
            continue
        if thread_id <= 0:
            logging.warning("invalid admin alert topic id %r", chunk)
            continue
        result[kind] = thread_id
    return result


def _read_setting(db_session, key: str) -> Optional[str]:
    setting = db_session.get(SystemSetting, key)
    return None if setting is None else setting.value


def admin_alert_route(db_session, kind: str = ADMIN_ALERT_HEALTH) -> AdminAlertRoute:
    chat_id = parse_admin_alert_chat_id(
        _read_setting(db_session, ADMIN_ALERT_CHAT_ID_SETTING)
    )
    if chat_id is None:
        return AdminAlertRoute()
    topics = parse_admin_alert_topics(
        _read_setting(db_session, ADMIN_ALERT_TOPICS_SETTING)
    )
    return AdminAlertRoute(chat_id=chat_id, thread_id=topics.get(kind))


def load_admin_alert_route(kind: str = ADMIN_ALERT_HEALTH) -> AdminAlertRoute:
    """Маршрут из БД своей короткой сессией.

    Любая ошибка чтения — прежний путь через env: алерт важнее маршрута.
    """
    from database import session_factory

    try:
        db_session = session_factory()
    except Exception:
        logging.exception("admin alert route: cannot open db session")
        return AdminAlertRoute()
    try:
        return admin_alert_route(db_session, kind)
    except Exception:
        logging.exception("admin alert route: cannot read settings")
        return AdminAlertRoute()
    finally:
        db_session.close()


def _post_message(token: str, payload: dict) -> tuple[bool, str]:
    """(доставлено, текст ошибки). Сетевые исключения — тоже ошибка, не raise."""
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage", json=payload
            )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if response.is_success:
        return True, ""
    return False, response.text[:200]


def _payload(chat_id, text: str, thread_id: Optional[int] = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    return payload


def _send_to_group(token: str, route: AdminAlertRoute, text: str) -> bool:
    delivered, error = _post_message(
        token, _payload(route.chat_id, text, route.thread_id)
    )
    if delivered:
        return True
    if route.thread_id is None:
        logging.warning(
            "admin alert not delivered to chat %s: %s", route.chat_id, error
        )
        return False
    # Тема закрыта или удалена — Telegram отвечает 400; повторяем в общую
    # ленту той же группы, чтобы алерт не пропал.
    logging.warning(
        "admin alert not delivered to topic %s of chat %s: %s; retrying to general",
        route.thread_id,
        route.chat_id,
        error,
    )
    delivered, error = _post_message(token, _payload(route.chat_id, text))
    if not delivered:
        logging.warning(
            "admin alert not delivered to chat %s: %s", route.chat_id, error
        )
    return delivered


def _send_to_env_recipients(token: str, text: str) -> bool:
    delivered = False
    for chat_id in alert_chat_ids():
        ok, error = _post_message(token, _payload(chat_id, text))
        if ok:
            delivered = True
        else:
            logging.warning(
                "censor alert: telegram send failed for %s: %s", chat_id, error
            )
    return delivered


def send_admin_telegram_alert(
    text: str, kind: str = ADMIN_ALERT_HEALTH, route: Optional[AdminAlertRoute] = None
) -> bool:
    """Шлёт text админам: в тему группы из настроек, иначе в env-список.

    Возвращает True, если хотя бы одному доставлено. Пустой токен — тихо
    False (алерты отключены). `route` — для тестов и вызовов, где сессия уже
    открыта; по умолчанию читается из БД.
    """
    token = settings.TELEGRAM_ALERT_BOT_TOKEN
    if not token:
        return False

    if route is None:
        route = load_admin_alert_route(kind)
    if route.to_group and _send_to_group(token, route, text):
        return True
    if route.to_group:
        logging.warning(
            "admin alert: group %s unreachable, falling back to env recipients",
            route.chat_id,
        )
    return _send_to_env_recipients(token, text)
