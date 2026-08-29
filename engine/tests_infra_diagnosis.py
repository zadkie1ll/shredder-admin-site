"""Тесты классификатора блокировок ТСПУ (engine/infra_diagnosis.py).

Чистые функции над результатами проб: ни сети, ни БД. Сценарии повторяют
разбор инцидента 28.08.2026, где одновременно оказались под фильтром и
адрес, и имя, — прежний механизм в такой ситуации менял бы адрес вслепую.
"""

from django.test import SimpleTestCase

from engine import infra_diagnosis as diag


def probe(ok, total, stages=None):
    """Результат пробы: сколько зондов пробилось из скольких."""
    if stages is None:
        blocked = total - ok
        stages = {}
        if ok:
            stages["tls_ok"] = ok
        if blocked:
            stages["tls_fail"] = blocked
    return {"ok_probes": ok, "total_probes": total, "stages": stages}


def hard_fail(total=12):
    """Жёсткая форма: TCP не устанавливается ни у одного зонда."""
    return {"ok_probes": 0, "total_probes": total, "stages": {"tcp_fail": total}}


class ProbeHelpersTests(SimpleTestCase):
    def test_probe_passed_threshold(self):
        self.assertTrue(diag.probe_passed(probe(11, 12)))
        self.assertFalse(diag.probe_passed(probe(1, 12)))
        # Ровно на пороге — не засчитано
        self.assertFalse(diag.probe_passed(probe(6, 12)))
        # Проба не проводилась
        self.assertFalse(diag.probe_passed(None))
        self.assertFalse(diag.probe_passed({}))

    def test_hard_block_detection(self):
        self.assertTrue(diag.is_hard_block(hard_fail()))
        # Обрыв на ClientHello — мягкая форма, не жёсткая
        self.assertFalse(diag.is_hard_block(probe(1, 12)))
        # Прошедшая проба не является блокировкой ни в какой форме
        self.assertFalse(diag.is_hard_block(probe(11, 12)))

    def test_refused_is_not_filtering(self):
        refused = {
            "ok_probes": 0, "total_probes": 12,
            "stages": {"tcp_refused": 12},
        }
        self.assertFalse(diag.is_hard_block(refused))
        self.assertEqual(diag.dominant_stage(refused), "tcp_refused")


