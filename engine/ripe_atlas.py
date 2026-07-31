"""Замеры доступности точек входа из сетей провайдеров РФ через RIPE Atlas.

Данные для вкладки «Замеры ТСПУ» в админке: по каждому проверяемому адресу
(IP точки входа + SNI, который шлют клиенты) создаётся one-off sslcert-измерение
на зондах Atlas в сетях российских операторов. Зонд, у которого TLS-обмен
состоялся (в результате есть cert/method/alert), «пробился»; зонд без ответа
(только err/таймаут) считается заблокированным — так же трактует результаты
скрипт censorcheck.tlab.pw, откуда взят и список ASN.

Измерения создаются приватными (is_public=False), чтобы IP точек входа не
светились в публичной базе Atlas.
"""

import logging
import math
import time
from datetime import datetime
from datetime import timedelta

import httpx
import orjson

from common.models.db import CensorCheck
from common.models.db import CensorCheckRun
from common.models.db import RipeApiKey

ATLAS_API = "https://atlas.ripe.net/api/v2"

# Сколько зондов запрашивать в географическом режиме (выбор по country:RU,
# группировка по федеральным округам). Atlas раздаёт их по стране, но с сильным
# перекосом в Москву и Питер — где зондов физически больше. ~20 кредитов/зонд.
GEO_PROBE_COUNT = 50

# Опорные города с федеральным округом. Округ зонда определяется по ближайшему
# городу из этого списка (зонды и так стоят в городах, поэтому это точнее, чем
# грубые прямоугольники округов, которые у РФ сильно изрезаны).
FEDERAL_DISTRICT_ANCHORS: list[tuple[float, float, str]] = [
    # Центральный
    (55.75, 37.62, "Центральный"), (51.67, 39.18, "Центральный"),
    (52.60, 39.60, "Центральный"), (54.20, 37.62, "Центральный"),
    (57.63, 39.87, "Центральный"), (51.73, 36.19, "Центральный"),
    (56.86, 35.92, "Центральный"), (50.60, 36.59, "Центральный"),
    # Северо-Западный
    (59.94, 30.31, "Северо-Западный"), (54.71, 20.51, "Северо-Западный"),
    (68.97, 33.08, "Северо-Западный"), (64.54, 40.54, "Северо-Западный"),
    (61.79, 34.35, "Северо-Западный"), (59.22, 39.90, "Северо-Западный"),
    (57.81, 28.33, "Северо-Западный"), (61.67, 50.83, "Северо-Западный"),
    # Южный
    (47.24, 39.70, "Южный"), (45.04, 38.98, "Южный"),
    (48.70, 44.52, "Южный"), (46.35, 48.03, "Южный"),
    (43.60, 39.73, "Южный"), (44.95, 34.10, "Южный"),
    # Северо-Кавказский
    (45.04, 41.97, "Северо-Кавказский"), (42.98, 47.50, "Северо-Кавказский"),
    (43.02, 44.68, "Северо-Кавказский"), (43.32, 45.69, "Северо-Кавказский"),
    # Приволжский
    (55.79, 49.12, "Приволжский"), (56.33, 44.00, "Приволжский"),
    (53.20, 50.15, "Приволжский"), (54.74, 55.97, "Приволжский"),
    (58.01, 56.25, "Приволжский"), (51.53, 46.03, "Приволжский"),
    (51.77, 55.10, "Приволжский"), (56.85, 53.20, "Приволжский"),
    (53.20, 45.00, "Приволжский"), (58.60, 49.66, "Приволжский"),
    (54.32, 48.40, "Приволжский"), (56.14, 47.25, "Приволжский"),
    # Уральский
    (56.84, 60.60, "Уральский"), (55.16, 61.40, "Уральский"),
    (57.15, 65.53, "Уральский"), (55.44, 65.34, "Уральский"),
    (61.25, 73.40, "Уральский"),
    # Сибирский
    (55.03, 82.92, "Сибирский"), (56.01, 92.85, "Сибирский"),
    (54.99, 73.37, "Сибирский"), (53.35, 83.76, "Сибирский"),
    (52.29, 104.28, "Сибирский"), (55.35, 86.08, "Сибирский"),
    (56.49, 84.95, "Сибирский"), (53.72, 91.44, "Сибирский"),
    # Дальневосточный
    (43.12, 131.90, "Дальневосточный"), (48.48, 135.08, "Дальневосточный"),
    (62.03, 129.73, "Дальневосточный"), (51.83, 107.58, "Дальневосточный"),
    (52.03, 113.50, "Дальневосточный"), (50.29, 127.53, "Дальневосточный"),
    (46.96, 142.74, "Дальневосточный"), (53.02, 158.65, "Дальневосточный"),
    (59.56, 150.80, "Дальневосточный"),
]


