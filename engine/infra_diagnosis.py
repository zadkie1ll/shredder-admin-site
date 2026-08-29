"""Классификация блокировок ТСПУ: раздельные вердикты по адресам и именам.

ТСПУ фильтрует не только IP-адреса, но и SNI, причём независимо: в одном
инциденте могут одновременно оказаться под фильтром и адрес сервера, и имя,
которое шлют клиенты. Прежний механизм измерял пару «адрес + имя», а вердикт
выносил про адрес — и на бане имени запускал бессмысленную (и рискованную)
ротацию адресов.

Схема проб построена на том, что доказанно живой адрес превращает отказ
любого имени на нём в доказательство бана этого имени:

  K(ip)   — проба адреса контрольным ПОСТОРОННИМ именем. Имя заведомо вне
            фильтра, значит отказ относится к адресу.
  T(ip,s) — проба имени на адресе, признанном живым. Адрес вне подозрений,
            значит отказ относится к имени.
  O(ip)   — проба контрольным именем с зондов вне РФ: отделяет мёртвый
            маршрут и аварию ноды от фильтрации.

Здесь только чистые функции над готовыми результатами проб: ни сети, ни БД,
ни побочных эффектов. Запуск измерений и запись в БД — в infra/infra_worker.

Каждый вердикт сопровождается evidence — журналом проверок с готовыми
человекочитаемыми объяснениями: какие адреса перепробованы, что сработало,
что нет и по какому признаку сделан вывод. Он идёт в алерты и в карточку.
"""

from engine.ripe_atlas import STAGE_TCP_FAIL
from engine.ripe_atlas import STAGE_TCP_REFUSED
from engine.ripe_atlas import STAGE_TLS_FAIL

# Доступность (процент пробившихся зондов), ниже которой проба считается
# провалившейся. Совпадает с порогом ripe_atlas.ALERT_BLOCKED_MAX: ниже
# половины зондов — это уже не флуктуация выборки.
PROBE_FAIL_MAX_PCT = 50

# Вердикты по адресу
IP_OK = "ok"
IP_BLOCKED = "blocked"
IP_UNKNOWN = "unknown"

# Вердикты по имени
SNI_OK = "ok"
SNI_BLOCKED = "blocked"
SNI_UNKNOWN = "unknown"

# Уверенность вердикта
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# Минимум ответивших зондов, ниже которого вердикт не считается надёжным:
# на выборке в пару зондов «половина не прошла» ничего не значит.
MIN_PROBES_HIGH_CONFIDENCE = 5


def probe_passed(probe: dict) -> bool:
    """Проба засчитана, если пробилось больше порога зондов."""
    if not (probe or {}).get("total_probes"):
        return False
    return availability_pct(probe) > PROBE_FAIL_MAX_PCT


def availability_pct(probe: dict) -> int:
    total = (probe or {}).get("total_probes") or 0
    if not total:
        return 0
    return round(((probe or {}).get("ok_probes") or 0) * 100 / total)


def dominant_stage(probe: dict) -> str | None:
    """Преобладающая стадия обрыва среди непробившихся зондов."""
    stages = dict((probe or {}).get("stages") or {})
    stages.pop("tls_ok", None)
    if not stages:
        return None
    return max(stages.items(), key=lambda item: item[1])[0]


def _stage_note(probe: dict) -> str:
    """Человекочитаемое пояснение к стадии обрыва."""
    stage = dominant_stage(probe)
    if stage == STAGE_TCP_FAIL:
        return (
            "TCP не устанавливается — рукопожатие не начиналось, "
            "имя в эфир не уходило"
        )
    if stage == STAGE_TLS_FAIL:
        return "TCP проходит, обрыв на ClientHello"
    if stage == STAGE_TCP_REFUSED:
        return "соединение отвергнуто — за портом никого нет"
    return ""


def _probe_label(probe: dict) -> str:
    return (
        f"{availability_pct(probe)}% зондов "
        f"({(probe or {}).get('ok_probes') or 0}"
        f"/{(probe or {}).get('total_probes') or 0})"
    )


def is_hard_block(probe: dict) -> bool:
    """Жёсткая форма: TCP не устанавливается.

    Доказывает бан адреса без сравнения проб — ClientHello не отправлялся,
    значит фильтрация по имени исключена. Быстрый путь классификатора.
    """
    return not probe_passed(probe) and dominant_stage(probe) == STAGE_TCP_FAIL


