"""Durable checkout mapping, deduplication and conservative recovery."""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from common.models.db import WebsitePaymentAttempt, PurchaseLoginToken, WataInvoice, WataTransaction, YkPayment
from engine.payments import ProviderRejected, recover_prepared_payment
from engine.sql_helpers import save_wata_invoice

logger = logging.getLogger(__name__)

# Воркер повторяет неизвестный исход не дольше этого окна от created_at, затем
# переводит попытку в review без обращения к провайдеру.
RECOVERY_WINDOW = timedelta(minutes=15)
# prepared блокирует новый заказ чуть дольше окна восстановления: поздний
# повтор YooKassa (таймаут 10 с) не должен выдать ссылку параллельно новой
# попытке. Старше — у попытки гарантированно нет ссылки, которую видел клиент.
PREPARED_REUSE_WINDOW = timedelta(minutes=20)
# ready переиспользуется, пока ссылка провайдера ещё может быть оплачена.
READY_REUSE_WINDOW = timedelta(minutes=15)
# Ссылку Wata, которой осталось меньше этого, не выдаём повторно.
WATA_LINK_MIN_REMAINING = timedelta(minutes=2)
# Совпадает со сроком ссылки Wata (payments.create_wata_payment_sync).
WATA_LINK_LIFETIME = timedelta(minutes=15)
RETRY_DELAY = timedelta(seconds=60)
# state: prepared -> ready | review | failed. review и failed терминальны для
# дедупликации: форма оплаты по такой попытке клиенту не выдавалась.
YOOKASSA_TERMINAL_STATUSES = ("succeeded", "canceled")


def fingerprint_for(user_id, gateway, tariff_id, email, price=None, promo=None):
    # user_id в отпечаток не входит: владение ограничивает запрос в
    # find_reusable_attempt. При merge бот переносит попытки на выжившего
    # (website_payment_attempts.user_id). Для Wata это безопасно: владелец
    # платежа ищется через wata_invoices.user_id, который merge тоже переносит.
    # Платёж ЮKassa payment-сервис ищет по metadata.username, поэтому чужую
    # (перенесённую) попытку ЮKassa find_reusable_attempt не отдаёт (XSVC-01).
    parts = [gateway, tariff_id, email]
    if price is not None or promo is not None:
        # Смена цены или промо-скидки не должна возвращать старую ссылку.
        # Без этих аргументов отпечаток прежний (обратная совместимость).
        parts += [None if price is None else str(price), bool(promo)]
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def _expires_within(expires_at, margin):
    # Naive/aware как в views.is_wata_invoice_expired: SQLite отдаёт naive UTC,
    # PostgreSQL для DateTime(timezone=True) — aware.
    if expires_at.tzinfo is None:
        return datetime.utcnow() + margin >= expires_at
    return datetime.now(timezone.utc) + margin >= expires_at


def ready_attempt_is_open(session, attempt):
    """True, пока исход у провайдера не терминальный и ссылку можно оплатить."""
    if attempt.gateway == "yookassa":
        if not attempt.provider_reference:
            return True
        terminal = session.query(YkPayment.id).filter(
            YkPayment.payment_id == attempt.provider_reference,
            YkPayment.status.in_(YOOKASSA_TERMINAL_STATUSES),
        ).first()
        return terminal is None
    if attempt.gateway == "wata":
        # Declined не мешает: одна ссылка Wata допускает повторную попытку.
        paid = session.query(WataTransaction.id).filter(
            WataTransaction.order_id == attempt.id,
            WataTransaction.transaction_status == "Paid",
        ).first()
        if paid is not None:
            return False
        invoice = session.query(WataInvoice).filter(
            WataInvoice.order_id == attempt.id,
        ).order_by(WataInvoice.creation_time.desc()).first()
        expires_at = invoice.expiration_datetime if invoice is not None else None
        if expires_at is None:
            expires_at = attempt.created_at + WATA_LINK_LIFETIME
        return not _expires_within(expires_at, WATA_LINK_MIN_REMAINING)
    return True


def attempt_owned_by_username(attempt, username):
    """Можно ли отдать попытку пользователю ``username``.

    Платёж ЮKassa payment-сервис привязывает к аккаунту по metadata.username
    (затем telegram_id > 0 и metadata.email). После merge бот переносит попытки
    проигравшего на выжившего, но у провайдера остаётся username проигравшего:
    такая ссылка списала бы деньги без продления (XSVC-01). Wata не проверяем:
    владелец заказа — wata_invoices.user_id, и merge его переносит.
    username=None — проверка выключена (прежнее поведение).
    """
    if username is None or attempt.gateway != "yookassa":
        return True
    payload = attempt.request_payload if isinstance(attempt.request_payload, dict) else {}
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata.get("username") == username