def federal_district(lat, lon) -> str:
    """Федеральный округ по координатам — по ближайшему опорному городу."""
    if lat is None or lon is None:
        return "неизвестно"
    best_district = "неизвестно"
    best_dist = None
    for a_lat, a_lon, district in FEDERAL_DISTRICT_ANCHORS:
        # Равнопромежуточная аппроксимация: по долготе масштабируем на cos(lat),
        # иначе на севере расстояния по долготе завышаются.
        dx = (lon - a_lon) * math.cos(math.radians((lat + a_lat) / 2))
        dy = lat - a_lat
        dist = dx * dx + dy * dy
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_district = district
    return best_district

# Провайдеры и количество зондов на ASN — как в censorcheck (33 зонда суммарно).
# Стоимость прогона ~20 кредитов на зонд.
ASN_PROBE_SPEC: list[tuple[int, int, str]] = [
    (12389, 3, "Ростелеком"),
    (8402, 5, "Билайн"),
    (25513, 5, "МГТС"),
    (8359, 3, "МТС"),
    (3216, 3, "Билайн"),
    (20485, 2, "ТТК"),
    (25490, 1, "РТК-Юг"),
    (43727, 1, "Мегафон"),
    (12714, 4, "Мегафон"),
    (34757, 2, "Sib Seti"),
    (29124, 2, "Iskratelecom"),
    (12768, 2, "Дом.ру"),
]
ASN_NAMES = {asn: name for asn, _, name in ASN_PROBE_SPEC}
REQUESTED_TOTAL = sum(count for _, count, _ in ASN_PROBE_SPEC)

# Лёгкий набор для частого мониторинга большого числа нод: ~10 зондов по
# крупнейшим операторам (≈200 кредитов вместо 660). Для «заблокирован ли IP
# в РФ» этого достаточно — сигнал бинарный.
LIGHT_ASN_PROBE_SPEC: list[tuple[int, int, str]] = [
    (12389, 2, "Ростелеком"),
    (8402, 2, "Билайн"),
    (8359, 2, "МТС"),
    (12714, 1, "Мегафон"),
    (25513, 1, "МГТС"),
    (12768, 1, "Дом.ру"),
    (20485, 1, "ТТК"),
]
GEO_PROBE_COUNT_LIGHT = 20

# Сколько ждём результаты после создания измерения: зонды отвечают волнами,
# основная масса приходит за 1-2 минуты. После дедлайна прогон закрывается с
# тем, что успело прийти (не ответившие зонды не считаются заблокированными —
# они просто не попадают в выборку).
COLLECT_DEADLINE = timedelta(minutes=4)

RUN_STATUS_PENDING = "pending"
RUN_STATUS_COMPLETE = "complete"
RUN_STATUS_ERROR = "error"

# Пороги алертов (доступность = % пробившихся зондов). Гистерезис, чтобы
# состояние не «дрожало» на границе: в «заблокировано» уходим при 50% и ниже,
# обратно в «доступно» — только с 70%.
#
# Граница блокировки совпадает с красным статусом «критичная блокировка» в UI
# (там оно тоже <= 50%): иначе замер, покрашенный красным, мог не дать алерт.
# Частный случай, на котором это ловилось: 3 из 6 зондов = ровно 50%.
ALERT_BLOCKED_MAX = 50
ALERT_RECOVER_AT = 70
ALERT_STATE_OK = "ok"
ALERT_STATE_BLOCKED = "blocked"


