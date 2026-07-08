"""Полная блокировка аккаунта (таблица user_blocks, ставится ботом через
/block-user). Заблокированный пользователь не может пользоваться кабинетом
и оплачивать подписку."""

import logging

from common.models.db import UserBlock

ACCOUNT_BLOCKED_MESSAGE = (
    "Ваш аккаунт заблокирован. Если вы считаете, что это ошибка, "
    "свяжитесь с нами: https://t.me/monkeyislandsupportbot"
)


def is_user_blocked(db_session, user_id: int) -> bool:
    """Fail-open: при ошибке (например, таблица user_blocks ещё не создана
    миграцией) считаем пользователя не заблокированным, чтобы не уронить
    кабинет/оплату для всех пользователей. Ошибка логируется."""
    try:
        return (
            db_session.query(UserBlock.user_id)
            .filter(UserBlock.user_id == user_id)
            .first()
            is not None
        )
    except Exception:
        logging.exception(
            "failed to check user block for user_id=%s, treating as not blocked",
            user_id,
        )
        return False
