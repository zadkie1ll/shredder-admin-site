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
        # Кэш «кто подключается» — module-global с ключом (node, окно):
        # без сброса тест видел бы payload соседнего теста той же ноды
        infra.reset_geo_analytics_cache()
        self.addCleanup(infra.reset_geo_analytics_cache)

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

    def make_domain(self, server, domain, client_snis=None):
        row = InfraServerDomain(
            server_id=server.id, domain=domain, client_snis=client_snis
        )
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

    def test_unlocated_addresses_fall_into_unknown_country_bucket(self):
        self.add_observation("a", "8.8.8.8", 20)
        self.add_observation("b", "1.1.1.1", 10)
        locations = {"8.8.8.8": geoip_lookup.GeoLocation("DE", "Германия")}
        database = geoip_lookup.GeoIpDatabase("/tmp/test.mmdb", 1)

        with mock.patch.object(
            geoip_lookup, "configured_database", return_value=database
        ), mock.patch.object(
            geoip_lookup, "lookup_ip", side_effect=lambda ip, _: locations.get(ip)
        ):
            geo = infra.who_connects_payload(self.session, "de-1", utcnow())["geo"]

        self.assertEqual(geo["located_ips"], 1)
        self.assertEqual(geo["unlocated_ips"], 1)
        self.assertEqual(
            [(row["name"], row["code"], row["hits"]) for row in geo["countries"]],
            [("Германия", "DE", 20), ("Не определилось", None, 10)],
        )
        self.assertEqual(geo["russian_regions"], [])

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

    def test_whole_payload_is_reused_within_cache_window(self):
        # Кэшируется весь блок «кто подключается», а не только гео: четыре
        # агрегатных запроса по ipguard_user_ips — самое дорогое место
        # карточки, и 5-секундный поллинг не должен выполнять их каждый тик
        self.add_observation("a", "8.8.8.8", 20)
        now = utcnow()
        with mock.patch.object(
            geoip_lookup, "configured_database", return_value=None
        ):
            first = infra.who_connects_payload(self.session, "de-1", now)
            self.add_observation("b", "1.1.1.1", 50)
            second = infra.who_connects_payload(self.session, "de-1", now)
            other_node = infra.who_connects_payload(self.session, "wl-1", now)

        self.assertIs(first, second)
        self.assertEqual(second["unique_ips_24h"], 1)
        # Кэш пер-нодовый: чужая нода не получает данные соседа
        self.assertEqual(other_node["unique_ips_24h"], 0)


