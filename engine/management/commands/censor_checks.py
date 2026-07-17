"""Обслуживание замеров ТСПУ: запуск по расписанию и сбор результатов.

Запускается кроном раз в минуту:
    * * * * * cd /app && python manage.py censor_checks

Без крона расписание тоже работает (лениво, при открытии вкладки в админке),
но тогда прогоны создаются только пока админку кто-то смотрит.
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from database import session_factory
from engine import ripe_atlas


class Command(BaseCommand):
    help = "Запускает просроченные замеры ТСПУ и собирает результаты pending-прогонов"

    def handle(self, *args, **options):
        api_key = settings.RIPE_ATLAS_API_KEY
        if not api_key:
            self.stdout.write("RIPE_ATLAS_API_KEY не задан — нечего делать")
            return

        db_session = session_factory()
        try:
            ripe_atlas.finalize_pending_runs(db_session, api_key)
            ripe_atlas.schedule_due_checks(db_session, api_key)
        except Exception:
            logging.exception("censor checks: cron maintenance failed")
        finally:
            db_session.close()
