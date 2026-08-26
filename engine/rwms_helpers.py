import hashlib
import os
import logging
from datetime import datetime
from datetime import timezone
from datetime import timedelta
from typing import Optional
from common.rwms_client_sync import RwmsClientSync
import proto.rwmanager_pb2 as proto

# --- Детерминированное имя подписки от email -------------------------------
#
# ЕДИНСТВЕННЫЙ генератор имени для email-регистраций во всём вебсайте: его
# используют и mobile_api (email-логин приложения), и сайтовые flow
# (magic link / Google OAuth / Yandex OAuth / оплата). Раньше сайт брал
# ``uuid4().hex``: если процесс падал между панельным AddUser и commit'ом в
# postgres, подписка оставалась в Remnawave НАВСЕГДА (повтор регистрации
# генерировал НОВЫЙ uuid и создавал ЕЩЁ ОДНУ подписку). Детерминированное имя
# закрывает это окно — повтор выводит ТО ЖЕ имя, находит подписку строгим
# чтением и принимает её (adoption), не трогая панель.
#
# Формат: "m" + 31 hex-символ sha256 нормализованного email = 32 символа.
# Панель принимает ^[a-zA-Z0-9_-]+$ длиной 3..36. Пространство имён не
# пересекается с другими генераторами парка: имена бота — только цифры
# (``str(telegram_id)``), исторические сайтовые — 32 hex-символа (``m`` не
# hex-символ), legacy-фолбэк ``mi_`` — второй символ ``i`` не hex.
#
# ВАЖНО: имя пересчитывается ТОЛЬКО при создании новой записи users. Ни один
# путь не пересчитывает username существующего пользователя, поэтому legacy-
# аккаунты со случайными uuid4-именами остаются нетронутыми и логинятся как
# раньше.
USERNAME_PREFIX = "m"
USERNAME_HASH_LENGTH = 31


class RwmsSubscriptionOwnershipError(Exception):
    """Найденная в панели подписка ЯВНО принадлежит другому email.

    Неоднозначное состояние (ручная запись/импорт/ошибочно созданная ранее
    подписка): принимать такую подписку нельзя — это отдало бы чужой доступ.
    Трогать панель тоже нельзя. Вызывающий код обязан прервать провижининг.
    """

    def __init__(self, username, panel_email, requested_email):
        self.username = username
        self.panel_email = panel_email
        self.requested_email = requested_email
        super().__init__(
            f"subscription {username} belongs to {panel_email}, "
            f"not to {requested_email}"
        )


def normalize_email(email) -> str:
    """Единая нормализация email для сравнения и хеширования."""
    return (email or "").strip().lower()


def deterministic_username(email: str) -> str:
    """Детерминированное имя подписки/пользователя для регистрации по email.

    Одинаковый email всегда даёт одинаковое имя, поэтому повтор провижининга
    после крэша между AddUser и commit'ом БД выводит ТО ЖЕ имя и может принять
    уже созданную подписку вместо создания второй. Усечённый до 124 бит дайджест
    делает коллизии между разными email практически невозможными.
    """
    digest = hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()
    return USERNAME_PREFIX + digest[:USERNAME_HASH_LENGTH]


def get_proto_optional(message, field_name, default=None):
    """Значение optional-поля protobuf либо ``default``.

    Живёт здесь (а не в engine/views.py), чтобы им могли пользоваться и
    mobile_api, и сайтовые flow — оба читают ответы панели.
    """
    try:
        if message.HasField(field_name):
            return getattr(message, field_name)
    except ValueError:
        value = getattr(message, field_name, default)
        return value if value not in ("", 0) else default

    return default


