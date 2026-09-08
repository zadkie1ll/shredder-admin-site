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
# Бан пары «адрес + имя»: имя падает на одном живом адресе, но проходит на
# другом. Это не бан имени (имя рабочее) и не бан адреса (контроль на нём
# проходит) — третий тип правила ТСПУ, замеченный 2026-09-02: monkora и
# space падали на .198 и проходили на .143 из той же /24, а через два часа
# бан пары снялся сам. Прежний классификатор называл это баном имени.
SNI_PAIR_BLOCKED = "pair_blocked"

# Уверенность вердикта
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# Минимум ответивших зондов, ниже которого вердикт не считается надёжным:
# на выборке в пару зондов «половина не прошла» ничего не значит.
MIN_PROBES_HIGH_CONFIDENCE = 5

# Состояние контрольного имени
CONTROL_BURN_PROVEN = "proven"
CONTROL_BURN_SUSPECTED = "suspected"


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


def sni_passes_by_ip(sni_probes: dict) -> dict:
    """{адрес: [имена, прошедшие на нём]} по сырым пробам имён.

    Считается ДО вердиктов по адресам и намеренно не смотрит на них: проход
    клиентского имени на адресе сам по себе доказывает, что адрес принимает
    соединения, независимо от того, что показал контроль.
    """
    result: dict[str, list[str]] = {}
    for (ip, sni), probe in (sni_probes or {}).items():
        if probe is not None and probe_passed(probe):
            result.setdefault(ip, []).append(sni)
    return result


def sni_tested_by_ip(sni_probes: dict) -> dict:
    """{адрес: [имена, которые на нём вообще проверялись]}."""
    result: dict[str, list[str]] = {}
    for (ip, sni), probe in (sni_probes or {}).items():
        if probe is not None:
            result.setdefault(ip, []).append(sni)
    return result


def control_burn_state(ip_probes: dict, sni_probes: dict | None = None) -> str:
    """Выгорело ли контрольное имя: "proven" | "suspected" | "".

    proven — контроль упал на адресе, где КЛИЕНТСКОЕ имя прошло. Клиентское
    имя доказало, что адрес принимает соединения, значит отказ относится к
    самому контрольному имени. Работает и на одноадресной ноде, где прежняя
    эвристика «упало на всех адресах» не могла сработать в принципе.

    suspected — контроль упал сразу на всех адресах парка, и опровергнуть
    это нечем. Одновременный бан всех адресов менее правдоподобен, чем
    выгорание контроля, но доказательства нет: вердикты не выносятся.
    """
    passes = sni_passes_by_ip(sni_probes or {})
    for ip, probe in (ip_probes or {}).items():
        if probe is None or probe_passed(probe):
            continue
        if passes.get(ip):
            return CONTROL_BURN_PROVEN
    probes = [probe for probe in (ip_probes or {}).values() if probe]
    if len(probes) >= 2 and all(not probe_passed(probe) for probe in probes):
        # Если на упавших адресах падало и КЛИЕНТСКОЕ имя, отказ контроля
        # подтверждён вторым независимым именем: версия «выгорел контроль»
        # перестаёт объяснять данные, адреса действительно не отвечают
        tested = sni_tested_by_ip(sni_probes or {})
        corroborated = any(
            tested.get(ip)
            for ip, probe in (ip_probes or {}).items()
            if probe is not None and not probe_passed(probe)
        )
        if not corroborated:
            return CONTROL_BURN_SUSPECTED
    return ""


