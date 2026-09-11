"""Read-only check that the common alembic migration for website tables is applied.

Запускается перед выкатом сайта (миграцию ждёт и бот; payment эти таблицы не
читает и в этом релизе не обновляется): код сайта без таблиц
website_payment_attempts / website_email_changes не может принимать оплаты.
Только чтение каталога через sqlalchemy.inspect — без raw SQL и без записи.
"""
from django.core.management.base import BaseCommand, CommandError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from common.models.db import WebsiteEmailChange, WebsitePaymentAttempt

REQUIRED_MODELS = (WebsitePaymentAttempt, WebsiteEmailChange)


def common_schema_problems(bind=None):
    """Расхождения схемы БД с ORM-моделями сайта; пустой список — всё на месте.

    Проверяются и колонки, а не только таблицы: так ловится пустая или
    частичная autogenerate-миграция (инцидент 2026-09-01).
    """
    if bind is None:
        import database

        bind = database.engine
    inspector = sa_inspect(bind)
    problems = []
    for model in REQUIRED_MODELS:
        table = model.__table__
        if not inspector.has_table(table.name, schema=table.schema):
            problems.append(f"нет таблицы {table.name}")
            continue
        existing = {
            column["name"]
            for column in inspector.get_columns(table.name, schema=table.schema)
        }
        missing = sorted(column.name for column in table.columns if column.name not in existing)
        if missing:
            problems.append(f"в таблице {table.name} нет колонок: {', '.join(missing)}")
    return problems


class Command(BaseCommand):
    help = (
        "Read-only: check that common alembic migration (website_payment_attempts, "
        "website_email_changes) is applied; exit code 1 otherwise"
    )

    def handle(self, *args, **options):
        try:
            problems = common_schema_problems()
        except SQLAlchemyError as exc:
            first_line = (str(exc).splitlines() or [""])[0]
            raise CommandError(
                f"Не удалось проверить схему common: {type(exc).__name__}: {first_line}"
            ) from exc
        if problems:
            raise CommandError(
                "Схема common не соответствует коду сайта: "
                + "; ".join(problems)
                + ". Примените alembic-миграцию common до выката website и бота."
            )
        names = ", ".join(model.__tablename__ for model in REQUIRED_MODELS)
        self.stdout.write(f"OK: схема common на месте ({names})")
