import hashlib
import os
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from datetime import timedelta
from typing import Optional
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from common.managed_traffic_limits import APPLIED_BY_SITE_REGISTER
from common.managed_traffic_limits import EVENT_TRAFFIC_LIMIT_APPLIED
from common.managed_traffic_limits import EVENT_TRAFFIC_LIMIT_RELEASED
from common.managed_traffic_limits import REASON_TRIAL
from common.managed_traffic_limits import RELEASE_ON_PAYMENT
from common.managed_traffic_limits import is_missing_table_error
from common.managed_traffic_limits import marker_event_payload
from common.managed_traffic_limits import normalize_strategy_name
from common.managed_traffic_limits import upsert_managed_limit
from common.models.db import EventLog
from common.models.db import ManagedTrafficLimit
from common.models.db import SystemSetting
from common.models.settings import DEFAULT_TRIAL_TRAFFIC_LIMIT_ENABLED
from common.models.settings import DEFAULT_TRIAL_TRAFFIC_LIMIT_GB
from common.models.settings import DEFAULT_TRIAL_TRAFFIC_LIMIT_STRATEGY
from common.models.settings import TRIAL_TRAFFIC_LIMIT_ENABLED_SETTING
from common.models.settings import TRIAL_TRAFFIC_LIMIT_GB_SETTING
from common.models.settings import TRIAL_TRAFFIC_LIMIT_STRATEGY_SETTING
from common.models.settings import parse_bool_setting
from common.models.settings import parse_positive_float_setting
from common.models.settings import normalize_trial_traffic_limit_strategy
from common.models.settings import trial_traffic_limit_bytes
from common.models.settings import trial_traffic_limit_strategy_proto_name
from common.rwms_client_sync import RwmsClientSync
import proto.rwmanager_pb2 as proto
from .sql_helpers import user_never_paid

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


# Формат email, пригодного для панели и для чека 54-ФЗ. Те же строки продублированы в payment,
# на сайте и в боте обоих проектов — общий модуль в common сознательно не
# заводился, чтобы не бампать submodule ради валидатора; при изменении
# править синхронно во всех шести местах.
#
# ЧТО ЭТО НА САМОМ ДЕЛЕ ДАЁТ. Панель валидирует email через pydantic EmailStr
# (Create/UpdateUserRequestDto), а email_validator в этих сервисах не
# установлен, поэтому побайтовым зеркалом EmailStr регэксп быть не может.
# Он проверен против настоящего EmailStr из SDK и закрывает весь реалистичный
# класс опечаток — 'ivan@mail.ru.', 'ivan@mail..ru', '.ivan@mail.ru',
# 'ivan@-mail.ru', 'ivan@mail_box.ru' и т.п.; на 40k случайных адресов
# расхождений «я разрешил, панель отвергнет» осталось 9, и все они — строки
# вида 'xx--yy.tld' (псевдо-punycode), которых пользователь не наберёт.
# Это фильтр, а не гарантия.
#
# ВАЖНО про INVALID_ARGUMENT: RWMS с 2026-09-10 отвечает им на невалидный
# запрос, НО ни один клиент парка этот код пока не различает — strict-варианты
# RwmsClient.update_user/add_user живут в common и отложены до планового
# бампа submodule. Пока их нет, терминальная ошибка по-прежнему уходит в
# бесконечный ретрай, поэтому не полагайся на неё как на страховку.
# Локальная часть: точки допустимы только МЕЖДУ символами (ведущая,
# хвостовая и сдвоенная точки — самые частые опечатки, и EmailStr их
# отвергает). Домен: метки не начинаются и не заканчиваются дефисом, без
# подчёркиваний, минимум одна точка, TLD от двух букв.
_EMAIL_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_EMAIL_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
EMAIL_RE = re.compile(rf"^{_EMAIL_LOCAL}@(?:{_EMAIL_LABEL}\.)+[A-Za-z]{{2,}}$")
EMAIL_MAX_LENGTH = 254


def is_valid_email(email) -> bool:
    """Похоже ли значение на пригодный адрес. Работает по НОРМАЛИЗОВАННОМУ
    виду, чтобы '  User@Example.COM ' не отвергался из-за регистра/пробелов."""
    normalized = normalize_email(email)
    return bool(EMAIL_RE.fullmatch(normalized)) and len(normalized) <= EMAIL_MAX_LENGTH


