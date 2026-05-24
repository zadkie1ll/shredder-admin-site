import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.models.db import User
from common.models.db import WataInvoice


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
