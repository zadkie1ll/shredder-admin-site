"""Тесты автоматической установки нод через админку.

БД — in-memory SQLite с реальными таблицами (паттерн mobile_api/tests.py),
RWMS — mock.Mock() с proto-объектами, HTTP — RequestFactory на view-функциях.
"""

import json
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from common.models.db import (
    Base,
    NodeInstallScript,
    NodeProvisionRequest,
    NodeProvisionStage,
    NodeProvisionStatus,
)
from engine import node_provisioning
import proto.rwmanager_pb2 as rw_proto


# Tests only: BigInteger PK не автоинкрементится в SQLite (см. mobile_api/tests.py)
@compiles(BigInteger, "sqlite")
def _compile_bigint_as_integer_on_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


# Tests only: JSONB — тип PostgreSQL, для SQLite рендерим его как JSON
@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json_on_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "JSON"


PROFILE_UUID = "5f7c1f24-9adc-4f1b-8ef2-6a1a0f6cbb1d"
INBOUND_UUIDS = [
    "2a5f4a52-77e5-45f8-b3a4-df6a1c1c8a01",
    "9c0be3af-13f9-4a7a-8ac2-51b1d0a2be02",
]
NODE_UUID = "091fae80-e27a-4e35-9ddb-8e737f4d2732"

SCRIPT_BODY = "#!/usr/bin/env bash\nset -euo pipefail\necho install\n"


def make_source_node(profile_uuid=PROFILE_UUID):
    return rw_proto.Node(
        uuid="src-node-uuid",
        name="Образец",
        address="9.9.9.9",
        is_connected=True,
        is_disabled=False,
        config_profile_uuid=profile_uuid,
        active_inbound_uuids=INBOUND_UUIDS,
    )


def make_rwms_mock(connected=False):
    rwms = mock.Mock()
    rwms.get_node_secret.return_value = SimpleNamespace(secret_key="panel-key")
    rwms.create_node.return_value = rw_proto.Node(
        uuid=NODE_UUID, name="DE Node", address="1.2.3.4"
    )
    rwms.get_nodes.return_value = rw_proto.GetNodesResponse(
        nodes=[
            rw_proto.Node(
                uuid=NODE_UUID,
                name="DE Node",
                address="1.2.3.4",
                is_connected=connected,
                config_profile_uuid=PROFILE_UUID,
                active_inbound_uuids=INBOUND_UUIDS,
            )
        ]
    )
    return rwms