def provider_name(asn) -> str:
    if asn is None:
        return "неизвестно"
    return ASN_NAMES.get(int(asn), f"AS{asn}")


def default_api_key(db_session):
    return (
        db_session.query(RipeApiKey)
        .filter(RipeApiKey.is_default.is_(True))
        .order_by(RipeApiKey.id.asc())
        .first()
    )


def resolve_key_row(db_session, check):
    """Строка ключа для проверки: явный ключ проверки → ключ «по умолчанию»."""
    if check is not None and check.api_key_id:
        row = db_session.get(RipeApiKey, check.api_key_id)
        if row:
            return row
    return default_api_key(db_session)


def resolve_api_key(db_session, check, fallback_key=""):
    """Строка ключа для проверки; None, если ни одного ключа нет."""
    row = resolve_key_row(db_session, check)
    if row:
        return row.api_key
    return fallback_key or None


def resolve_public_flag(db_session, check):
    """Создавать ли публичное измерение для этой проверки (по флагу ключа)."""
    row = resolve_key_row(db_session, check)
    return bool(row.public_measurements) if row else False


# Списки подключённых зондов меняются медленно, а замеров много (десятки нод
# по расписанию), поэтому держим их в памяти процесса недолгое время.
PROBE_LIST_TTL_SECONDS = 900
_probe_list_cache: dict = {}


def fetch_connected_probe_ids(params: dict, count: int) -> list:
    """ID зондов, подключённых ПРЯМО СЕЙЧАС, по фильтру params.

    Нужно потому, что выбор Atlas по asn/country назначает зонды из общего
    списка сети, включая давно неработающие: измеренная выдача была 6 ответов
    из 30 назначенных. При явном списке ID отвечают 28 из 31, причём приходят
    ответы от всех крупных операторов, а не только от мелких.
    """
    cache_key = (tuple(sorted(params.items())), count)
    cached = _probe_list_cache.get(cache_key)
    if cached and time.time() - cached[0] < PROBE_LIST_TTL_SECONDS:
        return list(cached[1])

    query = dict(params)
    query.update({"status": 1, "fields": "id", "page_size": count})
    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(f"{ATLAS_API}/probes/", params=query)
        if response.status_code != 200:
            return []
        payload = orjson.loads(response.content)
    except Exception:
        return []
    probe_ids = [
        p["id"] for p in payload.get("results", []) if isinstance(p, dict) and p.get("id")
    ]
    if probe_ids:
        _probe_list_cache[cache_key] = (time.time(), list(probe_ids))
    return probe_ids


def _explicit_probes_spec(probe_ids: list) -> list:
    return [
        {
            "requested": len(probe_ids),
            "type": "probes",
            "value": ",".join(str(pid) for pid in probe_ids),
        }
    ]


def _probes_spec(geo: bool, light: bool = False) -> list:
    """Выбор зондов: гео-режим — по всей РФ; иначе — по сетям операторов.

    Зонды выбираются ЯВНО (по id тех, что подключены сейчас) — выбор силами
    Atlas по asn/country назначает много мёртвых зондов, и до цели доходит
    лишь пятая часть. Если список получить не удалось, откатываемся на
    прежний выбор по asn/country, чтобы замер всё равно состоялся.

    light=True — уменьшенный набор для дешёвого частого мониторинга.
    """
    if geo:
        wanted = GEO_PROBE_COUNT_LIGHT if light else GEO_PROBE_COUNT
        probe_ids = fetch_connected_probe_ids({"country_code": "RU"}, wanted)
        if probe_ids:
            return _explicit_probes_spec(probe_ids)
        return [
            {
                "requested": wanted,
                "type": "country",
                "value": "RU",
                "tags": {"include": ["system-ipv4-works"]},
            }
        ]

    spec = LIGHT_ASN_PROBE_SPEC if light else ASN_PROBE_SPEC
    probe_ids = []
    for asn, count, _ in spec:
        probe_ids.extend(fetch_connected_probe_ids({"asn_v4": asn}, count))
    if probe_ids:
        return _explicit_probes_spec(probe_ids)

    return [
        {
            "requested": count,
            "type": "asn",
            "value": asn,
            "tags": {"include": ["system-ipv4-works"]},
        }
        for asn, count, _ in spec
    ]


