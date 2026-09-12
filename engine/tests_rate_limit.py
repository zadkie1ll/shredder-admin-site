"""Лимиты частоты, IP клиента за edge и связанные настройки сайта.

Без БД и сети: LocMemCache, модель команд Redis вместо сервера, моки сессий.
"""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.core.cache.backends.redis import RedisCache
from django.test import RequestFactory, SimpleTestCase, override_settings

from common.models.db import MagicToken
from engine import rate_limit
from engine import request_ip
from engine import views
from engine.rate_limit import ip_rate_limit_skipped
from engine.rate_limit import rate_limit_exceeded
from engine.rate_limit import rate_limit_hit
from engine.rate_limit import rate_limit_peek
from engine.request_ip import client_ip
from engine.request_ip import is_trusted_proxy_address
from engine.request_ip import is_unresolved_client_ip
from web_app import settings as project_settings


EDGE_IP = "203.0.113.10"
DOCKER_NGINX_IP = "172.18.0.4"
# Шлюз bridge-сети edge: с него docker userland-proxy отдаёт nginx IPv6-клиентов.
DOCKER_GATEWAY_IP = "172.19.0.1"
REAL_CLIENT_IP = "198.51.100.7"
UNRESOLVED_IP_WARNING = (
    "client IP unresolved (proxy chain fully trusted): IP rate limit skipped "
    "— check edge IPv6/docker-proxy"
)
PRIVATE_PROXY_NETWORKS = project_settings.csv_list(
    project_settings.DEFAULT_TRUSTED_PROXY_NETWORKS
)
EDGE_TOPOLOGY_TRUSTED = project_settings.unique_list(
    PRIVATE_PROXY_NETWORKS, [f"{EDGE_IP}/32"]
)
REPO_ROOT = Path(__file__).resolve().parent.parent


def load_settings_module(env=None, unset=()):
    """Выполнить web_app/settings.py заново с подменённым окружением."""
    patched = dict(os.environ)
    patched.update(env or {})
    for key in unset:
        patched.pop(key, None)
    spec = importlib.util.spec_from_file_location(
        "web_app_settings_probe", project_settings.__file__
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, patched, clear=True):
        spec.loader.exec_module(module)
    return module