class WhoConnectsNodeIpExclusionTests(InfraDbTestCase):
    """Адреса самих нод (infra_server_ips.on_interface=true) — не пользователи."""

    def add_observation(self, username, ip, hits, node="de-1", age=None):
        now = utcnow()
        last_seen = now - age if age else now
        self.session.add(
            UserIpObservation(
                username=username,
                ip=ip,
                subnet=f"{ip}/32",
                node=node,
                hits=hits,
                first_seen=last_seen - timedelta(hours=2),
                last_seen=last_seen,
            )
        )
        self.session.commit()

    def payload(self, node="de-1", geo_database=None, lookup=None):
        with mock.patch.object(
            geoip_lookup, "configured_database", return_value=geo_database
        ), mock.patch.object(
            geoip_lookup, "lookup_ip", side_effect=lookup or (lambda ip, _: None)
        ) as lookup_mock:
            payload = infra.who_connects_payload(self.session, node, utcnow())
        return payload, lookup_mock

    def test_node_interface_addresses_are_excluded_from_every_counter(self):
        server = self.make_server()
        self.make_ip(server, "5.5.5.5", on_interface=True)
        # Резерв не на интерфейсе — с него никто не подключается, не исключаем
        self.make_ip(server, "7.7.7.7", on_interface=False, source="manual")
        # Мост с белым IP — отдельный сервер инфраструктуры, тоже исключается
        bridge = self.make_server(machine_uid="m-bridge", node_name="bridge-1")
        self.make_ip(bridge, "6.6.6.6", on_interface=True)

        self.add_observation("bridge", "6.6.6.6", 500)
        self.add_observation("self", "5.5.5.5", 100)
        self.add_observation("a", "8.8.8.8", 20)
        self.add_observation("b", "7.7.7.7", 5)

        database = geoip_lookup.GeoIpDatabase("/tmp/test.mmdb", 1)
        location = geoip_lookup.GeoLocation("DE", "Германия")
        payload, lookup = self.payload(
            geo_database=database, lookup=lambda ip, _: location
        )

        self.assertEqual(payload["unique_ips_24h"], 2)
        self.assertEqual(payload["unique_ips_1h"], 2)
        self.assertEqual(payload["excluded_node_ips"], 2)
        self.assertEqual(
            [row["ip"] for row in payload["top_addresses"]], ["8.8.8.8", "7.7.7.7"]
        )
        # География считается по тем же адресам: нод нет ни в total_ips, ни
        # в GeoIP lookup'ах
        self.assertEqual(payload["geo"]["total_ips"], 2)
        self.assertEqual(payload["geo"]["total_hits"], 25)
        self.assertEqual(
            sorted(call.args[0] for call in lookup.call_args_list),
            ["7.7.7.7", "8.8.8.8"],
        )

    def test_excluded_counter_only_counts_window_of_this_node(self):
        server = self.make_server()
        self.make_ip(server, "5.5.5.5", on_interface=True)
        self.make_ip(server, "6.6.6.6", on_interface=True)
        # Адрес ноды виден на другой ноде и вне окна 24 ч — не в счётчике de-1
        self.add_observation("x", "5.5.5.5", 10, node="wl-1")
        self.add_observation("y", "6.6.6.6", 10, age=timedelta(days=2))
        self.add_observation("a", "8.8.8.8", 1)

        payload, _ = self.payload()

        self.assertEqual(payload["excluded_node_ips"], 0)
        self.assertEqual(payload["unique_ips_24h"], 1)

    def test_without_node_addresses_payload_is_unchanged(self):
        self.add_observation("a", "8.8.8.8", 20)
        self.add_observation("b", "1.1.1.1", 10)

        payload, _ = self.payload()

        self.assertEqual(payload["excluded_node_ips"], 0)
        self.assertEqual(payload["unique_ips_24h"], 2)
        self.assertEqual(
            [row["ip"] for row in payload["top_addresses"]], ["8.8.8.8", "1.1.1.1"]
        )

    def test_all_observations_from_nodes_gives_empty_list_but_counter(self):
        server = self.make_server()
        self.make_ip(server, "5.5.5.5", on_interface=True)
        self.add_observation("self", "5.5.5.5", 100)

        payload, _ = self.payload()

        self.assertEqual(payload["top_addresses"], [])
        self.assertEqual(payload["unique_ips_24h"], 0)
        self.assertEqual(payload["excluded_node_ips"], 1)

    def test_node_addresses_are_normalized_and_blank_rows_skipped(self):
        server = self.make_server()
        # Руками отредактированная строка в нестандартной записи IPv6 и мусор
        self.make_ip(server, "2A01:4F8:0:0:0:0:0:1", on_interface=True)
        self.make_ip(server, "not-an-ip", on_interface=True)
        self.make_ip(server, "   ", on_interface=True)

        excluded = infra.node_interface_ips(self.session)

        self.assertEqual(
            excluded, frozenset({"2A01:4F8:0:0:0:0:0:1", "2a01:4f8::1", "not-an-ip"})
        )

    def test_read_failure_logs_warning_and_keeps_previous_behaviour(self):
        self.add_observation("a", "8.8.8.8", 20)

        with mock.patch.object(
            infra, "select", side_effect=RuntimeError("relation is locked")
        ), self.assertLogs("infra", level="WARNING") as logs:
            payload, _ = self.payload()

        self.assertIn("node_ips_unavailable", logs.output[0])
        self.assertEqual(payload["excluded_node_ips"], 0)
        self.assertEqual(payload["unique_ips_24h"], 1)
        self.assertEqual(payload["top_addresses"][0]["ip"], "8.8.8.8")

    def test_detail_payload_exposes_excluded_counter(self):
        server = self.make_server()
        self.make_ip(server, "5.5.5.5", on_interface=True)
        self.add_observation("self", "5.5.5.5", 100)
        self.add_observation("a", "8.8.8.8", 1)

        with mock.patch.object(
            geoip_lookup, "configured_database", return_value=None
        ):
            detail = infra.server_detail_payload(self.session, server.id)

        self.assertEqual(detail["who_connects"]["excluded_node_ips"], 1)
        self.assertEqual(detail["who_connects"]["unique_ips_24h"], 1)


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

    def test_client_snis_are_stored_and_normalized(self):
        server = self.make_server()
        # Имён столько, сколько протоколов развели на haproxy; имя из
        # ClientHello может не быть доменом вовсе (localhost под отдельный
        # протокол) — точка здесь не обязательна
        infra.add_domain(
            self.session, server.id, "de.monkora.org",
            "Example.ORG, example.com , LOCALHOST",
        )
        row = self.session.query(InfraServerDomain).one()
        self.assertEqual(
            row.client_snis, ["example.org", "example.com", "localhost"]
        )

        # Дубли схлопываются, порядок сохраняется
        infra.set_domain_snis(
            self.session, server.id, "de.monkora.org",
            "example.org example.org example.net",
        )
        self.assertEqual(row.client_snis, ["example.org", "example.net"])

        # Пустое значение и список из одного домена — одна и та же старая
        # схема «имя = домен», хранится как NULL
        infra.set_domain_snis(self.session, server.id, "de.monkora.org", "")
        self.assertIsNone(row.client_snis)
        infra.set_domain_snis(
            self.session, server.id, "de.monkora.org", "de.monkora.org"
        )
        self.assertIsNone(row.client_snis)

        # Смешанная схема выразима: домен уходит в эфир сам И есть маска
        infra.set_domain_snis(
            self.session, server.id, "de.monkora.org",
            "de.monkora.org,example.org",
        )
        self.assertEqual(row.client_snis, ["de.monkora.org", "example.org"])
        self.assertTrue(infra.domain_own_name_on_wire(row))

        with self.assertRaises(infra.InfraError):
            infra.set_domain_snis(
                self.session, server.id, "de.monkora.org", ["bad/sni"]
            )
        with self.assertRaises(infra.InfraError):
            infra.set_domain_snis(
                self.session, server.id, "de.monkora.org",
                [f"n{i}.example" for i in range(infra.MAX_DOMAIN_SNIS + 1)],
            )
        with self.assertRaises(infra.InfraError):
            infra.set_domain_snis(self.session, server.id, "nope.example", "x")

    def test_server_snis_override_domain_names(self):
        # Имена относятся к ноде, а не к домену: список сервера — единственный
        # источник для замеров, старые client_snis доменов игнорируются
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "a.example.xyz", client_snis=["stale.example"])
        self.make_domain(server, "b.example.xyz")

        infra.set_server_snis(
            self.session, server.id, "Example.ORG, b.example.xyz , example.org"
        )
        self.assertEqual(server.client_snis, ["example.org", "b.example.xyz"])
        _ips, names, dropped = infra.server_probe_targets(self.session, server)
        self.assertEqual(names, ["example.org", "b.example.xyz"])
        self.assertEqual(dropped, [])
        targets = infra.server_domain_targets(self.session, server.id)
        self.assertEqual(
            targets,
            [
                {"domain": "a.example.xyz", "snis": ["example.org", "b.example.xyz"]},
                {"domain": "b.example.xyz", "snis": ["example.org", "b.example.xyz"]},
            ],
        )
        payload = infra.server_detail_payload(self.session, server.id)
        self.assertEqual(payload["client_snis"], ["example.org", "b.example.xyz"])
        self.assertEqual(payload["probe_snis"], ["example.org", "b.example.xyz"])
        self.assertEqual(payload["snis_source"], "server")
        by_domain = {d["domain"]: d for d in payload["domains_detail"]}
        self.assertFalse(by_domain["a.example.xyz"]["own_name"])
        self.assertTrue(by_domain["b.example.xyz"]["own_name"])

        # Пустое значение снимает список: снова старая схема по доменам
        infra.set_server_snis(self.session, server.id, "")
        self.assertIsNone(server.client_snis)
        _ips, names, _dropped = infra.server_probe_targets(self.session, server)
        self.assertEqual(names, ["stale.example", "b.example.xyz"])
        payload = infra.server_detail_payload(self.session, server.id)
        self.assertEqual(payload["client_snis"], [])
        self.assertEqual(payload["probe_snis"], ["stale.example", "b.example.xyz"])
        self.assertEqual(payload["snis_source"], "domains")

        with self.assertRaises(infra.InfraError):
            infra.set_server_snis(self.session, server.id, "a/b")
        with self.assertRaises(infra.InfraError):
            infra.set_server_snis(self.session, 10**9, "example.org")

    def test_set_domain_snis_apply_all(self):
        # haproxy разводит по SNI на общем адресе — набор имён обычно один
        # на ноду, вбивать его в каждый домен руками значит опечататься
        server = self.make_server()
        self.make_domain(server, "a.example.xyz")
        self.make_domain(server, "b.example.xyz")
        other = self.make_server(machine_uid="m-2")
        self.make_domain(other, "c.example.xyz")

        infra.set_domain_snis(
            self.session, server.id, "a.example.xyz",
            "example.com,example.org", apply_all=True,
        )
        rows = {
            row.domain: row.client_snis
            for row in self.session.query(InfraServerDomain).all()
        }
        self.assertEqual(rows["a.example.xyz"], ["example.com", "example.org"])
        self.assertEqual(rows["b.example.xyz"], ["example.com", "example.org"])
        # Чужой сервер не затронут
        self.assertIsNone(rows["c.example.xyz"])

    def test_probe_targets_deduplicate_and_report_truncation(self):
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(
            server, "a.example.xyz", client_snis=["example.com", "example.org"]
        )
        self.make_domain(
            server, "b.example.xyz", client_snis=["example.com", "example.net"]
        )
        self.make_domain(server, "c.example.xyz")
        ips, names, dropped = infra.server_probe_targets(self.session, server)
        self.assertEqual(ips, ["185.10.0.10"])
        # Общее имя двух доменов проверяется один раз; обход «в ширину»,
        # поэтому лимит не съедается одним доменом
        self.assertEqual(
            names, ["example.com", "c.example.xyz", "example.org", "example.net"]
        )
        self.assertEqual(dropped, [])

        self.make_domain(server, "d.example.xyz", client_snis=["example.info"])
        # Лимит — настройка: имён у ноды может быть и семь
        self.session.add(
            SystemSetting(key="infra_tspu_max_names", value="4")
        )
        self.session.commit()
        _ips, names, dropped = infra.server_probe_targets(self.session, server)
        self.assertEqual(len(names), 4)
        # Срезанное возвращается явно, а не исчезает молча
        self.assertEqual(dropped, ["example.net"])

        targets = infra.server_domain_targets(self.session, server.id)
        self.assertEqual(
            targets[0], {"domain": "a.example.xyz",
                         "snis": ["example.com", "example.org"]},
        )
        # Домен старой схемы: имя — он сам, список никогда не пустой
        self.assertEqual(
            targets[2], {"domain": "c.example.xyz", "snis": ["c.example.xyz"]},
        )

    def test_burned_control_names_are_skipped(self):
        self.session.add(
            SystemSetting(key="infra_control_names", value="ya.ru,ms.example")
        )
        self.session.add(
            SystemSetting(key="infra_control_names_burned", value="ya.ru")
        )
        self.session.commit()
        cfg = infra.get_settings(self.session)
        self.assertEqual(infra.active_control_names(cfg), ["ms.example"])

        # Выгорели все — работаем прежним списком: остаться без контроля хуже
        self.session.query(SystemSetting).filter(
            SystemSetting.key == "infra_control_names_burned"
        ).one().value = "ya.ru,ms.example"
        self.session.commit()
        cfg = infra.get_settings(self.session)
        self.assertEqual(infra.active_control_names(cfg), ["ya.ru", "ms.example"])

    def test_mark_control_name_burned_is_idempotent(self):
        self.assertTrue(infra.mark_control_name_burned(self.session, "Google.RU"))
        self.assertFalse(infra.mark_control_name_burned(self.session, "google.ru"))
        self.session.commit()
        row = self.session.get(SystemSetting, "infra_control_names_burned")
        self.assertEqual(row.value, "google.ru")
        self.assertFalse(infra.mark_control_name_burned(self.session, "  "))

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
        # Единственная проблема — гонки: алерт не шлётся, оценка живёт
        # только в карточке сервера
        self.assertTrue(noisy["collision_only"])

        # Фоновые гонки (десятки за интервал — транзитный клиентский DNS)
        # тревоги не поднимают; сотни — поднимают
        for delta, expected in ((199, "ok"), (200, "warn")):
            verdict = infra.evaluate_capacity(
                {
                    "conntrack": {
                        "count": 1000, "max": 262144, "usage_pct": 1,
                        "insert_failed_delta": delta,
                    },
                },
                70,
            )
            self.assertEqual(verdict["level"], expected)

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

    def test_master_process_limit_is_ignored(self):
        # worker_rlimit_nofile поднимает лимит только worker-процессам;
        # master сохраняет системный, и это норма — соединения обслуживает
        # не он. Реальный случай с боевой ноды после поднятия лимитов.
        verdict = infra.evaluate_capacity(
            {
                "nginx": {
                    "worker_connections": 65535,
                    "worker_rlimit_nofile": 262144,
                    "recent_errors": [],
                    "workers": [
                        {"pid": 1, "role": "master", "fd": 30,
                         "nofile_soft": 1024, "nofile_hard": 524288},
                        {"pid": 2, "role": "worker", "fd": 900,
                         "nofile_soft": 262144, "nofile_hard": 524288},
                    ],
                },
            },
            70,
        )
        self.assertEqual(verdict["level"], "ok")
        self.assertEqual(verdict["problems"], [])

    def test_low_limit_on_real_worker_still_reported(self):
        verdict = infra.evaluate_capacity(
            {
                "nginx": {
                    "worker_connections": 65535,
                    "worker_rlimit_nofile": 262144,
                    "recent_errors": [],
                    "workers": [
                        {"pid": 1, "role": "master", "fd": 30,
                         "nofile_soft": 1024, "nofile_hard": 524288},
                        {"pid": 2, "role": "worker", "fd": 100,
                         "nofile_soft": 1024, "nofile_hard": 524288},
                    ],
                },
            },
            70,
        )
        self.assertEqual(verdict["level"], "warn")
        self.assertIn("нужен перезапуск", " ".join(verdict["problems"]))

    def test_agent_without_roles_behaves_as_before(self):
        # Агент до v0.4.1 роли не присылает — судим по всем процессам
        verdict = infra.evaluate_capacity(
            {
                "nginx": {
                    "worker_connections": 65535,
                    "recent_errors": [],
                    "workers": [
                        {"pid": 1, "fd": 30, "nofile_soft": 1024,
                         "nofile_hard": 524288},
                    ],
                },
            },
            70,
        )
        self.assertEqual(verdict["level"], "warn")

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

    def test_collision_only_warn_stays_in_card(self):
        # Гонки вставки при свободной таблице — фон, не событие: телеграм
        # молчит, оценка видна в карточке (capacity_payload). Совет
        # «поднять лимиты» для этого случая вреден.
        server = self.make_server(capacity={
            "conntrack": {
                "count": 1000, "max": 262144, "usage_pct": 1,
                "insert_failed_delta": 500,
            },
        })
        with mock.patch.object(infra_worker, "_send_alert") as alert:
            infra_worker.check_capacity(self.session)
        alert.assert_not_called()
        self.assertIsNone(server.capacity_warn_alerted_at)

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

    def test_suppressed_when_tspu_checks_disabled(self):
        # Постоянное исключение из слежки за ТСПУ: внутренний сервер,
        # добавленный ради графиков, не жжёт кредиты RIPE Atlas даже при
        # обвале трафика (например, его IP давно забанен)
        now = utcnow()
        server = self.make_server(tspu_checks_enabled=False)
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


