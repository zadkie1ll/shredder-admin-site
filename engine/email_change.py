"""Latest-operation email confirmation and recoverable RWMS synchronization."""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from sqlalchemy import or_
from common.models.db import User, WebsiteEmailChange
from engine.rwms_helpers import usable_panel_email

logger = logging.getLogger(__name__)


def nonce_hash(nonce):
    return hashlib.sha256(nonce.encode()).hexdigest()


def issue_email_change(session, user, new_email):
    # Порядок блокировок на сайте: вызывающий (update_email, confirm_email,
    # письмо подтверждения после оплаты) держит FOR UPDATE на users, затем
    # пишется website_email_changes. sync_email_change users не блокирует и
    # чужих блокировок не ждёт (см. его docstring), поэтому с этими путями и с
    # merge бота (website_email_changes -> users) цикла не образует.
    challenge = session.query(WebsiteEmailChange).filter_by(user_id=user.id).first()
    if challenge is None:
        challenge = WebsiteEmailChange(user_id=user.id, sync_pending=False, attempts=0)
        session.add(challenge)
    nonce = secrets.token_urlsafe(32)
    challenge.token_hash = nonce_hash(nonce)
    challenge.requested_email = new_email
    challenge.previous_email = user.email or ""
    challenge.issued_at = datetime.utcnow()
    challenge.used_at = None
    return nonce


def consume_email_change(session, user, payload, max_age):
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return False
    challenge = session.query(WebsiteEmailChange).filter_by(user_id=user.id).first()
    now = datetime.utcnow()
    if (challenge is None or challenge.used_at is not None
            or challenge.issued_at < now - timedelta(seconds=max_age)
            or not secrets.compare_digest(challenge.token_hash, nonce_hash(nonce))
            or challenge.requested_email != payload["email"]
            or challenge.previous_email != (user.email or "")):
        return False
    challenge.used_at = now
    challenge.sync_pending = True
    challenge.next_attempt_at = now
    challenge.attempts = 0
    return True


def email_change_already_applied(session, user, payload, max_age):
    """Та же ссылка уже подтверждена, и её результат на месте. Ничего не меняет.

    Для идемпотентного повторного перехода по ссылке (почтовые сканеры
    открывают GET заранее): nonce совпадает с последней заявкой, заявка
    погашена не раньше max_age назад, адреса совпадают с подписанными в
    ссылке, а users.email уже равен запрошенному адресу.
    """
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return False
    challenge = session.query(WebsiteEmailChange).filter_by(user_id=user.id).first()
    if challenge is None or challenge.used_at is None:
        return False
    now = datetime.utcnow()
    return (challenge.used_at >= now - timedelta(seconds=max_age)
            and secrets.compare_digest(challenge.token_hash, nonce_hash(nonce))
            and challenge.requested_email == payload.get("email")
            and challenge.previous_email == (payload.get("previous_email") or "")
            and (user.email or "") == challenge.requested_email)


def has_fresh_unused_email_change(session, user_id, email, max_age):
    """Недавно выпущенная и ещё не погашенная заявка на этот же адрес."""
    challenge = session.query(WebsiteEmailChange).filter_by(user_id=user_id).first()
    return (challenge is not None
            and challenge.used_at is None
            and challenge.requested_email == email
            and challenge.issued_at >= datetime.utcnow() - timedelta(seconds=max_age))


def _change_version(change):
    # Перевыпуск меняет issued_at, погашение (подтверждение, merge бота) —
    # used_at и сбрасывает attempts, ретрай другого синка увеличивает attempts.
    return (change.issued_at, change.used_at, change.attempts)


def _retry_delay_seconds(attempts):
    return min(3600, 30 * 2 ** min(attempts, 7))


