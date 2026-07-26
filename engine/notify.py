"""Отправка админ-алертов в Telegram напрямую через Bot API.

Используется вкладкой «Замеры ТСПУ» для уведомлений о бане/восстановлении нод.
Сайт не подключён к Redis-очереди бота, поэтому шлём напрямую тем же токеном,
что и авторизационный бот. Получатели — TELEGRAM_ALERT_CHAT_ID (id через запятую).
"""

import logging

import httpx
from django.conf import settings

TELEGRAM_API = "https://api.telegram.org"


def alert_chat_ids() -> list[str]:
    raw = settings.TELEGRAM_ALERT_CHAT_ID or ""
    return [chat_id.strip() for chat_id in raw.split(",") if chat_id.strip()]


def send_admin_telegram_alert(text: str) -> bool:
    """Шлёт text всем получателям из TELEGRAM_ALERT_CHAT_ID.

    Возвращает True, если хотя бы одному доставлено. Пустой токен/список —
    тихо возвращает False (алерты просто отключены).
    """
    token = settings.TELEGRAM_ALERT_BOT_TOKEN
    chat_ids = alert_chat_ids()
    if not token or not chat_ids:
        return False

    delivered = False
    for chat_id in chat_ids:
        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(
                    f"{TELEGRAM_API}/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            if response.is_success:
                delivered = True
            else:
                logging.warning(
                    "censor alert: telegram send failed for %s: %s",
                    chat_id,
                    response.text[:200],
                )
        except Exception:
            logging.exception("censor alert: telegram send error for %s", chat_id)
    return delivered