class ClassifyTests(SimpleTestCase):
    def test_incident_scenario_both_bans(self):
        # Инцидент: адрес .160 под жёстким баном, .162 жив, имя под фильтром
        result = diag.classify(
            ip_probes={
                "103.75.125.160": hard_fail(),
                "103.75.125.162": probe(11, 12),
            },
            sni_probes={
                ("103.75.125.162", "nl.monkeyvillage.pro"): probe(1, 12),
                ("103.75.125.162", "nl.coffeconsulting.uk"): probe(11, 12),
            },
            control_name="ya.ru",
        )
        self.assertEqual(result["blocked_ips"], ["103.75.125.160"])
        self.assertEqual(result["blocked_snis"], ["nl.monkeyvillage.pro"])
        self.assertTrue(result["actionable"])
        self.assertEqual(result["confidence"], diag.CONFIDENCE_HIGH)
        # Журнал объясняет КАЖДЫЙ вывод, а не только итог
        text = diag.evidence_text(result["evidence"])
        self.assertIn("адрес заблокирован", text)
        self.assertIn("значит заблокировано имя", text)
        self.assertIn("имя чистое", text)

    def test_sni_block_only(self):
        # Адрес жив, имя под фильтром: менять адрес бессмысленно
        result = diag.classify(
            ip_probes={"185.10.0.10": probe(11, 12)},
            sni_probes={("185.10.0.10", "nl.example.space"): probe(0, 12)},
            control_name="ya.ru",
        )
        self.assertEqual(result["blocked_ips"], [])
        self.assertEqual(result["blocked_snis"], ["nl.example.space"])

    def test_ip_block_only(self):
        result = diag.classify(
            ip_probes={
                "185.10.0.10": probe(1, 12),
                "185.10.0.11": probe(11, 12),
            },
            sni_probes={("185.10.0.11", "nl.example.space"): probe(11, 12)},
            control_name="ya.ru",
        )
        self.assertEqual(result["blocked_ips"], ["185.10.0.10"])
        self.assertEqual(result["blocked_snis"], [])

    def test_all_names_blocked_on_live_address(self):
        # Раньше это был неразрешимый случай: теперь адрес доказанно жив,
        # значит забанены все имена, и замена адреса бессмысленна
        result = diag.classify(
            ip_probes={"185.10.0.10": probe(11, 12)},
            sni_probes={
                ("185.10.0.10", "a.example.org"): probe(0, 12),
                ("185.10.0.10", "b.example.org"): probe(1, 12),
            },
            control_name="ya.ru",
        )
        self.assertEqual(result["blocked_ips"], [])
        self.assertEqual(
            sorted(result["blocked_snis"]), ["a.example.org", "b.example.org"]
        )

    def test_names_not_probed_on_dead_address(self):
        # На мёртвом адресе падает всё — вывод об имени был бы ложным
        result = diag.classify(
            ip_probes={"185.10.0.10": hard_fail()},
            sni_probes={("185.10.0.10", "nl.example.space"): probe(0, 12)},
            control_name="ya.ru",
        )
        self.assertEqual(result["blocked_ips"], ["185.10.0.10"])
        self.assertEqual(result["blocked_snis"], [])
        self.assertIn("не проверялось", diag.evidence_text(result["evidence"]))

    def test_control_name_burned(self):
        # Контроль не проходит нигде — виновато само контрольное имя
        result = diag.classify(
            ip_probes={
                "185.10.0.10": probe(0, 12),
                "185.10.0.11": probe(1, 12),
                "185.10.0.12": probe(0, 12),
            },
            control_name="ya.ru",
        )
        self.assertTrue(result["control_burned"])
        self.assertEqual(result["blocked_ips"], [])
        self.assertFalse(result["actionable"])
        self.assertIn("выгорание", diag.evidence_text(result["evidence"]))

    def test_single_address_not_treated_as_burned_control(self):
        # У ноды один адрес: «не прошло везде» означает «не прошло на нём»
        result = diag.classify(
            ip_probes={"185.10.0.10": hard_fail()},
            control_name="ya.ru",
        )
        self.assertFalse(result["control_burned"])
        self.assertEqual(result["blocked_ips"], ["185.10.0.10"])

    def test_unreachable_from_outside_is_not_filtering(self):
        # Не отвечает ни из РФ, ни извне — маршрут или нода, не ТСПУ
        result = diag.classify(
            ip_probes={"185.10.0.10": probe(0, 12)},
            outside_probes={"185.10.0.10": probe(0, 6)},
            control_name="ya.ru",
        )
        self.assertEqual(result["blocked_ips"], [])
        self.assertEqual(result["ips"]["185.10.0.10"], diag.IP_UNKNOWN)
        self.assertIn("не фильтрация", diag.evidence_text(result["evidence"]))

    def test_outside_ok_confirms_filtering(self):
        result = diag.classify(
            ip_probes={"185.10.0.10": probe(0, 12)},
            outside_probes={"185.10.0.10": probe(6, 6)},
            control_name="ya.ru",
        )
        self.assertEqual(result["blocked_ips"], ["185.10.0.10"])
        self.assertIn("извне отвечает", diag.evidence_text(result["evidence"]))

    def test_weak_sample_blocks_automation(self):
        # Мало ответивших зондов — вердикт есть, но действовать нельзя
        result = diag.classify(
            ip_probes={"185.10.0.10": probe(0, 3)},
            control_name="ya.ru",
        )
        self.assertEqual(result["confidence"], diag.CONFIDENCE_MEDIUM)
        self.assertFalse(result["actionable"])

    def test_unhealthy_node_skips_probes(self):
        result = diag.classify(
            ip_probes={"185.10.0.10": probe(0, 12)},
            node_healthy=False,
            control_name="ya.ru",
        )
        self.assertFalse(result["actionable"])
        self.assertEqual(result["blocked_ips"], [])
        self.assertIn("Нода нездорова", diag.evidence_text(result["evidence"]))

    def test_name_passing_anywhere_is_clean(self):
        # Имя прошло хотя бы на одном живом адресе — оно рабочее
        result = diag.classify(
            ip_probes={
                "185.10.0.10": probe(11, 12),
                "185.10.0.11": probe(11, 12),
            },
            sni_probes={
                ("185.10.0.10", "nl.example.space"): probe(11, 12),
                ("185.10.0.11", "nl.example.space"): probe(11, 12),
            },
            control_name="ya.ru",
        )
        self.assertEqual(result["blocked_snis"], [])


class DomainSplitTests(SimpleTestCase):
    def test_only_clean_domains_are_repointed(self):
        domains = [
            {"domain": "a.example.org"},
            {"domain": "b.example.org"},
            {"domain": "c.example.org", "sni": "mask.example.net"},
        ]
        safe, unsafe = diag.domains_safe_to_repoint(domains, ["b.example.org"])
        self.assertEqual([d["domain"] for d in safe],
                         ["a.example.org", "c.example.org"])
        self.assertEqual([d["domain"] for d in unsafe], ["b.example.org"])

    def test_client_sni_wins_over_domain(self):
        # У ноды имя клиента может отличаться от домена A-записи
        domains = [{"domain": "ru-1.example.xyz", "sni": "max.ru"}]
        safe, unsafe = diag.domains_safe_to_repoint(domains, ["max.ru"])
        self.assertEqual(safe, [])
        self.assertEqual(len(unsafe), 1)

    def test_no_blocked_names_means_everything_safe(self):
        domains = [{"domain": "a.example.org"}, {"domain": "b.example.org"}]
        safe, unsafe = diag.domains_safe_to_repoint(domains, [])
        self.assertEqual(len(safe), 2)
        self.assertEqual(unsafe, [])