def expected_probe_total(geo: bool, light: bool = False) -> int:
    if geo:
        return GEO_PROBE_COUNT_LIGHT if light else GEO_PROBE_COUNT
    spec = LIGHT_ASN_PROBE_SPEC if light else ASN_PROBE_SPEC
    return sum(count for _, count, _ in spec)


def create_measurement(
    api_key: str,
    target_ip: str,
    sni: str,
    port: int,
    is_public: bool = False,
    geo: bool = False,
    light: bool = False,
) -> tuple:
    """Создаёт one-off sslcert-измерение; возвращает (msm_id, error_message).

    is_public=True — публичное измерение (результаты читаются без спец-права,
    нужно для ключей без права читать приватные результаты). По умолчанию
    приватное, чтобы IP точек входа не светились в публичной базе Atlas.

    geo=True — зонды выбираются по всей РФ (country:RU) для разбивки по
    федеральным округам; иначе — по сетям операторов.
    """
    payload = {
        "definitions": [
            {
                "target": target_ip,
                # Нейтральное описание: метаданные измерения не должны выдавать
                # назначение адреса даже внутри аккаунта.
                "description": "sslcert check",
                "type": "sslcert",
                "port": port,
                "hostname": sni,
                "af": 4,
                "is_public": is_public,
            }
        ],
        "probes": _probes_spec(geo, light),
        "is_oneoff": True,
    }
    try:
        # Используем обычный Client вместо AsyncClient
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f"{ATLAS_API}/measurements/",
                headers={"Authorization": f"Key {api_key}"},
                json=payload,
            )
        body = orjson.loads(response.content)
    except Exception:
        logging.exception("censor check: atlas create measurement failed")
        return None, "RIPE Atlas недоступен, измерение не создано"

    if response.status_code == 201 and body.get("measurements"):
        return body["measurements"][0], None

    # Наиболее частые причины: кончились кредиты, невалидный ключ. Достаём
    # человекочитаемый текст из вложенной структуры ошибки Atlas.
    detail = ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        errors = error.get("errors")
        if isinstance(errors, list) and errors:
            detail = "; ".join(
                str(item.get("detail", "")) for item in errors if isinstance(item, dict)
            )
        if not detail:
            detail = str(error.get("detail", ""))
    logging.warning(
        "censor check: atlas rejected measurement (%s): %s",
        response.status_code,
        detail or body,
    )
    return None, f"Atlas отклонил измерение: {detail or response.status_code}"


class ResultsForbidden(Exception):
    """Ключ не имеет права читать приватные результаты этого измерения."""


def fetch_results(api_key: str, msm_id: int):
    """Результаты измерения (список по зондам); None при ошибке запроса.

    Бросает ResultsForbidden при 403 — это значит, что ключ создал приватное
    измерение, но не имеет права «Get non-public results». Такой ключ должен
    создавать публичные измерения (флаг public_measurements).
    """
    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{ATLAS_API}/measurements/{msm_id}/results/",
                headers={"Authorization": f"Key {api_key}"},
            )
        if response.status_code == 403:
            raise ResultsForbidden()
        if response.status_code != 200:
            return None
        payload = orjson.loads(response.content)
    except ResultsForbidden:
        raise
    except Exception:
        return None
    return payload if isinstance(payload, list) else None


