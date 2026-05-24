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
) -> CreatedPayment:
    Configuration.account_id = shop_id
    Configuration.secret_key = secret

    # Генерируем ключ идемпотентности, чтобы избежать дублей при сбоях
    idempotency_key = str(uuid4())

    payment = Payment.create(
        {
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
        },
        idempotency_key,
    )

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