def normalize_email(email) -> str:
    """Единая нормализация email для сравнения и хеширования."""
    return (email or "").strip().lower()


def usable_panel_email(email, username: str, operation: str) -> Optional[str]:
    """email для запроса к панели, либо None — если класть его туда нельзя.

    Инцидент 2026-09-10 в Village, код общий. Сайт принимает email без
    серверной проверки формата (браузерная проверка ``type="email"``
    пропускает домен без точки — по спецификации WHATWG такой адрес валиден),
    и адрес вида ``milenapanowa@yandex`` попадал в ``users.email``. Панель
    Remnawave валидирует email через ``pydantic.EmailStr`` в
    ``CreateUserRequestDto``/``UpdateUserRequestDto``, поэтому запрос падал
    ещё до похода в Remnawave.

    Последствие на сайте было тихим и уже наступило в проде:
    ``RwmsClientSync.add_user`` глотает любой ``grpc.RpcError`` и возвращает
    ``None``, после чего регистрация уходит в
    ``create_local_site_user_without_rwms`` — пользователь «зарегистрирован»,
    magic link ушёл, а подписки в панели нет вовсе.

    Поэтому невалидный адрес просто не попадает в запрос: поле остаётся
    незаданным (``HasField=False``), RWMS передаёт ``None``, ``exclude_none``
    убирает его из PATCH — email в панели остаётся прежним, а не затирается,
    и подписка создаётся/продлевается. email в панели — метаданные для
    админки: чеки 54-ФЗ выставляются по ``users.email`` в момент создания
    счёта, письма шлёт отдельный сервис.
    """
    if not email:
        return None
    # Возвращается именно НОРМАЛИЗОВАННОЕ значение: проверять одно, а
    # отправлять в панель другое — это ровно та дыра, которую мы чиним.
    # Панельные и БД-адреса и так хранятся нормализованными, так что на
    # практике значение не меняется.
    normalized = normalize_email(email)
    if is_valid_email(normalized):
        return normalized
    logging.warning(
        "email %r of %s is not a valid address and is left out of the %s request "
        "to the panel: the panel keeps its current email, the subscription "
        "operation itself proceeds (fix the address in users.email)",
        email,
        username,
        operation,
    )
    return None


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


# --- Антиабьюз: лимит трафика НОВЫХ пробных подписок ------------------------
#
# Управляется ТОЛЬКО из админки сайта (system_settings), по умолчанию выключен.
# Применяется при создании подписки в панели (AddUserRequest.traffic_limit_bytes
# + traffic_limit_strategy) и ТОЛЬКО если пользователь never_paid: нет ни
# одного успешного платежа (см. sql_helpers.user_never_paid). Выключение
# настройки возвращает поведение «как раньше»: новые триалы создаются без
# лимита, уже ограниченные остаются до кнопки «снять лимит» в админке.


@dataclass(frozen=True)
class TrialTrafficLimit:
    """Лимит трафика пробной подписки, готовый к передаче в RWMS."""

    limit_gb: float
    limit_bytes: int
    # значение настройки в нижнем регистре: no_reset | day | week | month | month_rolling
    strategy_key: str
    # имя члена enum TrafficLimitStrategy в rwmanager.proto: NO_RESET | DAY | ...
    strategy_name: str

    @property
    def strategy(self) -> int:
        return getattr(proto.TrafficLimitStrategy, self.strategy_name)


def _setting_value(db_session, key):
    setting = db_session.get(SystemSetting, key)
    return setting.value if setting is not None else None


def trial_traffic_limit_enabled(db_session) -> bool:
    return parse_bool_setting(
        _setting_value(db_session, TRIAL_TRAFFIC_LIMIT_ENABLED_SETTING),
        DEFAULT_TRIAL_TRAFFIC_LIMIT_ENABLED,
    )


def trial_traffic_limit_configured(db_session) -> TrialTrafficLimit:
    """Сконфигурированный лимит (ГиБ + стратегия) независимо от тумблера.

    Нужен админ-действиям «применить лимит» в карточке/bulk и предпросмотру:
    тумблер отвечает только за автоматическое применение к новым триалам.
    """
    limit_gb = parse_positive_float_setting(
        _setting_value(db_session, TRIAL_TRAFFIC_LIMIT_GB_SETTING),
        DEFAULT_TRIAL_TRAFFIC_LIMIT_GB,
    )
    raw_strategy = _setting_value(db_session, TRIAL_TRAFFIC_LIMIT_STRATEGY_SETTING)
    strategy_key = normalize_trial_traffic_limit_strategy(raw_strategy)
    if raw_strategy is None:
        strategy_key = DEFAULT_TRIAL_TRAFFIC_LIMIT_STRATEGY
    return TrialTrafficLimit(
        limit_gb=limit_gb,
        limit_bytes=trial_traffic_limit_bytes(limit_gb),
        strategy_key=strategy_key,
        strategy_name=trial_traffic_limit_strategy_proto_name(strategy_key),
    )


