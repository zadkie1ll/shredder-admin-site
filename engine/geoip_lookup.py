"""Локальное GeoIP-обогащение клиентских адресов для админки.

Внешние web API намеренно не используются: адреса клиентов не покидают
инфраструктуру Monkey Island. Источник данных — локальная DB-IP City Lite
база в совместимом формате MMDB.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

try:
    import maxminddb
except ImportError:  # pragma: no cover - защита неполного локального окружения
    maxminddb = None


log = logging.getLogger("infra.geoip")


def _normalize_region_alias(value: str) -> str:
    """Нормализует подпись DB-IP для поиска канонического субъекта РФ."""

    normalized = str(value or "").strip().casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", normalized).split())


# DB-IP City Lite использует GeoNames-подписи на английском и, в отличие от
# GeoLite2, не кладёт iso_code в subdivisions. Держим локальную таблицу
# первого административного уровня: она не требует внешнего API и одновременно
# даёт стабильный ключ агрегации и русскую подпись интерфейса.
_RUSSIAN_REGION_ROWS = (
    ("AD", "Республика Адыгея", "Adygea", "Adygeya", "Adygeya Republic"),
    ("AL", "Республика Алтай", "Altai", "Altai Republic", "Altay Republic"),
    ("ALT", "Алтайский край", "Altai Krai", "Altayskiy Kray"),
    ("AMU", "Амурская область", "Amur", "Amur Oblast"),
    (
        "ARK",
        "Архангельская область",
        "Arkhangelsk",
        "Arkhangelsk Oblast",
        "Arkhangelskaya Oblast'",
    ),
    ("AST", "Астраханская область", "Astrakhan", "Astrakhan Oblast"),
    ("BA", "Республика Башкортостан", "Bashkortostan", "Bashkortostan Republic"),
    ("BEL", "Белгородская область", "Belgorod", "Belgorod Oblast"),
    ("BRY", "Брянская область", "Bryansk", "Bryansk Oblast"),
    ("BU", "Республика Бурятия", "Buryatia", "Buryatiya", "Buryatiya Republic"),
    ("CE", "Чеченская Республика", "Chechnya", "Chechen Republic"),
    ("CHE", "Челябинская область", "Chelyabinsk", "Chelyabinsk Oblast"),
    ("CHU", "Чукотский автономный округ", "Chukotka", "Chukotka Autonomous Okrug"),
    ("CU", "Чувашская Республика", "Chuvashia", "Chuvash Republic"),
    ("CR", "Республика Крым", "Crimea", "Republic of Crimea"),
    ("DA", "Республика Дагестан", "Dagestan", "Dagestan Republic"),
    ("IN", "Республика Ингушетия", "Ingushetia", "Ingushetiya", "Ingushetiya Republic"),
    ("IRK", "Иркутская область", "Irkutsk", "Irkutsk Oblast"),
    ("IVA", "Ивановская область", "Ivanovo", "Ivanovo Oblast"),
    ("YEV", "Еврейская автономная область", "Jewish Autonomous Oblast"),
    (
        "KB",
        "Кабардино-Балкарская Республика",
        "Kabardino-Balkaria",
        "Kabardino-Balkariya Republic",
    ),
    ("KGD", "Калининградская область", "Kaliningrad", "Kaliningrad Oblast"),
    ("KL", "Республика Калмыкия", "Kalmykia", "Kalmykiya", "Kalmykiya Republic"),
    ("KLU", "Калужская область", "Kaluga", "Kaluga Oblast"),
    ("KAM", "Камчатский край", "Kamchatka", "Kamchatka Krai"),
    (
        "KC",
        "Карачаево-Черкесская Республика",
        "Karachay-Cherkessia",
        "Karachayevo-Cherkesiya Republic",
    ),
    ("KEM", "Кемеровская область — Кузбасс", "Kemerovo", "Kemerovo Oblast", "Kuzbass"),
    ("KHA", "Хабаровский край", "Khabarovsk", "Khabarovsk Krai", "Khabarovskiy Kray"),
    ("KK", "Республика Хакасия", "Khakassia", "Khakasiya", "Khakasiya Republic"),
    (
        "KHM",
        "Ханты-Мансийский автономный округ — Югра",
        "Khanty-Mansi",
        "Khanty-Mansi Autonomous Okrug",
        "Yugra",
    ),
    ("KIR", "Кировская область", "Kirov", "Kirov Oblast"),
    ("KO", "Республика Коми", "Komi", "Komi Republic"),
    ("KR", "Республика Карелия", "Karelia", "Republic of Karelia"),
    ("KOS", "Костромская область", "Kostroma", "Kostroma Oblast"),
    ("KDA", "Краснодарский край", "Krasnodar", "Krasnodar Krai", "Krasnodarskiy Kray"),
    (
        "KYA",
        "Красноярский край",
        "Krasnoyarsk",
        "Krasnoyarsk Krai",
        "Krasnoyarskiy Kray",
    ),
    ("KGN", "Курганская область", "Kurgan", "Kurgan Oblast"),
    ("KRS", "Курская область", "Kursk", "Kursk Oblast"),
    (
        "LEN",
        "Ленинградская область",
        "Leningrad",
        "Leningrad Oblast",
        "Leningradskaya Oblast'",
    ),
    ("LIP", "Липецкая область", "Lipetsk", "Lipetsk Oblast"),
    ("MAG", "Магаданская область", "Magadan", "Magadan Oblast"),
    ("ME", "Республика Марий Эл", "Mari El", "Mariy-El Republic"),
    ("MO", "Республика Мордовия", "Mordovia", "Mordoviya Republic"),
    ("MOS", "Московская область", "Moscow Oblast", "Moskovskaya Oblast"),
    ("MOW", "Москва", "Moscow"),
    ("MUR", "Мурманская область", "Murmansk", "Murmansk Oblast"),
    ("NEN", "Ненецкий автономный округ", "Nenets", "Nenets Autonomous Okrug"),
    (
        "NIZ",
        "Нижегородская область",
        "Nizhny Novgorod",
        "Nizhny Novgorod Oblast",
        "Nizhegorodskaya Oblast'",
    ),
    ("NGR", "Новгородская область", "Novgorod", "Novgorod Oblast"),
    ("NVS", "Новосибирская область", "Novosibirsk", "Novosibirsk Oblast"),
    ("OMS", "Омская область", "Omsk", "Omsk Oblast"),
    ("ORE", "Оренбургская область", "Orenburg", "Orenburg Oblast"),
    ("ORL", "Орловская область", "Orel", "Orel Oblast", "Oryol Oblast"),
    ("PNZ", "Пензенская область", "Penza", "Penza Oblast"),
    ("PER", "Пермский край", "Perm", "Perm Krai", "Perm Kray"),
    (
        "PRI",
        "Приморский край",
        "Primorsky Krai",
        "Primorskiy Kray",
        "Primorskiy (Maritime) Kray",
    ),
    ("PSK", "Псковская область", "Pskov", "Pskov Oblast"),
    ("ROS", "Ростовская область", "Rostov", "Rostov Oblast"),
    ("RYA", "Рязанская область", "Ryazan", "Ryazan Oblast"),
    (
        "SA",
        "Республика Саха (Якутия)",
        "Sakha",
        "Yakutia",
        "Republic of Sakha (Yakutia)",
    ),
    ("SAK", "Сахалинская область", "Sakhalin", "Sakhalin Oblast"),
    ("SAM", "Самарская область", "Samara", "Samara Oblast"),
    ("SAR", "Саратовская область", "Saratov", "Saratov Oblast"),
    (
        "SE",
        "Республика Северная Осетия — Алания",
        "North Ossetia",
        "North Ossetia-Alania",
    ),
    ("SEV", "Севастополь", "Sevastopol"),
    ("SMO", "Смоленская область", "Smolensk", "Smolensk Oblast"),
    ("SPE", "Санкт-Петербург", "Saint Petersburg", "St Petersburg", "St.-Petersburg"),
    ("STA", "Ставропольский край", "Stavropol", "Stavropol Krai", "Stavropol Kray"),
    ("SVE", "Свердловская область", "Sverdlovsk", "Sverdlovsk Oblast"),
    ("TA", "Республика Татарстан", "Tatarstan", "Tatarstan Republic"),
    ("TAM", "Тамбовская область", "Tambov", "Tambov Oblast"),
    ("TOM", "Томская область", "Tomsk", "Tomsk Oblast"),
    ("TUL", "Тульская область", "Tula", "Tula Oblast"),
    ("TVE", "Тверская область", "Tver", "Tver Oblast"),
    ("TY", "Республика Тыва", "Tuva", "Tyva", "Tyva Republic"),
    ("TYU", "Тюменская область", "Tyumen", "Tyumen Oblast", "Tyumen' Oblast"),
    ("UD", "Удмуртская Республика", "Udmurtia", "Udmurtiya Republic"),
    ("ULY", "Ульяновская область", "Ulyanovsk", "Ulyanovsk Oblast"),
    ("VGG", "Волгоградская область", "Volgograd", "Volgograd Oblast"),
    ("VLA", "Владимирская область", "Vladimir", "Vladimir Oblast"),
    ("VLG", "Вологодская область", "Vologda", "Vologda Oblast"),
    ("VOR", "Воронежская область", "Voronezh", "Voronezh Oblast"),
    (
        "YAN",
        "Ямало-Ненецкий автономный округ",
        "Yamalo-Nenets",
        "Yamalo-Nenets Autonomous Okrug",
        "Yamal",
    ),
    ("YAR", "Ярославская область", "Yaroslavl", "Yaroslavl Oblast"),
    (
        "ZAB",
        "Забайкальский край",
        "Zabaykalsky Krai",
        "Zabaykalskiy Kray",
        "Zabaykalskiy (Transbaikal) Kray",
    ),
)

_RUSSIAN_REGIONS_BY_CODE = {
    code: russian_name for code, russian_name, *_aliases in _RUSSIAN_REGION_ROWS
}
_RUSSIAN_REGIONS_BY_ALIAS = {
    _normalize_region_alias(alias): (code, russian_name)
    for code, russian_name, *aliases in _RUSSIAN_REGION_ROWS
    for alias in (russian_name, *aliases)
}


@dataclass(frozen=True)
class GeoIpDatabase:
    path: str
    mtime_ns: int


@dataclass(frozen=True)
class GeoLocation:
    country_code: str
    country_name: str
    region_code: str | None = None
    region_name: str | None = None


_reader_lock = threading.Lock()
_reader = None
_reader_database: GeoIpDatabase | None = None
_warned_database_states: set[str] = set()


def _warn_once(key: str, message: str, *args) -> None:
    if key in _warned_database_states:
        return
    _warned_database_states.add(key)
    log.warning(message, *args)


def configured_database() -> GeoIpDatabase | None:
    """Возвращает ревизию доступной City-MMDB или ``None``.

    mtime входит в ключ lookup-кэша: после атомарного обновления фоновым
    Django-воркером новый файл подхватывается без перезапуска web-процесса.
    """

    raw_path = str(getattr(settings, "GEOIP_CITY_DB_PATH", "") or "").strip()
    if not raw_path:
        return None
    if maxminddb is None:
        _warn_once(
            "missing-package",
            "GEOIP_CITY_DB_PATH задан, но пакет maxminddb не установлен",
        )
        return None

    path = Path(raw_path).expanduser()
    try:
        stat = path.stat()
    except OSError as exc:
        _warn_once(
            f"missing:{path}",
            "GeoIP City database недоступна по пути %s: %s",
            path,
            exc,
        )
        return None
    if not path.is_file():
        _warn_once(f"not-file:{path}", "GeoIP City database не файл: %s", path)
        return None
    database = GeoIpDatabase(str(path.resolve()), stat.st_mtime_ns)
    if _reader_for(database) is None:
        return None
    return database


def _reader_for(database: GeoIpDatabase):
    global _reader, _reader_database

    with _reader_lock:
        if _reader is not None and _reader_database == database:
            return _reader
        if _reader is not None:
            try:
                _reader.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        try:
            _reader = maxminddb.open_database(database.path)
        except (OSError, RuntimeError, ValueError) as exc:
            _reader = None
            _reader_database = None
            _warn_once(
                f"open:{database.path}:{database.mtime_ns}",
                "Не удалось открыть GeoIP City database %s: %s",
                database.path,
                exc,
            )
            return None
        _reader_database = database
        return _reader


def _localized_name(record: dict, fallback: str) -> str:
    names = record.get("names") or {}
    return str(names.get("ru") or names.get("en") or fallback)


def _region_identity(
    country_code: str,
    region: dict | None,
) -> tuple[str | None, str | None]:
    """Извлекает стабильный код и читаемое имя административного региона."""

    if not region:
        return None, None
    raw_code = str(region.get("iso_code") or "").upper() or None
    raw_name = _localized_name(region, raw_code or "").strip() or None
    if country_code != "RU":
        return raw_code, raw_name

    if raw_code and raw_code in _RUSSIAN_REGIONS_BY_CODE:
        return raw_code, _RUSSIAN_REGIONS_BY_CODE[raw_code]
    canonical = _RUSSIAN_REGIONS_BY_ALIAS.get(_normalize_region_alias(raw_name or ""))
    if canonical:
        return canonical
    # DB-IP может добавить новый вариант подписи. Не теряем найденный регион:
    # агрегация использует нормализованное имя, даже когда кода по-прежнему нет.
    return raw_code, raw_name


@lru_cache(maxsize=65536)
def _lookup_cached(
    database_path: str,
    database_mtime_ns: int,
    ip: str,
) -> GeoLocation | None:
    database = GeoIpDatabase(database_path, database_mtime_ns)
    reader = _reader_for(database)
    if reader is None:
        return None
    try:
        response = reader.get(ip)
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning("GeoIP lookup failed for %s: %s", ip, exc)
        return None
    if not response:
        return None

    country = response.get("country") or {}
    if not country.get("iso_code"):
        country = response.get("registered_country") or {}
    country_code = str(country.get("iso_code") or "").upper()
    if not country_code:
        return None

    # Совместимые City-MMDB упорядочивают subdivisions от крупнейшей к точной.
    # Для рейтинга регионов РФ нужен верхний субъект, а не район/город.
    subdivisions = response.get("subdivisions") or []
    region = subdivisions[0] if subdivisions else None
    region_code, region_name = _region_identity(country_code, region)
    return GeoLocation(
        country_code=country_code,
        country_name=_localized_name(country, country_code),
        region_code=region_code,
        region_name=region_name,
    )


def lookup_ip(
    value: str,
    database: GeoIpDatabase | None = None,
) -> GeoLocation | None:
    """Определяет страну и регион публичного IP по локальной MMDB."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if not address.is_global:
        return None

    database = database or configured_database()
    if database is None:
        return None
    return _lookup_cached(database.path, database.mtime_ns, str(address))


def reset_caches() -> None:
    """Сбрасывает process-local состояние (используется тестами)."""

    global _reader, _reader_database
    _lookup_cached.cache_clear()
    _warned_database_states.clear()
    with _reader_lock:
        if _reader is not None:
            try:
                _reader.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        _reader = None
        _reader_database = None
