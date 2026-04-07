import uuid
from yookassa import Payment, Configuration
from common.models.tariff import Tariff, TrialPromotionTariff

def create_payment_sync(shop_id: str, secret: str, tariff: Tariff, username: str, telegram_id: int | None) -> str:
    Configuration.account_id = shop_id
    Configuration.secret_key = secret

    # Генерируем ключ идемпотентности, чтобы избежать дублей при сбоях
    idempotency_key = str(uuid.uuid4())

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