def trial_traffic_limit_for_new_trial(db_session) -> Optional[TrialTrafficLimit]:
    """Лимит для СОЗДАВАЕМОЙ пробной подписки нового пользователя.

    У нового пользователя платежей быть не может (строка users создаётся
    вместе с подпиской), поэтому проверяется только тумблер. ``None`` —
    лимит выключен: подписка создаётся без лимита, как раньше.
    """
    if not trial_traffic_limit_enabled(db_session):
        return None
    return trial_traffic_limit_configured(db_session)


def trial_traffic_limit_for_user(db_session, user) -> Optional[TrialTrafficLimit]:
    """Лимит для ПЕРЕСОЗДАНИЯ подписки существующего пользователя (достоверный
    NOT_FOUND в панели): только если тумблер включён И пользователь never_paid.
    Платившему (или если строки users нет) лимит не ставится."""
    if user is None or not trial_traffic_limit_enabled(db_session):
        return None
    if user_never_paid(db_session, getattr(user, "id", None)) is not True:
        return None
    return trial_traffic_limit_configured(db_session)


# --- Антиабьюз v2: маркеры управляемых лимитов (managed_traffic_limits) -----
#
# Ручные лимиты владельца НЕПРИКОСНОВЕННЫ. Автоматика снимает только лимиты,
# поставленные автоматикой, а «свой» лимит узнаёт по маркеру
# ``managed_traffic_limits`` (common/managed_traffic_limits.py). Каждое место
# сайта, где фича СТАВИТ лимит (регистрация, пересоздание, «Применить лимит»,
# массовые операции, backfill), пишет маркер В ТОЙ ЖЕ сессии, что и само
# действие; снятие (оплата, страховка user-notify, кнопки админки) — только при
# is_managed. Без таблицы (миграция не накачена) хелперы common ведут себя как
# «маркеров нет» с warning; действия админки при этом отвечают 503
# (managed_limits_table_available), регистрация и карточка клиента не падают.


def managed_limits_table_available(db_session) -> bool:
    """Таблица ``managed_traffic_limits`` существует (миграция накачена).

    Проба в savepoint: отсутствие таблицы (PostgreSQL 42P01 / SQLite «no such
    table») откатывает только savepoint, транзакция вызывающего кода цела.
    Любая другая ошибка БД пробрасывается."""
    try:
        with db_session.begin_nested():
            db_session.execute(select(ManagedTrafficLimit.user_id).limit(1))
    except DBAPIError as exc:
        if is_missing_table_error(exc):
            logging.warning(
                "managed_traffic_limits: таблица отсутствует (миграция не накачена)"
            )
            return False
        raise
    return True


def record_trial_limit_marker(
    db_session, user, traffic_limit, applied_by=APPLIED_BY_SITE_REGISTER
):
    """Маркер лимита пробного (reason=trial, release_on=payment) для
    пользователя, которому фича только что поставила ``traffic_limit`` —
    в сессии вызывающего кода, без commit. ``None`` — лимит не ставился,
    строки users нет или таблицы маркеров нет (warning из common)."""
    if traffic_limit is None or user is None or getattr(user, "id", None) is None:
        return None
    marker = upsert_managed_limit(
        db_session,
        user.id,
        traffic_limit.limit_bytes,
        traffic_limit.strategy_name,
        REASON_TRIAL,
        RELEASE_ON_PAYMENT,
        applied_by,
    )
    if marker is None:
        logging.warning(
            "trial traffic limit for %s applied WITHOUT managed marker "
            "(managed_traffic_limits table missing): payment will not lift it "
            "until the migration is applied and backfill is run",
            getattr(user, "username", user.id),
        )
    else:
        logging.info(
            "managed traffic limit marker written: user_id=%s limit=%s bytes "
            "strategy=%s applied_by=%s",
            user.id,
            marker.limit_bytes,
            marker.strategy,
            applied_by,
        )
    return marker