class _FrozenClockMixin:
    clock = 1_000_020.0

    def freeze_rate_limit_clock(self):
        patcher = mock.patch.object(rate_limit, "_now", side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reset_unresolved_ip_warning(self):
        """Предупреждение о неопределённом IP пишется раз в 10 минут на процесс:
        тест начинает с чистого состояния и возвращает прежнее после себя."""
        patcher = mock.patch.object(rate_limit, "_unresolved_ip_warned_at", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def unresolved_ip_warnings(self, logs):
        return [line for line in logs.output if UNRESOLVED_IP_WARNING in line]


class _FakeRedis:
    """Модель нужных RedisCacheClient команд: SET NX EX, EXISTS, INCR, EXPIRE, GET.

    ``expire_between_exists_and_incr`` имитирует истечение окна ровно между
    EXISTS и INCR внутри ``cache.incr``: настоящий Redis INCR тогда создаёт
    ключ со значением 1 и без TTL.
    """

    def __init__(self):
        self.values = {}
        self.ttl = {}
        self.expire_between_exists_and_incr = False

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.ttl[key] = ex
        return True

    def exists(self, key):
        present = key in self.values
        if present and self.expire_between_exists_and_incr:
            self.values.pop(key)
            self.ttl.pop(key)
            self.expire_between_exists_and_incr = False
        return int(present)

    def incr(self, key, delta=1):
        if key not in self.values:
            self.values[key] = 0
            self.ttl[key] = None
        self.values[key] = int(self.values[key]) + delta
        return self.values[key]

    def expire(self, key, timeout):
        if key not in self.values:
            return False
        self.ttl[key] = timeout
        return True

    def get(self, key):
        return self.values.get(key)


@override_settings(SECRET_KEY="rate-limit-test-secret")
class RateLimitTests(_FrozenClockMixin, SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.freeze_rate_limit_clock()

    def test_bucket_is_limited_after_configured_count(self):
        bucket = (("email", "user@example.com", 1, 90),)

        self.assertEqual(rate_limit_exceeded("magic", bucket), (False, 0))
        self.assertEqual(rate_limit_exceeded("magic", bucket), (True, 90))

    def test_identifiers_and_scopes_have_separate_buckets(self):
        first = (("email", "first@example.com", 1, 90),)
        second = (("email", "second@example.com", 1, 90),)

        self.assertFalse(rate_limit_exceeded("magic", first)[0])
        self.assertFalse(rate_limit_exceeded("magic", second)[0])
        self.assertFalse(rate_limit_exceeded("admin", first)[0])

    def test_rejected_request_does_not_consume_following_buckets(self):
        def buckets(ip):
            return (("ip", ip, 1, 900), ("global", "all", 2, 60))

        self.assertEqual(rate_limit_exceeded("magic", buckets("198.51.100.66")), (False, 0))
        for _ in range(20):
            self.assertEqual(
                rate_limit_exceeded("magic", buckets("198.51.100.66")), (True, 900)
            )

        # Отбитые по IP запросы общий бакет не расходовали.
        self.assertEqual(rate_limit_exceeded("magic", buckets("198.51.100.77")), (False, 0))

    def test_non_positive_or_missing_limit_disables_bucket(self):
        for limit in (0, -1, None):
            with self.subTest(limit=limit):
                bucket = (("global", f"all-{limit}", limit, 60),)
                for _ in range(5):
                    self.assertEqual(rate_limit_exceeded("magic", bucket), (False, 0))

        buckets = (("global", "all", 0, 60), ("ip", "192.0.2.1", 1, 30))
        self.assertEqual(rate_limit_exceeded("magic", buckets), (False, 0))
        self.assertEqual(rate_limit_exceeded("magic", buckets), (True, 30))

    def test_non_positive_window_disables_only_that_bucket(self):
        buckets = (("global", "all", 1, 0), ("ip", "192.0.2.1", 1, 30))

        self.assertEqual(rate_limit_exceeded("magic", buckets), (False, 0))
        self.assertEqual(rate_limit_exceeded("magic", buckets), (True, 30))

    def test_next_window_starts_a_new_counter(self):
        bucket = (("ip", "192.0.2.1", 2, 60),)
        self.clock = 6_000.0

        self.assertFalse(rate_limit_exceeded("magic", bucket)[0])
        self.assertFalse(rate_limit_exceeded("magic", bucket)[0])
        self.assertTrue(rate_limit_exceeded("magic", bucket)[0])

        self.clock = 6_060.0
        self.assertEqual(rate_limit_exceeded("magic", bucket), (False, 0))

    def test_cache_keys_do_not_contain_identifier_and_include_window(self):
        fake_cache = mock.Mock()
        fake_cache.add.return_value = True
        fake_cache.incr.return_value = 2
        self.clock = 9_000.0

        with mock.patch.object(rate_limit, "cache", fake_cache):
            rate_limit_exceeded("magic", (("email", "victim@example.com", 5, 900),))

        key = fake_cache.add.call_args.args[0]
        self.assertTrue(key.startswith("rate-limit:magic:email:10:"), key)
        self.assertNotIn("victim", key)
        fake_cache.touch.assert_not_called()

    def test_cache_failure_fails_open_and_is_logged(self):
        broken = mock.Mock()
        broken.add.side_effect = ConnectionError("redis down")
        broken.get.side_effect = ConnectionError("redis down")
        bucket = (("ip", "192.0.2.1", 1, 60),)

        with mock.patch.object(rate_limit, "cache", broken):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(rate_limit_exceeded("magic", bucket), (False, 0))
            with self.assertLogs(level="ERROR"):
                self.assertEqual(rate_limit_peek("magic", bucket), (False, 0))
            with self.assertLogs(level="ERROR"):
                self.assertEqual(rate_limit_hit("magic", bucket), {})

    def test_key_recreated_by_racy_redis_incr_gets_ttl_back(self):
        fake_redis = _FakeRedis()
        redis_cache = RedisCache("redis://127.0.0.1:1/0", {})
        bucket = (("global", "all", 500, 60),)

        with mock.patch(
            "django.core.cache.backends.redis.RedisCacheClient.get_client",
            return_value=fake_redis,
        ), mock.patch.object(rate_limit, "cache", redis_cache):
            self.assertEqual(rate_limit_exceeded("magic-link", bucket), (False, 0))
            fake_redis.expire_between_exists_and_incr = True
            self.assertEqual(rate_limit_exceeded("magic-link", bucket), (False, 0))

        self.assertEqual(len(fake_redis.values), 1)
        self.assertEqual(
            list(fake_redis.ttl.values()),
            [60],
            "ключ, созданный INCR в гонке, остался без TTL",
        )


@override_settings(SECRET_KEY="rate-limit-test-secret")
class RateLimitPeekAndHitTests(_FrozenClockMixin, SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.freeze_rate_limit_clock()

    def test_peek_never_consumes_and_hit_never_blocks(self):
        bucket = (("account-ip", "shared|192.0.2.1", 2, 900),)

        for _ in range(5):
            self.assertEqual(rate_limit_peek("admin-login", bucket), (False, 0))
        self.assertEqual(rate_limit_hit("admin-login", bucket), {"account-ip": 1})
        self.assertEqual(rate_limit_peek("admin-login", bucket), (False, 0))
        self.assertEqual(rate_limit_hit("admin-login", bucket), {"account-ip": 2})
        self.assertEqual(rate_limit_peek("admin-login", bucket), (True, 900))
        self.assertEqual(rate_limit_hit("admin-login", bucket), {"account-ip": 3})

    def test_hit_skips_disabled_buckets(self):
        buckets = (("off", "x", 0, 60), ("on", "x", 5, 60))

        self.assertEqual(rate_limit_hit("admin-login", buckets), {"on": 1})
        self.assertEqual(rate_limit_peek("admin-login", buckets), (False, 0))


@override_settings(
    SECRET_KEY="rate-limit-test-secret", TRUSTED_PROXY_NETWORKS=EDGE_TOPOLOGY_TRUSTED
)
class UnresolvedClientIpBucketTests(_FrozenClockMixin, SimpleTestCase):
    """AUTHZ-F1: бакет "ip" не считается и не блокирует, если IP клиента не определён.

    IPv6-клиенты edge за docker userland-proxy приходят с адреса шлюза bridge,
    и один бакет на всех запер бы их разом. Остальные бакеты не меняются.
    """

    UNRESOLVED = (
        None,
        "",
        "unknown",
        DOCKER_GATEWAY_IP,
        "10.0.0.7",
        "192.168.1.5",
        "fd00::7",
        "127.0.0.1",
        "::1",
        "169.254.1.1",
        "fe80::1",
        "0.0.0.0",
        "::",
        "::ffff:172.19.0.1",
        EDGE_IP,
        DOCKER_NGINX_IP,
    )

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.freeze_rate_limit_clock()
        self.reset_unresolved_ip_warning()

    def test_unresolved_ip_bucket_is_neither_counted_nor_blocking(self):
        with self.assertLogs(level="WARNING") as logs:
            for identifier in self.UNRESOLVED:
                with self.subTest(identifier=identifier):
                    bucket = (("ip", identifier, 1, 900),)
                    untouched = mock.Mock()
                    with mock.patch.object(rate_limit, "cache", untouched):
                        for _ in range(5):
                            self.assertEqual(
                                rate_limit_exceeded("magic-link", bucket), (False, 0)
                            )
                        self.assertEqual(
                            rate_limit_peek("admin-login", bucket), (False, 0)
                        )
                        self.assertEqual(rate_limit_hit("admin-login", bucket), {})
                    self.assertEqual(untouched.method_calls, [])

        self.assertEqual(len(self.unresolved_ip_warnings(logs)), 1)

    def test_public_ip_bucket_counts_and_blocks_as_before(self):
        # Документационные сети ipaddress.is_private считает частными, но для
        # лимитов это обычные адреса клиентов.
        for identifier in ("45.10.0.66", REAL_CLIENT_IP, "192.0.2.1", "2a00:1450:4001::1"):
            with self.subTest(identifier=identifier):
                bucket = (("ip", identifier, 2, 900),)
                with self.assertNoLogs(level="WARNING"):
                    self.assertFalse(ip_rate_limit_skipped("magic-link", identifier))
                    self.assertEqual(rate_limit_exceeded("magic-link", bucket), (False, 0))
                    self.assertEqual(rate_limit_exceeded("magic-link", bucket), (False, 0))
                    self.assertEqual(rate_limit_exceeded("magic-link", bucket), (True, 900))
                    self.assertEqual(rate_limit_peek("magic-link", bucket), (True, 900))
                    self.assertEqual(rate_limit_hit("magic-link", bucket), {"ip": 4})

    def test_other_buckets_still_count_for_unresolved_addresses(self):
        buckets = (("ip", DOCKER_GATEWAY_IP, 1, 900), ("global", "all", 2, 60))

        with self.assertLogs(level="WARNING"):
            self.assertEqual(rate_limit_exceeded("magic-link", buckets), (False, 0))
            self.assertEqual(rate_limit_exceeded("magic-link", buckets), (False, 0))
            self.assertEqual(rate_limit_exceeded("magic-link", buckets), (True, 60))

            login_buckets = (
                ("ip", DOCKER_GATEWAY_IP, 1, 900),
                ("account-ip", f"shared|{DOCKER_GATEWAY_IP}", 2, 900),
                ("email", "10.0.0.7", 1, 900),
            )
            self.assertEqual(
                rate_limit_hit("admin-login", login_buckets),
                {"account-ip": 1, "email": 1},
            )
            self.assertEqual(
                rate_limit_hit("admin-login", login_buckets),
                {"account-ip": 2, "email": 2},
            )
            self.assertEqual(rate_limit_peek("admin-login", login_buckets), (True, 900))

    def test_warning_is_logged_at_most_once_per_ten_minutes(self):
        clock = [7_000.0]
        bucket = (("ip", "unknown", 1, 900),)

        with mock.patch.object(rate_limit, "_monotonic", side_effect=lambda: clock[0]):
            with self.assertLogs(level="WARNING") as logs:
                for offset in (0, 300, 599.9):
                    clock[0] = 7_000.0 + offset
                    rate_limit_exceeded("magic-link", bucket)
                    self.assertTrue(
                        ip_rate_limit_skipped("mobile-exchange", DOCKER_GATEWAY_IP)
                    )
                self.assertEqual(len(self.unresolved_ip_warnings(logs)), 1)

                clock[0] = 7_600.0
                self.assertTrue(
                    ip_rate_limit_skipped("payment-anonymous", DOCKER_GATEWAY_IP)
                )
                clock[0] = 8_100.0
                rate_limit_exceeded("magic-link", bucket)

        warnings = self.unresolved_ip_warnings(logs)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all(line.startswith("WARNING") for line in warnings))
        self.assertIn("scope=magic-link ip=unknown", warnings[0])
        self.assertIn(f"scope=payment-anonymous ip={DOCKER_GATEWAY_IP}", warnings[1])


class DjangoFreeCommonTests(SimpleTestCase):
    def test_common_submodule_has_no_django_modules(self):
        """TR-06: common подключают бот и payment без Django."""
        offenders = []
        for path in sorted((REPO_ROOT / "common").rglob("*.py")):
            if "alembic" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "from django" in text or "import django" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])


class TrustedProxySettingsTests(SimpleTestCase):
    def test_origin_allowlist_is_appended_to_explicit_trusted_networks(self):
        module = load_settings_module(
            {
                "TRUSTED_PROXY_NETWORKS": "172.16.0.0/12, 203.0.113.10/32",
                "ORIGIN_ALLOWED_PROXY_CIDRS": "203.0.113.10/32,203.0.113.11/32",
            }
        )

        self.assertEqual(
            module.TRUSTED_PROXY_NETWORKS,
            ["172.16.0.0/12", "203.0.113.10/32", "203.0.113.11/32"],
        )

    def test_origin_allowlist_extends_default_private_networks(self):
        base = load_settings_module(
            {"ORIGIN_ALLOWED_PROXY_CIDRS": ""}, unset=("TRUSTED_PROXY_NETWORKS",)
        )
        with_edge = load_settings_module(
            {"ORIGIN_ALLOWED_PROXY_CIDRS": f"{EDGE_IP}/32"},
            unset=("TRUSTED_PROXY_NETWORKS",),
        )

        self.assertEqual(
            with_edge.TRUSTED_PROXY_NETWORKS,
            base.TRUSTED_PROXY_NETWORKS + [f"{EDGE_IP}/32"],
        )

    def test_real_edge_topology_resolves_client_from_settings(self):
        """REMOTE_ADDR — docker nginx, XFF «клиент, публичный edge»."""
        module = load_settings_module(
            {"ORIGIN_ALLOWED_PROXY_CIDRS": f"{EDGE_IP}/32"},
            unset=("TRUSTED_PROXY_NETWORKS",),
        )
        request = SimpleNamespace(
            META={
                "REMOTE_ADDR": DOCKER_NGINX_IP,
                "HTTP_X_FORWARDED_FOR": f"{REAL_CLIENT_IP}, {EDGE_IP}",
            }
        )

        with override_settings(TRUSTED_PROXY_NETWORKS=module.TRUSTED_PROXY_NETWORKS):
            self.assertEqual(client_ip(request), REAL_CLIENT_IP)

    def test_new_settings_defaults(self):
        keys = (
            "MAGIC_LINK_GLOBAL_RATE_WINDOW_SECONDS",
            "ADMIN_LOGIN_ACCOUNT_ALERT_LIMIT",
            "REQUEST_TIMING_EXPOSE_SERVER_TIMING",
            "RWMS_RPC_TIMEOUT_SECONDS",
            "RWMS_BULK_RPC_TIMEOUT_SECONDS",
            "CACHE_REDIS_SOCKET_TIMEOUT_SECONDS",
        )
        module = load_settings_module({"CACHE_REDIS_URL": ""}, unset=keys)

        self.assertEqual(module.MAGIC_LINK_GLOBAL_RATE_WINDOW_SECONDS, 60)
        self.assertEqual(module.ADMIN_LOGIN_ACCOUNT_ALERT_LIMIT, 200)
        self.assertIs(module.REQUEST_TIMING_EXPOSE_SERVER_TIMING, False)
        self.assertEqual(module.RWMS_RPC_TIMEOUT_SECONDS, 8.0)
        self.assertEqual(module.RWMS_BULK_RPC_TIMEOUT_SECONDS, 30.0)
        self.assertEqual(module.CACHE_REDIS_SOCKET_TIMEOUT_SECONDS, 0.5)
        self.assertNotIn("OPTIONS", module.CACHES["default"])

    def test_redis_cache_gets_socket_timeouts(self):
        module = load_settings_module(
            {
                "CACHE_REDIS_URL": "redis://127.0.0.1:1/2",
                "CACHE_REDIS_SOCKET_TIMEOUT_SECONDS": "1.5",
            }
        )

        self.assertEqual(
            module.CACHES["default"]["OPTIONS"],
            {"socket_connect_timeout": 1.5, "socket_timeout": 1.5},
        )

    def test_invalid_timeouts_fall_back_to_safe_defaults(self):
        for raw in ("abc", "0", "-1", "nan", "inf", ""):
            with self.subTest(raw=raw):
                with self.assertLogs(level="WARNING"):
                    module = load_settings_module(
                        {
                            "CACHE_REDIS_URL": "redis://127.0.0.1:1/2",
                            "CACHE_REDIS_SOCKET_TIMEOUT_SECONDS": raw,
                            "RWMS_RPC_TIMEOUT_SECONDS": raw,
                            "RWMS_BULK_RPC_TIMEOUT_SECONDS": raw,
                        }
                    )
                self.assertEqual(module.RWMS_RPC_TIMEOUT_SECONDS, 8.0)
                self.assertEqual(module.RWMS_BULK_RPC_TIMEOUT_SECONDS, 30.0)
                self.assertEqual(
                    module.CACHES["default"]["OPTIONS"]["socket_timeout"], 0.5
                )

    def test_explicit_rwms_timeouts_are_used(self):
        module = load_settings_module(
            {"RWMS_RPC_TIMEOUT_SECONDS": "12", "RWMS_BULK_RPC_TIMEOUT_SECONDS": "45.5"}
        )

        self.assertEqual(module.RWMS_RPC_TIMEOUT_SECONDS, 12.0)
        self.assertEqual(module.RWMS_BULK_RPC_TIMEOUT_SECONDS, 45.5)


@override_settings(TRUSTED_PROXY_NETWORKS=EDGE_TOPOLOGY_TRUSTED)
class ClientIpEdgeTopologyTests(SimpleTestCase):
    def request(self, forwarded, remote=DOCKER_NGINX_IP):
        return SimpleNamespace(
            META={"REMOTE_ADDR": remote, "HTTP_X_FORWARDED_FOR": forwarded}
        )

    def test_client_behind_public_edge_and_docker_nginx_is_resolved(self):
        from mobile_api import views as mobile_views

        request = self.request(f"{REAL_CLIENT_IP}, {EDGE_IP}")

        self.assertEqual(client_ip(request), REAL_CLIENT_IP)
        self.assertEqual(views.admin_client_ip(request), REAL_CLIENT_IP)
        self.assertEqual(mobile_views._client_ip(request), REAL_CLIENT_IP)

    def test_client_prepended_forwarded_value_is_ignored(self):
        request = self.request(f"6.6.6.6, {REAL_CLIENT_IP}, {EDGE_IP}")

        self.assertEqual(client_ip(request), REAL_CLIENT_IP)

    def test_without_edge_in_trusted_list_every_visitor_looks_like_edge(self):
        request = self.request(f"{REAL_CLIENT_IP}, {EDGE_IP}")

        with override_settings(TRUSTED_PROXY_NETWORKS=PRIVATE_PROXY_NETWORKS):
            self.assertEqual(client_ip(request), EDGE_IP)

    def test_trusted_proxy_membership_helper(self):
        self.assertTrue(is_trusted_proxy_address(EDGE_IP))
        self.assertTrue(is_trusted_proxy_address(DOCKER_NGINX_IP))
        self.assertFalse(is_trusted_proxy_address(REAL_CLIENT_IP))
        for value in ("", None, "not-an-ip"):
            self.assertFalse(is_trusted_proxy_address(value))

    def test_docker_proxy_gateway_chain_is_an_unresolved_client(self):
        """AUTHZ-F1: IPv6-клиент edge за docker userland-proxy."""
        gateway = self.request(f"{DOCKER_GATEWAY_IP}, {EDGE_IP}")
        real = self.request(f"{REAL_CLIENT_IP}, {EDGE_IP}")

        self.assertEqual(client_ip(gateway), DOCKER_GATEWAY_IP)
        self.assertTrue(is_unresolved_client_ip(client_ip(gateway)))
        self.assertFalse(is_unresolved_client_ip(client_ip(real)))

    def test_unresolved_client_ip_helper(self):
        for value in UnresolvedClientIpBucketTests.UNRESOLVED + ("not-an-ip", "127.0.0.2"):
            with self.subTest(value=value):
                self.assertTrue(is_unresolved_client_ip(value))
        for value in (
            REAL_CLIENT_IP,
            "45.10.0.66",
            "192.0.2.1",
            "203.0.113.7",
            "2a00:1450:4001::1",
            "::ffff:45.10.0.66",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_unresolved_client_ip(value))


class InvalidTrustedProxyNetworkLoggingTests(SimpleTestCase):
    """AUTHZ-F4: невалидная запись TRUSTED_PROXY_NETWORKS/ORIGIN_ALLOWED_PROXY_CIDRS
    не отбрасывается молча: ERROR один раз на значение за жизнь процесса."""

    # nginx allow такую запись принимает, ipaddress — нет (ведущий ноль).
    INVALID_EDGE = "203.0.113.010/32"

    def setUp(self):
        request_ip._trusted_proxy_networks.cache_clear()
        self.addCleanup(request_ip._trusted_proxy_networks.cache_clear)
        patcher = mock.patch.object(
            request_ip, "_reported_invalid_proxy_networks", set()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self):
        return SimpleNamespace(
            META={
                "REMOTE_ADDR": DOCKER_NGINX_IP,
                "HTTP_X_FORWARDED_FOR": f"{REAL_CLIENT_IP}, {EDGE_IP}",
            }
        )

    def test_invalid_entries_are_logged_once_per_value(self):
        networks = PRIVATE_PROXY_NETWORKS + [self.INVALID_EDGE, "edge.example.com"]

        with override_settings(TRUSTED_PROXY_NETWORKS=networks):
            with self.assertLogs(level="ERROR") as logs:
                # Запись отброшена: edge не доверен, клиентом выглядит сам edge.
                self.assertEqual(client_ip(self.request()), EDGE_IP)
            with self.assertNoLogs(level="ERROR"):
                self.assertEqual(client_ip(self.request()), EDGE_IP)

        errors = [line for line in logs.output if "invalid TRUSTED_PROXY_NETWORKS" in line]
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(line.startswith("ERROR") for line in errors))
        self.assertIn(repr(self.INVALID_EDGE), errors[0])
        self.assertIn(repr("edge.example.com"), errors[1])

        # Другой набор настроек с тем же значением разбирается заново, но
        # ошибка о нём повторно не пишется.
        request_ip._trusted_proxy_networks.cache_clear()
        with override_settings(TRUSTED_PROXY_NETWORKS=["10.0.0.0/8", self.INVALID_EDGE]):
            with self.assertNoLogs(level="ERROR"):
                self.assertTrue(is_trusted_proxy_address("10.1.2.3"))

    def test_valid_entries_are_not_logged(self):
        with override_settings(TRUSTED_PROXY_NETWORKS=EDGE_TOPOLOGY_TRUSTED):
            with self.assertNoLogs(level="ERROR"):
                self.assertEqual(client_ip(self.request()), REAL_CLIENT_IP)


