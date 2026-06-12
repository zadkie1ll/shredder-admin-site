"""Tariffs for the mobile paywall. Same source as the cabinet/bot (the shared
``common.models.tariff`` classes).

TODO(parity): use ``engine.views.get_runtime_actual_tariffs`` for DB-overridden
runtime prices instead of the static class defaults, once the import is decoupled
from ``engine.views`` module-level side effects.
"""

from common.models.tariff import OneMonthTariff, OneYearTariff, ThreeMonthsTariff

MOBILE_TARIFFS = [OneMonthTariff(), ThreeMonthsTariff(), OneYearTariff()]


def serialize_tariff(tariff):
    return {
        "id": tariff.db_tariff_id,
        "price": tariff.price,
        "description": tariff.description,
        "period_days": tariff.subscription_period.days,
    }