def assert_subscription_owned_by_email(rw_user, email, *, flow):
    """Guard перед adoption: подписка из панели действительно наша?

    Adoption принимает УЖЕ СУЩЕСТВУЮЩУЮ подписку как принадлежащую текущему
    email. Само по себе совпадение имени этого не доказывает: хеш-коллизия
    маловероятна, но подписку с таким именем могли создать руками, импортом или
    ошибочным прошлым кодом. Поэтому сверяем email панельной записи:

    - совпадает с запрошенным → adoption разрешён;
    - email отсутствует или не совпадает → состояние неоднозначно, adoption
      запрещён. Все текущие deterministic-email пути передают email в AddUser,
      поэтому пустое поле не доказывает crash-recovery и может принадлежать
      ручной/импортированной записи;
    - только точное совпадение после нормализации разрешает adoption.
    """
    panel_email = normalize_email(get_proto_optional(rw_user, "email", ""))
    requested_email = normalize_email(email)

    if panel_email and requested_email and panel_email == requested_email:
        return

    logging.critical(
        "ALERT: %s: refusing to adopt RWMS subscription %s — panel email %s "
        "does not match requested %s; panel left untouched, provisioning stopped",
        flow,
        getattr(rw_user, "username", "<unknown>"),
        panel_email,
        requested_email,
    )
    raise RwmsSubscriptionOwnershipError(
        getattr(rw_user, "username", "<unknown>"),
        panel_email or "<missing email>",
        requested_email or "<missing requested email>",
    )


def assert_subscription_owned_by_telegram_id(rw_user, telegram_id, *, flow):
    """Guard adoption для детерминированного Telegram username.

    Новые Telegram-only подписки именуются ``str(telegram_id)`` — так же, как
    подписки бота. Но совпадения username недостаточно для автоматической
    выдачи конфига: ручная/импортированная запись может иметь то же имя.
    Разрешаем adoption только при точном совпадении optional telegram_id.
    """
    panel_telegram_id = get_proto_optional(rw_user, "telegram_id")
    requested_telegram_id = int(telegram_id) if telegram_id is not None else None

    if (
        panel_telegram_id is not None
        and requested_telegram_id is not None
        and int(panel_telegram_id) == requested_telegram_id
    ):
        return

    logging.critical(
        "ALERT: %s: refusing to adopt RWMS subscription %s — panel telegram_id "
        "%s does not match requested %s; panel left untouched, provisioning stopped",
        flow,
        getattr(rw_user, "username", "<unknown>"),
        panel_telegram_id,
        requested_telegram_id,
    )
    raise RwmsSubscriptionOwnershipError(
        getattr(rw_user, "username", "<unknown>"),
        f"telegram_id={panel_telegram_id}",
        f"telegram_id={requested_telegram_id}",
    )


def _internal_squads_uuids() -> list[str]:
    squads_uuids_value = os.getenv("INTERNAL_SQUADS_UUIDS")
    squads_uuids = []

    if squads_uuids_value:
        for squad_uuid in squads_uuids_value.split(","):
            squad_uuid = squad_uuid.strip()
            if squad_uuid:
                try:
                    squads_uuids.append(squad_uuid)
                except ValueError:
                    ...

    return squads_uuids


def create_user_until(
    rwms_client: RwmsClientSync,
    username: str,
    expire_at: datetime,
    email: str | None = None,
    telegram_id: int | None = None,
) -> Optional[proto.UserResponse]:
    if expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=timezone.utc)

    response = rwms_client.add_user(
        proto.AddUserRequest(
            username=username,
            email=email,
            telegram_id=telegram_id,
            expire_at=expire_at,
            status=proto.UserStatus.ACTIVE,
            traffic_limit_strategy=proto.TrafficLimitStrategy.NO_RESET,
            active_internal_squads=[*_internal_squads_uuids()],
            created_at=datetime.now(),
        )
    )

    return response


def create_user(
    rwms_client: RwmsClientSync,
    username: str,
    trial_period_days: int,
    from_referrer: bool = False,
    email: str | None = None,
    telegram_id: int | None = None,
) -> Optional[proto.UserResponse]:
    if from_referrer:
        logging.info(
            f"creating subscription {username} with referral bonus, trial period {trial_period_days} days"
        )
    else:
        logging.info(
            f"creating subscription {username}, trial period {trial_period_days} days"
        )

    return create_user_until(
        rwms_client=rwms_client,
        username=username,
        expire_at=datetime.now(timezone.utc) + timedelta(days=trial_period_days),
        email=email,
        telegram_id=telegram_id,
    )