def _newest_owned(session, user_id, fingerprint, state, window, now, username):
    rows = session.query(WebsitePaymentAttempt).filter(
        WebsitePaymentAttempt.user_id == user_id,
        WebsitePaymentAttempt.fingerprint == fingerprint,
        WebsitePaymentAttempt.state == state,
        WebsitePaymentAttempt.created_at >= now - window,
    ).order_by(WebsitePaymentAttempt.created_at.desc()).all()
    for attempt in rows:
        if attempt_owned_by_username(attempt, username):
            return attempt
        logger.warning(
            "checkout attempt not reused: provider metadata belongs to another username "
            "attempt_id=%s user_id=%s state=%s", attempt.id, user_id, state,
        )
    return None


def find_reusable_attempt(session, user_id, fingerprint, username=None):
    # Caller holds User FOR UPDATE, serializing even concurrent requests with
    # different browser sessions. Unknown outcome is reused only while the
    # worker may still recover it; review/failed never block a new order.
    # username (XSVC-01/ZONE-BIND-01): попытка ЮКассы с чужим metadata.username
    # непригодна — берётся следующая по времени, иначе создаётся новая.
    now = datetime.utcnow()
    prepared = _newest_owned(session, user_id, fingerprint, "prepared", PREPARED_REUSE_WINDOW, now, username)
    if prepared is not None:
        return prepared
    ready = _newest_owned(session, user_id, fingerprint, "ready", READY_REUSE_WINDOW, now, username)
    if ready is not None and ready_attempt_is_open(session, ready):
        return ready
    return None


def persist_before_send(session, attempt, payload):
    attempt.request_payload = payload
    attempt.state = "prepared"
    attempt.next_attempt_at = datetime.utcnow() + RETRY_DELAY
    session.add(attempt)
    session.commit()  # Must succeed before a provider request can be issued.
    logger.info("checkout prepared attempt_id=%s user_id=%s gateway=%s", attempt.id, attempt.user_id, attempt.gateway)


def mark_attempt_failed(session, attempt_id):
    """Провайдер однозначно отказал в создании платежа: ссылки нет и не будет."""
    attempt = session.query(WebsitePaymentAttempt).filter_by(id=attempt_id).with_for_update().first()
    if attempt is None or attempt.state != "prepared":
        session.rollback()
        return False
    attempt.state = "failed"
    attempt.next_attempt_at = None
    session.commit()
    return True


def finish_attempt(session, attempt, created):
    # Both the web request and recovery worker use the same row lock.
    attempt = session.query(WebsitePaymentAttempt).filter_by(id=attempt.id).with_for_update().one()
    if attempt.gateway == "wata" and (created.reference != attempt.id or created.payload.get("orderId") != attempt.id):
        raise ValueError("Wata response does not match the prepared order")
    if attempt.state == "ready":
        return
    for token_hash in (attempt.status_token_hash, attempt.login_token_hash):
        if token_hash:
            row = session.query(PurchaseLoginToken).filter_by(token_hash=token_hash).first()
            if row is None:
                raise RuntimeError("Checkout token missing")
            row.payment_gateway = attempt.gateway
            row.payment_reference = created.reference
    if attempt.gateway == "wata":
        invoice = session.query(WataInvoice).filter_by(order_id=attempt.id).first()
        if invoice is None:
            save_wata_invoice(session, created.payload, attempt.tariff_id, attempt.user_id)
    attempt.provider_reference = created.reference
    attempt.confirmation_url = created.confirmation_url
    attempt.state = "ready"
    attempt.next_attempt_at = None
    logger.info("checkout mapped attempt_id=%s user_id=%s", attempt.id, attempt.user_id)


def _alert_wata_paid_without_invoice(session, attempt_id, user_id):
    # PAY-08: оплата по orderId без строки wata_invoices не видна сегментам
    # «платил» и скидке первой покупки. Только алерт, данные не трогаем.
    try:
        paid = session.query(WataTransaction.id).filter(
            WataTransaction.order_id == attempt_id,
            WataTransaction.transaction_status == "Paid",
        ).first()
        if paid is None:
            return
        invoice = session.query(WataInvoice.id).filter(WataInvoice.order_id == attempt_id).first()
        session.rollback()
        if invoice is None:
            logger.error(
                "ALERT: wata order paid without website invoice attempt_id=%s user_id=%s",
                attempt_id, user_id,
            )
    except Exception:
        session.rollback()
        logger.warning("checkout paid-without-invoice check failed attempt_id=%s", attempt_id, exc_info=True)