class NodeProvisioningDbTestCase(SimpleTestCase):
    """База: in-memory SQLite c таблицами установки нод."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(
            self.engine,
            tables=[
                NodeInstallScript.__table__,
                NodeProvisionRequest.__table__,
                NodeProvisionStage.__table__,
            ],
        )
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()
        # addCleanup — LIFO: сначала закрыть сессию, потом dispose движка
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session.close)

    def make_script(self, node_type="self_steal", content=SCRIPT_BODY):
        script = node_provisioning.save_script_version(
            self.session, node_type, content, "initial", "tester"
        )
        self.session.commit()
        return script

    def make_request(self, **kwargs):
        self.make_script(kwargs.get("node_type", "self_steal"))
        provision_request, token = node_provisioning.create_request(
            self.session,
            node_name=kwargs.get("node_name", "DE Node"),
            node_type=kwargs.get("node_type", "self_steal"),
            source_node=kwargs.get("source_node", make_source_node()),
            created_by="tester",
            ttl_minutes=kwargs.get("ttl_minutes", 60),
        )
        self.session.commit()
        return provision_request, token


class InstallScriptTests(NodeProvisioningDbTestCase):
    def test_create_group_persists_draft_and_first_save_becomes_v1(self):
        draft = node_provisioning.create_script_group(
            self.session, "Reality Europe", "tester"
        )
        self.session.commit()

        self.assertEqual(draft.version, 0)
        self.assertFalse(draft.is_active)
        script = node_provisioning.save_script_version(
            self.session, "Reality Europe", SCRIPT_BODY, "first", "tester"
        )
        self.session.commit()

        self.assertEqual(script.id, draft.id)
        self.assertEqual(script.version, 1)
        self.assertTrue(script.is_active)

    def test_save_creates_versions_and_switches_active(self):
        first = self.make_script()
        second = node_provisioning.save_script_version(
            self.session, "self_steal", SCRIPT_BODY + "echo v2\n", "v2", "tester"
        )
        self.session.commit()

        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        self.assertFalse(
            self.session.query(NodeInstallScript).get(first.id).is_active
        )
        self.assertTrue(second.is_active)

    def test_activate_older_version_rolls_back(self):
        first = self.make_script()
        node_provisioning.save_script_version(
            self.session, "self_steal", SCRIPT_BODY + "echo v2\n", "v2", "tester"
        )
        node_provisioning.activate_script_version(self.session, first.id)
        self.session.commit()

        active = node_provisioning.active_script(self.session, "self_steal")
        self.assertEqual(active.id, first.id)

    def test_save_accepts_dynamic_name_and_rejects_invalid_content(self):
        script = node_provisioning.save_script_version(
            self.session, "Мой Reality", SCRIPT_BODY, "", "tester"
        )
        self.assertEqual(script.node_type, "Мой Reality")
        with self.assertRaises(node_provisioning.ProvisionError):
            node_provisioning.save_script_version(
                self.session, "self_steal", "   ", "", "tester"
            )
        with self.assertRaises(node_provisioning.ProvisionError):
            node_provisioning.save_script_version(
                self.session, "x" * 33, SCRIPT_BODY, "", "tester"
            )

    def test_rename_changes_group_but_keeps_versions(self):
        first = self.make_script("Reality old")
        second = node_provisioning.save_script_version(
            self.session, "Reality old", SCRIPT_BODY + "echo v2\n", "v2", "tester"
        )
        node_provisioning.rename_script_group(
            self.session, "Reality old", "Reality new"
        )
        self.session.commit()

        self.assertIsNone(node_provisioning.active_script(self.session, "Reality old"))
        self.assertEqual(
            node_provisioning.active_script(self.session, "Reality new").id,
            second.id,
        )
        self.assertEqual(
            self.session.query(NodeInstallScript)
            .filter(NodeInstallScript.node_type == "Reality new")
            .count(),
            2,
        )
        self.assertIn(
            first.id,
            {
                script.id
                for script in self.session.query(NodeInstallScript)
                .filter(NodeInstallScript.node_type == "Reality new")
                .all()
            },
        )

    def test_duplicate_name_is_rejected_case_insensitively(self):
        node_provisioning.create_script_group(self.session, "Reality EU", "tester")
        with self.assertRaises(node_provisioning.ProvisionError):
            node_provisioning.create_script_group(
                self.session, "reality eu", "tester"
            )

    def test_delete_unused_group_and_protect_used_group(self):
        node_provisioning.create_script_group(self.session, "Unused", "tester")
        node_provisioning.delete_script_group(self.session, "Unused")
        self.assertEqual(
            self.session.query(NodeInstallScript)
            .filter(NodeInstallScript.node_type == "Unused")
            .count(),
            0,
        )

        provision_request, _token = self.make_request(node_type="Used script")
        with self.assertRaises(node_provisioning.ProvisionError) as context:
            node_provisioning.delete_script_group(self.session, "Used script")
        self.assertIn("используется", str(context.exception))
        self.assertIsNotNone(
            self.session.query(NodeInstallScript).get(provision_request.script_id)
        )

    def test_script_warnings_detect_interactive_read_and_missing_shebang(self):
        warnings = node_provisioning.script_warnings(
            'echo hi\nread -p "Продолжить?" -n 1 -r\n'
        )
        self.assertTrue(any("shebang" in w for w in warnings))
        self.assertTrue(any("read" in w for w in warnings))
        self.assertEqual(node_provisioning.script_warnings(SCRIPT_BODY), [])


class CreateRequestTests(NodeProvisioningDbTestCase):
    def test_create_requires_active_script(self):
        with self.assertRaises(node_provisioning.ProvisionError) as ctx:
            node_provisioning.create_request(
                self.session,
                node_name="DE Node",
                node_type="hysteria",
                source_node=make_source_node(),
                created_by="tester",
            )
        self.assertIn("нет активной версии", str(ctx.exception))

    def test_create_snapshots_config_profile(self):
        provision_request, token = self.make_request()
        self.assertEqual(provision_request.config_profile_uuid, PROFILE_UUID)
        self.assertEqual(provision_request.inbound_uuids, INBOUND_UUIDS)
        self.assertEqual(provision_request.status, NodeProvisionStatus.CREATED)
        self.assertGreater(len(token), 30)
        # В БД лежит только хеш токена
        self.assertNotEqual(provision_request.token_hash, token)
        self.assertEqual(
            provision_request.token_hash, node_provisioning.hash_token(token)
        )

    def test_create_accepts_arbitrary_selected_script(self):
        provision_request, _token = self.make_request(node_type="Reality Nordics")
        self.assertEqual(provision_request.node_type, "Reality Nordics")
        self.assertEqual(
            self.session.query(NodeInstallScript)
            .get(provision_request.script_id)
            .node_type,
            "Reality Nordics",
        )

    def test_create_rejects_source_without_profile(self):
        self.make_script()
        with self.assertRaises(node_provisioning.ProvisionError):
            node_provisioning.create_request(
                self.session,
                node_name="DE Node",
                node_type="self_steal",
                source_node=make_source_node(profile_uuid=""),
                created_by="tester",
            )


class ClaimTests(NodeProvisioningDbTestCase):
    def test_claim_registers_node_and_binds_ip(self):
        provision_request, token = self.make_request()
        rwms = make_rwms_mock()

        payload = node_provisioning.claim_request(
            self.session, provision_request, "1.2.3.4", rwms
        )
        self.session.commit()

        self.assertEqual(payload["secret_key"], "panel-key")
        self.assertEqual(
            payload["script_sha256"], node_provisioning.script_sha256(SCRIPT_BODY)
        )
        self.assertEqual(provision_request.status, NodeProvisionStatus.CLAIMED)
        self.assertEqual(provision_request.claimed_ip, "1.2.3.4")
        self.assertEqual(provision_request.remnawave_node_uuid, NODE_UUID)
        create_request_proto = rwms.create_node.call_args.args[0]
        self.assertEqual(create_request_proto.address, "1.2.3.4")
        self.assertEqual(create_request_proto.port, 2222)
        self.assertEqual(create_request_proto.config_profile_uuid, PROFILE_UUID)
        self.assertEqual(list(create_request_proto.inbound_uuids), INBOUND_UUIDS)

    def test_claim_is_idempotent_for_same_ip(self):
        provision_request, token = self.make_request()
        rwms = make_rwms_mock()
        node_provisioning.claim_request(self.session, provision_request, "1.2.3.4", rwms)
        payload = node_provisioning.claim_request(
            self.session, provision_request, "1.2.3.4", rwms
        )
        self.assertEqual(payload["secret_key"], "panel-key")

    def test_claim_rejects_foreign_ip_after_bind(self):
        provision_request, token = self.make_request()
        rwms = make_rwms_mock()
        node_provisioning.claim_request(self.session, provision_request, "1.2.3.4", rwms)

        with self.assertRaises(node_provisioning.ProvisionError) as ctx:
            node_provisioning.claim_request(
                self.session, provision_request, "5.6.7.8", rwms
            )
        self.assertEqual(ctx.exception.http_status, 403)

    def test_claim_rejects_expired_token(self):
        provision_request, token = self.make_request()
        provision_request.expires_at = datetime.now() - timedelta(minutes=1)

        with self.assertRaises(node_provisioning.ProvisionError) as ctx:
            node_provisioning.claim_request(
                self.session, provision_request, "1.2.3.4", make_rwms_mock()
            )
        self.assertEqual(ctx.exception.http_status, 410)

    def test_claim_fails_with_502_when_rwms_unavailable(self):
        provision_request, token = self.make_request()
        rwms = make_rwms_mock()
        rwms.get_node_secret.return_value = None

        with self.assertRaises(node_provisioning.ProvisionError) as ctx:
            node_provisioning.claim_request(
                self.session, provision_request, "1.2.3.4", rwms
            )
        self.assertEqual(ctx.exception.http_status, 502)

    def test_find_by_token_rejects_unknown_and_revoked(self):
        provision_request, token = self.make_request()
        with self.assertRaises(node_provisioning.ProvisionError):
            node_provisioning.find_request_by_token(self.session, "wrong-token")

        node_provisioning.revoke_request(provision_request)
        self.session.commit()
        with self.assertRaises(node_provisioning.ProvisionError):
            node_provisioning.find_request_by_token(self.session, token)


class ProgressAndCompleteTests(NodeProvisioningDbTestCase):
    def claimed_request(self):
        provision_request, token = self.make_request()
        node_provisioning.claim_request(
            self.session, provision_request, "1.2.3.4", make_rwms_mock()
        )
        return provision_request, token

    def test_script_running_moves_to_provisioning_and_stores_log(self):
        provision_request, _token = self.claimed_request()
        node_provisioning.record_progress(
            self.session, provision_request, "script", "running", None, "line1\nline2"
        )
        self.assertEqual(provision_request.status, NodeProvisionStatus.PROVISIONING)
        self.assertEqual(provision_request.install_log, "line1\nline2")

    def test_failed_stage_fails_request(self):
        provision_request, _token = self.claimed_request()
        node_provisioning.record_progress(
            self.session, provision_request, "script", "failed", "sha256 mismatch", None
        )
        self.assertEqual(provision_request.status, NodeProvisionStatus.FAILED)
        self.assertIn("sha256 mismatch", provision_request.error)

    def test_complete_zero_installs_nonzero_fails(self):
        provision_request, _token = self.claimed_request()
        node_provisioning.complete_request(self.session, provision_request, 0)
        self.assertEqual(provision_request.status, NodeProvisionStatus.INSTALLED)
        # Повторный complete идемпотентен
        node_provisioning.complete_request(self.session, provision_request, 0)

        other_request, _other_token = self.make_request(node_name="Other node")
        node_provisioning.claim_request(
            self.session, other_request, "4.4.4.4", make_rwms_mock()
        )
        node_provisioning.complete_request(self.session, other_request, 7)
        self.assertEqual(other_request.status, NodeProvisionStatus.FAILED)
        self.assertIn("кодом 7", other_request.error)

    def test_refresh_connect_status_marks_ready(self):
        provision_request, _token = self.claimed_request()
        node_provisioning.complete_request(self.session, provision_request, 0)

        node_provisioning.refresh_connect_status(
            self.session, provision_request, make_rwms_mock(connected=False)
        )
        self.assertEqual(provision_request.status, NodeProvisionStatus.INSTALLED)

        node_provisioning.refresh_connect_status(
            self.session, provision_request, make_rwms_mock(connected=True)
        )
        self.assertEqual(provision_request.status, NodeProvisionStatus.READY)


@override_settings(NODE_BOOTSTRAP_DOMAINS=["panel.test"])
class BootstrapViewsTests(NodeProvisioningDbTestCase):
    """HTTP-слой bootstrap-эндпоинтов: host-gating, Bearer-токен, статусы."""

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.session_patch = mock.patch(
            "engine.views.session_factory", side_effect=self.Session
        )
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def post_claim(self, token, host="panel.test", ip="1.2.3.4"):
        from engine.views import node_bootstrap_claim

        request = self.factory.post(
            "/node-bootstrap/claim/",
            HTTP_HOST=host,
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=ip,
        )
        return node_bootstrap_claim(request)

    def test_claim_happy_path(self):
        _provision_request, token = self.make_request()
        with mock.patch("engine.views.rwms_client", make_rwms_mock()):
            response = self.post_claim(token)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["result"]["secret_key"], "panel-key")
        self.assertEqual(payload["result"]["node_type"], "self_steal")

    def test_claim_rejected_on_foreign_host_and_without_bootstrap_domains(self):
        _provision_request, token = self.make_request()
        with mock.patch("engine.views.rwms_client", make_rwms_mock()):
            response = self.post_claim(token, host="cabinet.test")
            self.assertEqual(response.status_code, 404)

            with override_settings(NODE_BOOTSTRAP_DOMAINS=[]):
                response = self.post_claim(token)
                self.assertEqual(response.status_code, 404)

    def test_claim_with_unknown_token_is_403(self):
        with mock.patch("engine.views.rwms_client", make_rwms_mock()):
            response = self.post_claim("bad-token")
        self.assertEqual(response.status_code, 403)

    def test_script_endpoint_serves_pinned_content_with_ip_binding(self):
        from engine.views import node_bootstrap_script

        _provision_request, token = self.make_request()
        with mock.patch("engine.views.rwms_client", make_rwms_mock()):
            self.post_claim(token)

        request = self.factory.get(
            "/node-bootstrap/script/",
            HTTP_HOST="panel.test",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR="1.2.3.4",
        )
        response = node_bootstrap_script(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), SCRIPT_BODY)

        foreign = self.factory.get(
            "/node-bootstrap/script/",
            HTTP_HOST="panel.test",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR="5.6.7.8",
        )
        self.assertEqual(node_bootstrap_script(foreign).status_code, 403)

    def test_progress_and_complete_flow(self):
        from engine.views import node_bootstrap_complete, node_bootstrap_progress

        provision_request, token = self.make_request()
        request_id = provision_request.id
        with mock.patch("engine.views.rwms_client", make_rwms_mock()):
            self.post_claim(token)

        progress = self.factory.post(
            "/node-bootstrap/progress/",
            data=json.dumps(
                {"stage": "script", "status": "running", "log_tail": "hello"}
            ),
            content_type="application/json",
            HTTP_HOST="panel.test",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR="1.2.3.4",
        )
        self.assertEqual(node_bootstrap_progress(progress).status_code, 200)

        complete = self.factory.post(
            "/node-bootstrap/complete/",
            data=json.dumps({"exit_code": 0}),
            content_type="application/json",
            HTTP_HOST="panel.test",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR="1.2.3.4",
        )
        self.assertEqual(node_bootstrap_complete(complete).status_code, 200)

        check_session = self.Session()
        try:
            stored = check_session.query(NodeProvisionRequest).get(request_id)
            self.assertEqual(stored.status, NodeProvisionStatus.INSTALLED)
            self.assertEqual(stored.install_log, "hello")
        finally:
            check_session.close()

    def test_runner_served_only_on_panel_domain(self):
        from engine.views import node_bootstrap_runner

        request = self.factory.get("/node-bootstrap/runner/", HTTP_HOST="panel.test")
        response = node_bootstrap_runner(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"http://panel.test", response.content)
        self.assertIn(b"node-bootstrap", response.content)

        foreign = self.factory.get(
            "/node-bootstrap/runner/", HTTP_HOST="cabinet.test"
        )
        self.assertEqual(node_bootstrap_runner(foreign).status_code, 404)


@override_settings(NODE_BOOTSTRAP_DOMAINS=["panel.test"])
class AdminNodeProvisionApiTests(NodeProvisioningDbTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.session_patch = mock.patch(
            "engine.views.session_factory", side_effect=self.Session
        )
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def test_endpoints_require_admin(self):
        from engine.views import (
            support_admin_api_node_provision,
            support_admin_api_node_provision_detail,
            support_admin_api_node_scripts,
        )

        for view in (
            support_admin_api_node_scripts,
            support_admin_api_node_provision,
            support_admin_api_node_provision_detail,
        ):
            request = self.factory.get("/support-admin/api/x/")
            request.session = {}
            response = view(request)
            self.assertNotEqual(response.status_code, 200)

    def test_create_returns_one_liner_with_token_once(self):
        from engine.views import support_admin_api_node_provision

        self.make_script()
        request = self.factory.post(
            "/support-admin/api/node-provision/",
            {
                "action": "create",
                "node_name": "DE Node",
                "script_name": "self_steal",
                "source_node_uuid": NODE_UUID,
            },
        )
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.support_admin_actor", return_value="tester"),
            mock.patch("engine.views.admin_audit_write"),
            mock.patch("engine.views.rwms_client", make_rwms_mock()),
        ):
            response = support_admin_api_node_provision(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        one_liner = payload["result"]["one_liner"]
        self.assertIn("https://panel.test/node-bootstrap/runner/", one_liner)
        self.assertIn("| bash -s -- ", one_liner)
        # Токен из one-liner валиден и находит заявку
        token = one_liner.rsplit(" ", 1)[-1]
        found = node_provisioning.find_request_by_token(self.session, token)
        self.assertEqual(found.node_name, "DE Node")

    def test_create_blocked_without_active_script(self):
        from engine.views import support_admin_api_node_provision

        request = self.factory.post(
            "/support-admin/api/node-provision/",
            {
                "action": "create",
                "node_name": "DE Node",
                "node_type": "hysteria",
                "source_node_uuid": NODE_UUID,
            },
        )
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.support_admin_actor", return_value="tester"),
            mock.patch("engine.views.rwms_client", make_rwms_mock()),
        ):
            response = support_admin_api_node_provision(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "нет активной версии", json.loads(response.content)["message"]
        )

    def test_scripts_listing_is_dynamic_and_reports_drafts(self):
        from engine.views import support_admin_api_node_scripts

        self.make_script()
        node_provisioning.create_script_group(self.session, "Custom draft", "tester")
        self.session.commit()
        request = self.factory.get("/support-admin/api/node-scripts/")
        with mock.patch(
            "engine.views.require_support_admin_role", return_value=None
        ):
            response = support_admin_api_node_scripts(request)

        payload = json.loads(response.content)
        by_key = {t["key"]: t for t in payload["result"]["scripts"]}
        self.assertTrue(by_key["self_steal"]["has_active"])
        self.assertFalse(by_key["Custom draft"]["has_active"])
        self.assertEqual(by_key["self_steal"]["active"]["content"], SCRIPT_BODY)
        self.assertNotIn("hysteria", by_key)

    def test_scripts_api_can_create_rename_and_delete_group(self):
        from engine.views import support_admin_api_node_scripts

        def post(data):
            request = self.factory.post("/support-admin/api/node-scripts/", data)
            with (
                mock.patch(
                    "engine.views.require_support_admin_role", return_value=None
                ),
                mock.patch("engine.views.support_admin_actor", return_value="tester"),
                mock.patch("engine.views.admin_audit_write"),
            ):
                return support_admin_api_node_scripts(request)

        created = post({"action": "create", "name": "Custom Reality"})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(
            json.loads(created.content)["result"]["key"], "Custom Reality"
        )

        renamed = post(
            {
                "action": "rename",
                "node_type": "Custom Reality",
                "name": "Custom Hysteria",
            }
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(
            json.loads(renamed.content)["result"]["key"], "Custom Hysteria"
        )

        deleted = post({"action": "delete", "node_type": "Custom Hysteria"})
        self.assertEqual(deleted.status_code, 200)
        self.session.expire_all()
        self.assertEqual(self.session.query(NodeInstallScript).count(), 0)

    def test_detail_marks_ready_when_node_connected(self):
        from engine.views import support_admin_api_node_provision_detail

        provision_request, token = self.make_request()
        request_id = provision_request.id
        node_provisioning.claim_request(
            self.session, provision_request, "1.2.3.4", make_rwms_mock()
        )
        node_provisioning.complete_request(self.session, provision_request, 0)
        self.session.commit()

        request = self.factory.get(
            "/support-admin/api/node-provision/detail/", {"id": str(request_id)}
        )
        with (
            mock.patch("engine.views.require_support_admin_role", return_value=None),
            mock.patch("engine.views.rwms_client", make_rwms_mock(connected=True)),
        ):
            response = support_admin_api_node_provision_detail(request)

        payload = json.loads(response.content)
        self.assertEqual(payload["result"]["request"]["status"], "ready")
        stages = {s["stage"]: s["status"] for s in payload["result"]["stages"]}
        self.assertEqual(stages["connect"], "ok")