def sync_email_change(session_factory, rwms, user_id, request_type):
    """Отправить подтверждённый users.email в панель, не держа блокировок через RPC.

    1. Короткое чтение без блокировок: username, email и версия заявки;
       транзакция закрывается до RPC.
    2. RPC вне транзакции: get_user_by_username_strict + update_user. Пустой
       или отвергаемый панелью адрес (usable_panel_email -> None) в панель
       не шлётся: синк закрывается без RPC, иначе ретраился бы вечно.
    3. Короткая CAS-транзакция: строка website_email_changes FOR UPDATE
       SKIP LOCKED, users.email перечитывается обычным SELECT. Синк
       закрывается, только если заявку не перевыпустили и не погасили заново
       и users.email совпадает с отправленным; иначе остаётся pending с
       next_attempt_at=now и перешлётся с актуальным адресом.
       Ошибка RPC: attempts+1 и backoff — тоже только для той же версии.

    Блокировку users синк не берёт вовсе, а строку заявки не ждёт (SKIP
    LOCKED): продления и записи пользователя не простаивают на время RPC, и
    с merge бота (website_email_changes -> users) deadlock невозможен.
    Строка, занятая чужой транзакцией, остаётся pending — повторит воркер.

    Возвращает True, если синк закрыт этим вызовом.
    """
    session = session_factory()
    try:
        change = session.query(WebsiteEmailChange).filter_by(user_id=user_id).first()
        if change is None or not change.sync_pending:
            return False
        if change.next_attempt_at and change.next_attempt_at > datetime.utcnow():
            return False
        user = session.query(User).filter_by(id=user_id).first()
        if user is None:
            return False
        username = user.username
        sent_email = user.email or ""
        version = _change_version(change)
    finally:
        session.close()

    panel_email = usable_panel_email(sent_email, username, "UpdateUser")
    if panel_email is None:
        logger.warning(
            "email reconciliation closed without RPC: account email is empty or "
            "not accepted by the panel user_id=%s",
            user_id,
        )
        return _close_sync(session_factory, user_id, version, sent_email)

    try:
        subscription = rwms.get_user_by_username_strict(username)
        # A local-only account may not have a panel subscription yet.
        if subscription is not None:
            result = rwms.update_user(request_type(uuid=subscription.uuid, email=panel_email))
            if result is None:
                raise RuntimeError("RWMS update not acknowledged")
    except Exception as error:
        logger.warning("email reconciliation RPC failed user_id=%s error=%r", user_id, error)
        _postpone_sync(session_factory, user_id, version)
        return False

    closed = _close_sync(session_factory, user_id, version, sent_email)
    if closed:
        logger.info("email reconciliation succeeded user_id=%s", user_id)
    return closed


def _locked_change(session, user_id):
    return (
        session.query(WebsiteEmailChange)
        .filter_by(user_id=user_id)
        .with_for_update(skip_locked=True)
        .first()
    )


def _close_sync(session_factory, user_id, version, sent_email):
    session = session_factory()
    try:
        change = _locked_change(session, user_id)
        if change is None or not change.sync_pending:
            # Строку держит другая транзакция (подтверждение, merge бота,
            # параллельный синк) или синк уже закрыт: не ждём и не пишем.
            return False
        current_email = session.query(User.email).filter(User.id == user_id).scalar()
        if _change_version(change) == version and (current_email or "") == sent_email:
            change.sync_pending = False
            change.next_attempt_at = None
            session.commit()
            return True
        change.next_attempt_at = datetime.utcnow()
        session.commit()
        logger.info(
            "email reconciliation superseded during RPC, resend scheduled user_id=%s",
            user_id,
        )
        return False
    finally:
        session.close()


def _postpone_sync(session_factory, user_id, version):
    session = session_factory()
    try:
        change = _locked_change(session, user_id)
        if change is None or not change.sync_pending or _change_version(change) != version:
            # Заявку уже изменили (новое подтверждение, merge, другой синк):
            # у неё свой next_attempt_at, чужой backoff на неё не переносим.
            return False
        change.attempts += 1
        change.next_attempt_at = datetime.utcnow() + timedelta(
            seconds=_retry_delay_seconds(change.attempts)
        )
        attempts = change.attempts
        session.commit()
    finally:
        session.close()
    logger.warning("email reconciliation postponed user_id=%s attempt=%s", user_id, attempts)
    return True


def pending_email_users(session, limit=50):
    return [row[0] for row in session.query(WebsiteEmailChange.user_id).filter(
        WebsiteEmailChange.sync_pending.is_(True),
        or_(WebsiteEmailChange.next_attempt_at.is_(None), WebsiteEmailChange.next_attempt_at <= datetime.utcnow()),
    ).order_by(WebsiteEmailChange.next_attempt_at, WebsiteEmailChange.user_id).limit(limit).all()]