def fetch_scheduled_probes(api_key: str, msm_id: int):
    """Сколько зондов Atlas назначил на измерение; None при ошибке.

    Отвечают обычно не все назначенные (часть зондов молчит), поэтому это число
    нужно, чтобы в интерфейсе было видно представительность выборки.
    """
    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{ATLAS_API}/measurements/{msm_id}/",
                headers={"Authorization": f"Key {api_key}"},
                params={"fields": "probes_scheduled"},
            )
        if response.status_code != 200:
            return None
        payload = orjson.loads(response.content)
    except Exception:
        return None
    scheduled = payload.get("probes_scheduled") if isinstance(payload, dict) else None
    return scheduled or None


def fetch_probe_geo(prb_ids: list[int]) -> dict:
    """ASN и координаты по id зондов: в результатах sslcert их нет.

    Возвращает {prb_id: {"asn": asn, "lat": lat, "lon": lon}}.
    """
    if not prb_ids:
        return {}
    ids = ",".join(str(prb_id) for prb_id in sorted(set(prb_ids)))
    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{ATLAS_API}/probes/",
                params={"id__in": ids, "fields": "id,asn_v4,geometry"},
            )
        if response.status_code != 200:
            return {}
        payload = orjson.loads(response.content)
    except Exception:
        return {}
    info = {}
    for probe in payload.get("results", []):
        if not isinstance(probe, dict) or not probe.get("id"):
            continue
        geometry = probe.get("geometry") or {}
        coords = geometry.get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        info[probe["id"]] = {"asn": probe.get("asn_v4"), "lat": lat, "lon": lon}
    return info


def summarize_results(results: list, geo: bool = False) -> dict:
    """Сводка по зондам: пробился/заблокирован + разбивка по группам.

    geo=True — группировка по федеральным округам, иначе по провайдерам.
    В карточку каждого зонда кладутся и провайдер, и округ.
    """
    probe_info = fetch_probe_geo(
        [row.get("prb_id") for row in results if row.get("prb_id")]
    )
    probes = []
    blocked_by_group: dict[str, int] = {}
    ok_count = 0
    for row in results:
        # TLS-обмен состоялся (пусть даже alert'ом) — значит пакеты дошли.
        ok = any(key in row for key in ("cert", "method", "alert"))
        prb_id = row.get("prb_id")
        info = probe_info.get(prb_id) or {}
        asn = info.get("asn")
        provider = provider_name(asn)
        district = federal_district(info.get("lat"), info.get("lon"))
        group = district if geo else provider
        if ok:
            ok_count += 1
        else:
            blocked_by_group[group] = blocked_by_group.get(group, 0) + 1
        probes.append(
            {
                "prb_id": prb_id,
                "asn": asn,
                "provider": provider,
                "district": district,
                "ok": ok,
                "err": str(row.get("err", ""))[:200] if not ok else "",
                "rt_ms": row.get("rt"),
            }
        )
    probes.sort(key=lambda probe: (probe["ok"], probe["district"] if geo else probe["provider"]))
    return {
        "probes": probes,
        "total": len(probes),
        "ok": ok_count,
        "blocked": len(probes) - ok_count,
        # Ключ исторически называется blocked_by_provider; в гео-режиме здесь
        # округа. Фронт рендерит его как «Блокируют: <label> N» независимо.
        "blocked_by_provider": blocked_by_group,
    }


def run_availability_percent(run: CensorCheckRun) -> int:
    if not run.total_probes:
        return 0
    return round(run.ok_probes * 100 / run.total_probes)


def _blocked_summary_text(run: CensorCheckRun) -> str:
    groups = run.blocked_asns or {}
    if not groups:
        return "—"
    return ", ".join(f"{name} {count}" for name, count in groups.items())


