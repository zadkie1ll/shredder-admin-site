"""Тесты раздела «Инфраструктура → Серверы» (engine/infra.py, infra_worker.py).

БД — in-memory SQLite с реальными таблицами (паттерн tests_node_provisioning),
Cloudflare/Telegram/RIPE Atlas — mock, HTTP — RequestFactory на view-функциях.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import gzip
import os
from pathlib import Path
import tempfile
from unittest import mock

import httpx
from django.test import RequestFactory, SimpleTestCase
from django.test import override_settings

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from common.models.db import (
    Base,
    CensorCheck,
    CensorCheckRun,
    InfraAgentCommand,
    InfraAnomaly,
    InfraIpReplacement,
    InfraServer,
    InfraServerDomain,
    InfraServerIp,
    InfraTelemetry,
    InfraTelemetryAgg,
    SystemSetting,
    UserIpObservation,
)
from engine import infra
from engine import infra_worker
from engine import geoip_lookup
from engine import geoip_updater


@compiles(BigInteger, "sqlite")
def _compile_bigint_as_integer_on_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "JSON"


INFRA_TABLES = [
    InfraServer.__table__,
    InfraServerIp.__table__,
    InfraServerDomain.__table__,
    InfraTelemetry.__table__,
    InfraTelemetryAgg.__table__,
    InfraAgentCommand.__table__,
    InfraAnomaly.__table__,
    InfraIpReplacement.__table__,
    SystemSetting.__table__,
    CensorCheck.__table__,
    CensorCheckRun.__table__,
    UserIpObservation.__table__,
]


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class InfraDbTestCase(SimpleTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine, tables=INFRA_TABLES)
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session.close)

    def make_server(self, **kwargs):
        now = utcnow()
        defaults = dict(
            machine_uid=kwargs.pop("machine_uid", "m-1"),
            node_name="de-1",
            hostname="de-01",
            wan_interface="ens3",
            detected_link_speed_mbps=10000,
            first_seen_at=now - timedelta(days=30),
            last_seen_at=now,
            agent_started_at=now - timedelta(hours=5),
            boot_time=now - timedelta(days=3),
        )
        defaults.update(kwargs)
        server = InfraServer(**defaults)
        self.session.add(server)
        self.session.commit()
        return server

    def make_ip(self, server, ip, **kwargs):
        row = InfraServerIp(
            server_id=server.id,
            ip=ip,
            prefix_len=kwargs.pop("prefix_len", 24),
            source=kwargs.pop("source", "detected"),
            on_interface=kwargs.pop("on_interface", True),
            interface=kwargs.pop("interface", "ens3"),
            **kwargs,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def make_domain(self, server, domain):
        row = InfraServerDomain(server_id=server.id, domain=domain)
        self.session.add(row)
        self.session.commit()
        return row

    def add_samples(self, server, start, count, step_seconds=10, **values):
        for i in range(count):
            self.session.add(
                InfraTelemetry(
                    server_id=server.id,
                    ts=start + timedelta(seconds=i * step_seconds),
                    rx_bps=values.get("rx_bps", 100_000_000),
                    tx_bps=values.get("tx_bps", 20_000_000),
                    tcp_connections=values.get("tcp_connections", 4000),
                    conntrack=values.get("conntrack"),
                    cpu_load=values.get("cpu_load"),
                )
            )
        self.session.commit()


class GeoIpLookupTests(SimpleTestCase):
    def tearDown(self):
        geoip_lookup.reset_caches()
        super().tearDown()

    def test_local_city_record_uses_russian_country_and_top_level_region(self):
        reader = mock.Mock()
        reader.get.return_value = {
            "country": {
                "iso_code": "RU",
                "names": {"ru": "Россия", "en": "Russia"},
            },
            "subdivisions": [
                {
                    "iso_code": "MOW",
                    "names": {"ru": "Москва", "en": "Moscow"},
                },
                {
                    "iso_code": "C",
                    "names": {"ru": "Центральный округ"},
                },
            ],
        }
        database = geoip_lookup.GeoIpDatabase("/tmp/test.mmdb", 1)

        with mock.patch.object(geoip_lookup, "_reader_for", return_value=reader):
            location = geoip_lookup.lookup_ip("8.8.8.8", database)

        self.assertEqual(
            location,
            geoip_lookup.GeoLocation("RU", "Россия", "MOW", "Москва"),
        )

    def test_dbip_region_without_iso_code_is_not_discarded(self):
        reader = mock.Mock()
        reader.get.return_value = {
            "country": {"iso_code": "RU", "names": {"ru": "Россия"}},
            "subdivisions": [{"names": {"en": "Leningradskaya Oblast'"}}],
        }
        database = geoip_lookup.GeoIpDatabase("/tmp/test.mmdb", 1)

        with mock.patch.object(geoip_lookup, "_reader_for", return_value=reader):
            location = geoip_lookup.lookup_ip("77.105.170.88", database)

        self.assertEqual(
            location,
            geoip_lookup.GeoLocation(
                "RU", "Россия", "LEN", "Ленинградская область"
            ),
        )

    def test_dbip_common_russian_region_aliases_are_localized(self):
        database = geoip_lookup.GeoIpDatabase("/tmp/test.mmdb", 1)
        cases = (
            ("Moscow", "MOW", "Москва"),
            ("St.-Petersburg", "SPE", "Санкт-Петербург"),
            ("Kuzbass", "KEM", "Кемеровская область — Кузбасс"),
            ("Rostov", "ROS", "Ростовская область"),
        )

        for index, (source_name, expected_code, expected_name) in enumerate(cases):
            reader = mock.Mock()
            reader.get.return_value = {
                "country": {"iso_code": "RU", "names": {"ru": "Россия"}},
                "subdivisions": [{"names": {"en": source_name}}],
            }
            with self.subTest(source_name=source_name), mock.patch.object(
                geoip_lookup, "_reader_for", return_value=reader
            ):
                location = geoip_lookup.lookup_ip(
                    f"77.105.170.{88 + index}", database
                )
                self.assertEqual(location.region_code, expected_code)
                self.assertEqual(location.region_name, expected_name)

    def test_private_or_invalid_addresses_never_reach_database(self):
        database = geoip_lookup.GeoIpDatabase("/tmp/test.mmdb", 1)
        with mock.patch.object(geoip_lookup, "_reader_for") as reader:
            self.assertIsNone(geoip_lookup.lookup_ip("192.168.1.1", database))
            self.assertIsNone(geoip_lookup.lookup_ip("не-ip", database))
        reader.assert_not_called()

    def test_corrupt_database_is_treated_as_not_configured(self):
        with tempfile.NamedTemporaryFile(suffix=".mmdb") as database_file:
            database_file.write(b"not-an-mmdb")
            database_file.flush()
            with override_settings(GEOIP_CITY_DB_PATH=database_file.name):
                self.assertIsNone(geoip_lookup.configured_database())


class GeoIpUpdaterTests(SimpleTestCase):
    NOW = datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        geoip_updater.reset_state_for_tests()

    def database_path(self):
        return Path(self.temp_dir.name) / "DBIP-City-Lite.mmdb"

    @staticmethod
    def archive_bytes(database_content=b"test-mmdb"):
        return gzip.compress(database_content)

    def settings_override(self, **kwargs):
        values = {
            "GEOIP_CITY_DB_PATH": str(self.database_path()),
            "GEOIPUPDATE_INTERVAL_HOURS": 168,
        }
        values.update(kwargs)
        return override_settings(**values)

    def test_missing_database_path_disables_download_without_touching_disk(self):
        with override_settings(GEOIP_CITY_DB_PATH=""), mock.patch.object(
            geoip_updater.httpx, "Client"
        ) as client:
            result = geoip_updater.update_if_due(now=self.NOW)

        self.assertEqual(result, "disabled")
        self.assertFalse(self.database_path().exists())
        self.assertFalse(
            self.database_path().with_suffix(".mmdb.lock").exists()
        )
        client.assert_not_called()

    def test_download_extract_validate_and_atomic_replace(self):
        archive = self.archive_bytes(b"new-database")

        def handler(request):
            self.assertEqual(
                str(request.url),
                geoip_updater.download_url(2026, 8),
            )
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, content=archive, request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.settings_override(), mock.patch.object(
            geoip_updater.httpx, "Client", return_value=client
        ) as client_factory, mock.patch.object(
            geoip_updater, "_validate_database"
        ) as validate:
            result = geoip_updater.update_if_due(now=self.NOW)
            second = geoip_updater.update_if_due(now=self.NOW + 60)

        self.assertEqual(result, "updated")
        self.assertEqual(second, "current")
        self.assertEqual(self.database_path().read_bytes(), b"new-database")
        self.assertTrue(
            self.database_path().with_suffix(".mmdb.checked").exists()
        )
        validate.assert_called_once()
        client_factory.assert_called_once()
        self.assertTrue(client_factory.call_args.kwargs["follow_redirects"])

    def test_current_month_404_falls_back_to_previous_release(self):
        archive = self.archive_bytes(b"previous-month-database")
        requested_urls = []

        def handler(request):
            requested_urls.append(str(request.url))
            if str(request.url) == geoip_updater.download_url(2026, 8):
                return httpx.Response(404, request=request)
            return httpx.Response(200, content=archive, request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.settings_override(), mock.patch.object(
            geoip_updater.httpx, "Client", return_value=client
        ), mock.patch.object(geoip_updater, "_validate_database"):
            result = geoip_updater.update_if_due(now=self.NOW)

        self.assertEqual(result, "updated")
        self.assertEqual(
            requested_urls,
            [
                geoip_updater.download_url(2026, 8),
                geoip_updater.download_url(2026, 7),
            ],
        )
        self.assertEqual(
            self.database_path().read_bytes(), b"previous-month-database"
        )

    def test_bad_archive_keeps_previous_database_and_throttles_retry(self):
        self.database_path().parent.mkdir(parents=True, exist_ok=True)
        self.database_path().write_bytes(b"previous-database")
        os_time = self.NOW - (169 * 60 * 60)
        self.database_path().touch()
        os.utime(self.database_path(), (os_time, os_time))

        def handler(request):
            return httpx.Response(200, content=b"not-a-tar", request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.settings_override(), mock.patch.object(
            geoip_updater.httpx, "Client", return_value=client
        ) as client_factory, self.assertLogs("infra.geoip-updater", level="ERROR"):
            first = geoip_updater.update_if_due(now=self.NOW)
            second = geoip_updater.update_if_due(now=self.NOW + 30)

        self.assertEqual(first, "error")
        self.assertEqual(second, "current")
        self.assertEqual(self.database_path().read_bytes(), b"previous-database")
        client_factory.assert_called_once()

    def test_mmdb_validation_failure_never_replaces_previous_database(self):
        self.database_path().write_bytes(b"previous-database")
        os_time = self.NOW - (169 * 60 * 60)
        os.utime(self.database_path(), (os_time, os_time))
        archive = self.archive_bytes(b"invalid-mmdb")

        def handler(request):
            return httpx.Response(200, content=archive, request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.settings_override(), mock.patch.object(
            geoip_updater.httpx, "Client", return_value=client
        ), mock.patch.object(
            geoip_updater,
            "_validate_database",
            side_effect=ValueError("invalid MMDB"),
        ), self.assertLogs("infra.geoip-updater", level="ERROR"):
            result = geoip_updater.update_if_due(now=self.NOW)

        self.assertEqual(result, "error")
        self.assertEqual(self.database_path().read_bytes(), b"previous-database")

    def test_infra_leader_runs_geo_updater_before_table_check(self):
        with mock.patch.object(
            geoip_updater, "update_if_due", return_value="current"
        ) as update, mock.patch.object(
            infra_worker, "_infra_tables_ready", return_value=False
        ):
            infra_worker.run_maintenance()

        update.assert_called_once_with()

    def test_geo_update_failure_does_not_stop_other_infra_maintenance(self):
        with mock.patch.object(
            geoip_updater, "update_if_due", side_effect=OSError("read-only")
        ), mock.patch.object(
            infra_worker, "_infra_tables_ready", return_value=False
        ) as tables_ready, self.assertLogs("infra-worker", level="ERROR"):
            infra_worker.run_maintenance()

        tables_ready.assert_called_once_with()


class WhoConnectsGeoTests(InfraDbTestCase):
    def setUp(self):
        super().setUp()
        infra.reset_geo_analytics_cache()

    def tearDown(self):
        infra.reset_geo_analytics_cache()
        super().tearDown()

    def add_observation(self, username, ip, hits, node="de-1"):
        now = utcnow()
        self.session.add(
            UserIpObservation(
                username=username,
                ip=ip,
                subnet=f"{ip}/32",
                node=node,
                hits=hits,
                first_seen=now - timedelta(hours=2),
                last_seen=now,
            )
        )
        self.session.commit()

    def test_payload_aggregates_all_countries_and_russian_regions(self):
        self.add_observation("a", "8.8.8.8", 20)
        self.add_observation("b", "1.1.1.1", 10)
        self.add_observation("c", "9.9.9.9", 5)
        locations = {
            "8.8.8.8": geoip_lookup.GeoLocation("RU", "Россия", "MOW", "Москва"),
            "1.1.1.1": geoip_lookup.GeoLocation(
                "RU", "Россия", "SAM", "Самарская область"
            ),
            "9.9.9.9": geoip_lookup.GeoLocation("DE", "Германия"),
        }
        database = geoip_lookup.GeoIpDatabase("/tmp/test.mmdb", 1)

        with mock.patch.object(
            geoip_lookup, "configured_database", return_value=database
        ), mock.patch.object(
            geoip_lookup,
            "lookup_ip",
            side_effect=lambda ip, _: locations[ip],
        ):
            payload = infra.who_connects_payload(self.session, "de-1", utcnow())

        geo = payload["geo"]
        self.assertTrue(geo["enabled"])
        self.assertEqual(geo["source"], "DB-IP City Lite")
        self.assertEqual(geo["located_ips"], 3)
        self.assertEqual(geo["total_hits"], 35)
        self.assertEqual(geo["russia_share_pct"], 85.7)
        self.assertEqual(
            [(row["name"], row["hits"], row["addresses"]) for row in geo["countries"]],
            [("Россия", 30, 2), ("Германия", 5, 1)],
        )
        self.assertEqual(
            [(row["name"], row["share_pct"]) for row in geo["russian_regions"]],
            [("Москва", 57.1), ("Самарская область", 28.6)],
        )

    def test_regions_without_iso_codes_do_not_merge_into_unknown(self):
        self.add_observation("a", "8.8.8.8", 20)
        self.add_observation("b", "1.1.1.1", 10)
        locations = {
            "8.8.8.8": geoip_lookup.GeoLocation("RU", "Россия", None, "Москва"),
            "1.1.1.1": geoip_lookup.GeoLocation(
                "RU", "Россия", None, "Самарская область"
            ),
        }
        database = geoip_lookup.GeoIpDatabase("/tmp/test.mmdb", 1)

        with mock.patch.object(
            geoip_lookup, "configured_database", return_value=database
        ), mock.patch.object(
            geoip_lookup,
            "lookup_ip",
            side_effect=lambda ip, _: locations[ip],
        ):
            geo = infra.who_connects_payload(
                self.session, "de-1", utcnow()
            )["geo"]

        self.assertEqual(
            [(row["name"], row["hits"]) for row in geo["russian_regions"]],
            [("Москва", 20), ("Самарская область", 10)],
        )

    def test_missing_database_preserves_existing_ip_payload(self):
        self.add_observation("a", "8.8.8.8", 20)
        with mock.patch.object(
            geoip_lookup, "configured_database", return_value=None
        ), mock.patch.object(geoip_lookup, "lookup_ip") as lookup:
            payload = infra.who_connects_payload(self.session, "de-1", utcnow())

        self.assertFalse(payload["geo"]["enabled"])
        self.assertEqual(payload["top_addresses"][0]["ip"], "8.8.8.8")
        lookup.assert_not_called()

    def test_geo_aggregation_is_reused_within_cache_window(self):
        self.add_observation("a", "8.8.8.8", 20)
        database = geoip_lookup.GeoIpDatabase("/tmp/test.mmdb", 1)
        location = geoip_lookup.GeoLocation("RU", "Россия", "MOW", "Москва")
        now = utcnow()

        with mock.patch.object(
            geoip_lookup, "configured_database", return_value=database
        ), mock.patch.object(
            geoip_lookup,
            "lookup_ip",
            return_value=location,
        ) as lookup:
            first = infra.who_connects_payload(self.session, "de-1", now)
            second = infra.who_connects_payload(self.session, "de-1", now)

        self.assertIs(first["geo"], second["geo"])
        lookup.assert_called_once_with("8.8.8.8", database)


class SettingsTests(InfraDbTestCase):
    def test_defaults_and_overrides(self):
        cfg = infra.get_settings(self.session)
        self.assertEqual(cfg["infra_load_threshold_pct"], 85)
        self.assertTrue(cfg["infra_auto_replace_enabled"])

        self.session.add(
            SystemSetting(key="infra_load_threshold_pct", value="90")
        )
        self.session.add(
            SystemSetting(key="infra_auto_replace_enabled", value="false")
        )
        self.session.commit()
        cfg = infra.get_settings(self.session)
        self.assertEqual(cfg["infra_load_threshold_pct"], 90)
        self.assertFalse(cfg["infra_auto_replace_enabled"])

    def test_bad_override_falls_back_to_default(self):
        self.session.add(
            SystemSetting(key="infra_anomaly_traffic_ratio", value="мусор")
        )
        self.session.commit()
        cfg = infra.get_settings(self.session)
        self.assertEqual(cfg["infra_anomaly_traffic_ratio"], 0.35)

    def test_validate_setting(self):
        self.assertEqual(
            infra.validate_setting("infra_load_duration_minutes", "30"), "30"
        )
        with self.assertRaises(infra.InfraError):
            infra.validate_setting("infra_load_duration_minutes", "20")
        with self.assertRaises(infra.InfraError):
            infra.validate_setting("infra_anomaly_traffic_ratio", "1.5")
        with self.assertRaises(infra.InfraError):
            infra.validate_setting("unknown_key", "1")
        self.assertEqual(
            infra.validate_setting("infra_auto_replace_enabled", "true"), "true"
        )


class ComputationTests(InfraDbTestCase):
    def test_utilization_uses_max_of_rx_tx(self):
        # RX=370M, TX=80M при лимите 400 Mbit → 92.5% (не (370+80)/400)
        self.assertEqual(
            infra.utilization_pct(370_000_000, 80_000_000, 400), 92.5
        )
        self.assertIsNone(infra.utilization_pct(1, 1, None))

    def test_effective_bandwidth_prefers_override(self):
        server = self.make_server(bandwidth_limit_mbps=400)
        self.assertEqual(infra.effective_bandwidth_mbps(server), 400)
        server.bandwidth_limit_mbps = None
        self.assertEqual(infra.effective_bandwidth_mbps(server), 10000)

    def test_is_online_threshold(self):
        cfg = infra.get_settings(self.session)
        server = self.make_server()
        self.assertTrue(infra.is_online(server, cfg))
        server.last_seen_at = utcnow() - timedelta(seconds=300)
        self.assertFalse(infra.is_online(server, cfg))


class MutationTests(InfraDbTestCase):
    def test_add_manual_ip_and_duplicates(self):
        server = self.make_server()
        row = infra.add_manual_ip(self.session, server.id, "185.10.0.11", "24")
        self.assertEqual(row.source, "manual")
        self.assertFalse(row.on_interface)
        with self.assertRaises(infra.InfraError):
            infra.add_manual_ip(self.session, server.id, "185.10.0.11", "24")
        with self.assertRaises(infra.InfraError):
            infra.add_manual_ip(self.session, server.id, "не-ip", "24")
        with self.assertRaises(infra.InfraError):
            infra.add_manual_ip(self.session, server.id, "172.17.0.1", "16")

    def test_delete_ip_on_interface_is_forbidden(self):
        server = self.make_server()
        active = self.make_ip(server, "185.10.0.10", on_interface=True)
        reserve = self.make_ip(server, "185.10.0.11", on_interface=False)
        with self.assertRaises(infra.InfraError):
            infra.delete_ip(self.session, server.id, active.id)
        infra.delete_ip(self.session, server.id, reserve.id)
        self.session.commit()
        rows = self.session.query(InfraServerIp).all()
        self.assertEqual([r.ip for r in rows], ["185.10.0.10"])

    def test_domain_validation_and_uniqueness(self):
        server = self.make_server()
        infra.add_domain(self.session, server.id, "De.Example.XYZ")
        row = self.session.query(InfraServerDomain).one()
        self.assertEqual(row.domain, "de.example.xyz")
        # Round-robin: тот же домен можно привязать к другому серверу
        other = self.make_server(machine_uid="m-2")
        infra.add_domain(self.session, other.id, "de.example.xyz")
        self.assertEqual(self.session.query(InfraServerDomain).count(), 2)
        # Но не дважды к одному
        with self.assertRaises(infra.InfraError):
            infra.add_domain(self.session, server.id, "de.example.xyz")
        with self.assertRaises(infra.InfraError):
            infra.add_domain(self.session, server.id, "bad domain")

    def test_ensure_ip_command_is_deduplicated(self):
        server = self.make_server()
        reserve = self.make_ip(server, "185.10.0.11", on_interface=False)
        first = infra.create_ensure_ip_command(
            self.session, server, reserve, "tester"
        )
        second = infra.create_ensure_ip_command(
            self.session, server, reserve, "tester"
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.payload["ip"], "185.10.0.11")
        self.assertEqual(first.payload["interface"], "ens3")

    def test_request_replacement_idempotent(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        first = infra.request_replacement(
            self.session, server.id, "185.10.0.10", "tester"
        )
        second = infra.request_replacement(
            self.session, server.id, "185.10.0.10", "tester"
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.domains, ["de.example.xyz"])
        with self.assertRaises(infra.InfraError):
            infra.request_replacement(self.session, server.id, "9.9.9.9", "t")


class CountryCodeTests(InfraDbTestCase):
    def test_derived_from_node_name_prefix(self):
        server = self.make_server(node_name="de-1")
        self.assertEqual(infra.server_country_code(server), "DE")

    def test_uk_alias_maps_to_gb(self):
        server = self.make_server(node_name="uk-1")
        self.assertEqual(infra.server_country_code(server), "GB")

    def test_non_country_prefix_gives_no_flag(self):
        server = self.make_server(node_name="wl-1")
        self.assertIsNone(infra.server_country_code(server))
        server2 = self.make_server(machine_uid="m-2", node_name="mi.fornex.nl")
        self.assertIsNone(infra.server_country_code(server2))

    def test_explicit_overrides_prefix(self):
        server = self.make_server(node_name="wl-1", country_code="NL")
        self.assertEqual(infra.server_country_code(server), "NL")

    def test_update_server_validates_country(self):
        server = self.make_server()
        infra.update_server(self.session, server.id, {"country_code": "nl"})
        self.assertEqual(server.country_code, "NL")
        infra.update_server(self.session, server.id, {"country_code": "uk"})
        self.assertEqual(server.country_code, "GB")
        infra.update_server(self.session, server.id, {"country_code": ""})
        self.assertIsNone(server.country_code)
        with self.assertRaises(infra.InfraError):
            infra.update_server(self.session, server.id, {"country_code": "XX"})

    def test_payloads_expose_country(self):
        self.make_server(node_name="de-1")
        payload = infra.server_list_payload(self.session)
        self.assertEqual(payload["servers"][0]["country_code"], "DE")


class ForceTspuCheckTests(InfraDbTestCase):
    def test_creates_check_and_starts_run(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")

        def fake_start_run(db, check, api_key, is_public, geo, light):
            run = CensorCheckRun(check_id=check.id, status="pending")
            db.add(run)
            db.flush()
            check.last_started_at = utcnow()
            return run

        with mock.patch(
            "engine.ripe_atlas.start_run", side_effect=fake_start_run
        ), mock.patch(
            "engine.ripe_atlas.resolve_api_key", return_value="key"
        ), mock.patch(
            "engine.ripe_atlas.resolve_public_flag", return_value=False
        ):
            result = infra.force_tspu_check(self.session, server, "test")

        self.assertEqual(len(result["run_ids"]), 1)
        self.assertEqual(result["errors"], [])
        check = self.session.query(CensorCheck).one()
        self.assertEqual(check.target_ip, "185.10.0.10")
        self.assertEqual(check.sni, "de.example.xyz")
        self.assertTrue(check.light_mode)
        self.assertIsNone(check.interval_minutes)

    def test_pending_run_is_reused_without_new_start(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        check = CensorCheck(
            name="n", target_ip="185.10.0.10", sni="x.example", port=443
        )
        self.session.add(check)
        self.session.flush()
        run = CensorCheckRun(check_id=check.id, status="pending")
        self.session.add(run)
        self.session.commit()

        with mock.patch("engine.ripe_atlas.start_run") as start_run:
            result = infra.force_tspu_check(self.session, server, "test")
        start_run.assert_not_called()
        self.assertEqual(result["run_ids"], [run.id])

    def test_no_domains_and_no_checks_reports_error(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        result = infra.force_tspu_check(self.session, server, "test")
        self.assertEqual(result["run_ids"], [])
        self.assertEqual(len(result["errors"]), 1)

    def test_targets_narrowed_to_dns_snapshot(self):
        # Свежий слепок DNS есть: меряем только адреса из A-записей —
        # запасные IP на интерфейсе кредиты не жгут
        now = utcnow()
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=True)  # запасной
        domain = self.make_domain(server, "de.example.xyz")
        domain.last_a_ips = ["185.10.0.10"]
        domain.dns_checked_at = now
        self.session.commit()

        def fake_start_run(db, c, api_key, is_public, geo, light):
            run = CensorCheckRun(check_id=c.id, status="pending")
            db.add(run)
            db.flush()
            return run

        with mock.patch(
            "engine.ripe_atlas.start_run", side_effect=fake_start_run
        ), mock.patch(
            "engine.ripe_atlas.resolve_api_key", return_value="key"
        ), mock.patch(
            "engine.ripe_atlas.resolve_public_flag", return_value=False
        ):
            result = infra.force_tspu_check(self.session, server, "test")
        self.assertEqual(len(result["run_ids"]), 1)
        check = self.session.query(CensorCheck).one()
        self.assertEqual(check.target_ip, "185.10.0.10")

    def test_no_targets_when_actives_not_in_dns(self):
        now = utcnow()
        server = self.make_server()
        self.make_ip(server, "185.10.0.11", on_interface=True)
        domain = self.make_domain(server, "de.example.xyz")
        domain.last_a_ips = ["45.9.9.9"]  # нода выведена из DNS
        domain.dns_checked_at = now
        self.session.commit()
        result = infra.force_tspu_check(self.session, server, "test")
        self.assertEqual(result["run_ids"], [])
        self.assertIn("не в DNS", result["errors"][0])

    def test_private_ips_are_not_tspu_targets(self):
        # docker0/warp-адреса недостижимы извне — замер по ним впустую
        # сжёг бы кредиты Atlas
        server = self.make_server()
        self.make_ip(server, "172.17.0.1", on_interface=True, interface="docker0")
        result = infra.force_tspu_check(self.session, server, "test")
        self.assertEqual(result["run_ids"], [])
        self.assertIn("Нет активных IPv4", result["errors"][0])

    def test_fresh_error_run_is_not_reused(self):
        # Свежий (< 5 мин) прогон со статусом error — не замер: запускаем
        # новый. Старый complete недельной давности тоже не «свежее
        # доказательство» — фильтр по created_at самого прогона.
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        check = CensorCheck(
            name="n", target_ip="185.10.0.10", sni="x.example", port=443,
            last_started_at=utcnow() - timedelta(minutes=1),
        )
        self.session.add(check)
        self.session.flush()
        self.session.add(
            CensorCheckRun(
                check_id=check.id,
                status="complete",
                created_at=utcnow() - timedelta(days=7),
            )
        )
        self.session.add(CensorCheckRun(check_id=check.id, status="error"))
        self.session.commit()

        def fake_start_run(db, c, api_key, is_public, geo, light):
            run = CensorCheckRun(check_id=c.id, status="pending")
            db.add(run)
            db.flush()
            return run

        with mock.patch(
            "engine.ripe_atlas.start_run", side_effect=fake_start_run
        ) as start_run, mock.patch(
            "engine.ripe_atlas.resolve_api_key", return_value="key"
        ), mock.patch(
            "engine.ripe_atlas.resolve_public_flag", return_value=False
        ):
            result = infra.force_tspu_check(self.session, server, "test")
        start_run.assert_called_once()
        self.assertEqual(len(result["run_ids"]), 1)

    def test_targets_capped(self):
        server = self.make_server()
        for i in range(infra.TSPU_MAX_TARGETS + 3):
            self.make_ip(server, f"185.10.0.{10 + i}", on_interface=True)
        self.make_domain(server, "de.example.xyz")

        def fake_start_run(db, c, api_key, is_public, geo, light):
            run = CensorCheckRun(check_id=c.id, status="pending")
            db.add(run)
            db.flush()
            return run

        with mock.patch(
            "engine.ripe_atlas.start_run", side_effect=fake_start_run
        ), mock.patch(
            "engine.ripe_atlas.resolve_api_key", return_value="key"
        ), mock.patch(
            "engine.ripe_atlas.resolve_public_flag", return_value=False
        ):
            result = infra.force_tspu_check(self.session, server, "test")
        self.assertEqual(len(result["run_ids"]), infra.TSPU_MAX_TARGETS)
        self.assertTrue(any("защита кредитов" in e for e in result["errors"]))


class AggregationTests(InfraDbTestCase):
    def test_minute_and_quarter_aggregates_with_volume(self):
        server = self.make_server()
        start = infra._floor_dt(utcnow() - timedelta(minutes=40), 900)
        self.add_samples(server, start, count=6 * 30, step_seconds=10)

        infra.aggregate_telemetry(self.session)
        self.session.commit()

        minutes = (
            self.session.query(InfraTelemetryAgg)
            .filter(InfraTelemetryAgg.bucket_seconds == 60)
            .all()
        )
        self.assertGreaterEqual(len(minutes), 28)
        sample_minute = minutes[0]
        self.assertEqual(sample_minute.rx_bps_avg, 100_000_000)
        self.assertEqual(sample_minute.tx_bps_avg, 20_000_000)
        self.assertEqual(sample_minute.sample_count, 6)
        # Объём за минуту: 100 Mbit/s * 60 c / 8 = 750 MБ
        self.assertEqual(sample_minute.rx_bytes, 750_000_000)

        quarters = (
            self.session.query(InfraTelemetryAgg)
            .filter(InfraTelemetryAgg.bucket_seconds == 900)
            .all()
        )
        self.assertGreaterEqual(len(quarters), 1)
        self.assertEqual(quarters[0].rx_bps_avg, 100_000_000)

    def test_aggregation_is_idempotent(self):
        server = self.make_server()
        start = infra._floor_dt(utcnow() - timedelta(minutes=10), 60)
        self.add_samples(server, start, count=30, step_seconds=10)
        infra.aggregate_telemetry(self.session)
        self.session.commit()
        count_before = self.session.query(InfraTelemetryAgg).count()
        infra.aggregate_telemetry(self.session)
        self.session.commit()
        self.assertEqual(
            self.session.query(InfraTelemetryAgg).count(), count_before
        )

    def test_prune_removes_old_rows(self):
        server = self.make_server()
        old = utcnow() - timedelta(hours=48)
        self.session.add(
            InfraTelemetry(server_id=server.id, ts=old, rx_bps=1, tx_bps=1)
        )
        self.session.add(
            InfraTelemetryAgg(
                server_id=server.id,
                bucket_seconds=60,
                bucket_start=utcnow() - timedelta(days=20),
                sample_count=1,
            )
        )
        self.session.commit()
        infra.prune_telemetry(self.session)
        self.session.commit()
        self.assertEqual(self.session.query(InfraTelemetry).count(), 0)
        self.assertEqual(self.session.query(InfraTelemetryAgg).count(), 0)


class BaselineAnomalyTests(InfraDbTestCase):
    def seed_baseline(self, server, now, days=7, traffic=600_000_000, conns=5000):
        for day in range(1, days + 1):
            center = now - timedelta(days=day)
            for minute_offset in range(-30, 31, 5):
                self.session.add(
                    InfraTelemetryAgg(
                        server_id=server.id,
                        bucket_seconds=60,
                        bucket_start=center + timedelta(minutes=minute_offset),
                        rx_bps_avg=int(traffic * 0.85),
                        tx_bps_avg=int(traffic * 0.15),
                        tcp_avg=conns,
                        sample_count=6,
                    )
                )
        self.session.commit()

    def test_baseline_median(self):
        server = self.make_server()
        now = utcnow()
        self.seed_baseline(server, now)
        cfg = infra.get_settings(self.session)
        baseline = infra.compute_baseline(self.session, server.id, now, cfg)
        self.assertIsNotNone(baseline)
        self.assertAlmostEqual(
            baseline["traffic_bps"], 600_000_000, delta=2_000_000
        )
        self.assertEqual(baseline["connections"], 5000)

    def test_baseline_requires_history(self):
        server = self.make_server()
        now = utcnow()
        self.seed_baseline(server, now, days=2)
        cfg = infra.get_settings(self.session)
        self.assertIsNone(
            infra.compute_baseline(self.session, server.id, now, cfg)
        )

    def test_evaluate_network_drop(self):
        cfg = infra.get_settings(self.session)
        baseline = {"traffic_bps": 700_000_000, "connections": 6200}
        minutes = [
            {"minute": None, "traffic_bps": 180_000_000, "connections": 1450}
            for _ in range(5)
        ]
        verdict = infra.evaluate_network_drop(minutes, baseline, cfg)
        self.assertIsNotNone(verdict)
        self.assertAlmostEqual(verdict["traffic_ratio"], 0.257, places=2)
        self.assertAlmostEqual(verdict["connection_ratio"], 0.234, places=2)

    def test_evaluate_requires_both_ratios_low(self):
        cfg = infra.get_settings(self.session)
        baseline = {"traffic_bps": 700_000_000, "connections": 6200}
        # Трафик упал, но соединения в норме — не аномалия (например, ночь)
        minutes = [
            {"minute": None, "traffic_bps": 100_000_000, "connections": 6000}
            for _ in range(5)
        ]
        self.assertIsNone(infra.evaluate_network_drop(minutes, baseline, cfg))

    def test_min_baseline_floor_checked_against_unscaled(self):
        cfg = infra.get_settings(self.session)
        # Масштабированная норма 15 Mbit < порога 20, но НЕмасштабированная
        # (floor) 45 Mbit — детекция обязана работать: бан ловится и после
        # дробления DNS на мелком сервере
        baseline = {
            "traffic_bps": 15_000_000,
            "connections": 500,
            "floor_traffic_bps": 45_000_000,
        }
        minutes = [
            {"minute": None, "traffic_bps": 1_000_000, "connections": 30}
            for _ in range(5)
        ]
        verdict = infra.evaluate_network_drop(minutes, baseline, cfg)
        self.assertIsNotNone(verdict)

    def test_evaluate_skips_small_baseline_and_gaps(self):
        cfg = infra.get_settings(self.session)
        small = {"traffic_bps": 5_000_000, "connections": 100}
        minutes = [
            {"minute": None, "traffic_bps": 100, "connections": 1}
            for _ in range(5)
        ]
        self.assertIsNone(infra.evaluate_network_drop(minutes, small, cfg))
        baseline = {"traffic_bps": 700_000_000, "connections": 6200}
        # Пропуски телеметрии: меньше точек, чем нужно окну
        self.assertIsNone(
            infra.evaluate_network_drop(minutes[:2], baseline, cfg)
        )


class CheckXrayWorkerTests(InfraDbTestCase):
    def test_unknown_health_is_silent(self):
        # Старый агент / нет pid: host — все поля NULL, никаких алертов
        self.make_server()
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.check_xray(self.session)
        alert.assert_not_called()

    def test_down_needs_confirmation_before_alert(self):
        server = self.make_server(xray_process_running=False)
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_xray(self.session)
        # Первый тик только фиксирует начало инцидента
        alert.assert_not_called()
        self.assertIsNotNone(server.xray_down_since)

        server.xray_down_since = utcnow() - timedelta(minutes=5)
        self.session.commit()
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_xray(self.session)
        alert.assert_called_once()
        self.assertIn("XRAY не работает", alert.call_args[0][0])
        self.assertIsNotNone(server.xray_down_alerted_at)

        # Инцидент уже заалерчен — повторов нет
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.check_xray(self.session)
        alert.assert_not_called()

    def test_crash_loop_alerts_even_with_running_process(self):
        server = self.make_server(
            xray_process_running=True,
            xray_crash_loop=True,
            xray_down_since=utcnow() - timedelta(minutes=5),
        )
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_xray(self.session)
        alert.assert_called_once()
        self.assertIn("crash-loop", alert.call_args[0][0])
        self.assertIsNotNone(server.xray_down_alerted_at)

    def test_recovery_alert(self):
        server = self.make_server(
            xray_process_running=True,
            xray_process_uptime_seconds=300,
            xray_down_alerted_at=utcnow() - timedelta(minutes=10),
        )
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_xray(self.session)
        alert.assert_called_once()
        self.assertIn("снова работает", alert.call_args[0][0])
        self.assertIsNotNone(server.xray_recovered_alerted_at)
        self.assertIsNone(server.xray_down_since)

    def test_offline_server_is_ignored(self):
        server = self.make_server(
            last_seen_at=utcnow() - timedelta(hours=2),
            xray_process_running=False,
            xray_down_since=utcnow() - timedelta(minutes=30),
        )
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.check_xray(self.session)
        alert.assert_not_called()
        # Оффлайн покрывается своим алертом; xray-инцидент сбрасывается
        self.assertIsNone(server.xray_down_since)


class ManualDiagnosisTests(InfraDbTestCase):
    def test_manual_diagnosis_creates_anomaly(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        with mock.patch.object(
            infra, "start_ip_diagnosis",
            return_value={"runs": {"185.10.0.10|ya.ru": 7}, "errors": [],
                          "control_name": "ya.ru"},
        ):
            result = infra.start_manual_diagnosis(
                self.session, server, actor="tester"
            )
        self.session.commit()

        self.assertIsNotNone(result["anomaly_id"])
        anomaly = self.session.query(InfraAnomaly).one()
        self.assertEqual(anomaly.kind, "manual_check")
        self.assertEqual(anomaly.status, "checking")
        # Дальше сработает обычный конвейер: фаза имён и классификация
        self.assertEqual(anomaly.details["ip_runs"], {"185.10.0.10|ya.ru": 7})
        self.assertEqual(anomaly.details["control_name"], "ya.ru")

    def test_manual_diagnosis_does_not_duplicate_running_one(self):
        server = self.make_server()
        self.session.add(
            InfraAnomaly(
                server_id=server.id, status="checking",
                details={"ip_runs": {"a|b": 1}}, created_at=utcnow(),
            )
        )
        self.session.commit()

        with mock.patch.object(infra, "start_ip_diagnosis") as started:
            result = infra.start_manual_diagnosis(self.session, server)
        # Параллельные диагностики одного сервера только жгли бы кредиты
        started.assert_not_called()
        self.assertEqual(result["runs"], {})
        self.assertEqual(self.session.query(InfraAnomaly).count(), 1)

    def test_manual_diagnosis_without_probes_creates_nothing(self):
        server = self.make_server()
        with mock.patch.object(
            infra, "start_ip_diagnosis",
            return_value={"runs": {}, "errors": ["Нет активных публичных IPv4"],
                          "control_name": "ya.ru"},
        ):
            result = infra.start_manual_diagnosis(self.session, server)
        self.assertIsNone(result["anomaly_id"])
        self.assertEqual(self.session.query(InfraAnomaly).count(), 0)


class CapacityTests(InfraDbTestCase):
    CRIT = {
        "conntrack": {
            "count": 65536, "max": 65536, "usage_pct": 100,
            "insert_failed_delta": 42,
        },
        "nginx": {
            "worker_connections": 1024,
            "recent_errors": ["accept4() failed"],
            "workers": [
                {"pid": 1, "fd": 1000, "nofile_soft": 1024,
                 "nofile_hard": 524288},
            ],
        },
    }
    WARN = {
        "conntrack": {
            "count": 180000, "max": 262144, "usage_pct": 69,
            "insert_failed_delta": 0,
        },
        "nginx": {"worker_connections": 65535, "recent_errors": [],
                  "workers": []},
    }

    def test_evaluate_capacity_levels(self):
        crit = infra.evaluate_capacity(self.CRIT, 70)
        self.assertEqual(crit["level"], "crit")
        joined = " ".join(crit["problems"])
        self.assertIn("conntrack переполнен", joined)
        self.assertIn("accept4() failed", joined)
        # Фактический лимит процесса ниже возможного — отдельный диагноз
        self.assertIn("лимитом дескрипторов", joined)

        warn = infra.evaluate_capacity(
            dict(self.WARN, conntrack=dict(self.WARN["conntrack"],
                                           usage_pct=85)),
            70,
        )
        self.assertEqual(warn["level"], "warn")

        self.assertEqual(infra.evaluate_capacity(self.WARN, 70)["level"], "ok")
        # Нет данных (агент старой версии) — не выдумываем проблем
        self.assertEqual(infra.evaluate_capacity(None, 70)["level"], "ok")

    def test_single_insert_failure_is_not_overflow(self):
        # Реальный случай с ноды: таблица заполнена на 19%, одна неудавшаяся
        # вставка за интервал. Это гонка при создании записи, а не
        # переполнение — тревожить нельзя, иначе ложный crit заблокирует
        # изменения DNS и реальный бан останется необработанным
        verdict = infra.evaluate_capacity(
            {
                "conntrack": {
                    "count": 49711, "max": 262144, "usage_pct": 19,
                    "insert_failed_delta": 1,
                },
            },
            70,
        )
        self.assertEqual(verdict["level"], "ok")
        self.assertEqual(verdict["problems"], [])

    def test_overflow_requires_full_table(self):
        # Отказы вставки считаются переполнением только у заполненной таблицы
        full = infra.evaluate_capacity(
            {
                "conntrack": {
                    "count": 260000, "max": 262144, "usage_pct": 99,
                    "insert_failed_delta": 3,
                },
            },
            70,
        )
        self.assertEqual(full["level"], "crit")
        self.assertIn("переполнен", " ".join(full["problems"]))

        # Много отказов при свободной таблице — это тоже повод посмотреть,
        # но причина не в размере, и уровень другой
        noisy = infra.evaluate_capacity(
            {
                "conntrack": {
                    "count": 1000, "max": 262144, "usage_pct": 1,
                    "insert_failed_delta": 500,
                },
            },
            70,
        )
        self.assertEqual(noisy["level"], "warn")
        self.assertIn("переполнением это не объясняется",
                      " ".join(noisy["problems"]))

    def test_record_pluralization(self):
        # «не удалось создать 1 записей» режет глаз в алерте
        for count, expected in ((1, "1 запись"), (3, "3 записи"),
                                (11, "11 записей"), (25, "25 записей")):
            verdict = infra.evaluate_capacity(
                {
                    "conntrack": {
                        "count": 260000, "max": 262144, "usage_pct": 99,
                        "insert_failed_delta": count,
                    },
                },
                70,
            )
            self.assertIn(expected, " ".join(verdict["problems"]))

    def test_descriptor_advice_depends_on_config(self):
        workers = [{"pid": 1, "fd": 50, "nofile_soft": 1024,
                    "nofile_hard": 524288}]
        # Директива не задана: перезапуск не поможет, нужно задать её
        missing = infra.evaluate_capacity(
            {"nginx": {"worker_connections": 65535, "recent_errors": [],
                       "workers": workers}},
            70,
        )
        self.assertIn("не задана", " ".join(missing["problems"]))

        # Директива задана, но процессы её не подхватили — вот тут перезапуск
        stale = infra.evaluate_capacity(
            {"nginx": {"worker_connections": 65535,
                       "worker_rlimit_nofile": 262144,
                       "recent_errors": [], "workers": workers}},
            70,
        )
        self.assertIn("нужен перезапуск", " ".join(stale["problems"]))

    def test_crit_capacity_alerts_once_per_cooldown(self):
        server = self.make_server(capacity=self.CRIT)
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_capacity(self.session)
        alert.assert_called_once()
        self.assertIn("упёрлась в лимиты", alert.call_args[0][0])
        self.assertIsNotNone(server.capacity_crit_alerted_at)

        # Повтор в пределах кулдауна не шлётся
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.check_capacity(self.session)
        alert.assert_not_called()

    def test_warn_capacity_is_preventive(self):
        capacity = dict(
            self.WARN,
            conntrack=dict(self.WARN["conntrack"], usage_pct=85),
        )
        server = self.make_server(capacity=capacity)
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_capacity(self.session)
        alert.assert_called_once()
        text = alert.call_args[0][0]
        self.assertIn("Пора поднять лимиты", text)
        self.assertIn("клиенты не затронуты", text)
        self.assertIsNotNone(server.capacity_warn_alerted_at)

    def test_recovery_clears_alert_state(self):
        server = self.make_server(
            capacity=self.WARN,
            capacity_warn_alerted_at=utcnow() - timedelta(hours=1),
        )
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.check_capacity(self.session)
        alert.assert_not_called()
        self.assertIsNone(server.capacity_warn_alerted_at)

    def test_offline_server_is_skipped(self):
        self.make_server(
            last_seen_at=utcnow() - timedelta(hours=2), capacity=self.CRIT
        )
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.check_capacity(self.session)
        alert.assert_not_called()


class DetectAnomalyWorkerTests(InfraDbTestCase):
    def seed_drop(self, server, now):
        # 5 минут сырых сэмплов с обвалом
        start = infra._floor_dt(now - timedelta(minutes=5), 60)
        self.add_samples(
            server, start, count=30, step_seconds=10,
            rx_bps=150_000_000, tx_bps=30_000_000, tcp_connections=1400,
        )

    def test_detects_and_forces_tspu(self):
        now = utcnow()
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        BaselineAnomalyTests.seed_baseline(self, server, now)
        self.seed_drop(server, now)

        with mock.patch.object(
            infra, "start_ip_diagnosis",
            return_value={"runs": {"185.10.0.10|ya.ru": 7}, "errors": [],
                          "control_name": "ya.ru"},
        ) as forced, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            infra_worker.detect_anomalies(self.session)
        self.session.commit()

        forced.assert_called_once()
        anomaly = self.session.query(InfraAnomaly).one()
        self.assertEqual(anomaly.status, "checking")
        self.assertEqual(anomaly.censor_run_ids, [7])
        self.assertLess(float(anomaly.traffic_ratio), 0.35)

    def test_suppressed_after_agent_restart(self):
        now = utcnow()
        server = self.make_server(agent_started_at=now - timedelta(minutes=3))
        BaselineAnomalyTests.seed_baseline(self, server, now)
        self.seed_drop(server, now)
        with mock.patch.object(infra, "start_ip_diagnosis") as forced:
            infra_worker.detect_anomalies(self.session)
        forced.assert_not_called()
        self.assertEqual(self.session.query(InfraAnomaly).count(), 0)

    def test_suppressed_during_warmup(self):
        now = utcnow()
        server = self.make_server(first_seen_at=now - timedelta(hours=10))
        BaselineAnomalyTests.seed_baseline(self, server, now)
        self.seed_drop(server, now)
        with mock.patch.object(infra, "start_ip_diagnosis") as forced:
            infra_worker.detect_anomalies(self.session)
        forced.assert_not_called()

    def test_suppressed_while_snoozed(self):
        # Пауза детектора (ручная или от DNS-вотчера) глушит срабатывание
        now = utcnow()
        server = self.make_server(
            anomaly_suppressed_until=now + timedelta(hours=10)
        )
        BaselineAnomalyTests.seed_baseline(self, server, now)
        self.seed_drop(server, now)
        with mock.patch.object(infra, "start_ip_diagnosis") as forced:
            infra_worker.detect_anomalies(self.session)
        forced.assert_not_called()
        self.assertEqual(self.session.query(InfraAnomaly).count(), 0)

    def test_suppressed_while_xray_down(self):
        # Мёртвый xray роняет трафик так же, как бан, но замер бессмысленен:
        # об этом алертит check_xray, детектор молчит до восстановления
        now = utcnow()
        server = self.make_server(xray_process_running=False)
        BaselineAnomalyTests.seed_baseline(self, server, now)
        self.seed_drop(server, now)
        with mock.patch.object(infra, "start_ip_diagnosis") as forced:
            infra_worker.detect_anomalies(self.session)
        forced.assert_not_called()
        self.assertEqual(self.session.query(InfraAnomaly).count(), 0)

    def test_suppressed_while_xray_crash_loop(self):
        now = utcnow()
        server = self.make_server(
            xray_process_running=True, xray_crash_loop=True
        )
        BaselineAnomalyTests.seed_baseline(self, server, now)
        self.seed_drop(server, now)
        with mock.patch.object(infra, "start_ip_diagnosis") as forced:
            infra_worker.detect_anomalies(self.session)
        forced.assert_not_called()
        self.assertEqual(self.session.query(InfraAnomaly).count(), 0)

    def test_server_drained_from_dns_is_skipped(self):
        # Ноду вывели из DNS (слепок доменов её IP не содержит): обвал
        # трафика — намеренный слив, детектор молчит и замеры не жжёт
        now = utcnow()
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        domain = self.make_domain(server, "de.example.xyz")
        domain.last_a_ips = ["45.9.9.9"]  # трафик уехал на другую ноду
        domain.dns_checked_at = now
        self.session.commit()
        BaselineAnomalyTests.seed_baseline(self, server, now)
        self.seed_drop(server, now)
        with mock.patch.object(infra, "start_ip_diagnosis") as forced:
            infra_worker.detect_anomalies(self.session)
        forced.assert_not_called()
        self.assertEqual(self.session.query(InfraAnomaly).count(), 0)

    def test_stale_dns_snapshot_falls_back_to_active(self):
        # Слепок протух (вотчер умер/Cloudflare отключили) — по устаревшим
        # данным детектор НЕ выключаем
        now = utcnow()
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        domain = self.session.query(InfraServerDomain).one()
        domain.last_a_ips = ["45.9.9.9"]  # IP сервера отсутствует
        domain.dns_checked_at = now - timedelta(days=3)  # но слепок старый
        self.session.commit()
        self.assertTrue(infra.server_is_in_dns(self.session, server))
        domain.dns_checked_at = now  # свежий слепок — решение принимается
        self.session.commit()
        self.assertFalse(infra.server_is_in_dns(self.session, server))

    def test_server_present_in_dns_snapshot_fires(self):
        now = utcnow()
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        domain = self.make_domain(server, "de.example.xyz")
        domain.last_a_ips = ["185.10.0.10", "45.9.9.9"]
        domain.dns_checked_at = now
        self.session.commit()
        BaselineAnomalyTests.seed_baseline(self, server, now)
        self.seed_drop(server, now)
        with mock.patch.object(
            infra, "start_ip_diagnosis",
            return_value={"runs": {"185.10.0.10|ya.ru": 7}, "errors": [],
                          "control_name": "ya.ru"},
        ) as forced, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            infra_worker.detect_anomalies(self.session)
        forced.assert_called_once()

    def test_blocked_only_server_is_skipped(self):
        # После dns_cleanup: единственный IP заблокирован и вычищен из DNS —
        # детектор не спамит алертами «замер не запустился» каждый кулдаун
        now = utcnow()
        server = self.make_server()
        self.make_ip(
            server, "185.10.0.10", on_interface=True, blocked_at=now
        )
        domain = self.make_domain(server, "de.example.xyz")
        domain.last_a_ips = ["45.9.9.9"]
        domain.dns_checked_at = now
        self.session.commit()
        BaselineAnomalyTests.seed_baseline(self, server, now)
        self.seed_drop(server, now)
        with mock.patch.object(infra, "start_ip_diagnosis") as forced:
            infra_worker.detect_anomalies(self.session)
        forced.assert_not_called()

    def test_dns_rebalance_scale_prevents_false_positive(self):
        # Разгрузка через вторую A-запись: трафик и соединения на ~25% от
        # старой нормы (дробление на 4 ноды) — при scale 0.25 это 100% новой
        # нормы, тревоги нет
        now = utcnow()
        server = self.make_server(
            baseline_scale=0.25,
            baseline_scale_until=now + timedelta(days=5),
        )
        BaselineAnomalyTests.seed_baseline(self, server, now)  # норма 600M/5000
        start = infra._floor_dt(now - timedelta(minutes=5), 60)
        self.add_samples(
            server, start, count=30, step_seconds=10,
            rx_bps=130_000_000, tx_bps=25_000_000, tcp_connections=1250,
        )
        with mock.patch.object(infra, "start_ip_diagnosis") as forced:
            infra_worker.detect_anomalies(self.session)
        forced.assert_not_called()
        self.assertEqual(self.session.query(InfraAnomaly).count(), 0)

    def test_real_ban_detected_even_with_scale(self):
        # Даже при активной перекалибровке (x0.5) реальный бан — обвал почти
        # к нулю — детектируется: защита не отключается
        now = utcnow()
        server = self.make_server(
            baseline_scale=0.5,
            baseline_scale_until=now + timedelta(days=5),
        )
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        BaselineAnomalyTests.seed_baseline(self, server, now)
        start = infra._floor_dt(now - timedelta(minutes=5), 60)
        self.add_samples(
            server, start, count=30, step_seconds=10,
            rx_bps=25_000_000, tx_bps=5_000_000, tcp_connections=250,
        )
        with mock.patch.object(
            infra, "start_ip_diagnosis",
            return_value={"runs": {"185.10.0.10|ya.ru": 7}, "errors": [],
                          "control_name": "ya.ru"},
        ) as forced, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            infra_worker.detect_anomalies(self.session)
        self.session.commit()
        forced.assert_called_once()
        self.assertEqual(self.session.query(InfraAnomaly).count(), 1)

    def test_no_anomaly_without_drop(self):
        now = utcnow()
        server = self.make_server()
        BaselineAnomalyTests.seed_baseline(self, server, now)
        start = infra._floor_dt(now - timedelta(minutes=5), 60)
        self.add_samples(
            server, start, count=30, step_seconds=10,
            rx_bps=550_000_000, tx_bps=90_000_000, tcp_connections=4900,
        )
        with mock.patch.object(infra, "start_ip_diagnosis") as forced:
            infra_worker.detect_anomalies(self.session)
        forced.assert_not_called()


class ProcessAnomalyTests(InfraDbTestCase):
    CONTROL = "ya.ru"

    def make_anomaly(self, server, ip_runs, sni_runs=None, status="checking"):
        """Аномалия в формате двухфазной диагностики.

        ip_runs/sni_runs — {"адрес|имя": run_id}: по ним классификатор
        отличает пробы адресов (контрольным именем) от проб имён.
        """
        details = {
            "control_name": self.CONTROL,
            "ip_runs": ip_runs,
        }
        if sni_runs:
            details["sni_runs"] = sni_runs
        run_ids = list(ip_runs.values()) + list((sni_runs or {}).values())
        anomaly = InfraAnomaly(
            server_id=server.id,
            status=status,
            censor_run_ids=run_ids,
            details=details,
            created_at=utcnow(),
        )
        self.session.add(anomaly)
        self.session.commit()
        return anomaly

    def make_run(
        self, target_ip, ok_probes, blocked_probes, status="complete",
        sni=None, stage="tls_fail",
    ):
        check = CensorCheck(
            name="c", target_ip=target_ip, sni=sni or self.CONTROL, port=443
        )
        self.session.add(check)
        self.session.flush()
        results = [{"prb_id": i, "ok": True, "stage": "tls_ok"}
                   for i in range(ok_probes)]
        results += [{"prb_id": 1000 + i, "ok": False, "stage": stage}
                    for i in range(blocked_probes)]
        run = CensorCheckRun(
            check_id=check.id,
            status=status,
            total_probes=ok_probes + blocked_probes,
            ok_probes=ok_probes,
            blocked_probes=blocked_probes,
            results=results,
        )
        self.session.add(run)
        self.session.commit()
        return run

    def test_confirmed_block_creates_replacement(self):
        # Адрес не проходит с контрольным именем — забанен именно адрес
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        ip_run = self.make_run("185.10.0.10", ok_probes=2, blocked_probes=18)
        sni_run = self.make_run(
            "185.10.0.11", ok_probes=18, blocked_probes=2,
            sni="de.example.xyz",
        )
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": ip_run.id},
            {"185.10.0.11|de.example.xyz": sni_run.id},
            status="checking_sni",
        )

        with mock.patch.object(infra_worker, "_send_alert", return_value=True):
            infra_worker.process_anomalies(self.session)
        self.session.commit()

        self.session.refresh(anomaly)
        self.assertEqual(anomaly.status, "confirmed")
        replacement = self.session.query(InfraIpReplacement).one()
        self.assertEqual(replacement.old_ip, "185.10.0.10")
        self.assertEqual(replacement.status, "pending")
        self.assertEqual(replacement.anomaly_id, anomaly.id)

    def test_sni_block_does_not_touch_dns(self):
        # Адрес жив, имя под фильтром: замена адреса запрещена, уходит алерт
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        ip_run = self.make_run("185.10.0.10", ok_probes=18, blocked_probes=2)
        sni_run = self.make_run(
            "185.10.0.10", ok_probes=1, blocked_probes=19,
            sni="de.example.xyz",
        )
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": ip_run.id},
            {"185.10.0.10|de.example.xyz": sni_run.id},
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        self.assertEqual(anomaly.status, "confirmed")
        self.assertEqual(anomaly.details["verdict"]["blocked_snis"],
                         ["de.example.xyz"])
        self.assertEqual(anomaly.details["verdict"]["blocked_ips"], [])
        # Замена НЕ создана: менять адрес при бане имени бессмысленно
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)
        text = alert.call_args[0][0]
        self.assertIn("заблокировано имя", text)
        self.assertIn("не менялись намеренно", text)
        # Алерт объясняет, какими проверками это установлено
        self.assertIn("Как это выяснено", text)

    def test_both_bans_replace_only_clean_domains(self):
        # Сценарий инцидента: забанены и адрес, и одно из имён
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_domain(server, "clean.example.xyz")
        self.make_domain(server, "burned.example.xyz")
        dead_run = self.make_run(
            "185.10.0.10", ok_probes=0, blocked_probes=20, stage="tcp_fail"
        )
        live_run = self.make_run("185.10.0.20", ok_probes=19, blocked_probes=1)
        clean_run = self.make_run(
            "185.10.0.20", ok_probes=18, blocked_probes=2,
            sni="clean.example.xyz",
        )
        burned_run = self.make_run(
            "185.10.0.20", ok_probes=0, blocked_probes=20,
            sni="burned.example.xyz",
        )
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": dead_run.id, "185.10.0.20|ya.ru": live_run.id},
            {
                "185.10.0.20|clean.example.xyz": clean_run.id,
                "185.10.0.20|burned.example.xyz": burned_run.id,
            },
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        verdict = anomaly.details["verdict"]
        self.assertEqual(verdict["blocked_ips"], ["185.10.0.10"])
        self.assertEqual(verdict["blocked_snis"], ["burned.example.xyz"])
        # Адрес меняется, но ТОЛЬКО в домене с чистым именем
        replacement = self.session.query(InfraIpReplacement).one()
        self.assertEqual(replacement.old_ip, "185.10.0.10")
        self.assertEqual(replacement.domains, ["clean.example.xyz"])
        # И отдельно предупреждение про имя
        self.assertTrue(
            any("заблокировано имя" in call[0][0]
                for call in alert.call_args_list)
        )

    def test_all_names_blocked_skips_replacement(self):
        # Все имена сервера под фильтром: переводить клиентов некуда
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_domain(server, "one.example.xyz")
        dead_run = self.make_run(
            "185.10.0.10", ok_probes=0, blocked_probes=20, stage="tcp_fail"
        )
        live_run = self.make_run("185.10.0.20", ok_probes=19, blocked_probes=1)
        burned_run = self.make_run(
            "185.10.0.20", ok_probes=0, blocked_probes=20,
            sni="one.example.xyz",
        )
        self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": dead_run.id, "185.10.0.20|ya.ru": live_run.id},
            {"185.10.0.20|one.example.xyz": burned_run.id},
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()

        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)
        self.assertTrue(
            any("все имена сервера" in call[0][0]
                for call in alert.call_args_list)
        )

    def test_control_name_burned_blocks_all_actions(self):
        # Контроль не проходит нигде — виновато контрольное имя, не адреса
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        run_a = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        run_b = self.make_run("185.10.0.20", ok_probes=1, blocked_probes=19)
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": run_a.id, "185.10.0.20|ya.ru": run_b.id},
        )

        with mock.patch.object(infra_worker, "_send_alert", return_value=True):
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        # Ни одной замены: сначала нужно сменить контрольное имя
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)
        self.assertEqual(anomaly.details["verdict"]["blocked_ips"], [])

    def test_tspu_ok_dismisses_anomaly(self):
        server = self.make_server()
        run = self.make_run("185.10.0.10", ok_probes=20, blocked_probes=0)
        anomaly = self.make_anomaly(
            server, {"185.10.0.10|ya.ru": run.id}, status="checking_sni"
        )
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)
        self.assertEqual(anomaly.status, "dismissed")
        alert.assert_not_called()
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)

    def test_auto_replace_disabled_only_alerts(self):
        self.session.add(
            SystemSetting(key="infra_auto_replace_enabled", value="false")
        )
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        run = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        self.make_anomaly(
            server, {"185.10.0.10|ya.ru": run.id}, status="checking_sni"
        )
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        alert.assert_called_once()
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)


class ReplacementFlowTests(InfraDbTestCase):
    def setUp(self):
        super().setUp()
        # Проверка кандидата ходит в RIPE Atlas; в тестах подменяем её
        # созданием прогона, чтобы гонять state machine без сети
        def fake_probes(db, pairs, probe_ids=None, reason=""):
            runs = {}
            for target_ip, sni in pairs:
                check = CensorCheck(
                    name="verify", target_ip=target_ip, sni=sni, port=443
                )
                db.add(check)
                db.flush()
                run = CensorCheckRun(check_id=check.id, status="pending")
                db.add(run)
                db.flush()
                runs[f"{target_ip}|{sni}"] = run.id
            return {"runs": runs, "errors": []}

        patcher = mock.patch.object(
            infra, "start_diagnosis_probes", side_effect=fake_probes
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def complete_verification(self, replacement, ok=True):
        """Отмечает проверку кандидата завершённой с нужным исходом."""
        run = self.session.get(CensorCheckRun, replacement.verify_run_id)
        run.status = "complete"
        run.total_probes = 12
        run.ok_probes = 11 if ok else 1
        run.results = [
            {"prb_id": 1, "ok": ok, "stage": "tls_ok" if ok else "tcp_fail"}
        ]
        self.session.commit()

    def cloudflare_mocks(self, old_ip="185.10.0.10"):
        """Контекст с подменённым Cloudflare: DNS в тестах не трогаем."""
        records = {"de.example.xyz": [{"id": "r1", "content": old_ip}]}
        return (
            mock.patch("engine.cloudflare_dns.is_enabled", return_value=True),
            mock.patch(
                "engine.cloudflare_dns.list_a_records",
                side_effect=lambda domain: records.get(domain, []),
            ),
            mock.patch(
                "engine.cloudflare_dns.ensure_a_record", return_value=True
            ),
            mock.patch("engine.cloudflare_dns.delete_a_records", return_value=1),
        )

    def test_blocked_candidate_is_skipped_and_next_one_tried(self):
        # Кандидат сам оказался под фильтром: в DNS он не публикуется,
        # помечается заблокированным, и перебирается следующий резерв
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(
            server, "185.10.0.11", on_interface=False, source="manual",
            interface=None,
        )
        self.make_ip(
            server, "185.10.0.12", on_interface=False, source="manual",
            interface=None,
        )
        replacement = self.make_pending(server)

        cf1, cf2, cf3, cf4 = self.cloudflare_mocks()
        with cf1, cf2, cf3, cf4, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)
            first_candidate = replacement.new_ip
            self.assertIn(first_candidate, ("185.10.0.11", "185.10.0.12"))

            # агент поднял адрес на интерфейсе
            command = self.session.query(InfraAgentCommand).first()
            command.status = "ok"
            self.session.commit()
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "verifying")

            # проверка показала, что кандидат заблокирован
            self.complete_verification(replacement, ok=False)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)

        # Заблокированный кандидат не попал в DNS и помечен
        blocked_row = (
            self.session.query(InfraServerIp)
            .filter(InfraServerIp.ip == first_candidate)
            .one()
        )
        self.assertIsNotNone(blocked_row.blocked_at)
        # Замена продолжается со следующим резервом, а не падает
        self.assertIn(replacement.status, ("pending", "installing"))
        self.assertNotEqual(replacement.new_ip, first_candidate)
        # В журнале объяснено, почему кандидат забракован
        journal = " ".join(item["message"] for item in replacement.log or [])
        self.assertIn("ЗАБЛОКИРОВАН", journal)
        self.assertIn("пробуем следующий резерв", journal)

    def test_replacement_not_confirmed_reports_failure(self):
        # Адрес заменён, но клиентская связка всё равно не работает —
        # это не успех, и алерт обязан это сказать
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(
            server, "185.10.0.11", on_interface=False, source="manual",
            interface=None,
        )
        replacement = self.make_pending(server)

        cf1, cf2, cf3, cf4 = self.cloudflare_mocks()
        with cf1, cf2, cf3, cf4, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_replacements(self.session)
            self.session.commit()
            command = self.session.query(InfraAgentCommand).one()
            command.status = "ok"
            self.session.commit()

            infra_worker.process_replacements(self.session)   # -> verifying
            self.session.commit()
            self.session.refresh(replacement)
            self.complete_verification(replacement, ok=True)

            infra_worker.process_replacements(self.session)   # -> dns_add
            self.session.commit()
            infra_worker.process_replacements(self.session)   # -> dns_remove
            self.session.commit()
            infra_worker.process_replacements(self.session)   # -> confirming
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "confirming")

            # постпроверка показала, что связка не работает
            self.complete_verification(replacement, ok=False)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)

        self.assertEqual(replacement.status, "done")
        text = alert.call_args[0][0]
        self.assertIn("проблема не решена", text)
        self.assertIn("не только в адресе", text)
        journal = " ".join(item["message"] for item in replacement.log or [])
        self.assertIn("НЕ работает", journal)

    def test_verification_failure_does_not_block_replacement(self):
        # Проверку провести нечем (нет контрольного имени) — замена идёт
        # прежним путём, но в журнале остаётся след
        self.session.add(
            SystemSetting(key="infra_control_names", value="")
        )
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(
            server, "185.10.0.11", on_interface=False, source="manual",
            interface=None,
        )
        replacement = self.make_pending(server)

        cf1, cf2, cf3, cf4 = self.cloudflare_mocks()
        with cf1, cf2, cf3, cf4, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            infra_worker.process_replacements(self.session)
            self.session.commit()
            command = self.session.query(InfraAgentCommand).one()
            command.status = "ok"
            self.session.commit()
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)

        self.assertEqual(replacement.status, "dns_add")
        journal = " ".join(item["message"] for item in replacement.log or [])
        self.assertIn("без проверки", journal)

    def make_pending(self, server, old_ip="185.10.0.10", domains=None):
        replacement = InfraIpReplacement(
            server_id=server.id,
            old_ip=old_ip,
            domains=domains if domains is not None else ["de.example.xyz"],
            status="pending",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self.session.add(replacement)
        self.session.commit()
        return replacement

    def test_full_happy_path(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(
            server, "185.10.0.11", source="manual", on_interface=False,
            interface=None,
        )
        replacement = self.make_pending(server)

        cf_records = {"de.example.xyz": [{"id": "r1", "content": "185.10.0.10"}]}
        added, removed = [], []

        def fake_list(domain):
            return cf_records[domain]

        def fake_ensure(domain, ip, ttl=60):
            added.append((domain, ip))
            return True

        def fake_delete(domain, ip):
            removed.append((domain, ip))
            return 1

        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records", side_effect=fake_list
        ), mock.patch(
            "engine.cloudflare_dns.ensure_a_record", side_effect=fake_ensure
        ), mock.patch(
            "engine.cloudflare_dns.delete_a_records", side_effect=fake_delete
        ), mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            # pending -> installing (команда агенту)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "installing")
            self.assertEqual(replacement.new_ip, "185.10.0.11")
            command = self.session.query(InfraAgentCommand).one()
            self.assertEqual(command.payload["ip"], "185.10.0.11")
            self.assertEqual(command.payload["interface"], "ens3")

            # агент выполнил команду
            command.status = "ok"
            self.session.commit()

            # installing -> verifying: кандидат проверяется ДО публикации
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "verifying")

            # проверка показала, что кандидат чист -> dns_add
            self.complete_verification(replacement, ok=True)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "dns_add")

            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "dns_remove")
            self.assertEqual(added, [("de.example.xyz", "185.10.0.11")])
            self.assertEqual(removed, [])  # старую запись ещё не трогали

            # dns_remove -> confirming: проверяем, что связка работает
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "confirming")
            self.assertEqual(removed, [("de.example.xyz", "185.10.0.10")])

            # постпроверка прошла -> done
            self.complete_verification(replacement, ok=True)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "done")

        alert.assert_called_once()
        old_row = (
            self.session.query(InfraServerIp)
            .filter(InfraServerIp.ip == "185.10.0.10")
            .one()
        )
        self.assertIsNotNone(old_row.blocked_at)

    def test_candidate_already_on_interface_skips_install(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=True)
        replacement = self.make_pending(server)
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[{"id": "r1", "content": "185.10.0.10"}],
        ):
            infra_worker.process_replacements(self.session)
        self.session.commit()
        self.session.refresh(replacement)
        self.assertEqual(replacement.status, "dns_add")
        self.assertEqual(replacement.new_ip, "185.10.0.11")
        self.assertEqual(self.session.query(InfraAgentCommand).count(), 0)

    def test_no_reserve_with_multiple_a_records_cleans_dns(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        replacement = self.make_pending(server)
        deleted = []
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[
                {"id": "r1", "content": "185.10.0.10"},
                {"id": "r2", "content": "185.10.0.99"},
            ],
        ), mock.patch(
            "engine.cloudflare_dns.delete_a_records",
            side_effect=lambda d, ip: deleted.append((d, ip)) or 1,
        ), mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_replacements(self.session)
        self.session.commit()
        self.session.refresh(replacement)
        self.assertEqual(replacement.status, "dns_cleanup")
        self.assertEqual(deleted, [("de.example.xyz", "185.10.0.10")])
        alert.assert_called_once()

    def test_no_reserve_single_a_record_requires_manual(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        replacement = self.make_pending(server)
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[{"id": "r1", "content": "185.10.0.10"}],
        ), mock.patch(
            "engine.cloudflare_dns.delete_a_records"
        ) as delete_mock, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_replacements(self.session)
        self.session.commit()
        self.session.refresh(replacement)
        self.assertEqual(replacement.status, "manual_required")
        delete_mock.assert_not_called()
        alert.assert_called_once()
        self.assertIn("ручное вмешательство", alert.call_args[0][0].lower())

    def test_old_ip_absent_from_dns_closes_without_switching(self):
        # Забаненный запасной адрес, не стоящий в DNS: замена закрывается
        # «переключать нечего», резерв не расходуется, DNS не трогается
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=True)  # в DNS
        self.make_ip(server, "185.10.0.12", source="manual", on_interface=False)
        replacement = self.make_pending(server, old_ip="185.10.0.10")
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[{"id": "r1", "content": "185.10.0.11"}],
        ), mock.patch(
            "engine.cloudflare_dns.ensure_a_record"
        ) as add_mock, mock.patch(
            "engine.cloudflare_dns.delete_a_records"
        ) as del_mock:
            infra_worker.process_replacements(self.session)
        self.session.commit()
        self.session.refresh(replacement)
        self.assertEqual(replacement.status, "done")
        self.assertIsNone(replacement.new_ip)  # резерв не тронут
        add_mock.assert_not_called()
        del_mock.assert_not_called()
        old_row = (
            self.session.query(InfraServerIp)
            .filter(InfraServerIp.ip == "185.10.0.10")
            .one()
        )
        self.assertIsNotNone(old_row.blocked_at)

    def test_private_ip_is_never_a_candidate(self):
        # Приватные адреса (docker0/warp) из инвентаря не должны попасть
        # в DNS как «резерв» ни при каких обстоятельствах
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "172.17.0.1", on_interface=True, interface="docker0")
        replacement = self.make_pending(server)
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[{"id": "r1", "content": "185.10.0.10"}],
        ), mock.patch.object(infra_worker, "_send_alert", return_value=True):
            infra_worker.process_replacements(self.session)
        self.session.commit()
        self.session.refresh(replacement)
        # Единственный «кандидат» приватный — значит, кандидатов нет
        self.assertEqual(replacement.status, "manual_required")

    def test_blocked_ip_is_not_a_candidate(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(
            server, "185.10.0.11", on_interface=False, blocked_at=utcnow()
        )
        replacement = self.make_pending(server)
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[{"id": "r1", "content": "185.10.0.10"}],
        ), mock.patch.object(infra_worker, "_send_alert", return_value=True):
            infra_worker.process_replacements(self.session)
        self.session.commit()
        self.session.refresh(replacement)
        self.assertEqual(replacement.status, "manual_required")

    def test_concurrent_replacements_pick_different_reserves(self):
        # ТСПУ подтвердил бан двух IP сразу: замены не должны выбрать один
        # и тот же резерв (в DNS новый IP первой замены появится позже)
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_ip(
            server, "185.10.0.11", source="manual", on_interface=False
        )
        self.make_ip(
            server, "185.10.0.12", source="manual", on_interface=False
        )
        first = self.make_pending(server, old_ip="185.10.0.10")
        second = self.make_pending(server, old_ip="185.10.0.20")
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[
                {"id": "r1", "content": "185.10.0.10"},
                {"id": "r2", "content": "185.10.0.20"},
            ],
        ):
            infra_worker.process_replacements(self.session)
        self.session.commit()
        self.session.refresh(first)
        self.session.refresh(second)
        self.assertIsNotNone(first.new_ip)
        self.assertIsNotNone(second.new_ip)
        self.assertNotEqual(first.new_ip, second.new_ip)

    def test_agent_failure_fails_replacement(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=False)
        replacement = self.make_pending(server)
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[{"id": "r1", "content": "185.10.0.10"}],
        ), mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_replacements(self.session)
            self.session.commit()
            command = self.session.query(InfraAgentCommand).one()
            command.status = "failed"
            command.error = "RTNETLINK: permission denied"
            self.session.commit()
            infra_worker.process_replacements(self.session)
            self.session.commit()
        self.session.refresh(replacement)
        self.assertEqual(replacement.status, "failed")
        alert.assert_called_once()


class DnsWatchTests(InfraDbTestCase):
    def test_new_a_record_rescales_baseline(self):
        server = self.make_server()
        domain = self.make_domain(server, "de.example.xyz")
        # Первый снимок: только запоминаем, перекалибровки нет
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[{"id": "r1", "content": "185.10.0.10"}],
        ):
            infra_worker.watch_dns_changes(self.session)
        self.session.commit()
        self.session.refresh(server)
        self.session.refresh(domain)
        self.assertIsNone(server.baseline_scale)
        self.assertEqual(domain.last_a_ips, ["185.10.0.10"])

        # Появилась вторая A-запись (перебалансировка): норма x0.5,
        # детектор НЕ отключается (anomaly_suppressed_until не трогается)
        domain.dns_checked_at = utcnow() - timedelta(minutes=30)
        self.session.commit()
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[
                {"id": "r1", "content": "185.10.0.10"},
                {"id": "r2", "content": "45.9.9.9"},
            ],
        ):
            infra_worker.watch_dns_changes(self.session)
        self.session.commit()
        self.session.refresh(server)
        self.assertEqual(float(server.baseline_scale), 0.5)
        self.assertGreater(
            server.baseline_scale_until, utcnow() + timedelta(hours=100)
        )
        self.assertIsNone(server.anomaly_suppressed_until)

    def test_shared_domain_rescales_all_linked_servers(self):
        # Сценарий «вернул ноду в ротацию»: домен привязан к A и C, у обоих
        # свой слепок; новая A-запись перекалибрует норму обоим (A пережил
        # период удвоенного трафика — без ×0.5 его baseline дал бы ложняк)
        server_a = self.make_server(machine_uid="m-a", node_name="nl-a")
        server_c = self.make_server(machine_uid="m-c", node_name="nl-c")
        domain_a = self.make_domain(server_a, "nl.example.xyz")
        domain_c = self.make_domain(server_c, "nl.example.xyz")
        for row in (domain_a, domain_c):
            row.last_a_ips = ["185.10.0.10"]
            row.dns_checked_at = utcnow() - timedelta(minutes=30)
        self.session.commit()

        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[
                {"id": "r1", "content": "185.10.0.10"},
                {"id": "r2", "content": "185.10.0.20"},
            ],
        ) as list_mock:
            infra_worker.watch_dns_changes(self.session)
        self.session.commit()
        # Cloudflare спрошен один раз на домен, несмотря на две привязки
        self.assertEqual(list_mock.call_count, 1)
        for server in (server_a, server_c):
            self.session.refresh(server)
            self.assertEqual(float(server.baseline_scale), 0.5)

    def test_server_with_two_domains_rescales_once_per_pass(self):
        # Одна перебалансировка видна в строках обоих доменов сервера —
        # фактор применяется один раз (x0.5), не перемножается в x0.25
        server = self.make_server()
        d1 = self.make_domain(server, "de.example.xyz")
        d2 = self.make_domain(server, "de2.example.xyz")
        for row in (d1, d2):
            row.last_a_ips = ["185.10.0.10"]
            row.dns_checked_at = utcnow() - timedelta(minutes=30)
        self.session.commit()
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[
                {"id": "r1", "content": "185.10.0.10"},
                {"id": "r2", "content": "185.10.0.20"},
            ],
        ):
            infra_worker.watch_dns_changes(self.session)
        self.session.commit()
        self.session.refresh(server)
        self.assertEqual(float(server.baseline_scale), 0.5)

    def test_no_rescale_during_active_replacement(self):
        # Промежуточное состояние нашей же ротации (ADD нового, REMOVE
        # старого ещё не прошёл) не считается перебалансировкой
        server = self.make_server()
        domain = self.make_domain(server, "de.example.xyz")
        domain.last_a_ips = ["185.10.0.10"]
        domain.dns_checked_at = utcnow() - timedelta(minutes=30)
        self.session.add(
            InfraIpReplacement(
                server_id=server.id,
                old_ip="185.10.0.10",
                new_ip="185.10.0.11",
                domains=["de.example.xyz"],
                status="dns_remove",
            )
        )
        self.session.commit()
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[
                {"id": "r1", "content": "185.10.0.10"},
                {"id": "r2", "content": "185.10.0.11"},
            ],
        ):
            infra_worker.watch_dns_changes(self.session)
        self.session.commit()
        self.session.refresh(server)
        self.assertIsNone(server.baseline_scale)
        self.session.refresh(domain)
        # Слепок при этом обновлён
        self.assertIn("185.10.0.11", domain.last_a_ips)

    def test_readded_recent_ip_does_not_rescale(self):
        # Слив на время работ и возврат записи: адрес есть в памяти
        # recent_a_ips — повторная перекалибровка не нужна (иначе каждый
        # цикл работ раскручивал бы scale вниз)
        server = self.make_server()
        domain = self.make_domain(server, "de.example.xyz")
        domain.last_a_ips = ["185.10.0.10"]
        domain.recent_a_ips = {
            "185.10.0.10": utcnow().isoformat(sep=" ", timespec="seconds"),
            "45.9.9.9": (utcnow() - timedelta(days=2)).isoformat(
                sep=" ", timespec="seconds"
            ),
        }
        domain.dns_checked_at = utcnow() - timedelta(minutes=30)
        self.session.commit()
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[
                {"id": "r1", "content": "185.10.0.10"},
                {"id": "r2", "content": "45.9.9.9"},
            ],
        ):
            infra_worker.watch_dns_changes(self.session)
        self.session.commit()
        self.session.refresh(server)
        self.assertIsNone(server.baseline_scale)

    def test_neighbor_rotation_does_not_rescale_shared_domain(self):
        # Ротация сервера B на общем домене (промежуточное состояние
        # ADD-до-REMOVE) не должна перекалибровать соседа A
        server_a = self.make_server(machine_uid="m-a", node_name="nl-a")
        server_b = self.make_server(machine_uid="m-b", node_name="nl-b")
        domain_a = self.make_domain(server_a, "nl.example.xyz")
        domain_a.last_a_ips = ["185.10.0.10", "185.10.0.20"]
        domain_a.dns_checked_at = utcnow() - timedelta(minutes=30)
        self.session.add(
            InfraIpReplacement(
                server_id=server_b.id,
                old_ip="185.10.0.20",
                new_ip="185.10.0.21",
                domains=["nl.example.xyz"],
                status="dns_remove",
            )
        )
        self.session.commit()
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[
                {"id": "r1", "content": "185.10.0.10"},
                {"id": "r2", "content": "185.10.0.20"},
                {"id": "r3", "content": "185.10.0.21"},
            ],
        ):
            infra_worker.watch_dns_changes(self.session)
        self.session.commit()
        self.session.refresh(server_a)
        self.assertIsNone(server_a.baseline_scale)

    def test_record_removal_does_not_rescale(self):
        server = self.make_server()
        domain = self.make_domain(server, "de.example.xyz")
        domain.last_a_ips = ["185.10.0.10", "45.9.9.9"]
        domain.dns_checked_at = utcnow() - timedelta(minutes=30)
        self.session.commit()
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records",
            return_value=[{"id": "r1", "content": "185.10.0.10"}],
        ):
            infra_worker.watch_dns_changes(self.session)
        self.session.commit()
        self.session.refresh(server)
        self.assertIsNone(server.baseline_scale)

    def test_recently_checked_domain_is_skipped(self):
        server = self.make_server()
        domain = self.make_domain(server, "de.example.xyz")
        domain.dns_checked_at = utcnow() - timedelta(minutes=2)
        self.session.commit()
        with mock.patch(
            "engine.cloudflare_dns.is_enabled", return_value=True
        ), mock.patch(
            "engine.cloudflare_dns.list_a_records"
        ) as list_mock:
            infra_worker.watch_dns_changes(self.session)
        list_mock.assert_not_called()

    def test_snooze_helpers(self):
        server = self.make_server()
        infra.snooze_anomaly_detector(self.session, server.id, 24)
        self.session.commit()
        self.session.refresh(server)
        self.assertIsNotNone(server.anomaly_suppressed_until)
        infra.unsnooze_anomaly_detector(self.session, server.id)
        self.session.commit()
        self.session.refresh(server)
        self.assertIsNone(server.anomaly_suppressed_until)
        with self.assertRaises(infra.InfraError):
            infra.snooze_anomaly_detector(self.session, server.id, "999999")


class OfflineAlertTests(InfraDbTestCase):
    def test_offline_alert_and_recovery(self):
        server = self.make_server(
            last_seen_at=utcnow() - timedelta(minutes=15)
        )
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        self.session.commit()
        alert.assert_called_once()
        self.assertIn("недоступен", alert.call_args[0][0].lower())
        self.session.refresh(server)
        self.assertIsNotNone(server.offline_alerted_at)

        # Повторный тик — без дублирующего алерта
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        alert.assert_not_called()

        # Восстановление
        server.offline_alerted_at = utcnow() - timedelta(minutes=10)
        server.last_seen_at = utcnow()
        self.session.commit()
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        self.session.commit()
        alert.assert_called_once()
        self.assertIn("онлайн", alert.call_args[0][0].lower())
        self.session.refresh(server)
        # Времена не сбрасываются: recovery позже offline = инцидент закрыт
        self.assertIsNotNone(server.offline_alerted_at)
        self.assertIsNotNone(server.recovered_alerted_at)
        self.assertGreater(
            server.recovered_alerted_at, server.offline_alerted_at
        )

        # Повторный онлайн-тик recovery не дублирует
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        alert.assert_not_called()

    def test_flapping_pairs_rate_limited_by_cooldown(self):
        # Сервер снова офлайн через 5 минут после прошлой пары алертов:
        # новый 🔴 подавляется кулдауном (60 мин по умолчанию)
        server = self.make_server(
            last_seen_at=utcnow() - timedelta(minutes=15),
            offline_alerted_at=utcnow() - timedelta(minutes=5),
            recovered_alerted_at=utcnow() - timedelta(minutes=3),
        )
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        alert.assert_not_called()

        # А после истечения кулдауна новый инцидент алертится
        server.offline_alerted_at = utcnow() - timedelta(minutes=90)
        server.recovered_alerted_at = utcnow() - timedelta(minutes=80)
        self.session.commit()
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        alert.assert_called_once()

    def test_archived_servers_are_ignored(self):
        self.make_server(
            last_seen_at=utcnow() - timedelta(hours=5), is_archived=True
        )
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.check_offline(self.session)
        alert.assert_not_called()


class LoadAlertTests(InfraDbTestCase):
    def seed_load(self, server, rx_bps):
        start = utcnow() - timedelta(minutes=4)
        self.add_samples(
            server, start, count=24, step_seconds=10, rx_bps=rx_bps,
            tx_bps=int(rx_bps * 0.2),
        )

    def test_alert_after_sustained_high_load(self):
        server = self.make_server(bandwidth_limit_mbps=400)
        self.seed_load(server, rx_bps=370_000_000)  # 92.5%
        server.load_high_since = utcnow() - timedelta(minutes=20)
        self.session.commit()
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_load(self.session)
        self.session.commit()
        alert.assert_called_once()
        message = alert.call_args[0][0]
        self.assertIn("Высокая нагрузка", message)
        self.assertIn("400 Mbit/s", message)
        self.session.refresh(server)
        self.assertIsNotNone(server.load_alerted_at)

    def test_no_alert_before_duration(self):
        server = self.make_server(bandwidth_limit_mbps=400)
        self.seed_load(server, rx_bps=370_000_000)
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.check_load(self.session)
        self.session.commit()
        alert.assert_not_called()
        self.session.refresh(server)
        self.assertIsNotNone(server.load_high_since)

    def test_recovery_alert(self):
        server = self.make_server(bandwidth_limit_mbps=400)
        self.seed_load(server, rx_bps=100_000_000)  # 25%
        server.load_alerted_at = utcnow() - timedelta(minutes=30)
        server.load_high_since = utcnow() - timedelta(minutes=60)
        self.session.commit()
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_load(self.session)
        self.session.commit()
        alert.assert_called_once()
        self.assertIn("нормализовалась", alert.call_args[0][0])
        self.session.refresh(server)
        self.assertIsNone(server.load_alerted_at)
        self.assertIsNone(server.load_high_since)

    def test_recovery_alert_retried_when_delivery_fails(self):
        server = self.make_server(bandwidth_limit_mbps=400)
        self.seed_load(server, rx_bps=100_000_000)
        server.load_alerted_at = utcnow() - timedelta(minutes=30)
        self.session.commit()
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=False
        ):
            infra_worker.check_load(self.session)
        self.session.commit()
        self.session.refresh(server)
        # Не доставили — состояние не снимаем, recovery уйдёт следующим тиком
        self.assertIsNotNone(server.load_alerted_at)


class PayloadTests(InfraDbTestCase):
    def test_server_list_payload(self):
        server = self.make_server(
            cur_rx_bps=100_000_000, cur_tx_bps=10_000_000,
            cur_tcp_connections=4218, bandwidth_limit_mbps=400,
        )
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=False)
        self.make_domain(server, "de.example.xyz")
        payload = infra.server_list_payload(self.session)
        self.assertEqual(payload["totals"]["count"], 1)
        self.assertEqual(payload["totals"]["online"], 1)
        item = payload["servers"][0]
        self.assertEqual(item["utilization_pct"], 25.0)
        self.assertEqual(item["ips"], {"active": 1, "reserve": 1, "blocked": 0})
        self.assertEqual(item["domains"], ["de.example.xyz"])

    def test_diagnosis_payload_exposes_verdict_and_evidence(self):
        server = self.make_server()
        anomaly = InfraAnomaly(
            server_id=server.id,
            status="confirmed",
            details={
                "control_name": "ya.ru",
                "verdict": {
                    "blocked_ips": ["185.10.0.10"],
                    "blocked_snis": ["burned.example.xyz"],
                    "confidence": "high",
                    "actionable": True,
                },
                "evidence": [
                    {"step": "ip_probe", "result": "fail", "text": "адрес мёртв"}
                ],
            },
            created_at=utcnow(),
        )
        self.session.add(anomaly)
        self.session.commit()

        detail = infra.server_detail_payload(self.session, server.id)
        diagnosis = detail["diagnosis"]
        self.assertEqual(diagnosis["blocked_ips"], ["185.10.0.10"])
        self.assertEqual(diagnosis["blocked_snis"], ["burned.example.xyz"])
        self.assertEqual(diagnosis["confidence"], "high")
        self.assertTrue(diagnosis["actionable"])
        self.assertEqual(len(diagnosis["evidence"]), 1)

    def test_diagnosis_payload_skips_anomalies_without_verdict(self):
        # Аномалия ещё проверяется — вердикта нет, показывать нечего
        server = self.make_server()
        self.session.add(
            InfraAnomaly(
                server_id=server.id, status="checking",
                details={"ip_runs": {}}, created_at=utcnow(),
            )
        )
        self.session.commit()
        detail = infra.server_detail_payload(self.session, server.id)
        self.assertIsNone(detail["diagnosis"])

    def test_capacity_payload_carries_verdict(self):
        server = self.make_server(
            capacity={
                "conntrack": {
                    "count": 65536, "max": 65536, "usage_pct": 100,
                    "insert_failed_delta": 5,
                },
            }
        )
        detail = infra.server_detail_payload(self.session, server.id)
        self.assertEqual(detail["capacity"]["level"], "crit")
        self.assertTrue(detail["capacity"]["problems"])
        # Сырой снимок тоже доступен — для подробностей в карточке
        self.assertIn("conntrack", detail["capacity"]["raw"])

        plain = self.make_server(machine_uid="m-2")
        detail = infra.server_detail_payload(self.session, plain.id)
        self.assertIsNone(detail["capacity"])

    def test_xray_health_in_payloads(self):
        server = self.make_server(
            xray_process_running=False, xray_crash_loop=False
        )
        item = infra.server_list_payload(self.session)["servers"][0]
        self.assertTrue(item["xray_down"])
        detail = infra.server_detail_payload(self.session, server.id)
        self.assertIs(detail["server"]["xray_process_running"], False)

        server.xray_process_running = True
        server.xray_process_uptime_seconds = 3600
        self.session.commit()
        item = infra.server_list_payload(self.session)["servers"][0]
        self.assertFalse(item["xray_down"])
        detail = infra.server_detail_payload(self.session, server.id)
        self.assertEqual(
            detail["server"]["xray_process_uptime_seconds"], 3600
        )

    def test_entry_payload_ok_in_dns(self):
        # Показываются только IP, реально стоящие в A-записях, и только
        # домены, чьи записи на них указывают
        now = utcnow()
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=True)  # запасной
        self.make_ip(server, "10.0.0.5", on_interface=True)  # приватный
        branded = self.make_domain(server, "uk.monkeyisland.space")
        branded.last_a_ips = ["185.10.0.10"]
        branded.dns_checked_at = now
        other = self.make_domain(server, "uk.easyemploy.org")
        other.last_a_ips = ["45.9.9.9"]  # смотрит в другое место
        other.dns_checked_at = now
        self.session.commit()

        entry = infra.server_detail_payload(self.session, server.id)["entry"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["ips"], ["185.10.0.10"])
        self.assertEqual(entry["domains"], ["uk.monkeyisland.space"])

    def test_entry_payload_multiple_ips_during_rotation(self):
        # Переходный период ротации: старый и новый IP оба в DNS
        now = utcnow()
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=True)
        domain = self.make_domain(server, "uk.monkeyisland.space")
        domain.last_a_ips = ["185.10.0.10", "185.10.0.11"]
        domain.dns_checked_at = now
        self.session.commit()

        entry = infra.server_detail_payload(self.session, server.id)["entry"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(
            sorted(entry["ips"]), ["185.10.0.10", "185.10.0.11"]
        )

    def test_entry_payload_not_in_dns(self):
        now = utcnow()
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        domain = self.make_domain(server, "uk.monkeyisland.space")
        domain.last_a_ips = ["45.9.9.9"]  # нода выведена из DNS
        domain.dns_checked_at = now
        self.session.commit()

        entry = infra.server_detail_payload(self.session, server.id)["entry"]
        self.assertEqual(entry["status"], "not_in_dns")
        self.assertEqual(entry["ips"], ["185.10.0.10"])
        self.assertEqual(entry["domains"], [])

    def test_entry_payload_dns_stale_and_no_domains(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        entry = infra.server_detail_payload(self.session, server.id)["entry"]
        self.assertEqual(entry["status"], "no_domains")
        self.assertEqual(entry["ips"], ["185.10.0.10"])

        domain = self.make_domain(server, "uk.monkeyisland.space")
        domain.last_a_ips = ["185.10.0.10"]
        domain.dns_checked_at = utcnow() - timedelta(hours=48)  # протух
        self.session.commit()
        entry = infra.server_detail_payload(self.session, server.id)["entry"]
        self.assertEqual(entry["status"], "dns_stale")
        self.assertEqual(entry["ips"], ["185.10.0.10"])
        self.assertEqual(entry["domains"], [])

    def test_entry_payload_none_without_active_public_ips(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=False)  # резерв
        blocked = self.make_ip(server, "185.10.0.11", on_interface=True)
        blocked.blocked_at = utcnow()
        self.session.commit()

        entry = infra.server_detail_payload(self.session, server.id)["entry"]
        self.assertEqual(entry["status"], "none")
        self.assertEqual(entry["ips"], [])

    def test_detail_and_series_payload(self):
        server = self.make_server()
        start = infra._floor_dt(utcnow() - timedelta(minutes=30), 60)
        self.add_samples(server, start, count=60, step_seconds=10)
        infra.aggregate_telemetry(self.session)
        self.session.commit()

        detail = infra.server_detail_payload(self.session, server.id)
        self.assertEqual(detail["server"]["node_name"], "de-1")
        self.assertIn("who_connects", detail)

        series = infra.telemetry_series_payload(self.session, server.id, "3h")
        self.assertGreaterEqual(len(series["points"]), 55)
        self.assertEqual(series["summary"]["rx_peak_bps"], 100_000_000)

        series24 = infra.telemetry_series_payload(self.session, server.id, "24h")
        self.assertGreater(len(series24["points"]), 0)
        with self.assertRaises(infra.InfraError):
            infra.telemetry_series_payload(self.session, server.id, "1y")


class InfraViewTests(InfraDbTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        from engine import views as views_module

        self.views = views_module
        patchers = [
            mock.patch.object(
                self.views, "require_support_admin_role", return_value=None
            ),
            mock.patch.object(
                self.views, "support_admin_actor", return_value="tester"
            ),
            mock.patch.object(self.views, "admin_audit_write"),
            mock.patch.object(
                self.views, "session_factory", side_effect=self.Session
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_servers_list_and_update(self):
        server = self.make_server()
        response = self.views.support_admin_api_infra_servers(
            self.factory.get("/support-admin/api/infra-servers/")
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"node_name": "de-1"', response.content)

        response = self.views.support_admin_api_infra_servers(
            self.factory.post(
                "/support-admin/api/infra-servers/",
                {
                    "action": "update",
                    "id": server.id,
                    "display_name": "Germany-01",
                    "bandwidth_limit_mbps": "400",
                    "notes": "",
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        check_session = self.Session()
        try:
            stored = check_session.get(InfraServer, server.id)
            self.assertEqual(stored.display_name, "Germany-01")
            self.assertEqual(stored.bandwidth_limit_mbps, 400)
        finally:
            check_session.close()

    def test_add_ip_error_returns_message(self):
        server = self.make_server()
        response = self.views.support_admin_api_infra_servers(
            self.factory.post(
                "/support-admin/api/infra-servers/",
                {"action": "add_ip", "id": server.id, "ip": "мусор"},
            )
        )
        self.assertEqual(response.status_code, 400)
        import json as json_module

        payload = json_module.loads(response.content)
        self.assertIn("Некорректный IP", payload["message"])

    def test_settings_view(self):
        response = self.views.support_admin_api_infra_settings(
            self.factory.get("/support-admin/api/infra-settings/")
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"infra_load_threshold_pct", response.content)

        response = self.views.support_admin_api_infra_settings(
            self.factory.post(
                "/support-admin/api/infra-settings/",
                {"key": "infra_load_duration_minutes", "value": "30"},
            )
        )
        self.assertEqual(response.status_code, 200)
        check_session = self.Session()
        try:
            stored = check_session.get(
                SystemSetting, "infra_load_duration_minutes"
            )
            self.assertEqual(stored.value, "30")
        finally:
            check_session.close()

    def test_detail_view_404(self):
        response = self.views.support_admin_api_infra_server_detail(
            self.factory.get("/support-admin/api/infra-server-detail/?id=999")
        )
        self.assertEqual(response.status_code, 404)