def diag_evidence_text(evidence):
    from engine import infra_diagnosis

    return infra_diagnosis.evidence_text(evidence)


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
        self._seed_known_good("de.example.xyz")
        ip_run = self.make_run("185.10.0.10", ok_probes=2, blocked_probes=18)
        sni_run = self.make_run(
            "185.10.0.11", ok_probes=18, blocked_probes=2,
            sni="de.example.xyz",
        )
        # Свидетель на подозрительном адресе: имя там тоже не проходит
        witness = self.make_run(
            "185.10.0.10", ok_probes=0, blocked_probes=20,
            sni="de.example.xyz",
        )
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": ip_run.id},
            {
                "185.10.0.11|de.example.xyz": sni_run.id,
                "185.10.0.10|de.example.xyz": witness.id,
            },
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
        # Имя уже наблюдалось рабочим: без истории вердикт «забанено»
        # не выносится (защита от опечатки в карточке)
        self._seed_known_good("de.example.xyz")
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
        self.assertIn("DNS это не блокирует", text)
        # Алерт объясняет, какими проверками это установлено
        self.assertIn("Как это выяснено", text)

    def test_both_bans_replace_all_domains_but_publish_only_clean(self):
        # Сценарий инцидента: забанены и адрес, и одно из имён. Старый адрес
        # убирается из ВСЕХ доменов (он мёртв и под забаненным именем), а
        # новый публикуется только под чистым — забаненное имя уходит в
        # заявку списком banned_names
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_domain(server, "clean.example.xyz")
        self.make_domain(server, "burned.example.xyz")
        # Имя уже наблюдалось рабочим: без истории вердикт «забанено»
        # не выносится (защита от опечатки в карточке)
        self._seed_known_good("clean.example.xyz")
        self._seed_known_good("burned.example.xyz")
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
        replacement = self.session.query(InfraIpReplacement).one()
        self.assertEqual(replacement.old_ip, "185.10.0.10")
        self.assertEqual(
            replacement.domains, ["burned.example.xyz", "clean.example.xyz"]
        )
        created = replacement.log[0]
        self.assertEqual(created["step"], "created")
        # DNS переставляется у всех доменов: вердикт по имени публикацию
        # больше не блокирует
        self.assertEqual(created["banned_names"], [])
        self.assertEqual(
            created["sni_banned"], {"burned.example.xyz": ["burned.example.xyz"]}
        )
        # И отдельно предупреждение про имя
        self.assertTrue(
            any("заблокировано клиентское имя" in call[0][0]
                for call in alert.call_args_list)
        )

    def test_no_sni_phase_marks_names_unknown_not_clean(self):
        # Инцидент 2026-09-02, de-2: единственный активный адрес забанен,
        # проверять имена было не на чем. Раньше пустой список забаненных
        # имён читался как «все чисты», и xyz уехал на .132 без проверки.
        # Теперь имена «неизвестны»: замена идёт по всем доменам, но
        # публикация — только после проверки каждого имени на кандидате
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "a.example.xyz")
        self.make_domain(server, "b.example.xyz")
        self._seed_known_good("a.example.xyz")
        dead_run = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        # Имя-свидетель на подозрительном адресе тоже не проходит: отказ
        # контроля подтверждён вторым независимым именем
        witness = self.make_run(
            "185.10.0.10", ok_probes=0, blocked_probes=20, sni="a.example.xyz"
        )
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": dead_run.id},
            {"185.10.0.10|a.example.xyz": witness.id},
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
        self.assertEqual(verdict["blocked_snis"], [])
        self.assertEqual(
            verdict["unknown_snis"], ["a.example.xyz", "b.example.xyz"]
        )
        evidence = diag_evidence_text(anomaly.details["evidence"])
        self.assertIn("не проверялись", evidence)
        self.assertIn("чистыми не считаются", evidence)
        # Алерт «заблокировано имя» не уходит: имена не забанены, а неизвестны
        self.assertFalse(
            any("заблокировано имя" in call[0][0]
                for call in alert.call_args_list)
        )
        replacement = self.session.query(InfraIpReplacement).one()
        self.assertEqual(
            replacement.domains, ["a.example.xyz", "b.example.xyz"]
        )
        self.assertEqual(replacement.log[0]["banned_names"], [])

    def test_pair_block_is_not_a_name_ban(self):
        # Инцидент 2026-09-02, de-1: имя падает на живом .198, но проходит
        # на другом живом адресе. Это бан пары «адрес + имя», а не имени:
        # алерт «заблокировано имя» не уходит, имя остаётся в публикации
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_ip(server, "185.10.0.30", on_interface=True)
        self.make_domain(server, "x.example.xyz")
        self.make_domain(server, "y.example.xyz")
        dead_run = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        live_a = self.make_run("185.10.0.20", ok_probes=19, blocked_probes=1)
        live_b = self.make_run("185.10.0.30", ok_probes=19, blocked_probes=1)
        x_on_a = self.make_run(
            "185.10.0.20", ok_probes=0, blocked_probes=20, sni="x.example.xyz"
        )
        x_on_b = self.make_run(
            "185.10.0.30", ok_probes=19, blocked_probes=1, sni="x.example.xyz"
        )
        y_on_a = self.make_run(
            "185.10.0.20", ok_probes=19, blocked_probes=1, sni="y.example.xyz"
        )
        y_on_b = self.make_run(
            "185.10.0.30", ok_probes=19, blocked_probes=1, sni="y.example.xyz"
        )
        anomaly = self.make_anomaly(
            server,
            {
                "185.10.0.10|ya.ru": dead_run.id,
                "185.10.0.20|ya.ru": live_a.id,
                "185.10.0.30|ya.ru": live_b.id,
            },
            {
                "185.10.0.20|x.example.xyz": x_on_a.id,
                "185.10.0.30|x.example.xyz": x_on_b.id,
                "185.10.0.20|y.example.xyz": y_on_a.id,
                "185.10.0.30|y.example.xyz": y_on_b.id,
                "185.10.0.10|x.example.xyz": self.make_run(
                    "185.10.0.10", ok_probes=0, blocked_probes=20,
                    sni="x.example.xyz").id,
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
        self.assertEqual(verdict["blocked_snis"], [])
        self.assertEqual(
            verdict["pair_blocked"],
            [{"ip": "185.10.0.20", "sni": "x.example.xyz"}],
        )
        self.assertFalse(
            any("заблокировано имя" in call[0][0]
                for call in alert.call_args_list)
        )
        self.assertIn(
            "бан пары", diag_evidence_text(anomaly.details["evidence"])
        )
        replacement = self.session.query(InfraIpReplacement).one()
        self.assertEqual(replacement.log[0]["banned_names"], [])

    def test_neighbour_pass_downgrades_name_ban_to_pair(self):
        # Имя общее для трёх нод: у соседа той же волны оно на живом адресе
        # прошло — значит здесь бан пары, а не имени. Сосед старше окна
        # волны в расчёт не идёт
        neighbour = self.make_server(machine_uid="m-2", node_name="de-3")
        neighbour_anomaly = InfraAnomaly(
            server_id=neighbour.id,
            status="dismissed",
            details={
                "evidence": [
                    {
                        "step": "sni_probe", "ip": "185.10.0.99",
                        "sni": "de.example.xyz", "result": "pass",
                        "text": "проходит",
                    }
                ]
            },
            created_at=utcnow() - timedelta(minutes=12),
            resolved_at=utcnow() - timedelta(minutes=10),
        )
        self.session.add(neighbour_anomaly)
        self.session.commit()

        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        # Имя уже наблюдалось рабочим: без истории вердикт «забанено»
        # не выносится (защита от опечатки в карточке)
        self._seed_known_good("de.example.xyz")
        dead_run = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        live_run = self.make_run("185.10.0.20", ok_probes=19, blocked_probes=1)
        name_run = self.make_run(
            "185.10.0.20", ok_probes=0, blocked_probes=20, sni="de.example.xyz"
        )
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": dead_run.id, "185.10.0.20|ya.ru": live_run.id},
            {"185.10.0.20|de.example.xyz": name_run.id},
            status="checking_sni",
        )
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        verdict = anomaly.details["verdict"]
        self.assertEqual(verdict["blocked_snis"], [])
        self.assertEqual(
            verdict["pair_blocked"],
            [{"ip": "185.10.0.20", "sni": "de.example.xyz"}],
        )
        evidence = diag_evidence_text(anomaly.details["evidence"])
        self.assertIn("de-3", evidence)
        self.assertIn("185.10.0.99", evidence)
        self.assertFalse(
            any("заблокировано имя" in call[0][0]
                for call in alert.call_args_list)
        )

        # Тот же сосед, но старше окна волны: имя забанено
        neighbour_anomaly.resolved_at = utcnow() - timedelta(hours=3)
        self.session.commit()
        anomaly2 = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": dead_run.id, "185.10.0.20|ya.ru": live_run.id},
            {"185.10.0.20|de.example.xyz": name_run.id},
            status="checking_sni",
        )
        with mock.patch.object(infra_worker, "_send_alert", return_value=True):
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly2)
        self.assertEqual(
            anomaly2.details["verdict"]["blocked_snis"], ["de.example.xyz"]
        )

    def test_phase2_probes_two_live_addresses_with_full_probe_set(self):
        # Фаза имён идёт на нескольких живых адресах (до SNI_PROBE_MAX_IPS)
        # полным набором зондов: иначе бан пары от бана имени не отличить
        server = self.make_server()
        for ip in ("185.10.0.10", "185.10.0.20", "185.10.0.30", "185.10.0.40"):
            self.make_ip(server, ip, on_interface=True)
        self.make_domain(server, "a.example.xyz")
        self.make_domain(server, "b.example.xyz")
        dead = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        live = {
            ip: self.make_run(ip, ok_probes=19, blocked_probes=1)
            for ip in ("185.10.0.20", "185.10.0.30", "185.10.0.40")
        }
        anomaly = self.make_anomaly(
            server,
            {
                "185.10.0.10|ya.ru": dead.id,
                "185.10.0.20|ya.ru": live["185.10.0.20"].id,
                "185.10.0.30|ya.ru": live["185.10.0.30"].id,
                "185.10.0.40|ya.ru": live["185.10.0.40"].id,
            },
            status="checking",
        )
        calls = []

        def fake_probes(db, pairs, probe_ids=None, reason="", light=True):
            calls.append({"pairs": list(pairs), "reason": reason, "light": light})
            return {
                "runs": {
                    f"{ip}|{sni}": 1000 + index
                    for index, (ip, sni) in enumerate(pairs)
                },
                "errors": [],
            }

        with mock.patch.object(
            infra, "start_diagnosis_probes", side_effect=fake_probes
        ), mock.patch.object(infra_worker, "_send_alert", return_value=True):
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        self.assertEqual(anomaly.status, "checking_sni")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["light"])
        self.assertEqual(
            sorted(calls[0]["pairs"]),
            sorted(
                [
                    (ip, name)
                    for ip in ("185.10.0.20", "185.10.0.30")
                    for name in ("a.example.xyz", "b.example.xyz")
                ]
                # плюс свидетель на подозрительном адресе: без него вердикт
                # «забанен» держался бы на одном контрольном имени. Свидетель
                # — СЛЕДУЮЩЕЕ контрольное имя: оно чужое, уходит в
                # default_backend ноды и отвечает всегда, пока адрес жив, а
                # клиентское имя на инбаунде Reality не отвечает никогда
                + [("185.10.0.10", "www.microsoft.com")]
            ),
        )
        self.assertEqual(
            anomaly.details["sni_probe_ips"], ["185.10.0.20", "185.10.0.30"]
        )

    def test_all_names_blocked_skips_replacement(self):
        # Все имена сервера под фильтром: переводить клиентов некуда
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_domain(server, "one.example.xyz")
        # Имя уже наблюдалось рабочим: без истории вердикт «забанено»
        # не выносится (защита от опечатки в карточке)
        self._seed_known_good("one.example.xyz")
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
        # Контроль не проходит нигде, клиентским именем опровергнуть нечем:
        # подозрение на выгорание контроля, вердикты не выносятся
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        run_a = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        run_b = self.make_run("185.10.0.20", ok_probes=1, blocked_probes=19)
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": run_a.id, "185.10.0.20|ya.ru": run_b.id},
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        # Ни одной замены: сначала нужно сменить контрольное имя
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)
        self.assertEqual(anomaly.details["verdict"]["blocked_ips"], [])
        self.assertEqual(
            anomaly.details["verdict"]["control_burn"], "suspected"
        )
        # Раньше выгорание проходило молча — админ не знал, что диагностика
        # ослепла
        self.assertTrue(
            any("выгорание контрольного имени" in call[0][0]
                for call in alert.call_args_list)
        )
        # По подозрению имя автоматически не снимается
        self.assertIsNone(
            self.session.get(SystemSetting, "infra_control_names_burned")
        )

    def test_suspect_ips_go_to_sni_phase(self):
        # Живых адресов нет: фаза 2 всё равно запускается — проход
        # клиентского имени на «мёртвом» адресе доказал бы, что выгорел
        # контроль, а не адрес
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        run = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        anomaly = self.make_anomaly(server, {"185.10.0.10|ya.ru": run.id})

        with mock.patch.object(
            infra, "start_sni_diagnosis",
            return_value={"runs": {"185.10.0.10|de.example.xyz": 777},
                          "errors": []},
        ) as started:
            with mock.patch.object(
                infra_worker, "_send_alert", return_value=True
            ):
                infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        self.assertEqual(anomaly.status, "checking_sni")
        self.assertEqual(started.call_args[0][2], [])
        self.assertEqual(
            started.call_args[1]["suspect_ips"], ["185.10.0.10"]
        )
        self.assertEqual(anomaly.details["sni_probe_mode"], "witness")

    def test_hard_blocked_ips_skip_sni_phase(self):
        # TCP не устанавливается: клиентское имя там не пройдёт по
        # определению — зонды не жжём
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        run = self.make_run(
            "185.10.0.10", ok_probes=0, blocked_probes=20, stage="tcp_fail"
        )
        anomaly = self.make_anomaly(server, {"185.10.0.10|ya.ru": run.id})

        with mock.patch.object(infra, "start_sni_diagnosis") as started:
            with mock.patch.object(
                infra_worker, "_send_alert", return_value=True
            ):
                infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        started.assert_not_called()
        self.assertEqual(anomaly.status, "confirmed")

    def test_client_sni_pass_proves_control_burned(self):
        # Контроль упал, а клиентское имя на том же адресе прошло: адрес
        # жив, выгорело контрольное имя — оно помечается и ротируется
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        ip_run = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        sni_run = self.make_run(
            "185.10.0.10", ok_probes=19, blocked_probes=1,
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

        self.assertEqual(anomaly.details["verdict"]["control_burn"], "proven")
        self.assertEqual(anomaly.details["verdict"]["blocked_ips"], [])
        self.assertEqual(anomaly.status, "dismissed")
        # Замены нет: адрес доказанно жив
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)
        setting = self.session.get(SystemSetting, "infra_control_names_burned")
        self.assertEqual(setting.value, "ya.ru")
        self.assertTrue(
            any("контрольное имя выгорело" in call[0][0]
                for call in alert.call_args_list)
        )

    def _seed_known_good(self, sni):
        """История «имя когда-то проходило»: без неё вердикт по имени не
        выносится (защита от опечатки в карточке)."""
        self.make_run("203.0.113.1", ok_probes=20, blocked_probes=0, sni=sni)

    def test_custom_sni_ban_still_replaces_dns(self):
        # У домена свои клиентские имена: домен в эфир не уходит, бан одного
        # имени публикацию нового адреса не блокирует
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(
            server, "de.example.xyz",
            client_snis=["example.org", "example.com"],
        )
        self._seed_known_good("example.org")
        ip_run = self.make_run("185.10.0.10", ok_probes=2, blocked_probes=18)
        live_run = self.make_run("185.10.0.11", ok_probes=18, blocked_probes=2)
        banned_run = self.make_run(
            "185.10.0.11", ok_probes=0, blocked_probes=20, sni="example.org"
        )
        clean_run = self.make_run(
            "185.10.0.11", ok_probes=19, blocked_probes=1, sni="example.com"
        )
        # Свидетель на подозрительном адресе: через .10 не идёт и имя
        witness = self.make_run(
            "185.10.0.10", ok_probes=0, blocked_probes=20, sni="example.org"
        )
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": ip_run.id, "185.10.0.11|ya.ru": live_run.id},
            {
                "185.10.0.11|example.org": banned_run.id,
                "185.10.0.11|example.com": clean_run.id,
                "185.10.0.10|example.org": witness.id,
            },
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        self.assertEqual(
            anomaly.details["verdict"]["blocked_snis"], ["example.org"]
        )
        replacement = self.session.query(InfraIpReplacement).one()
        self.assertEqual(replacement.old_ip, "185.10.0.10")
        created = replacement.log[0]
        # Домен не в бане: A-запись переставляется как обычно
        self.assertEqual(created["banned_names"], [])
        self.assertEqual(
            created["sni_banned"], {"de.example.xyz": ["example.org"]}
        )
        self.assertEqual(
            created["domain_snis"],
            {"de.example.xyz": ["example.org", "example.com"]},
        )
        # Алерт называет ИМЕННО забаненное имя, а не первое имя домена
        sni_alerts = [
            call[0][0] for call in alert.call_args_list
            if "клиентское имя (SNI)" in call[0][0]
        ]
        self.assertTrue(sni_alerts)
        self.assertIn("example.org", sni_alerts[0])
        self.assertIn("продолжают работать", sni_alerts[0])

    def test_one_banned_name_of_three_keeps_live_address(self):
        # Адрес жив (any-of по именам), один протокол под фильтром: замены
        # нет, DNS не трогаем, уходит только алерт про имя
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(
            server, "de.example.xyz",
            client_snis=["example.org", "example.com", "example.net"],
        )
        for name in ("example.org", "example.com", "example.net"):
            self._seed_known_good(name)
        ip_run = self.make_run("185.10.0.10", ok_probes=18, blocked_probes=2)
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": ip_run.id},
            {
                "185.10.0.10|example.org": self.make_run(
                    "185.10.0.10", ok_probes=19, blocked_probes=1,
                    sni="example.org").id,
                "185.10.0.10|example.com": self.make_run(
                    "185.10.0.10", ok_probes=0, blocked_probes=20,
                    sni="example.com").id,
                "185.10.0.10|example.net": self.make_run(
                    "185.10.0.10", ok_probes=18, blocked_probes=2,
                    sni="example.net").id,
            },
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        self.assertEqual(anomaly.details["verdict"]["blocked_ips"], [])
        self.assertEqual(
            anomaly.details["verdict"]["blocked_snis"], ["example.com"]
        )
        # Живой адрес не меняем: сжигать резерв из-за имени нельзя
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)
        self.assertEqual(anomaly.status, "confirmed")
        text = alert.call_args_list[0][0][0]
        self.assertIn("example.com", text)
        self.assertIn("example.org", text)

    def test_never_passing_name_is_not_declared_blocked(self):
        # Опечатка в карточке или имя, которого нода не обслуживает,
        # выглядит на пробе точно как бан ТСПУ — вердикт не выносим
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz", client_snis=["exmaple.org"])
        ip_run = self.make_run("185.10.0.10", ok_probes=18, blocked_probes=2)
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": ip_run.id},
            {"185.10.0.10|exmaple.org": self.make_run(
                "185.10.0.10", ok_probes=0, blocked_probes=20,
                sni="exmaple.org").id},
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        self.assertEqual(anomaly.details["verdict"]["blocked_snis"], [])
        self.assertEqual(
            anomaly.details["verdict"]["unproven_snis"], ["exmaple.org"]
        )
        self.assertIn("exmaple.org", anomaly.details["verdict"]["unknown_snis"])
        self.assertTrue(
            any("ни разу не проходило" in call[0][0]
                for call in alert.call_args_list)
        )

    def test_control_alone_does_not_trigger_replacement(self):
        # Ровно инцидент 07.09.2026: контрольное имя не проходит, живых
        # адресов нет, второго свидетеля нет — замены быть не должно
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz", client_snis=["example.org"])
        ip_run = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        # Имя-свидетель падает, но оно никогда и нигде не проходило —
        # доказательством его отказ не является
        witness = self.make_run(
            "185.10.0.10", ok_probes=0, blocked_probes=20, sni="example.org"
        )
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": ip_run.id},
            {"185.10.0.10|example.org": witness.id},
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        self.assertEqual(anomaly.details["verdict"]["blocked_ips"], [])
        self.assertEqual(
            anomaly.details["verdict"]["unconfirmed_ips"], ["185.10.0.10"]
        )
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)
        # Молчать тоже нельзя: админ должен знать, что адрес подозрителен
        self.assertTrue(
            any("подтвердить нечем" in call[0][0]
                for call in alert.call_args_list)
        )

    def test_witness_pass_keeps_address_and_burns_control(self):
        # Контроль упал, а боевое имя на том же адресе прошло: адрес живой,
        # выгорел контроль — замены нет, имя ротируется
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz", client_snis=["example.org"])
        ip_run = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        witness = self.make_run(
            "185.10.0.10", ok_probes=19, blocked_probes=1, sni="example.org"
        )
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": ip_run.id},
            {"185.10.0.10|example.org": witness.id},
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        self.assertEqual(anomaly.details["verdict"]["blocked_ips"], [])
        self.assertEqual(anomaly.details["verdict"]["control_burn"], "proven")
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)
        self.assertEqual(
            self.session.get(
                SystemSetting, "infra_control_names_burned"
            ).value,
            "ya.ru",
        )
        self.assertTrue(
            any("контрольное имя выгорело" in call[0][0]
                for call in alert.call_args_list)
        )

    def test_self_sni_node_recovers_without_human(self):
        # Главный боевой сценарий: домен = SNI (self-SNI, так идёт почти весь
        # парк), два адреса, один выгорел, имя НЕ забанено. Замена должна
        # пройти сама, ночью, без участия человека
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_ip(
            server, "185.10.0.30", on_interface=False, source="manual",
            interface=None,
        )
        self.make_domain(server, "de.example.xyz")
        self._seed_known_good("de.example.xyz")
        anomaly = self.make_anomaly(
            server,
            {
                "185.10.0.10|ya.ru": self.make_run(
                    "185.10.0.10", ok_probes=0, blocked_probes=20).id,
                "185.10.0.20|ya.ru": self.make_run(
                    "185.10.0.20", ok_probes=19, blocked_probes=1).id,
            },
            {
                # Имя живо на живом адресе и не проходит на выгоревшем
                "185.10.0.20|de.example.xyz": self.make_run(
                    "185.10.0.20", ok_probes=19, blocked_probes=1,
                    sni="de.example.xyz").id,
                "185.10.0.10|de.example.xyz": self.make_run(
                    "185.10.0.10", ok_probes=0, blocked_probes=20,
                    sni="de.example.xyz").id,
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
        # Имя чистое: оно прошло на живом адресе
        self.assertEqual(verdict["blocked_snis"], [])
        # Замена создана автоматически, домен в неё входит
        replacement = self.session.query(InfraIpReplacement).one()
        self.assertEqual(replacement.old_ip, "185.10.0.10")
        self.assertEqual(replacement.domains, ["de.example.xyz"])
        self.assertEqual(replacement.log[0]["banned_names"], [])
        # На мёртвом адресе вердикт по имени не выносится: там падает всё
        self.assertEqual(verdict["pair_blocked"], [])

    def test_alert_reports_live_addresses_and_their_names(self):
        # «Какой адрес живой и с какими SNI отработал» — первое, что нужно
        # при разборе ночной аварии
        self.session.add(
            SystemSetting(key="infra_auto_replace_enabled", value="false")
        )
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.20", on_interface=True)
        self.make_domain(
            server, "de.example.xyz", client_snis=["example.org", "example.com"]
        )
        for name in ("example.org", "example.com"):
            self._seed_known_good(name)
        anomaly = self.make_anomaly(
            server,
            {
                "185.10.0.10|ya.ru": self.make_run(
                    "185.10.0.10", ok_probes=0, blocked_probes=20).id,
                "185.10.0.20|ya.ru": self.make_run(
                    "185.10.0.20", ok_probes=19, blocked_probes=1).id,
            },
            {
                "185.10.0.20|example.org": self.make_run(
                    "185.10.0.20", ok_probes=19, blocked_probes=1,
                    sni="example.org").id,
                "185.10.0.20|example.com": self.make_run(
                    "185.10.0.20", ok_probes=18, blocked_probes=2,
                    sni="example.com").id,
                "185.10.0.10|example.org": self.make_run(
                    "185.10.0.10", ok_probes=0, blocked_probes=20,
                    sni="example.org").id,
            },
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        text = " ".join(call[0][0] for call in alert.call_args_list)
        self.assertIn("Живые адреса", text)
        self.assertIn("185.10.0.20", text)
        self.assertIn("example.org", text)
        self.assertIn("example.com", text)

    def test_second_control_name_is_the_witness(self):
        # Клиентские имена на инбаунде Reality не отвечают на обычный
        # ClientHello зонда НИКОГДА (Reality пересылает соединение в dest).
        # Свидетелем такое имя быть не может — берётся следующее контрольное
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        run = self.make_run("185.10.0.10", ok_probes=0, blocked_probes=20)
        anomaly = self.make_anomaly(server, {"185.10.0.10|ya.ru": run.id})

        with mock.patch.object(
            infra, "start_sni_diagnosis",
            return_value={"runs": {"185.10.0.10|www.microsoft.com": 5},
                          "errors": [], "witness_names": ["www.microsoft.com"]},
        ) as started:
            with mock.patch.object(
                infra_worker, "_send_alert", return_value=True
            ):
                infra_worker.process_anomalies(self.session)
        self.session.commit()

        self.assertEqual(started.call_args[1]["suspect_ips"], ["185.10.0.10"])
        self.session.refresh(anomaly)
        self.assertEqual(anomaly.status, "checking_sni")

    def test_control_witness_pass_saves_the_address(self):
        # Контроль №1 упал, контроль №2 на том же адресе прошёл: адрес жив,
        # вердикта «забанен» нет — ровно случай 2.58.66.180 с google.ru
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(server, "de.example.xyz")
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": self.make_run(
                "185.10.0.10", ok_probes=0, blocked_probes=20).id},
            {"185.10.0.10|www.microsoft.com": self.make_run(
                "185.10.0.10", ok_probes=19, blocked_probes=1,
                sni="www.microsoft.com").id},
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        verdict = anomaly.details["verdict"]
        self.assertEqual(verdict["blocked_ips"], [])
        self.assertEqual(verdict["control_burn"], "proven")
        # Контрольное имя не получает вердикта «имя забанено»
        self.assertEqual(verdict["blocked_snis"], [])
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)

    def test_all_names_banned_cancels_replacement(self):
        # Все имена сервера под фильтром: новый адрес ничего не починит,
        # резерв сжигать нельзя
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_domain(
            server, "de.example.xyz", client_snis=["example.org", "example.com"]
        )
        for name in ("example.org", "example.com"):
            self._seed_known_good(name)
        ip_run = self.make_run("185.10.0.10", ok_probes=2, blocked_probes=18)
        live_run = self.make_run("185.10.0.11", ok_probes=18, blocked_probes=2)
        anomaly = self.make_anomaly(
            server,
            {"185.10.0.10|ya.ru": ip_run.id, "185.10.0.11|ya.ru": live_run.id},
            {
                "185.10.0.11|example.org": self.make_run(
                    "185.10.0.11", ok_probes=0, blocked_probes=20,
                    sni="example.org").id,
                "185.10.0.11|example.com": self.make_run(
                    "185.10.0.11", ok_probes=0, blocked_probes=20,
                    sni="example.com").id,
                "185.10.0.10|example.org": self.make_run(
                    "185.10.0.10", ok_probes=0, blocked_probes=20,
                    sni="example.org").id,
            },
            status="checking_sni",
        )

        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.process_anomalies(self.session)
        self.session.commit()
        self.session.refresh(anomaly)

        self.assertEqual(
            anomaly.details["verdict"]["blocked_ips"], ["185.10.0.10"]
        )
        self.assertEqual(self.session.query(InfraIpReplacement).count(), 0)
        self.assertTrue(
            any("все имена сервера" in call[0][0]
                for call in alert.call_args_list)
        )

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
        self.probe_calls = []

        def fake_probes(db, pairs, probe_ids=None, reason="", light=True):
            self.probe_calls.append(
                {"pairs": list(pairs), "reason": reason, "light": light}
            )
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

    def complete_step_runs(self, replacement, step, ok=True, per_name=None):
        """Завершает все прогоны шага (verifying_names / confirming) с нужным
        исходом; per_name — исход по конкретным именам."""
        entry = next(
            item for item in reversed(replacement.log or [])
            if item.get("step") == step
        )
        for key, run_id in entry["runs"].items():
            name = key.split("|", 1)[1]
            result = (per_name or {}).get(name, ok)
            run = self.session.get(CensorCheckRun, run_id)
            run.status = "complete"
            run.total_probes = 12
            run.ok_probes = 11 if result else 1
            run.results = [
                {"prb_id": 1, "ok": result,
                 "stage": "tls_ok" if result else "tls_fail"}
            ]
        self.session.commit()

    def cloudflare_recorder(self, records):
        """Cloudflare-моки с записью добавлений/удалений: (patchers, added,
        removed)."""
        added, removed = [], []
        patchers = (
            mock.patch("engine.cloudflare_dns.is_enabled", return_value=True),
            mock.patch(
                "engine.cloudflare_dns.list_a_records",
                side_effect=lambda domain: records.get(domain, []),
            ),
            mock.patch(
                "engine.cloudflare_dns.ensure_a_record",
                side_effect=lambda d, ip, ttl=60: added.append((d, ip)) or True,
            ),
            mock.patch(
                "engine.cloudflare_dns.delete_a_records",
                side_effect=lambda d, ip: removed.append((d, ip)) or 1,
            ),
        )
        return patchers, added, removed

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

            infra_worker.process_replacements(self.session)   # -> verifying_names
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "verifying_names")
            self.complete_step_runs(replacement, "verifying_names", ok=True)

            infra_worker.process_replacements(self.session)   # -> dns_add
            self.session.commit()
            infra_worker.process_replacements(self.session)   # -> dns_remove
            self.session.commit()
            infra_worker.process_replacements(self.session)   # -> confirming
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "confirming")

            # постпроверка показала, что связка не работает
            self.complete_step_runs(replacement, "confirming", ok=False)
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

        # Адрес без контроля не проверяется, но имена на кандидате —
        # проверяются всё равно: это отдельный шаг с клиентскими именами
        self.assertEqual(replacement.status, "verifying_names")
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

    def make_pending_with_snis(self, server, domain_snis, sni_banned=None):
        """Заявка с картой имён в журнале — как её создаёт диагностика."""
        replacement = InfraIpReplacement(
            server_id=server.id,
            old_ip="185.10.0.10",
            domains=list(domain_snis.keys()),
            status="pending",
            created_at=utcnow(),
            updated_at=utcnow(),
            log=[{
                "ts": "2026-09-08T00:00:00",
                "step": "created",
                "message": "Заявка на замену 185.10.0.10 (test)",
                "banned_names": [],
                "domain_snis": domain_snis,
                "sni_banned": sni_banned or {},
            }],
        )
        self.session.add(replacement)
        self.session.commit()
        return replacement

    def drive_to_names_verification(self, replacement):
        """Прогоняет заявку до шага проверки имён на кандидате."""
        infra_worker.process_replacements(self.session)
        self.session.commit()
        command = self.session.query(InfraAgentCommand).first()
        if command is not None:
            command.status = "ok"
            self.session.commit()
            infra_worker.process_replacements(self.session)
            self.session.commit()
        self.session.refresh(replacement)
        self.complete_verification(replacement, ok=True)
        infra_worker.process_replacements(self.session)
        self.session.commit()
        self.session.refresh(replacement)

    def test_candidate_publishes_domain_if_any_name_passes(self):
        # Три имени на домен: одно не проходит, два работают. Оставлять
        # клиентов рабочих протоколов на мёртвом адресе нельзя
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(
            server, "185.10.0.11", source="manual", on_interface=False,
            interface=None,
        )
        replacement = self.make_pending_with_snis(
            server,
            {"de.example.xyz": ["example.org", "example.com", "example.net"]},
        )

        cf1, cf2, cf3, cf4 = self.cloudflare_mocks()
        with cf1, cf2, cf3, cf4, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            self.drive_to_names_verification(replacement)
            self.assertEqual(replacement.status, "verifying_names")
            # Проверяются ВСЕ имена, лимита на этом шаге нет намеренно
            last = self.probe_calls[-1]
            self.assertEqual(
                [sni for _ip, sni in last["pairs"]],
                ["example.org", "example.com", "example.net"],
            )
            self.complete_step_runs(
                replacement, "verifying_names",
                per_name={"example.com": False},
            )
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)

        entry = next(
            item for item in reversed(replacement.log)
            if item.get("step") == "names_verified"
        )
        self.assertEqual(entry["publish"], ["de.example.xyz"])
        self.assertEqual(entry["skipped"], [])
        self.assertEqual(
            entry["failing_names"], {"de.example.xyz": ["example.com"]}
        )

    def test_candidate_publishes_domain_even_when_no_name_passes(self):
        # Ни одно имя не прошло на кандидате. Решение владельца: адрес всё
        # равно публикуем — кандидат признан живым контрольным именем, а
        # старый адрес мёртв. Какие имена не прошли, видно в журнале
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(
            server, "185.10.0.11", source="manual", on_interface=False,
            interface=None,
        )
        replacement = self.make_pending_with_snis(
            server, {"de.example.xyz": ["example.org", "example.com"]}
        )

        cf1, cf2, cf3, cf4 = self.cloudflare_mocks()
        with cf1, cf2, cf3, cf4, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            self.drive_to_names_verification(replacement)
            self.complete_step_runs(replacement, "verifying_names", ok=False)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)

        entry = next(
            item for item in reversed(replacement.log)
            if item.get("step") == "names_verified"
        )
        self.assertEqual(entry["publish"], ["de.example.xyz"])
        self.assertEqual(entry["skipped"], [])
        self.assertEqual(
            entry["failing_names"],
            {"de.example.xyz": ["example.org", "example.com"]},
        )

    def test_candidate_publishes_when_only_banned_names_fail(self):
        # Упали ровно те имена, которые вердикт уже назвал забаненными: на
        # новом адресе они и не могли пройти, а старый адрес мёртв
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(
            server, "185.10.0.11", source="manual", on_interface=False,
            interface=None,
        )
        replacement = self.make_pending_with_snis(
            server,
            {"de.example.xyz": ["example.org"]},
            sni_banned={"de.example.xyz": ["example.org"]},
        )

        cf1, cf2, cf3, cf4 = self.cloudflare_mocks()
        with cf1, cf2, cf3, cf4, mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            self.drive_to_names_verification(replacement)
            self.complete_step_runs(replacement, "verifying_names", ok=False)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)

        entry = next(
            item for item in reversed(replacement.log)
            if item.get("step") == "names_verified"
        )
        self.assertEqual(entry["publish"], ["de.example.xyz"])

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

            # проверка показала, что кандидат чист -> verifying_names:
            # каждое клиентское имя проверяется на кандидате отдельно
            self.complete_verification(replacement, ok=True)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)
            self.assertEqual(replacement.status, "verifying_names")

            # имя на кандидате проходит -> dns_add
            self.complete_step_runs(replacement, "verifying_names", ok=True)
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
            self.complete_step_runs(replacement, "confirming", ok=True)
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
        # Пробы кандидата, имён и постпроверки идут ПОЛНЫМ набором зондов
        light_by_reason = {
            call["reason"]: call["light"] for call in self.probe_calls
        }
        self.assertEqual(
            light_by_reason,
            {
                "CANDIDATE_VERIFY": False,
                "CANDIDATE_NAMES_VERIFY": False,
                "REPLACEMENT_CONFIRM": False,
            },
        )

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
        # Установка не нужна, но проверка адреса и имён на нём — нужна:
        # 2026-09-02 .198 стоял на интерфейсе и два имени на нём не проходили
        self.assertEqual(replacement.status, "verifying")
        self.assertEqual(replacement.new_ip, "185.10.0.11")
        self.assertEqual(self.session.query(InfraAgentCommand).count(), 0)

    def _run_flow_until_confirming(self, replacement, per_name=None):
        """pending → verifying → verifying_names → dns_add → dns_remove →
        confirming для кандидата, уже стоящего на интерфейсе."""
        infra_worker.process_replacements(self.session)   # -> verifying
        self.session.commit()
        self.session.refresh(replacement)
        self.assertEqual(replacement.status, "verifying")
        self.complete_verification(replacement, ok=True)
        infra_worker.process_replacements(self.session)   # -> verifying_names
        self.session.commit()
        self.session.refresh(replacement)
        self.assertEqual(replacement.status, "verifying_names")
        self.complete_step_runs(
            replacement, "verifying_names", ok=True, per_name=per_name
        )
        infra_worker.process_replacements(self.session)   # -> dns_add
        self.session.commit()
        infra_worker.process_replacements(self.session)   # -> dns_remove
        self.session.commit()
        infra_worker.process_replacements(self.session)   # -> confirming
        self.session.commit()
        self.session.refresh(replacement)

    def test_domain_is_published_even_when_its_name_fails_on_candidate(self):
        # Кандидат жив по контролю, имя a на нём проходит, имя b — нет.
        # Решение владельца: DNS переставляется всё равно — старый адрес
        # мёртв, оставлять на нём клиентов хуже любого исхода здесь
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=True)
        replacement = self.make_pending(
            server, domains=["a.example.xyz", "b.example.xyz"]
        )
        patchers, added, removed = self.cloudflare_recorder({
            "a.example.xyz": [{"id": "r1", "content": "185.10.0.10"}],
            "b.example.xyz": [
                {"id": "r2", "content": "185.10.0.10"},
                {"id": "r3", "content": "185.10.0.99"},
            ],
        })
        with patchers[0], patchers[1], patchers[2], patchers[3], mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            self._run_flow_until_confirming(
                replacement,
                per_name={"a.example.xyz": True, "b.example.xyz": False},
            )
            self.assertEqual(replacement.status, "confirming")
            self.complete_step_runs(replacement, "confirming", ok=True)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)

        self.assertEqual(replacement.status, "done")
        self.assertEqual(
            sorted(added),
            [("a.example.xyz", "185.10.0.11"), ("b.example.xyz", "185.10.0.11")],
        )
        self.assertEqual(
            sorted(removed),
            [("a.example.xyz", "185.10.0.10"), ("b.example.xyz", "185.10.0.10")],
        )
        text = alert.call_args[0][0]
        self.assertIn("IP автоматически заменён", text)
        self.assertIn("b.example.xyz", text)
        journal = " ".join(item["message"] for item in replacement.log or [])
        self.assertIn("не прошло ни одно его имя", journal)

    def test_last_record_is_kept_for_unpublished_name(self):
        # Домен b исключён из публикации явным banned_names: новый адрес под
        # него не пишется, а старый — его единственная запись. Не удаляем:
        # домен без записей хуже домена с мёртвой
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=True)
        replacement = infra.request_replacement(
            self.session, server.id, "185.10.0.10", "test",
            domains=["a.example.xyz", "b.example.xyz"],
            banned_names=["b.example.xyz"],
        )
        self.session.commit()
        patchers, added, removed = self.cloudflare_recorder({
            "a.example.xyz": [{"id": "r1", "content": "185.10.0.10"}],
            "b.example.xyz": [{"id": "r2", "content": "185.10.0.10"}],
        })
        with patchers[0], patchers[1], patchers[2], patchers[3], mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ):
            self._run_flow_until_confirming(
                replacement,
                per_name={"a.example.xyz": True, "b.example.xyz": False},
            )
        self.assertEqual(removed, [("a.example.xyz", "185.10.0.10")])
        journal = " ".join(item["message"] for item in replacement.log or [])
        self.assertIn("единственная", journal)

    def test_banned_name_is_not_probed_but_dead_ip_removed(self):
        # Имя b забанено по вердикту диагностики: на кандидате оно не
        # проверяется и не публикуется, но мёртвый адрес из его записей
        # убирается
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=True)
        replacement = infra.request_replacement(
            self.session, server.id, "185.10.0.10", "test",
            domains=["a.example.xyz", "b.example.xyz"],
            banned_names=["b.example.xyz"],
        )
        self.session.commit()
        patchers, added, removed = self.cloudflare_recorder({
            "a.example.xyz": [{"id": "r1", "content": "185.10.0.10"}],
            "b.example.xyz": [
                {"id": "r2", "content": "185.10.0.10"},
                {"id": "r3", "content": "185.10.0.99"},
            ],
        })
        with patchers[0], patchers[1], patchers[2], patchers[3], mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            self._run_flow_until_confirming(replacement)
            self.complete_step_runs(replacement, "confirming", ok=True)
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)

        names_probe = [
            call for call in self.probe_calls
            if call["reason"] == "CANDIDATE_NAMES_VERIFY"
        ]
        self.assertEqual(names_probe[0]["pairs"], [("185.10.0.11", "a.example.xyz")])
        self.assertEqual(added, [("a.example.xyz", "185.10.0.11")])
        self.assertEqual(
            sorted(removed),
            [("a.example.xyz", "185.10.0.10"), ("b.example.xyz", "185.10.0.10")],
        )
        text = alert.call_args[0][0]
        self.assertIn("имя забанено", text)
        self.assertIn("b.example.xyz", text)

    def test_confirmation_covers_all_published_names(self):
        # Постпроверка идёт по всем опубликованным именам, а не по первому
        # по алфавиту; неработающая связка — честный алерт с именем
        server = self.make_server()
        self.make_ip(server, "185.10.0.10", on_interface=True)
        self.make_ip(server, "185.10.0.11", on_interface=True)
        replacement = self.make_pending(
            server, domains=["a.example.xyz", "b.example.xyz"]
        )
        patchers, added, removed = self.cloudflare_recorder({
            "a.example.xyz": [{"id": "r1", "content": "185.10.0.10"}],
            "b.example.xyz": [{"id": "r2", "content": "185.10.0.10"}],
        })
        with patchers[0], patchers[1], patchers[2], patchers[3], mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            self._run_flow_until_confirming(replacement)
            confirm = [
                call for call in self.probe_calls
                if call["reason"] == "REPLACEMENT_CONFIRM"
            ]
            self.assertEqual(
                sorted(confirm[0]["pairs"]),
                [("185.10.0.11", "a.example.xyz"), ("185.10.0.11", "b.example.xyz")],
            )
            self.complete_step_runs(
                replacement, "confirming",
                per_name={"a.example.xyz": True, "b.example.xyz": False},
            )
            infra_worker.process_replacements(self.session)
            self.session.commit()
            self.session.refresh(replacement)

        self.assertEqual(replacement.status, "done")
        text = alert.call_args[0][0]
        self.assertIn("проблема не решена", text)
        self.assertIn("b.example.xyz", text)
        self.assertEqual(
            sorted(added),
            [("a.example.xyz", "185.10.0.11"), ("b.example.xyz", "185.10.0.11")],
        )

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

    def test_tspu_checks_toggle(self):
        server = self.make_server()
        self.assertTrue(server.tspu_checks_enabled)
        infra.set_tspu_checks_enabled(self.session, server.id, False)
        self.session.commit()
        self.session.refresh(server)
        self.assertFalse(server.tspu_checks_enabled)
        infra.set_tspu_checks_enabled(self.session, server.id, True)
        self.session.commit()
        self.session.refresh(server)
        self.assertTrue(server.tspu_checks_enabled)


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

    # --- массовая потеря heartbeat: один сводный алерт --------------------

    def _mass_servers(self, count, **kwargs):
        return [
            self.make_server(
                machine_uid=f"m-mass-{index}", node_name=f"xx-{index}", **kwargs
            )
            for index in range(count)
        ]

    def test_mass_offline_sends_single_alert_with_diagnosis(self):
        # Инцидент 2026-09-02: 35 нод разом «OFFLINE» из-за коллектора/БД —
        # один сводный алерт с диагнозом вместо 35 одинаковых 🔴
        servers = self._mass_servers(
            6, last_seen_at=utcnow() - timedelta(minutes=15)
        )
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        self.session.commit()
        alert.assert_called_once()
        text_value = alert.call_args[0][0]
        self.assertIn("Мониторинг ослеп", text_value)
        self.assertIn("6 из 6", text_value)
        self.assertIn("xx-0", text_value)
        self.assertIn("коллектор", text_value)
        self.assertIn("автозамена", text_value)
        for server in servers:
            self.session.refresh(server)
            self.assertIsNotNone(server.offline_alerted_at)

        # Повторный тик — дедуп, тишина
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        alert.assert_not_called()

    def test_mass_recovery_sends_single_alert(self):
        servers = self._mass_servers(
            6,
            last_seen_at=utcnow(),
            offline_alerted_at=utcnow() - timedelta(minutes=10),
        )
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        self.session.commit()
        alert.assert_called_once()
        text_value = alert.call_args[0][0]
        self.assertIn("Мониторинг восстановлен", text_value)
        self.assertIn("6 серверов", text_value)
        for server in servers:
            self.session.refresh(server)
            self.assertIsNotNone(server.recovered_alerted_at)
            self.assertGreater(
                server.recovered_alerted_at, server.offline_alerted_at
            )

    def test_below_threshold_alerts_per_server(self):
        self._mass_servers(3, last_seen_at=utcnow() - timedelta(minutes=15))
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        self.assertEqual(alert.call_count, 3)
        for call in alert.call_args_list:
            self.assertIn("Сервер недоступен", call[0][0])

    def test_mass_alert_disabled_by_zero_threshold(self):
        self.session.add(
            SystemSetting(key="infra_mass_offline_threshold", value="0")
        )
        self.session.commit()
        self._mass_servers(6, last_seen_at=utcnow() - timedelta(minutes=15))
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        self.assertEqual(alert.call_count, 6)

    def test_mass_alert_caps_listed_names(self):
        self._mass_servers(20, last_seen_at=utcnow() - timedelta(minutes=15))
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=True
        ) as alert:
            infra_worker.check_offline(self.session)
        alert.assert_called_once()
        self.assertIn("и ещё 5", alert.call_args[0][0])

    def test_undelivered_mass_alert_keeps_servers_unalerted(self):
        servers = self._mass_servers(
            6, last_seen_at=utcnow() - timedelta(minutes=15)
        )
        with mock.patch.object(
            infra_worker, "_send_alert", return_value=False
        ):
            infra_worker.check_offline(self.session)
        self.session.commit()
        for server in servers:
            self.session.refresh(server)
            self.assertIsNone(server.offline_alerted_at)

    def test_zero_is_valid_only_for_mass_threshold(self):
        self.assertEqual(
            infra.validate_setting("infra_mass_offline_threshold", "0"), "0"
        )
        with self.assertRaises(infra.InfraError):
            infra.validate_setting("infra_offline_after_seconds", "0")