def maybe_send_alert(db_session, check: CensorCheck, run: CensorCheckRun) -> None:
    """Шлёт TG-алерт при СМЕНЕ состояния ноды (доступна↔заблокирована).

    Только для завершённых прогонов с данными. Спама нет: пока состояние не
    меняется, повторных алертов не будет (last_alert_state).
    """
    # Импорт внутри функции — модуль notify тянет django.conf.settings, а
    # ripe_atlas может импортироваться в контексте без готовых настроек.
    from engine import notify

    if not check.alerts_enabled or run.status != RUN_STATUS_COMPLETE:
        return

    pct = run_availability_percent(run)
    prev = check.last_alert_state
    if pct <= ALERT_BLOCKED_MAX:
        new_state = ALERT_STATE_BLOCKED
    elif pct >= ALERT_RECOVER_AT:
        new_state = ALERT_STATE_OK
    else:
        # Зона гистерезиса — состояние не меняем.
        return

    if new_state == prev:
        logging.info(
            "censor alert: check %s состояние не изменилось (%s, %s%%), алерт не нужен",
            check.id,
            new_state,
            pct,
        )
        return

    label = check.name or check.sni
    port = f":{check.port}" if check.port != 443 else ""
    if new_state == ALERT_STATE_BLOCKED:
        text = (
            f"🚨 <b>Нода недоступна из РФ</b>\n"
            f"{label}\n"
            f"IP: <code>{check.target_ip}{port}</code> · SNI: <code>{check.sni}</code>\n"
            f"Доступность: <b>{pct}%</b> "
            f"({run.ok_probes}/{run.total_probes} зондов)\n"
            f"Не проходят: {_blocked_summary_text(run)}"
        )
    elif prev == ALERT_STATE_BLOCKED:
        # «Восстановилась» шлём только если раньше был бан.
        text = (
            f"✅ <b>Нода снова доступна из РФ</b>\n"
            f"{label}\n"
            f"IP: <code>{check.target_ip}{port}</code>\n"
            f"Доступность: <b>{pct}%</b> "
            f"({run.ok_probes}/{run.total_probes} зондов)"
        )
    else:
        # Первый успешный замер: уведомлять не о чем, просто запоминаем «ok».
        logging.info(
            "censor alert: check %s initial state -> %s (%s%%), нечего слать",
            check.id,
            new_state,
            pct,
        )
        check.last_alert_state = new_state
        db_session.commit()
        return

    delivered = notify.send_admin_telegram_alert(text)
    if delivered:
        # Состояние двигаем ТОЛЬКО после успешной доставки: иначе при пустом
        # TELEGRAM_ALERT_CHAT_ID или неотправленном /start авария была бы
        # помечена как «уведомлено» и алерт по ней потерялся бы навсегда.
        check.last_alert_state = new_state
        db_session.commit()
        logging.info(
            "censor alert: check %s %s -> %s (%s%%), отправлено",
            check.id,
            prev,
            new_state,
            pct,
        )
    else:
        logging.warning(
            "censor alert: check %s %s -> %s (%s%%) НЕ доставлено — проверьте "
            "TELEGRAM_ALERT_CHAT_ID и что админ нажал /start боту; повторим на "
            "следующем прогоне",
            check.id,
            prev,
            new_state,
            pct,
        )


def start_run(
    db_session,
    check: CensorCheck,
    api_key: str,
    is_public: bool = False,
    geo: bool = False,
    light: bool = False,
) -> CensorCheckRun:
    """Создаёт измерение и запись прогона (при ошибке — прогон со статусом error)."""
    msm_id, error = create_measurement(
        api_key, check.target_ip, check.sni, check.port, is_public, geo, light
    )
    run = CensorCheckRun(
        check_id=check.id,
        msm_id=msm_id,
        status=RUN_STATUS_PENDING if msm_id else RUN_STATUS_ERROR,
        error_message=error,
    )
    check.last_started_at = datetime.utcnow()
    db_session.add(run)
    db_session.commit()
    return run