def panel_limit_matches(rw_user, traffic_limit) -> bool:
    """Панельная запись несёт ровно ``traffic_limit`` (байты И стратегия)."""
    if rw_user is None or traffic_limit is None:
        return False
    panel_bytes = getattr(rw_user, "traffic_limit_bytes", None)
    if panel_bytes is None or isinstance(panel_bytes, bool):
        return False
    try:
        if int(panel_bytes) != int(traffic_limit.limit_bytes):
            return False
    except (TypeError, ValueError):
        return False
    return (
        normalize_strategy_name(getattr(rw_user, "traffic_limit_strategy", None))
        == traffic_limit.strategy_name
    )


def adopted_trial_limit(db_session, rw_user) -> Optional[TrialTrafficLimit]:
    """Лимит для маркера при adoption подписки (crash-окно между AddUser и
    commit): наш прошлый AddUser поставил лимит, а маркер пропал вместе с
    транзакцией. Правило то же, что у backfill: тумблер включён И панель несёт
    ровно текущий лимит пробных (байты и стратегия). Иначе ``None`` — панель
    не трогаем, маркер не пишем (ручной лимит владельца остаётся ручным)."""
    limit = trial_traffic_limit_for_new_trial(db_session)
    if limit is None or not panel_limit_matches(rw_user, limit):
        return None
    return limit


def add_traffic_limit_event(db_session, user_id, event_type, marker, **extra):
    """Событие ``event_logs`` ``traffic_limit_applied`` /
    ``traffic_limit_released`` (payload ``marker_event_payload``) — история
    лимитов там, где сайт уже пишет event_logs. Без маркера (таблицы нет) —
    ничего не пишем."""
    if marker is None or user_id is None:
        return None
    if event_type not in (EVENT_TRAFFIC_LIMIT_APPLIED, EVENT_TRAFFIC_LIMIT_RELEASED):
        raise ValueError(f"unknown traffic limit event: {event_type!r}")
    event = EventLog(
        user_id=int(user_id),
        event_type=event_type,
        event_payload=marker_event_payload(marker, **extra),
    )
    db_session.add(event)
    return event


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
    traffic_limit: TrialTrafficLimit | None = None,
) -> Optional[proto.UserResponse]:
    """AddUser в панели. ``traffic_limit`` (антиабьюз пробных) задаёт
    ``traffic_limit_bytes`` (новое optional-поле 13 AddUserRequest) и
    стратегию сброса; без него запрос прежний — без лимита, NO_RESET."""
    if expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=timezone.utc)

    request = proto.AddUserRequest(
        username=username,
        # email здесь НЕ метаданные, а ИДЕНТИЧНОСТЬ: adoption после краха
        # между AddUser и commit'ом сверяет панельный email с запрошенным
        # (assert_subscription_owned_by_email), и подписка, созданная с
        # пустым email, не может быть принята НИКОГДА — гард сочтёт её
        # чужой и навсегда остановит провижининг этого пользователя.
        # Поэтому здесь usable_panel_email НЕ применяется: формат адреса
        # проверяется на входе (pay / send_magic_link / update_email /
        # confirm_email), а если невалидный адрес всё же дойдёт сюда,
        # честнее уронить AddUser, чем создать неусыновляемую подписку.
        email=email,
        telegram_id=telegram_id,
        expire_at=expire_at,
        status=proto.UserStatus.ACTIVE,
        traffic_limit_strategy=proto.TrafficLimitStrategy.NO_RESET,
        active_internal_squads=[*_internal_squads_uuids()],
        created_at=datetime.now(),
    )
    if traffic_limit is not None:
        request.traffic_limit_bytes = int(traffic_limit.limit_bytes)
        request.traffic_limit_strategy = traffic_limit.strategy
        logging.info(
            "creating subscription %s with trial traffic limit %s GiB (%s bytes), "
            "strategy %s",
            username,
            traffic_limit.limit_gb,
            traffic_limit.limit_bytes,
            traffic_limit.strategy_name,
        )

    return rwms_client.add_user(request)


def create_user(
    rwms_client: RwmsClientSync,
    username: str,
    trial_period_days: int,
    from_referrer: bool = False,
    email: str | None = None,
    telegram_id: int | None = None,
    traffic_limit: TrialTrafficLimit | None = None,
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
        traffic_limit=traffic_limit,
    )
