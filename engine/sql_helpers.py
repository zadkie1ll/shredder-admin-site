import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from common.models.db import User
from common.models.db import WataInvoice
from common.models.segments import user_has_payment_sql
from common.models.segments import user_never_paid_from_row


def lock_registration_email(db_session, email) -> None:
    """Сериализовать регистрации по одному email транзакционным advisory-локом.

    Без него две одновременные регистрации на один новый email (два клика по
    «войти», магик-линк и OAuth параллельно, ретрай платёжной формы) могут обе
    дойти до панельного ``AddUser``: проигравший по уникальному индексу
    ``users.email`` откатит строку в postgres, но СОЗДАННАЯ ИМ ПОДПИСКА в
    Remnawave останется сиротой. Лок держится до конца транзакции
    (``pg_advisory_xact_lock`` снимается на commit/rollback), поэтому его нужно
    брать ДО любых обращений к панели.

    Ключ — ``hashtext`` нормализованного email. Пустой email (регистрация
    только по telegram_id) и не-postgres бэкенды (SQLite в тестах) — no-op.
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        return
    if db_session.get_bind().dialect.name != "postgresql":
        return
    db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:email))"),
        {"email": normalized},
    )


def lock_registration_telegram_id(db_session, telegram_id) -> None:
    """Сериализовать регистрации одной Telegram-учётки.

    Email-lock не защищает Telegram widget/Mini App, где email отсутствует.
    Без отдельного лока два запроса одновременно создавали две разные
    Remnawave-подписки, после чего одна DB-транзакция проигрывала уникальному
    ``users.telegram_id`` и оставляла подписку-сироту. Пространство ключей
    отделено префиксом от email-lock; на SQLite это no-op, как и email-вариант.
    """
    if telegram_id is None:
        return
    if db_session.get_bind().dialect.name != "postgresql":
        return
    db_session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:identity))"),
        {"identity": f"telegram:{int(telegram_id)}"},
    )


def user_never_paid(db_session, user_id) -> bool | None:
    """«Пробная без платежа» (never_paid) для одного пользователя.

    Единый источник семантики — ``common/models/segments.py``
    (``PAYS_EXISTS_SQL``): нет ни одной ``yk_payments.status='succeeded'`` и
    ни одной ``wata_transactions.transaction_status='Paid'`` (JOIN
    ``wata_invoices`` по ``order_id``). Тот же предикат, что у сегментов
    ``trial_active``/``never_paid`` рассылок и у бота
    (``has_payment_for_user_by_tg_id``).

    Возвращает ``True`` — не платил, ``False`` — платил, ``None`` — строки
    users нет (вызывающий код не должен трактовать это как «не платил»).
    """
    if user_id is None:
        return None
    row = db_session.execute(
        text(user_has_payment_sql("id")), {"user_id": int(user_id)}
    ).first()
    return user_never_paid_from_row(row)


def save_wata_invoice(
    session: Session, invoice_json: dict, tariff_id: str, email: str
) -> None:
    user_id = session.scalar(select(User.id).where(User.email == email).limit(1))

    if user_id is None:
        logging.error(f"not found user id for email {email}")
        return

    session.add(
        WataInvoice(
            user_id=user_id,
            invoice_id=invoice_json["id"],
            amount=invoice_json["amount"],
            currency=invoice_json["currency"],
            status=invoice_json["status"],
            url=invoice_json["url"],
            terminal_name=invoice_json["terminalName"],
            terminal_public_id=invoice_json["terminalPublicId"],
            creation_time=datetime.fromisoformat(
                invoice_json["creationTime"].replace("Z", "+00:00")
            ),
            order_id=invoice_json["orderId"],
            description=invoice_json["description"],
            success_redirect_url=invoice_json.get("successRedirectUrl"),
            fail_redirect_url=invoice_json.get("failRedirectUrl"),
            expiration_datetime=datetime.fromisoformat(
                invoice_json["expirationDateTime"].replace("Z", "+00:00")
            ),
            tariff_id=tariff_id,
        )
    )