class _MagicLinkSession:
    def __init__(self, user=None):
        self.user = user
        self.added = []
        self.closed = False

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.user

    def add(self, obj):
        # НЕ проставляем здесь token: настоящая сессия применяет питоновский
        # column default (`MagicToken.token = Column(default=uuid.uuid4)`) во
        # время INSERT, то есть на flush(), а не на add(). Раньше дубль ставил
        # токен именно тут — и код, забывший flush(), выглядел в тестах
        # исправным, пока в проде уходили письма со ссылкой
        # /login/magic/None/ (инцидент 2026-09-12).
        self.added.append(obj)

    def flush(self):
        """Как у SQLAlchemy: именно здесь применяются питоновские дефолты."""
        for obj in self.added:
            if isinstance(obj, MagicToken) and obj.token is None:
                obj.token = "magic-token"

    def close(self):
        self.closed = True


@override_settings(
    SECRET_KEY="magic-link-rate-test",
    MAGIC_LINK_IP_RATE_LIMIT=3,
    MAGIC_LINK_EMAIL_RATE_LIMIT=2,
    MAGIC_LINK_RATE_WINDOW_SECONDS=900,
    MAGIC_LINK_GLOBAL_RATE_LIMIT=5,
    MAGIC_LINK_GLOBAL_RATE_WINDOW_SECONDS=120,
    TRUSTED_PROXY_NETWORKS=PRIVATE_PROXY_NETWORKS,
)
class MagicLinkRateLimitViewTests(_FrozenClockMixin, SimpleTestCase):
    BROKEN = "broken@@example"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.freeze_rate_limit_clock()
        self.existing_user = None
        session_patch = mock.patch(
            "engine.views.session_factory",
            side_effect=lambda: _MagicLinkSession(self.existing_user),
        )
        self.session_factory = session_patch.start()
        self.addCleanup(session_patch.stop)
        send_patch = mock.patch("engine.views.send_magic_link_email")
        self.send_email = send_patch.start()
        self.addCleanup(send_patch.stop)

    def post(self, email, ip):
        request = RequestFactory().post(
            "/login/send-link/", {"email": email}, REMOTE_ADDR=ip
        )
        request.session = {}
        return views.send_magic_link(request)

    def test_ip_flood_does_not_exhaust_global_bucket_for_other_clients(self):
        statuses = [
            self.post(f"junk{i}@example.com", "45.10.0.66").status_code
            for i in range(20)
        ]

        self.assertEqual(statuses[:3], [200, 200, 200])
        self.assertEqual(set(statuses[3:]), {429})
        victim = self.post("victim@example.com", "45.10.0.77")
        self.assertEqual(victim.status_code, 200)
        self.assertEqual(json.loads(victim.content), {"status": "ok"})
        self.assertEqual(self.send_email.call_count, 4)

    def test_ip_limited_request_does_not_touch_database_or_victim_bucket(self):
        for i in range(3):
            self.post(f"junk{i}@example.com", "45.10.0.66")
        calls_before = self.session_factory.call_count

        for _ in range(5):
            response = self.post("victim@example.com", "45.10.0.66")
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response["Retry-After"], "900")
        self.assertEqual(self.session_factory.call_count, calls_before)

        # Отбитые по IP запросы не расходовали лимит адреса жертвы.
        self.assertEqual(self.post("victim@example.com", "45.10.0.77").status_code, 200)

    def test_email_bucket_answers_neutral_ok_without_sending(self):
        self.assertEqual(self.post("victim@example.com", "45.10.0.1").status_code, 200)
        self.assertEqual(self.post("victim@example.com", "45.10.0.2").status_code, 200)
        self.assertEqual(self.send_email.call_count, 2)

        with self.assertLogs(level="WARNING") as logs:
            response = self.post("victim@example.com", "45.10.0.3")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"status": "ok"})
        self.assertEqual(self.send_email.call_count, 2)
        self.assertNotIn("victim", "\n".join(logs.output))

    def test_global_bucket_returns_429_with_configured_window(self):
        for i in range(5):
            self.assertEqual(
                self.post(f"user{i}@example.com", f"45.10.1.{i + 1}").status_code, 200
            )

        with self.assertLogs(level="WARNING"):
            response = self.post("late@example.com", "45.10.1.50")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "120")

    def test_malformed_addresses_do_not_consume_email_or_global_buckets(self):
        with self.assertLogs(level="WARNING"):
            for i in range(10):
                response = self.post(self.BROKEN, f"45.10.2.{i + 1}")
                self.assertEqual(response.status_code, 400)

        self.assertEqual(self.post("real@example.com", "45.10.2.100").status_code, 200)

    def test_owner_of_stored_malformed_address_still_gets_links(self):
        self.existing_user = SimpleNamespace(id=42, email=self.BROKEN, username="m123")

        with mock.patch(
            "engine.views.get_registration_context",
            return_value={"referrer": None, "traffic_source": None, "ymid": None},
        ), mock.patch("engine.views.sync_existing_user_tracking"):
            statuses = [
                self.post(self.BROKEN, f"45.10.3.{i + 1}").status_code for i in range(4)
            ]

        self.assertEqual(statuses, [200, 200, 200, 200])
        self.assertEqual(self.send_email.call_count, 4)

    def post_via_edge(self, email, forwarded_client):
        request = RequestFactory().post(
            "/login/send-link/",
            {"email": email},
            REMOTE_ADDR=DOCKER_NGINX_IP,
            HTTP_X_FORWARDED_FOR=f"{forwarded_client}, {EDGE_IP}",
        )
        request.session = {}
        return views.send_magic_link(request)

    @override_settings(TRUSTED_PROXY_NETWORKS=EDGE_TOPOLOGY_TRUSTED)
    def test_ipv6_clients_behind_docker_proxy_do_not_share_ip_bucket(self):
        """AUTHZ-F1: все такие клиенты приходят как шлюз bridge; IP-бакет
        пропускается, общий бакет продолжает работать."""
        self.reset_unresolved_ip_warning()

        with self.assertLogs(level="WARNING") as logs:
            statuses = [
                self.post_via_edge(f"user{i}@example.com", DOCKER_GATEWAY_IP).status_code
                for i in range(5)
            ]
            late = self.post_via_edge("late@example.com", DOCKER_GATEWAY_IP)

        # IP-лимит 3 не сработал, общий (5 за 120 с) по-прежнему держит поток.
        self.assertEqual(statuses, [200] * 5)
        self.assertEqual(late.status_code, 429)
        self.assertEqual(late["Retry-After"], "120")
        self.assertEqual(self.send_email.call_count, 5)
        self.assertEqual(len(self.unresolved_ip_warnings(logs)), 1)

    @override_settings(TRUSTED_PROXY_NETWORKS=EDGE_TOPOLOGY_TRUSTED)
    def test_public_client_behind_edge_keeps_its_own_ip_bucket(self):
        statuses = [
            self.post_via_edge(f"user{i}@example.com", REAL_CLIENT_IP).status_code
            for i in range(3)
        ]
        limited = self.post_via_edge("late@example.com", REAL_CLIENT_IP)

        self.assertEqual(statuses, [200, 200, 200])
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited["Retry-After"], "900")


