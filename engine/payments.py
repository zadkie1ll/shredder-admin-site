import httpx
import orjson
import os
from dataclasses import dataclass
from uuid import uuid4
from datetime import datetime
from datetime import timedelta
from yookassa import Payment
from yookassa import Configuration
from yookassa.client import ApiClient
from yookassa.domain.exceptions import BadRequestError
from yookassa.domain.exceptions import ForbiddenError
from yookassa.domain.exceptions import NotFoundError
from yookassa.domain.exceptions import UnauthorizedError
from yookassa.domain.request import PaymentRequest
from common.models.tariff import Tariff
from common.models.tariff import TrialPromotionTariff

# Фискализация (54-ФЗ): магазин ЮКассы требует объект receipt в каждом
# платеже. Ставка НДС "без НДС" (УСН). Email для чека передаёт вызывающий код:
# он может прийти из формы оплаты (анонимная покупка с лендинга, аккаунт без
# email) и ещё не быть подтверждён; без email receipt не добавляется.
# Автоплатёж (save_payment_method) сайт включает только при email в аккаунте:
# yk-recurrent берёт email чека из users.email.
RECEIPT_VAT_CODE = 1
PAYMENT_HTTP_TIMEOUT_SECONDS = max(
    0.5,
    float(os.getenv("PAYMENT_HTTP_TIMEOUT_SECONDS", "10")),
)

# Однозначный отказ провайдера на создание платежа: запрос получен и отвергнут,
# платёж точно не создан. Таймауты, обрывы, 429, 5xx, 202 после ретраев и
# неразборчивый ответ сюда НЕ относятся — это неизвестный исход.
YOOKASSA_REJECTED_ERRORS = (BadRequestError, UnauthorizedError, ForbiddenError, NotFoundError)
WATA_REJECTED_STATUS_CODES = frozenset({400, 401, 403, 404, 422})


class ProviderRejected(Exception):
    """Провайдер ответил 400/401/403/404(/422 у Wata) на создание платежа."""


class TimeoutApiClient(ApiClient):
    """YooKassa SDK transport with an actual requests network timeout."""

    def execute(self, body, method, path, query_params, request_headers):
        session = self.get_session()
        self.log_request(body, method, path, query_params, request_headers)
        try:
            raw_response = session.request(
                method,
                self.endpoint + path,
                params=query_params,
                headers=request_headers,
                json=body,
                verify=self.configuration.verify,
                timeout=PAYMENT_HTTP_TIMEOUT_SECONDS,
            )
        finally:
            session.close()

        self.log_response(
            raw_response.content,
            self.get_response_info(raw_response),
            raw_response.headers,
        )
        return raw_response


class TimeoutPayment(Payment):
    def __init__(self):
        self.client = TimeoutApiClient()


def build_receipt(email: str, description: str, price: int) -> dict:
    return {
        "customer": {"email": email},
        "items": [
            {
                "description": description,
                "quantity": "1.00",
                "amount": {"value": f"{price}.00", "currency": "RUB"},
                "vat_code": RECEIPT_VAT_CODE,
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }
        ],
    }


@dataclass(frozen=True)
class CreatedPayment:
    confirmation_url: str
    reference: str
    payload: dict | None = None


def validate_yookassa_request_locally(payment_data: dict) -> None:
    """Те же проверки, что SDK делает внутри Payment.create без сети.

    Вызывается ДО коммита prepared-попытки: битый email чека, пустые ключи
    магазина и прочие локальные ошибки не должны выглядеть «неизвестным
    исходом» (ConfigurationError, ValueError, TypeError).
    """
    Configuration.instantiate()
    PaymentRequest(payment_data).validate()


def _create_yookassa_payment(payment_data: dict, idempotency_key: str):
    try:
        return TimeoutPayment.create(payment_data, idempotency_key)
    except YOOKASSA_REJECTED_ERRORS as exc:
        raise ProviderRejected(
            f"YooKassa rejected payment creation: {type(exc).__name__}"
        ) from exc


def create_yk_payment_sync(
    shop_id: str,
    secret: str,
    tariff: Tariff,
    username: str,
    telegram_id: int | None,
    return_url: str,
    email: str | None = None,
    promo: bool = False,
    idempotency_key: str | None = None,
    before_send=None,
    save_payment_method: bool = True,
    account_email: str | None = None,
) -> CreatedPayment:
    Configuration.account_id = shop_id
    Configuration.secret_key = secret

    # Генерируем ключ идемпотентности, чтобы избежать дублей при сбоях
    idempotency_key = idempotency_key or str(uuid4())

    payment_data = {
        # False — разовый платёж без сохранения карты: автосписание yk-recurrent
        # берёт email чека только из users.email, без него оно не пройдёт.
        "save_payment_method": bool(save_payment_method),
        "amount": {"value": tariff.price, "currency": "RUB"},
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "metadata": {
            "username": username,
            "telegram_id": telegram_id,
            "subscription_period": tariff.db_tariff_id,
            "autopay": False,
            "trial_promotion": isinstance(tariff, TrialPromotionTariff),
            "from_trial": False,
            # Персональная промо-скидка на первую покупку: payment-сервис
            # заведёт рекуррент по регулярной runtime-цене тарифа.
            "promo": promo,
        },
        "capture": True,
        "description": tariff.description,
    }

    if account_email:
        # Email АККАУНТА (users.email), не email чека: чек вводится в форме и не
        # подтверждён. payment-сервис ищет пользователя username → telegram_id
        # > 0 → metadata.email (поле опциональное), поэтому после merge аккаунта
        # оплата старой ссылки найдёт выжившего с этим email (XSVC-01).
        payment_data["metadata"]["email"] = account_email

    if email:
        payment_data["receipt"] = build_receipt(
            email, tariff.description, tariff.price
        )

    validate_yookassa_request_locally(payment_data)
    if before_send is not None:
        before_send(payment_data)
    payment = _create_yookassa_payment(payment_data, idempotency_key)

    return CreatedPayment(
        confirmation_url=payment.confirmation.confirmation_url,
        reference=payment.id,
    )