def classify(
    ip_probes: dict,
    sni_probes: dict | None = None,
    outside_probes: dict | None = None,
    node_healthy: bool = True,
    control_name: str = "",
    external_sni_results: dict | None = None,
    known_good_snis: set | None = None,
) -> dict:
    """Раздельные вердикты по адресам и именам.

    ip_probes:      {ip: проба контрольным именем с зондов РФ}
    sni_probes:     {(ip, sni): проба этим именем на этом адресе}
    outside_probes: {ip: проба контрольным именем с зондов вне РФ}
    external_sni_results: {(ip, sni): {"result": "pass"|"fail",
                    "server": имя сервера, "at": время}} — результаты проб
                    того же имени на живых адресах ДРУГИХ серверов той же
                    волны. Имя общее для нескольких нод, и вердикт по нему
                    обязан учитывать всё, что известно о нём в парке.

    Проба — словарь {"ok_probes", "total_probes", "stages"}; None означает,
    что проба не проводилась.

    Имя считается забаненным, только если оно не проходит НИ НА ОДНОМ живом
    адресе, где проверялось (включая чужие серверы волны). Падает на одних
    живых адресах и проходит на других — это бан пары «адрес + имя»
    (pair_blocked), само имя чистое.

    known_good_snis: имена, которые хоть раз наблюдались прошедшими (по
                    истории замеров). Имя вне этого множества, не прошедшее
                    нигде и сейчас, считается НЕПРОВЕРЕННЫМ, а не
                    забаненным: на ноде без default_backend неизвестный SNI
                    даёт молчаливый обрыв после ClientHello, неотличимый от
                    фильтрации, — так выглядит опечатка в карточке или имя,
                    которого нода не обслуживает. None означает «истории
                    нет, доверяем всем именам» (поведение до этой проверки).

    Отказ контрольного имени на адресе, где ПРОШЛО клиентское имя, означает
    не бан адреса, а выгорание самого контроля: адрес признаётся живым,
    control_burn="proven", и имя надо ротировать. Пока контроль выгорел,
    адрес объявляется заблокированным только при отказе и клиентского имени
    тоже; если клиентским именем адрес не проверяли — вердикта нет.

    Возвращает {"ips", "snis", "blocked_ips", "blocked_snis", "pair_blocked",
    "confidence", "evidence", "control_burned", "control_burn",
    "burned_control_name", "actionable"}.
    actionable=False означает, что автоматические действия запрещены:
    причина — в evidence.
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
            "pair_blocked": [], "confidence": CONFIDENCE_HIGH,
            "evidence": evidence, "control_burned": False,
            "control_burn": "", "burned_control_name": "",
            "unproven_snis": [], "unconfirmed_ips": [],
            "actionable": False,
        }

    live_by_sni = sni_passes_by_ip(sni_probes)
    tested_by_sni = sni_tested_by_ip(sni_probes)
    # «Проверенное» имя — то, что хоть раз где-то проходило: по истории
    # замеров, в этом прогоне или у соседа по волне. Только отказ такого
    # имени что-то доказывает; отказ имени, не проходившего никогда, с
    # равной вероятностью означает опечатку в карточке
    proven_names = set(known_good_snis or set())
    for names in live_by_sni.values():
        proven_names.update(names)
    for (_ip, sni), item in (external_sni_results or {}).items():
        if (item or {}).get("result") == "pass":
            proven_names.add(sni)
    trust_all_names = known_good_snis is None
    proven_tested_by_ip = {
        ip: [n for n in names if trust_all_names or n in proven_names]
        for ip, names in tested_by_sni.items()
    }
    burn = control_burn_state(ip_probes, sni_probes)
    if burn == CONTROL_BURN_SUSPECTED:
        evidence.append(
            {
                "step": "control_check",
                "result": "fail",
                "text": (
                    f"Контрольное имя {control_name or '—'} не проходит ни на "
                    f"одном из {len(ip_probes)} адресов, и ни на одном из них "
                    "не прошло клиентское имя. Одновременный бан всех адресов "
                    "менее правдоподобен, чем выгорание самого контрольного "
                    "имени — вердикты не выносятся, нужно следующее имя из "
                    "списка"
                ),
            }
        )
        return {
            "ips": {}, "snis": {}, "blocked_ips": [], "blocked_snis": [],
            "pair_blocked": [], "confidence": CONFIDENCE_LOW,
            "evidence": evidence, "control_burned": True,
            "control_burn": CONTROL_BURN_SUSPECTED,
            "burned_control_name": control_name,
            "unproven_snis": [], "unconfirmed_ips": [],
            "actionable": False,
        }
    if burn == CONTROL_BURN_PROVEN:
        burned_at = [
            ip for ip, probe in ip_probes.items()
            if probe is not None and not probe_passed(probe) and live_by_sni.get(ip)
        ]
        evidence.append(
            {
                "step": "control_check", "result": "fail", "burned": True,
                "text": (
                    f"Контрольное имя {control_name or '—'} не проходит на "
                    + ", ".join(burned_at)
                    + ", где клиентское имя ("
                    + ", ".join(
                        sorted({n for ip in burned_at for n in live_by_sni.get(ip, [])})
                    )
                    + ") проходит. Адрес принимает соединения — выгорело само "
                    "контрольное имя, его надо заменить следующим из списка. "
                    "Вердикты ниже опираются на клиентские имена"
                ),
            }
        )

    # --- статус адресов -----------------------------------------------------
    # При выгоревшем контроле его отказ ничего не доказывает: адрес признаётся
    # заблокированным, только если на нём упало и клиентское имя, а без проб
    # клиентского имени вердикт не выносится вовсе.
    weak_sample = False
    unconfirmed: list[str] = []
    for ip, probe in ip_probes.items():
        if probe is None:
            if live_by_sni.get(ip):
                ips[ip] = IP_OK
                evidence.append(
                    {
                        "step": "ip_probe", "ip": ip, "result": "pass",
                        "text": (
                            f"Адрес {ip}: контролем не проверялся, но "
                            f"клиентское имя {', '.join(live_by_sni[ip])} на нём "
                            "проходит — адрес жив"
                        ),
                    }
                )
                continue
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

        if live_by_sni.get(ip):
            # Контроль упал, а клиентское имя прошло: адрес принимает
            # соединения, отказ относится к контрольному имени
            ips[ip] = IP_OK
            evidence.append(
                {
                    "step": "ip_probe", "ip": ip, "sni": control_name,
                    "result": "pass", "control_burned": True,
                    "text": (
                        f"Адрес {ip}: контрольное имя {control_name} не проходит "
                        f"({_probe_label(probe)}), но клиентское имя "
                        f"{', '.join(live_by_sni[ip])} проходит — адрес жив, "
                        "под фильтром контрольное имя"
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

        if not is_hard_block(probe) and not proven_tested_by_ip.get(ip):
            # ЕДИНСТВЕННЫЙ свидетель — контрольное имя, а оно бывает и
            # выгоревшим, и забаненным в паре именно с этим адресом. Раньше
            # такого адреса хватало для автозамены: так живая нода уехала на
            # резерв 07.09.2026. Теперь нужен отказ второго, доказанно
            # рабочего имени — либо жёсткая форма, где вывод не требует имён.
            ips[ip] = IP_UNKNOWN
            unconfirmed.append(ip)
            untried = tested_by_sni.get(ip) or []
            evidence.append(
                {
                    "step": "ip_probe", "ip": ip, "sni": control_name,
                    "result": "inconclusive",
                    "text": (
                        f"Адрес {ip}: контрольное имя {control_name} не проходит "
                        f"({_probe_label(probe)}), но подтвердить это нечем"
                        + (
                            " — из клиентских имён на нём проверялись только "
                            "никогда не проходившие ("
                            + ", ".join(untried) + ")"
                            if untried
                            else " — клиентским именем адрес не проверялся"
                        )
                        + (
                            "; контрольное имя к тому же выгорело"
                            if burn == CONTROL_BURN_PROVEN
                            else ""
                        )
                        + ". Вердикт не выносится: одного контрольного имени "
                        "для замены адреса недостаточно"
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
        burned_note = (
            " Контрольное имя выгорело, но на этом адресе не прошло и "
            "клиентское имя ("
            + ", ".join(tested_by_sni.get(ip, []))
            + ") — отказ обоих имён указывает на адрес."
            if burn == CONTROL_BURN_PROVEN
            else ""
        )
        evidence.append(
            {
                "step": "ip_probe", "ip": ip, "sni": control_name,
                "result": "fail", "hard": is_hard_block(probe),
                "text": (
                    f"Адрес {ip}: контрольное имя {control_name} не проходит "
                    f"({_probe_label(probe)}"
                    + (f", {note}" if note else "")
                    + f"){outside_note} — адрес заблокирован.{hard}{burned_note}"
                ),
            }
        )

    # --- статус имён (только на живых адресах) ------------------------------
    # Сначала собираем по каждому имени, где оно прошло и где упало, и
    # только потом выносим вердикт: одна и та же проба «имя не проходит на
    # живом адресе» означает бан имени, если имя не проходит нигде, и бан
    # пары «адрес + имя», если на другом живом адресе оно проходит.
    external_sni_results = external_sni_results or {}
    passes: dict[str, list[str]] = {}
    fails: dict[str, list[str]] = {}
    for (ip, sni), probe in sni_probes.items():
        if ips.get(ip) != IP_OK or probe is None:
            continue
        total = probe.get("total_probes") or 0
        if total and total < MIN_PROBES_HIGH_CONFIDENCE:
            weak_sample = True
        if probe_passed(probe):
            passes.setdefault(sni, []).append(ip)
        else:
            fails.setdefault(sni, []).append(ip)
    external_passes: dict[str, list[dict]] = {}
    for (ip, sni), item in external_sni_results.items():
        if (item or {}).get("result") == "pass":
            external_passes.setdefault(sni, []).append(dict(item, ip=ip))

    pair_blocked: list[dict] = []
    unproven: list[str] = []
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
        if probe_passed(probe):
            # Одного успеха достаточно: имя работает хотя бы где-то
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
        note = _stage_note(probe)
        label = _probe_label(probe) + (f", {note}" if note else "")
        if passes.get(sni):
            # Здесь падает, на другом живом адресе этого же сервера проходит
            snis[sni] = SNI_OK
            pair_blocked.append({"ip": ip, "sni": sni})
            evidence.append(
                {
                    "step": "sni_probe", "ip": ip, "sni": sni,
                    "result": "pair", "pair_blocked": True,
                    "text": (
                        f"Имя {sni} на живом адресе {ip}: не проходит ({label}), "
                        f"но на живом адресе {', '.join(passes[sni])} проходит — "
                        "бан пары «адрес + имя», само имя чистое"
                    ),
                }
            )
            continue
        if external_passes.get(sni):
            # У соседнего сервера той же волны имя на живом адресе прошло:
            # имя рабочее, а здесь бан пары
            ext = external_passes[sni][-1]
            snis[sni] = SNI_OK
            pair_blocked.append({"ip": ip, "sni": sni})
            evidence.append(
                {
                    "step": "sni_probe", "ip": ip, "sni": sni,
                    "result": "pair", "pair_blocked": True,
                    "text": (
                        f"Имя {sni} на живом адресе {ip}: не проходит ({label}), "
                        f"но на сервере {ext.get('server') or '?'} на адресе "
                        f"{ext.get('ip')} ({ext.get('at') or '—'}) проходит — "
                        "бан пары «адрес + имя», само имя чистое"
                    ),
                }
            )
            continue
        if not trust_all_names and sni not in proven_names:
            # Имя не проходило НИКОГДА и НИГДЕ: на ноде без default_backend
            # неизвестный SNI рвётся молча после ClientHello — ровно как под
            # фильтром. Объявить такое имя забаненным значило бы поверить
            # опечатке в карточке
            snis[sni] = SNI_UNKNOWN
            if sni not in unproven:
                unproven.append(sni)
            evidence.append(
                {
                    "step": "sni_probe", "ip": ip, "sni": sni,
                    "result": "warn", "unproven": True,
                    "text": (
                        f"Имя {sni} на живом адресе {ip}: не проходит ({label}), "
                        "но оно не проходило ни разу за всю историю замеров — "
                        "вердикт не выносится: так же выглядит опечатка в "
                        "карточке или имя, которого нода не обслуживает "
                        "(ACL haproxy / serverNames инбаунда)"
                    ),
                }
            )
            continue
        snis[sni] = SNI_BLOCKED
        where = (
            f" ни на одном из {len(fails[sni])} живых адресов"
            if len(fails.get(sni, [])) > 1
            else ""
        )
        evidence.append(
            {
                "step": "sni_probe", "ip": ip, "sni": sni, "result": "fail",
                "text": (
                    f"Имя {sni} на живом адресе {ip}: не проходит ({label}) — "
                    f"адрес доказанно жив{where}, значит заблокировано имя"
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
        "pair_blocked": pair_blocked,
        "confidence": confidence,
        "evidence": evidence,
        "control_burned": burn == CONTROL_BURN_PROVEN,
        "control_burn": burn,
        "burned_control_name": control_name if burn else "",
        "unproven_snis": unproven,
        "unconfirmed_ips": unconfirmed,
        "actionable": confidence == CONFIDENCE_HIGH,
    }


def domain_snis(domain: dict) -> list:
    """Имена, которые клиенты шлют в ClientHello для этого домена.

    Никогда не пустой список: у домена старой схемы это он сам.
    """
    item = domain or {}
    names = [str(n).strip().lower() for n in (item.get("snis") or []) if n]
    return names or [item.get("domain") or ""]


def domain_own_name(domain: dict) -> bool:
    """Уходит ли сам домен A-записи в эфир.

    Домен, которого нет в собственном списке имён, ТСПУ не видит вовсе:
    клиент резолвит его в адрес, но в рукопожатие шлёт имя своего
    протокола. Всё, что решается по такому домену, решается про адрес.
    """
    return (domain or {}).get("domain") in domain_snis(domain)


def domains_safe_to_repoint(domains: list, blocked_snis: list) -> tuple:
    """A-запись переставляется ВСЕГДА; вердикт по имени на это не влияет.

    Решение владельца (09.09.2026). Прежде домен, который сам уходил в эфир,
    при бане своего имени замораживался: считалось, что публиковать чистый
    адрес под фильтруемым именем незачем, а новый адрес рискует уйти в бан
    следом. На практике это оставляло клиентов на МЁРТВОМ адресе до ручного
    вмешательства, причём в самом частом случае — когда имя вовсе не
    забанено, а виноват адрес.

    Теперь так: нашли живой адрес — перевели на него DNS, а состояние имён
    ушло в алерт отдельно. Цена решения: если имя действительно под
    фильтром, клиенты не заработают и на новом адресе, а резервный адрес
    окажется связан с фильтруемым именем. Взамен ночная авария с баном
    адреса чинится без участия человека.

    Сигнатура сохранена ради вызывающего кода и тестов: второй список
    всегда пуст.
    """
    return list(domains), []


def domains_with_blocked_sni(domains: list, blocked_snis: list) -> list:
    """Домены, у которых под фильтр попало хотя бы одно имя.

    Возвращает [{"domain", "blocked": [...], "clean": [...], "own": bool}].
    Их A-записи меняются как обычно (кроме случая, когда домен уходит в эфир
    сам и чистых имён не осталось), но клиенты соответствующих протоколов не
    заработают, пока имя не заменят в конфигах: об этом нужен отдельный
    алерт, и называть он обязан ИМЕННО забаненное имя, а не первое из
    списка — иначе владелец пойдёт менять работающий протокол.
    """
    blocked = set(blocked_snis or [])
    result = []
    for d in domains:
        names = domain_snis(d)
        hit = [n for n in names if n in blocked]
        if not hit:
            continue
        result.append({
            "domain": (d or {}).get("domain"),
            "blocked": hit,
            "clean": [n for n in names if n not in blocked],
            "own": domain_own_name(d),
        })
    return result


def evidence_text(evidence: list) -> str:
    """Журнал проверок сплошным текстом — для алертов и карточки."""
    lines = []
    for item in evidence or []:
        mark = {
            "pass": "✓", "fail": "✗", "skipped": "–",
            "inconclusive": "?", "warn": "!", "pair": "≠",
        }.get(item.get("result"), "·")
        lines.append(f"{mark} {item.get('text', '')}")
    return "\n".join(lines)
