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
from datetime import datetime
from datetime import timedelta

import httpx
import orjson

from common.models.db import CensorCheck
from common.models.db import CensorCheckRun
from common.models.db import RipeApiKey

ATLAS_API = "https://atlas.ripe.net/api/v2"

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

# Сколько ждём результаты после создания измерения: зонды отвечают волнами,
# основная масса приходит за 1-2 минуты. После дедлайна прогон закрывается с
# тем, что успело прийти (не ответившие зонды не считаются заблокированными —
# они просто не попадают в выборку).
COLLECT_DEADLINE = timedelta(minutes=4)

RUN_STATUS_PENDING = "pending"
RUN_STATUS_COMPLETE = "complete"
RUN_STATUS_ERROR = "error"


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


def resolve_api_key(db_session, check, fallback_key=""):
    """Ключ для проверки: явный ключ проверки → ключ «по умолчанию» → env.

    Возвращает строку ключа или None, если ни одного ключа нет.
    """
    if check is not None and check.api_key_id:
        row = db_session.get(RipeApiKey, check.api_key_id)
        if row:
            return row.api_key
    default = default_api_key(db_session)
    if default:
        return default.api_key
    return fallback_key or None


def create_measurement(api_key: str, target_ip: str, sni: str, port: int) -> tuple:
    """Создаёт one-off sslcert-измерение; возвращает (msm_id, error_message)."""
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
                "is_public": False,
            }
        ],
        "probes": [
            {
                "requested": count,
                "type": "asn",
                "value": asn,
                "tags": {"include": ["system-ipv4-works"]},
            }
            for asn, count, _ in ASN_PROBE_SPEC
        ],
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


def fetch_results(api_key: str, msm_id: int):
    """Результаты измерения (список по зондам); None при ошибке запроса."""
    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{ATLAS_API}/measurements/{msm_id}/results/",
                headers={"Authorization": f"Key {api_key}"},
            )
        if response.status_code != 200:
            return None
        payload = orjson.loads(response.content)
    except Exception:
        return None
    return payload if isinstance(payload, list) else None


def fetch_probe_asns(prb_ids: list[int]) -> dict:
    """ASN v4 по id зондов: в результатах sslcert самого ASN нет."""
    if not prb_ids:
        return {}
    ids = ",".join(str(prb_id) for prb_id in sorted(set(prb_ids)))
    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{ATLAS_API}/probes/",
                params={"id__in": ids, "fields": "id,asn_v4"},
            )
        if response.status_code != 200:
            return {}
        payload = orjson.loads(response.content)
    except Exception:
        return {}
    return {
        probe["id"]: probe.get("asn_v4")
        for probe in payload.get("results", [])
        if isinstance(probe, dict) and probe.get("id")
    }


def summarize_results(results: list) -> dict:
    """Сводка по зондам: пробился/заблокирован + разбивка по провайдерам."""
    probe_asns = fetch_probe_asns(
        [row.get("prb_id") for row in results if row.get("prb_id")]
    )
    probes = []
    blocked_by_provider: dict[str, int] = {}
    ok_count = 0
    for row in results:
        # TLS-обмен состоялся (пусть даже alert'ом) — значит пакеты дошли.
        ok = any(key in row for key in ("cert", "method", "alert"))
        prb_id = row.get("prb_id")
        asn = probe_asns.get(prb_id)
        provider = provider_name(asn)
        if ok:
            ok_count += 1
        else:
            blocked_by_provider[provider] = blocked_by_provider.get(provider, 0) + 1
        probes.append(
            {
                "prb_id": prb_id,
                "asn": asn,
                "provider": provider,
                "ok": ok,
                "err": str(row.get("err", ""))[:200] if not ok else "",
                "rt_ms": row.get("rt"),
            }
        )
    probes.sort(key=lambda probe: (probe["ok"], probe["provider"]))
    return {
        "probes": probes,
        "total": len(probes),
        "ok": ok_count,
        "blocked": len(probes) - ok_count,
        "blocked_by_provider": blocked_by_provider,
    }


def start_run(db_session, check: CensorCheck, api_key: str) -> CensorCheckRun:
    """Создаёт измерение и запись прогона (при ошибке — прогон со статусом error)."""
    msm_id, error = create_measurement(
        api_key, check.target_ip, check.sni, check.port
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

    results = fetch_results(api_key, run.msm_id)
    deadline_passed = datetime.utcnow() - run.created_at >= COLLECT_DEADLINE
    if results is None:
        if deadline_passed:
            run.status = RUN_STATUS_ERROR
            run.error_message = "Не удалось получить результаты измерения"
            run.completed_at = datetime.utcnow()
            db_session.commit()
            return True
        return False

    summary = summarize_results(results)
    run.total_probes = summary["total"]
    run.ok_probes = summary["ok"]
    run.blocked_probes = summary["blocked"]
    run.results = summary["probes"]
    run.blocked_asns = summary["blocked_by_provider"]

    if summary["total"] >= REQUESTED_TOTAL or deadline_passed:
        if summary["total"] == 0:
            run.status = RUN_STATUS_ERROR
            run.error_message = "Ни один зонд не ответил"
        else:
            run.status = RUN_STATUS_COMPLETE
        run.completed_at = datetime.utcnow()
        db_session.commit()
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
            start_run(db_session, check, api_key)