def _postpone_attempt(session, attempt_id):
    # Отдельная короткая транзакция после rollback: сбой одной попытки не
    # должен оставлять её первой в очереди на каждом цикле.
    try:
        attempt = session.query(WebsitePaymentAttempt).filter_by(id=attempt_id).with_for_update(skip_locked=True).first()
        if attempt is None or attempt.state != "prepared":
            session.rollback()
            return False
        attempt.attempts = (attempt.attempts or 0) + 1
        attempt.next_attempt_at = datetime.utcnow() + RETRY_DELAY
        session.commit()
        return True
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        logger.error("checkout recovery could not be postponed attempt_id=%s", attempt_id, exc_info=True)
        return False


def reconcile_payment_attempt(session_factory, settings, attempt_id):
    """Одна попытка восстановления; никогда не выбрасывает исключение."""
    session = session_factory()
    try:
        try:
            attempt = session.query(WebsitePaymentAttempt).filter_by(id=attempt_id).with_for_update(skip_locked=True).first()
            now = datetime.utcnow()
            if (attempt is None or attempt.state != "prepared"
                    or (attempt.next_attempt_at is not None and attempt.next_attempt_at > now)):
                session.rollback()
                return False
            gateway = attempt.gateway
            user_id = attempt.user_id
            # Never replay YooKassa after the key retention window. A deliberately
            # shorter budget (15 min) keeps recovery within the checkout lifetime.
            if now - attempt.created_at > RECOVERY_WINDOW:
                attempt.state = "review"
                attempt.next_attempt_at = None
                session.commit()
                logger.error("checkout needs reconciliation attempt_id=%s gateway=%s user_id=%s", attempt_id, gateway, user_id)
                if gateway == "wata":
                    _alert_wata_paid_without_invoice(session, attempt_id, user_id)
                return False
            attempt.attempts += 1
            try:
                created = recover_prepared_payment(gateway, attempt.request_payload, attempt_id, settings)
            except ProviderRejected:
                if gateway != "yookassa":
                    raise
                # Повтор тем же ключом отвергнут однозначно (400/401/403/404):
                # платежа нет, клиент может оформить новый заказ.
                attempt.state = "failed"
                attempt.next_attempt_at = None
                session.commit()
                logger.error("checkout recovery rejected by provider, attempt failed attempt_id=%s user_id=%s",
                             attempt_id, user_id, exc_info=True)
                return False
            if created is not None:
                finish_attempt(session, attempt, created)
            else:
                attempt.next_attempt_at = now + RETRY_DELAY
                logger.info("checkout recovery found no provider link yet attempt_id=%s gateway=%s", attempt_id, gateway)
            ready = attempt.state == "ready"
            session.commit()
            return ready
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
            logger.warning("checkout recovery postponed attempt_id=%s", attempt_id, exc_info=True)
            _postpone_attempt(session, attempt_id)
            return False
    finally:
        try:
            session.close()
        except Exception:
            logger.warning("checkout recovery session close failed attempt_id=%s", attempt_id, exc_info=True)


def pending_payment_ids(session, limit=1, gateway=None, recoverable=None):
    """Созревшие prepared-попытки.

    gateway ограничивает шлюз (Wata: не больше одного GET /links за цикл >= 30 с
    на клиента). recoverable=True — ещё в окне восстановления (будет запрос к
    провайдеру), False — только перевод в review без сети, None — все.
    """
    now = datetime.utcnow()
    query = session.query(WebsitePaymentAttempt.id).filter(
        WebsitePaymentAttempt.state == "prepared",
        WebsitePaymentAttempt.next_attempt_at <= now,
    )
    if gateway is not None:
        query = query.filter(WebsitePaymentAttempt.gateway == gateway)
    if recoverable is True:
        query = query.filter(WebsitePaymentAttempt.created_at >= now - RECOVERY_WINDOW)
    elif recoverable is False:
        query = query.filter(WebsitePaymentAttempt.created_at < now - RECOVERY_WINDOW)
    return [row[0] for row in query.order_by(WebsitePaymentAttempt.next_attempt_at).limit(limit).all()]