class RunStepTests(SimpleTestCase):
    """Шаг тика воркера: statement_timeout на транзакцию, commit/rollback."""

    class _FakeSession:
        def __init__(self):
            self.statements = []
            self.committed = False
            self.rolled_back = False
            self.closed = False

        def execute(self, statement, *args, **kwargs):
            self.statements.append(str(statement))

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    def _run(self, fn, dialect="postgresql", timeout=120):
        session = self._FakeSession()
        fake_engine = mock.Mock()
        fake_engine.dialect.name = dialect
        with mock.patch.object(
            infra_worker, "session_factory", return_value=session
        ), mock.patch.object(infra_worker, "engine", fake_engine), self.settings(
            INFRA_WORKER_STEP_TIMEOUT=timeout
        ):
            ok = infra_worker._run_step("step", fn)
        return ok, session

    def test_statement_timeout_is_set_local_on_postgres(self):
        # Инцидент 2026-09-02: INSERT агрегации висел 28 минут и весь тик
        # с ним — OFFLINE-проверка и детектор не выполнялись вовсе
        ok, session = self._run(lambda db: None)
        self.assertTrue(ok)
        self.assertEqual(
            session.statements, ["SET LOCAL statement_timeout = 120000"]
        )
        self.assertTrue(session.committed)
        self.assertTrue(session.closed)

    def test_no_timeout_on_other_dialects_or_when_disabled(self):
        _ok, session = self._run(lambda db: None, dialect="sqlite")
        self.assertEqual(session.statements, [])
        _ok, session = self._run(lambda db: None, timeout=0)
        self.assertEqual(session.statements, [])

    def test_failed_step_rolls_back_and_does_not_raise(self):
        def boom(db):
            raise RuntimeError("canceling statement due to statement timeout")

        with self.assertLogs("infra-worker", level="ERROR"):
            ok, session = self._run(boom)
        self.assertFalse(ok)
        self.assertTrue(session.rolled_back)
        self.assertFalse(session.committed)
        self.assertTrue(session.closed)