def control_name_burned(ip_probes: dict) -> bool:
    """Выгорело ли само контрольное имя.

    Если контроль не проходит сразу на ВСЕХ адресах парка, объяснение
    «все наши адреса разом забанили» менее правдоподобно, чем «контрольное
    имя попало под фильтр». Без этой проверки выгорание контроля выглядело
    бы как тотальный бан и запустило бы бессмысленные замены.
    """
    probes = [probe for probe in ip_probes.values() if probe]
    if len(probes) < 2:
        return False
    return all(not probe_passed(probe) for probe in probes)


def classify(
    ip_probes: dict,
    sni_probes: dict | None = None,
    outside_probes: dict | None = None,
    node_healthy: bool = True,
    control_name: str = "",
) -> dict:
    """Раздельные вердикты по адресам и именам.

    ip_probes:      {ip: проба контрольным именем с зондов РФ}
    sni_probes:     {(ip, sni): проба этим именем на этом адресе}
    outside_probes: {ip: проба контрольным именем с зондов вне РФ}

    Проба — словарь {"ok_probes", "total_probes", "stages"}; None означает,
    что проба не проводилась.

    Возвращает {"ips", "snis", "blocked_ips", "blocked_snis", "confidence",
    "evidence", "control_burned", "actionable"}. actionable=False означает,
    что автоматические действия запрещены: причина — в evidence.
    """
    sni_probes = sni_probes or {}
    outside_probes = outside_probes or {}
    evidence: list[dict] = []
    ips: dict[str, str] = {}
    snis: dict[str, str] = {}

    if not node_healthy:
        evidence.append(
            {
                "step": "node_health",
                "result": "fail",
                "text": (
                    "Нода нездорова (процесс мёртв или исчерпаны лимиты) — "
                    "внешние пробы не запускались, изменения DNS запрещены"
                ),
            }
        )
        return {
            "ips": {}, "snis": {}, "blocked_ips": [], "blocked_snis": [],
            "confidence": CONFIDENCE_HIGH, "evidence": evidence,
            "control_burned": False, "actionable": False,
        }

    burned = control_name_burned(ip_probes)
    if burned:
        evidence.append(
            {
                "step": "control_check",
                "result": "fail",
                "text": (
                    f"Контрольное имя {control_name or '—'} не проходит ни на "
                    f"одном из {len(ip_probes)} адресов. Одновременный бан всех "
                    "адресов менее правдоподобен, чем выгорание самого "
                    "контрольного имени — вердикты не выносятся, нужно "
                    "следующее имя из списка"
                ),
            }
        )
        return {
            "ips": {}, "snis": {}, "blocked_ips": [], "blocked_snis": [],
            "confidence": CONFIDENCE_LOW, "evidence": evidence,
            "control_burned": True, "actionable": False,
        }

    # --- статус адресов -----------------------------------------------------
    weak_sample = False
    for ip, probe in ip_probes.items():
        if probe is None:
            ips[ip] = IP_UNKNOWN
            evidence.append(
                {
                    "step": "ip_probe", "ip": ip, "result": "skipped",
                    "text": f"Адрес {ip}: не проверялся",
                }
            )
            continue
        total = probe.get("total_probes") or 0
        if total and total < MIN_PROBES_HIGH_CONFIDENCE:
            weak_sample = True
        if probe_passed(probe):
            ips[ip] = IP_OK
            evidence.append(
                {
                    "step": "ip_probe", "ip": ip, "sni": control_name,
                    "result": "pass",
                    "text": (
                        f"Адрес {ip}: контрольное имя {control_name} проходит "
                        f"({_probe_label(probe)}) — адрес жив"
                    ),
                }
            )
            continue

        outside = outside_probes.get(ip)
        if outside is not None and not probe_passed(outside):
            # Снаружи тоже не работает — это не фильтрация, а маршрут/нода
            ips[ip] = IP_UNKNOWN
            evidence.append(
                {
                    "step": "ip_probe", "ip": ip, "sni": control_name,
                    "result": "inconclusive",
                    "text": (
                        f"Адрес {ip}: не отвечает ни из РФ "
                        f"({_probe_label(probe)}), ни извне "
                        f"({_probe_label(outside)}) — это не фильтрация, "
                        "а недоступность маршрута или самой ноды"
                    ),
                }
            )
            continue

        ips[ip] = IP_BLOCKED
        note = _stage_note(probe)
        outside_note = (
            f", при этом извне отвечает ({_probe_label(outside)})"
            if outside is not None and probe_passed(outside)
            else ""
        )
        hard = " Жёсткая форма: вывод не требует проб по именам." if is_hard_block(probe) else ""
        evidence.append(
            {
                "step": "ip_probe", "ip": ip, "sni": control_name,
                "result": "fail", "hard": is_hard_block(probe),
                "text": (
                    f"Адрес {ip}: контрольное имя {control_name} не проходит "
                    f"({_probe_label(probe)}"
                    + (f", {note}" if note else "")
                    + f"){outside_note} — адрес заблокирован.{hard}"
                ),
            }
        )

    # --- статус имён (только на живых адресах) ------------------------------
    for (ip, sni), probe in sni_probes.items():
        if ips.get(ip) != IP_OK:
            # На мёртвом адресе падает всё; вывод об имени был бы ложным
            evidence.append(
                {
                    "step": "sni_probe", "ip": ip, "sni": sni,
                    "result": "skipped",
                    "text": (
                        f"Имя {sni}: не проверялось на {ip} — адрес не признан "
                        "живым, отказ ничего не доказал бы"
                    ),
                }
            )
            continue
        if probe is None:
            continue
        total = probe.get("total_probes") or 0
        if total and total < MIN_PROBES_HIGH_CONFIDENCE:
            weak_sample = True
        if probe_passed(probe):
            # Одного успеха достаточно: имя работает хотя бы где-то
            if snis.get(sni) != SNI_BLOCKED:
                snis[sni] = SNI_OK
            evidence.append(
                {
                    "step": "sni_probe", "ip": ip, "sni": sni, "result": "pass",
                    "text": (
                        f"Имя {sni} на живом адресе {ip}: проходит "
                        f"({_probe_label(probe)}) — имя чистое"
                    ),
                }
            )
            continue
        snis[sni] = SNI_BLOCKED
        note = _stage_note(probe)
        evidence.append(
            {
                "step": "sni_probe", "ip": ip, "sni": sni, "result": "fail",
                "text": (
                    f"Имя {sni} на живом адресе {ip}: не проходит "
                    f"({_probe_label(probe)}"
                    + (f", {note}" if note else "")
                    + ") — адрес доказанно жив, значит заблокировано имя"
                ),
            }
        )

    blocked_ips = [ip for ip, state in ips.items() if state == IP_BLOCKED]
    blocked_snis = [sni for sni, state in snis.items() if state == SNI_BLOCKED]

    if weak_sample:
        confidence = CONFIDENCE_MEDIUM
        evidence.append(
            {
                "step": "confidence", "result": "warn",
                "text": (
                    "Ответило мало зондов — уверенность понижена, "
                    "автоматические действия запрещены"
                ),
            }
        )
    else:
        confidence = CONFIDENCE_HIGH

    return {
        "ips": ips,
        "snis": snis,
        "blocked_ips": blocked_ips,
        "blocked_snis": blocked_snis,
        "confidence": confidence,
        "evidence": evidence,
        "control_burned": False,
        "actionable": confidence == CONFIDENCE_HIGH,
    }


def domains_safe_to_repoint(domains: list, blocked_snis: list) -> tuple:
    """Делит домены на те, где замена адреса осмысленна, и заблокированные.

    Публиковать чистый адрес под фильтруемым именем нельзя: имя всё равно не
    работает, а связывать новый адрес с ним незачем. Возвращает
    (можно_менять, нельзя_менять).
    """
    blocked = set(blocked_snis or [])
    safe = [d for d in domains if (d.get("sni") or d.get("domain")) not in blocked]
    unsafe = [d for d in domains if (d.get("sni") or d.get("domain")) in blocked]
    return safe, unsafe


def evidence_text(evidence: list) -> str:
    """Журнал проверок сплошным текстом — для алертов и карточки."""
    lines = []
    for item in evidence or []:
        mark = {
            "pass": "✓", "fail": "✗", "skipped": "–",
            "inconclusive": "?", "warn": "!",
        }.get(item.get("result"), "·")
        lines.append(f"{mark} {item.get('text', '')}")
    return "\n".join(lines)