class _AdminSessionDict(dict):
    modified = False

    def cycle_key(self):
        self["cycle_key_called"] = True


class _AdminAccountSession:
    def __init__(self, account=None):
        self.account = account

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.account

    def add(self, obj):
        return None

    def commit(self):
        return None

    def close(self):
        return None


@override_settings(
    SECRET_KEY="admin-login-rate-test",
    SUPPORT_ADMIN_PASSWORD="shared-admin-pass",
    SUPPORT_STAFF_PASSWORD="",
    ADMIN_LOGIN_IP_RATE_LIMIT=30,
    ADMIN_LOGIN_ACCOUNT_RATE_LIMIT=10,
    ADMIN_LOGIN_RATE_WINDOW_SECONDS=900,
    ADMIN_LOGIN_ACCOUNT_ALERT_LIMIT=200,
    TRUSTED_PROXY_NETWORKS=PRIVATE_PROXY_NETWORKS,
)
class SupportAdminLoginRateLimitTests(_FrozenClockMixin, SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.freeze_rate_limit_clock()
        self.account = None
        session_patch = mock.patch(
            "engine.views.session_factory",
            side_effect=lambda: _AdminAccountSession(self.account),
        )
        session_patch.start()
        self.addCleanup(session_patch.stop)
        password_patch = mock.patch(
            "engine.views.check_password",
            side_effect=lambda raw, hashed: raw == "account-pass",
        )
        password_patch.start()
        self.addCleanup(password_patch.stop)

    def post(self, ip, password, login=""):
        request = RequestFactory().post(
            "/support-admin/login/",
            {"password": password, "login": login},
            REMOTE_ADDR=ip,
        )
        request.session = _AdminSessionDict()
        return views.support_admin_login(request)

    def test_failed_shared_attempts_from_one_ip_do_not_lock_out_other_ips(self):
        with self.assertLogs(level="WARNING"):
            for _ in range(10):
                self.assertEqual(self.post("45.20.0.1", "wrong").status_code, 403)
            limited = self.post("45.20.0.1", "wrong")
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited["Retry-After"], "900")
            # До проверки пароля: верный пароль с того же IP тоже ждёт окно.
            self.assertEqual(self.post("45.20.0.1", "shared-admin-pass").status_code, 429)

        self.assertEqual(self.post("45.20.0.2", "shared-admin-pass").status_code, 302)

    def test_successful_logins_do_not_consume_limits(self):
        for _ in range(15):
            self.assertEqual(self.post("45.20.0.1", "shared-admin-pass").status_code, 302)

    def test_personal_account_failures_are_keyed_by_account_and_ip(self):
        self.account = SimpleNamespace(
            login="admin", role="admin", password_hash="hash", last_login_at=None
        )

        with self.assertLogs(level="WARNING"):
            for _ in range(10):
                self.assertEqual(
                    self.post("45.20.1.1", "wrong", login="admin").status_code, 403
                )
            self.assertEqual(
                self.post("45.20.1.1", "account-pass", login="admin").status_code, 429
            )

        self.assertEqual(
            self.post("45.20.1.2", "account-pass", login="admin").status_code, 302
        )

    @override_settings(ADMIN_LOGIN_IP_RATE_LIMIT=3)
    def test_ip_bucket_blocks_every_account_from_that_ip(self):
        with self.assertLogs(level="WARNING"):
            for login in ("a", "b", "c"):
                self.assertEqual(self.post("45.20.2.1", "wrong", login=login).status_code, 403)
            self.assertEqual(self.post("45.20.2.1", "shared-admin-pass").status_code, 429)

        self.assertEqual(self.post("45.20.2.2", "shared-admin-pass").status_code, 302)

    @override_settings(ADMIN_LOGIN_ACCOUNT_ALERT_LIMIT=3)
    def test_account_alert_is_logged_once_without_blocking_or_secrets(self):
        with self.assertLogs(level="WARNING") as logs:
            for i in range(5):
                response = self.post(
                    f"45.20.3.{i + 1}", "wrong-pass-123", login="boss.secret"
                )
                self.assertEqual(response.status_code, 403)

        output = "\n".join(logs.output)
        alerts = [line for line in logs.output if "ALERT" in line]
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0].startswith("ERROR"))
        self.assertNotIn("boss", output)
        self.assertNotIn("wrong-pass-123", output)

        self.account = SimpleNamespace(
            login="boss.secret", role="admin", password_hash="hash", last_login_at=None
        )
        self.assertEqual(
            self.post("45.20.3.9", "account-pass", login="boss.secret").status_code, 302
        )

    def test_cache_outage_does_not_block_login(self):
        broken = mock.Mock()
        broken.get.side_effect = ConnectionError("redis down")
        broken.add.side_effect = ConnectionError("redis down")

        with mock.patch.object(rate_limit, "cache", broken), self.assertLogs(level="ERROR"):
            self.assertEqual(self.post("45.20.4.1", "wrong").status_code, 403)
            self.assertEqual(self.post("45.20.4.1", "shared-admin-pass").status_code, 302)

    @override_settings(ADMIN_LOGIN_IP_RATE_LIMIT=3)
    def test_unresolved_ip_skips_only_the_ip_bucket(self):
        """AUTHZ-F1: IP за прокси не определён. IP-бакет не запирает всех за
        этим адресом, пара «аккаунт+IP» по-прежнему считает и блокирует."""
        self.reset_unresolved_ip_warning()

        with self.assertLogs(level="WARNING") as logs:
            for login in ("a", "b", "c", "d", "e"):
                self.assertEqual(
                    self.post("127.0.0.1", "wrong", login=login).status_code, 403
                )
            self.assertEqual(self.post("127.0.0.1", "shared-admin-pass").status_code, 302)

            for _ in range(10):
                self.assertEqual(self.post(DOCKER_GATEWAY_IP, "wrong").status_code, 403)
            self.assertEqual(
                self.post(DOCKER_GATEWAY_IP, "shared-admin-pass").status_code, 429
            )

        self.assertEqual(len(self.unresolved_ip_warnings(logs)), 1)