class MaintenanceOrderTests(SimpleTestCase):
    def test_offline_check_runs_first_and_aggregate_before_detector(self):
        names = []

        def fake_step(name, fn, *args):
            names.append(name)
            return True

        saved = infra_worker._tick_counter
        try:
            # Следующий тик — чётный: детектор аномалий в него попадает
            infra_worker._tick_counter = infra_worker._ANOMALY_EVERY_TICKS - 1
            with mock.patch.object(
                infra_worker, "_run_step", side_effect=fake_step
            ), mock.patch.object(
                infra_worker, "_infra_tables_ready", return_value=True
            ), mock.patch("engine.geoip_updater.update_if_due"):
                infra_worker.run_maintenance()
        finally:
            infra_worker._tick_counter = saved

        self.assertEqual(names[0], "offline")
        self.assertLess(names.index("aggregate"), names.index("anomaly-detect"))
        self.assertIn("replacements", names)


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
        self.assertTrue(item["tspu_checks_enabled"])

    def test_payloads_expose_tspu_flag(self):
        server = self.make_server(tspu_checks_enabled=False)
        listed = infra.server_list_payload(self.session)["servers"][0]
        self.assertFalse(listed["tspu_checks_enabled"])
        detail = infra.server_detail_payload(self.session, server.id)
        self.assertFalse(detail["server"]["tspu_checks_enabled"])

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
        self.assertFalse(diagnosis["stale"])
        self.assertLess(diagnosis["age_minutes"], 5)

    def test_old_diagnosis_is_marked_stale(self):
        # Вердикт — снимок момента, а не свойство домена: бан пары
        # «адрес + имя» снимался сам за пару часов (2026-09-02). Недельной
        # давности вердикт, показанный как текущий, дезинформирует
        server = self.make_server()
        old_at = utcnow() - timedelta(days=7)
        self.session.add(
            InfraAnomaly(
                server_id=server.id,
                status="confirmed",
                details={
                    "verdict": {
                        "blocked_ips": [], "blocked_snis": ["de.example.xyz"],
                        "confidence": "high", "actionable": True,
                    },
                },
                created_at=old_at,
                resolved_at=old_at,
            )
        )
        self.session.commit()

        diagnosis = infra.server_detail_payload(
            self.session, server.id
        )["diagnosis"]
        self.assertTrue(diagnosis["stale"])
        self.assertGreater(diagnosis["age_minutes"], infra.DIAGNOSIS_FRESH_MINUTES)
        # Сам вердикт остаётся в карточке как история
        self.assertEqual(diagnosis["blocked_snis"], ["de.example.xyz"])

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

    def test_domain_sni_actions(self):
        server = self.make_server()
        response = self.views.support_admin_api_infra_servers(
            self.factory.post(
                "/support-admin/api/infra-servers/",
                {
                    "action": "add_domain", "id": server.id,
                    "domain": "de.monkora.org",
                    "client_snis": "example.org, example.com",
                },
            )
        )
        self.assertEqual(response.status_code, 200)

        response = self.views.support_admin_api_infra_servers(
            self.factory.post(
                "/support-admin/api/infra-servers/",
                {
                    "action": "set_domain_snis", "id": server.id,
                    "domain": "de.monkora.org",
                    "client_snis": "example.com,localhost",
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        check_session = self.Session()
        try:
            row = (
                check_session.query(InfraServerDomain)
                .filter(InfraServerDomain.server_id == server.id)
                .one()
            )
            self.assertEqual(row.client_snis, ["example.com", "localhost"])
        finally:
            check_session.close()

        # Мусор в поле не должен уехать в Atlas одним именем
        response = self.views.support_admin_api_infra_servers(
            self.factory.post(
                "/support-admin/api/infra-servers/",
                {
                    "action": "set_domain_snis", "id": server.id,
                    "domain": "de.monkora.org", "client_snis": "a/b",
                },
            )
        )
        self.assertEqual(response.status_code, 400)

    def test_server_sni_action(self):
        server = self.make_server()
        response = self.views.support_admin_api_infra_servers(
            self.factory.post(
                "/support-admin/api/infra-servers/",
                {
                    "action": "set_server_snis", "id": server.id,
                    "client_snis": "Example.org, localhost",
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        import json as json_module

        self.assertEqual(
            json_module.loads(response.content)["client_snis"],
            ["example.org", "localhost"],
        )
        check_session = self.Session()
        try:
            row = check_session.get(InfraServer, server.id)
            self.assertEqual(row.client_snis, ["example.org", "localhost"])
        finally:
            check_session.close()

        # Мусор в поле не должен уехать в Atlas одним именем
        response = self.views.support_admin_api_infra_servers(
            self.factory.post(
                "/support-admin/api/infra-servers/",
                {"action": "set_server_snis", "id": server.id, "client_snis": "a/b"},
            )
        )
        self.assertEqual(response.status_code, 400)

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
