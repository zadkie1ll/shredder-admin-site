import httpx
import orjson
from dataclasses import dataclass
from uuid import uuid4
from datetime import datetime
from datetime import timedelta
from yookassa import Payment
from yookassa import Configuration
from common.models.tariff import Tariff
from common.models.tariff import TrialPromotionTariff

# Фискализация (54-ФЗ): магазин ЮКассы требует объект receipt в каждом
# платеже. Ставка НДС "без НДС" (УСН). У пользователей сайта email есть
# всегда (это их логин в кабинете).
RECEIPT_VAT_CODE = 1


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


def create_yk_payment_sync(
    shop_id: str,
    secret: str,
    tariff: Tariff,
    username: str,
    telegram_id: int | None,
    return_url: str,
    email: str | None = None,
) -> CreatedPayment:
    Configuration.account_id = shop_id
    Configuration.secret_key = secret

    # Генерируем ключ идемпотентности, чтобы избежать дублей при сбоях
    idempotency_key = str(uuid4())

    payment_data = {
        "save_payment_method": True,
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
        },
        "capture": True,
        "description": tariff.description,
    }

    if email:
        payment_data["receipt"] = build_receipt(
            email, tariff.description, tariff.price
        )

    payment = Payment.create(payment_data, idempotency_key)

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
        "orderId": str(uuid4()),
        "expirationDateTime": (datetime.utcnow() + timedelta(minutes=15)).isoformat()
        + "Z",
    }
    if success_redirect_url:
        payload["successRedirectUrl"] = success_redirect_url
    if fail_redirect_url:
        payload["failRedirectUrl"] = fail_redirect_url

    # Используем обычный Client вместо AsyncClient
    with httpx.Client() as client:
        response = client.post(url, headers=headers, json=payload)

    response.raise_for_status()

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
