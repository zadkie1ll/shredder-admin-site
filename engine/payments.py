import httpx
import orjson
from uuid import uuid4
from datetime import datetime
from datetime import timedelta
from yookassa import Payment
from yookassa import Configuration
from common.models.tariff import Tariff
from common.models.tariff import TrialPromotionTariff

def create_yk_payment_sync(shop_id: str, secret: str, tariff: Tariff, username: str, telegram_id: int | None) -> str:
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
                # После оплаты на сайте логичнее возвращать в ЛК
                "return_url": "https://твой-домен.com/dashboard/", 
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
        idempotency_key
    )

    return payment.confirmation.confirmation_url

def create_wata_payment_sync(wata_host: str, wata_token: str, tariff: Tariff):
    url = f"{wata_host}/links"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {wata_token}",
    }

    payload = {
        "amount": f"{tariff.price}.00",
        "currency": "RUB",
        "description": tariff.description,
        "orderId": str(uuid4()),
        "expirationDateTime": (datetime.utcnow() + timedelta(minutes=15)).isoformat() + "Z",
    }

    # Используем обычный Client вместо AsyncClient
    with httpx.Client() as client:
        response = client.post(url, headers=headers, json=payload)

    response.raise_for_status()

    # Декодируем через orjson
    return orjson.loads(response.content)