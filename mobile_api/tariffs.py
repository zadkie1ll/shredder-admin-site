"""Runtime tariffs for the mobile paywall and the website cabinet."""

from common.models.tariff import OneMonthTariff, OneYearTariff, ThreeMonthsTariff
from common.runtime_tariffs import resolve_runtime_tariffs

MOBILE_TARIFFS = [OneMonthTariff(), ThreeMonthsTariff(), OneYearTariff()]


def get_mobile_tariffs(db_session):
    return resolve_runtime_tariffs(db_session, MOBILE_TARIFFS)


def serialize_tariff(tariff):
    return {
        "id": tariff.db_tariff_id,
        "price": tariff.price,
        "description": tariff.description,
        "period_days": tariff.subscription_period.days,
    }