def create_wata_payment_sync(
    wata_host: str,
    wata_token: str,
    tariff: Tariff,
    success_redirect_url: str | None = None,
    fail_redirect_url: str | None = None,
    idempotency_key: str | None = None,
    before_send=None,
) -> CreatedPayment:
    url = f"{wata_host}/links"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {wata_token}",
    }

    payload = {
        "amount": float(tariff.price),
        "currency": "RUB",
        "description": tariff.description,
        "orderId": idempotency_key or str(uuid4()),
        "expirationDateTime": (datetime.utcnow() + timedelta(minutes=15)).isoformat()
        + "Z",
    }
    if success_redirect_url:
        payload["successRedirectUrl"] = success_redirect_url
    if fail_redirect_url:
        payload["failRedirectUrl"] = fail_redirect_url

    if before_send is not None:
        before_send(payload)

    # Используем обычный Client вместо AsyncClient
    with httpx.Client(timeout=PAYMENT_HTTP_TIMEOUT_SECONDS) as client:
        response = client.post(url, headers=headers, json=payload)

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in WATA_REJECTED_STATUS_CODES:
            raise ProviderRejected(
                f"Wata rejected payment link creation: HTTP {exc.response.status_code}"
            ) from exc
        raise

    # Декодируем через orjson
    payment_json = orjson.loads(response.content)
    return CreatedPayment(
        confirmation_url=payment_json["url"],
        reference=payment_json["orderId"],
        payload=payment_json,
    )


def fetch_wata_transaction_status(
    wata_host: str,
    wata_token: str,
    order_id: str,
    timeout: float = 4.0,
) -> str | None:
    """Активно спрашивает у Wata статус транзакции по orderId (read-only).

    Возвращает:
      - "Paid"      — есть успешная транзакция по заказу;
      - "Declined"  — все транзакции по заказу отклонены (и нет ожидающих);
      - None        — заказ ещё не оплачен/в процессе, не найден, либо любая
                      ошибка/таймаут/лимит. В этом случае вызывающий код обязан
                      вести себя как раньше (опираться на вебхук). Неопределённость
                      НИКОГДА не трактуется как успех.

    Внимание: у Wata GET лимитирован 1 запросом в 30 секунд на объект, поэтому
    вызывать эту функцию нужно редко и с троттлингом на стороне вызывающего.
    """
    if not (wata_host and wata_token and order_id):
        return None

    url = f"{wata_host}/transactions"
    headers = {"Authorization": f"Bearer {wata_token}"}
    params = {"orderId": order_id, "maxResultCount": 50}

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers, params=params)
        if response.status_code != 200:
            return None
        payload = orjson.loads(response.content)
    except Exception:
        return None

    items = payload.get("items") if isinstance(payload, dict) else None
    if not items:
        return None

    statuses = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = item.get("status") or item.get("transactionStatus")
        if status:
            statuses.append(status)

    if "Paid" in statuses:
        return "Paid"
    # Ещё есть незавершённые попытки — рано говорить об отказе.
    if any(s in ("Created", "Pending") for s in statuses):
        return None
    if statuses and all(s == "Declined" for s in statuses):
        return "Declined"
    return None


def recover_prepared_payment(gateway, payload, key, settings):
    """Recover the exact saved operation; Wata is NEVER blindly POSTed again."""
    if gateway == "yookassa":
        Configuration.account_id = settings.YOOKASSA_SHOP_ID
        Configuration.secret_key = settings.YOOKASSA_SECRET_KEY
        payment = _create_yookassa_payment(payload, key)
        return CreatedPayment(getattr(payment.confirmation, "confirmation_url", None) or "", payment.id)
    headers = {"Authorization": f"Bearer {settings.WATA_TOKEN}"}
    with httpx.Client(timeout=PAYMENT_HTTP_TIMEOUT_SECONDS) as client:
        response = client.get(f"{settings.WATA_HOST}/links", headers=headers,
                              params={"orderId": key, "maxResultCount": 2})
        response.raise_for_status()
        matches = [item for item in response.json().get("items", []) if item.get("orderId") == key]
        if len(matches) != 1:
            return None  # Not found or ambiguous is not permission to recreate.
        detail = client.get(f"{settings.WATA_HOST}/links/{matches[0]['id']}", headers=headers)
        detail.raise_for_status()
        invoice = detail.json()
    if (invoice.get("orderId") != key or invoice.get("currency") != payload["currency"]
            or str(invoice.get("amount")) != str(payload["amount"])):
        # Decimal-normalized comparison is below; do not infer ownership by email.
        from decimal import Decimal
        if (invoice.get("orderId") != key or invoice.get("currency") != payload["currency"]
                or Decimal(str(invoice.get("amount"))) != Decimal(str(payload["amount"]))):
            raise ValueError("Wata recovery identity/amount mismatch")
    return CreatedPayment(invoice["url"], key, invoice)