@override_settings(
    SECRET_KEY="anonymous-payment-rate-test",
    PAYMENT_ANON_IP_RATE_LIMIT=1,
    PAYMENT_ANON_EMAIL_RATE_LIMIT=10,
    PAYMENT_ANON_RATE_WINDOW_SECONDS=900,
    PAYMENT_ANON_GLOBAL_RATE_LIMIT=600,
    PAYMENT_ANON_GLOBAL_RATE_WINDOW_SECONDS=60,
    TRUSTED_PROXY_NETWORKS=EDGE_TOPOLOGY_TRUSTED,
)
class AnonymousPaymentUnresolvedIpTests(_FrozenClockMixin, SimpleTestCase):
    """AUTHZ-F1 для оплаты с лендинга без входа: IP-бакет не общий на всех
    клиентов за docker userland-proxy, email-бакет продолжает работать."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.freeze_rate_limit_clock()
        self.reset_unresolved_ip_warning()

    def request(self, forwarded_client):
        return RequestFactory().post(
            "/pay/",
            REMOTE_ADDR=DOCKER_NGINX_IP,
            HTTP_X_FORWARDED_FOR=f"{forwarded_client}, {EDGE_IP}",
        )

    def limited(self, forwarded_client, email):
        return views.anonymous_payment_rate_limited(self.request(forwarded_client), email)

    def test_unresolved_ip_does_not_limit_anonymous_payment(self):
        with self.assertLogs(level="WARNING") as logs:
            for i in range(5):
                self.assertEqual(
                    self.limited(DOCKER_GATEWAY_IP, f"buyer{i}@example.test"), (False, 0)
                )

        self.assertEqual(len(self.unresolved_ip_warnings(logs)), 1)

    def test_email_bucket_still_applies_and_public_ip_is_limited(self):
        with self.settings(PAYMENT_ANON_EMAIL_RATE_LIMIT=2), self.assertLogs(level="WARNING"):
            for _ in range(2):
                self.assertEqual(
                    self.limited(DOCKER_GATEWAY_IP, "victim@example.test"), (False, 0)
                )
            self.assertEqual(
                self.limited(DOCKER_GATEWAY_IP, "victim@example.test"), (True, 900)
            )

        self.assertEqual(self.limited(REAL_CLIENT_IP, "buyer@example.test"), (False, 0))
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(
                self.limited(REAL_CLIENT_IP, "other@example.test"), (True, 900)
            )
        self.assertIn("anonymous payment IP rate limit reached", "\n".join(logs.output))