def finalize_run(db_session, run: CensorCheckRun, api_key: str) -> bool:
    """Пробует закрыть pending-прогон; True, если статус изменился.

    Прогон закрывается, когда ответили все запрошенные зонды либо истёк
    COLLECT_DEADLINE. До того — обновляет промежуточные цифры, чтобы в UI
    было видно, как собираются ответы.
    """
    if run.status != RUN_STATUS_PENDING or not run.msm_id:
        return False

    try:
        results = fetch_results(api_key, run.msm_id)
    except ResultsForbidden:
        # Ключ создал приватное измерение, но не может его прочитать. Закрываем
        # сразу — ждать бессмысленно.
        run.status = RUN_STATUS_ERROR
        run.error_message = (
            "У ключа нет права читать приватные результаты. Включите «публичные "
            "измерения» у этого ключа (для своего ключа добавьте право "
            "Get non-public results)."
        )
        run.completed_at = datetime.utcnow()
        db_session.commit()
        return True
    deadline_passed = datetime.utcnow() - run.created_at >= COLLECT_DEADLINE
    if results is None:
        if deadline_passed:
            run.status = RUN_STATUS_ERROR
            run.error_message = "Не удалось получить результаты измерения"
            run.completed_at = datetime.utcnow()
            db_session.commit()
            return True
        return False

    check = db_session.get(CensorCheck, run.check_id)
    geo = bool(check.geo_mode) if check else False
    light = bool(check.light_mode) if check else False

    summary = summarize_results(results, geo=geo)
    run.total_probes = summary["total"]
    run.ok_probes = summary["ok"]
    run.blocked_probes = summary["blocked"]
    run.results = summary["probes"]
    run.blocked_asns = summary["blocked_by_provider"]

    if summary["total"] >= expected_probe_total(geo, light) or deadline_passed:
        # Один запрос на завершение прогона: сколько зондов Atlas реально назначил.
        run.scheduled_probes = fetch_scheduled_probes(api_key, run.msm_id)
        if summary["total"] == 0:
            run.status = RUN_STATUS_ERROR
            run.error_message = "Ни один зонд не ответил"
        else:
            run.status = RUN_STATUS_COMPLETE
        run.completed_at = datetime.utcnow()
        db_session.commit()
        if check:
            maybe_send_alert(db_session, check, run)
        return True

    db_session.commit()
    return False


def finalize_pending_runs(db_session, fallback_key: str = "") -> None:
    runs = (
        db_session.query(CensorCheckRun)
        .filter(CensorCheckRun.status == RUN_STATUS_PENDING)
        .all()
    )
    for run in runs:
        check = db_session.get(CensorCheck, run.check_id)
        api_key = resolve_api_key(db_session, check, fallback_key)
        if not api_key:
            continue
        finalize_run(db_session, run, api_key)


def schedule_due_checks(db_session, fallback_key: str = "") -> None:
    """Запускает прогоны просроченных по расписанию проверок.

    Клейм через UPDATE с условием по last_started_at: при параллельном вызове
    из нескольких воркеров gunicorn/крона прогон создаст только тот, чей
    UPDATE реально изменил строку.
    """
    now = datetime.utcnow()
    checks = (
        db_session.query(CensorCheck)
        .filter(CensorCheck.is_enabled.is_(True))
        .filter(CensorCheck.interval_minutes.isnot(None))
        .all()
    )
    for check in checks:
        api_key = resolve_api_key(db_session, check, fallback_key)
        if not api_key:
            # Нет ни ключа проверки, ни ключа по умолчанию — пропускаем, чтобы
            # не крутить last_started_at на проверке, которую нечем запустить.
            continue
        cutoff = now - timedelta(minutes=check.interval_minutes)
        if check.last_started_at is not None and check.last_started_at > cutoff:
            continue
        claimed = (
            db_session.query(CensorCheck)
            .filter(CensorCheck.id == check.id)
            .filter(
                (CensorCheck.last_started_at.is_(None))
                | (CensorCheck.last_started_at <= cutoff)
            )
            .update({CensorCheck.last_started_at: now})
        )
        db_session.commit()
        if claimed:
            is_public = resolve_public_flag(db_session, check)
            start_run(
                db_session,
                check,
                api_key,
                is_public,
                bool(check.geo_mode),
                bool(check.light_mode),
            )
