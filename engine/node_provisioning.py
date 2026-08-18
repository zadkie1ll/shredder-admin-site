"""Автоматическая установка нод через админку (pull-модель).

Поток: админ создаёт заявку (NodeProvisionRequest) с одноразовым токеном и
запускает на свежем сервере обёртку

    curl -fsSL https://<панельный-домен>/node-bootstrap/runner | bash -s -- <токен>

Обёртка по токену забирает SECRET_KEY панели (через RWMS) и зафиксированную
в заявке версию install-скрипта (копия devops-скрипта из таблицы
node_install_scripts), выполняет установку и шлёт прогресс обратно.
Нода создаётся в панели Remnawave уже на claim (идемпотентно, через RWMS),
поэтому момент её первого коннекта — естественное подтверждение успеха.
Сертификаты в поток не входят: их деплоят вручную
(manage-node-certificates.sh из devops-репозитория).

Безопасность: токен хранится только хешем, живёт NODE_BOOTSTRAP_TOKEN_TTL
до claim, после claim привязан к IP сервера; сам SECRET_KEY не логируется.
"""

import hashlib
import logging
import secrets
from datetime import datetime
from datetime import timedelta

from common.models.db import NodeInstallScript
from common.models.db import NodeProvisionRequest
from common.models.db import NodeProvisionStage
from common.models.db import NodeProvisionStatus

import proto.rwmanager_pb2 as rw_proto

logger = logging.getLogger("engine")

NODE_TYPES = [
    ("self_steal", "Self-steal (Reality + haproxy/nginx)"),
    ("hysteria", "Hysteria2"),
    ("whitelist", "Whitelist (чистый Reality)"),
    ("whitelist_self_steal", "Whitelist self-steal"),
]
NODE_TYPE_KEYS = {key for key, _ in NODE_TYPES}

STAGES = ["claim", "script", "connect"]

DEFAULT_SSH_PORT = 40022
REMNANODE_PORT = 2222
INSTALL_LOG_MAX_BYTES = 64 * 1024
# Статусы, в которых токен ещё принимается bootstrap-эндпоинтами
TOKEN_ALIVE_STATUSES = {
    NodeProvisionStatus.CREATED,
    NodeProvisionStatus.CLAIMED,
    NodeProvisionStatus.PROVISIONING,
    NodeProvisionStatus.INSTALLED,
}


class ProvisionError(Exception):
    """Ошибка bootstrap-потока; http_status уезжает в ответ API."""

    def __init__(self, message, http_status=400):
        super().__init__(message)
        self.http_status = http_status


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def script_sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def active_script(db_session, node_type):
    return (
        db_session.query(NodeInstallScript)
        .filter(
            NodeInstallScript.node_type == node_type,
            NodeInstallScript.is_active.is_(True),
        )
        .order_by(NodeInstallScript.version.desc())
        .first()
    )


def script_warnings(content: str) -> list[str]:
    """Мягкие проверки скрипта при сохранении: только предупреждения.

    Скрипт исполняется без stdin, поэтому интерактивный read либо повесит
    установку, либо уронит её через set -e.
    """
    warnings = []
    if not content.startswith("#!"):
        warnings.append("Нет shebang в первой строке (ожидается #!/usr/bin/env bash).")
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("read ") or " read " in f" {stripped} ":
            warnings.append(
                f"Строка {line_number}: интерактивный read — под автоматической "
                "установкой stdin закрыт, скрипт повиснет или упадёт."
            )
    return warnings


def save_script_version(db_session, node_type, content, comment, created_by):
    """Новая версия скрипта; становится активной, старая остаётся в истории."""
    if node_type not in NODE_TYPE_KEYS:
        raise ProvisionError("Неизвестный тип ноды")
    if not content.strip():
        raise ProvisionError("Пустой скрипт")

    last = (
        db_session.query(NodeInstallScript)
        .filter(NodeInstallScript.node_type == node_type)
        .order_by(NodeInstallScript.version.desc())
        .first()
    )
    db_session.query(NodeInstallScript).filter(
        NodeInstallScript.node_type == node_type,
        NodeInstallScript.is_active.is_(True),
    ).update({"is_active": False}, synchronize_session=False)

    script = NodeInstallScript(
        node_type=node_type,
        version=(last.version + 1) if last else 1,
        content=content,
        is_active=True,
        comment=(comment or None),
        created_by=created_by,
    )
    db_session.add(script)
    db_session.flush()
    return script


def activate_script_version(db_session, script_id):
    """Откат: сделать активной существующую версию."""
    script = db_session.query(NodeInstallScript).get(script_id)
    if script is None:
        raise ProvisionError("Версия скрипта не найдена", http_status=404)
    db_session.query(NodeInstallScript).filter(
        NodeInstallScript.node_type == script.node_type,
        NodeInstallScript.is_active.is_(True),
    ).update({"is_active": False}, synchronize_session=False)
    # Тоже bulk update: присваивание script.is_active=True не сработает, если
    # объект в identity map несёт устаревшее True после прошлых bulk update
    # (атрибут не станет dirty и UPDATE не уйдёт в БД).
    db_session.query(NodeInstallScript).filter(
        NodeInstallScript.id == script_id
    ).update({"is_active": True}, synchronize_session=False)
    db_session.expire(script)
    return script


def create_request(
    db_session,
    *,
    node_name,
    node_type,
    source_node,
    created_by,
    country_code=None,
    ssh_port=DEFAULT_SSH_PORT,
    ttl_minutes=60,
):
    """Создать заявку; возвращает (заявка, токен). Токен показывается один раз.

    source_node — proto.Node ноды-образца: с неё снимается конфиг-профиль
    панели, чтобы claim не зависел от её дальнейшей судьбы.
    """
    node_name = (node_name or "").strip()
    if not (3 <= len(node_name) <= 30):
        raise ProvisionError("Имя ноды: от 3 до 30 символов (ограничение панели)")
    if node_type not in NODE_TYPE_KEYS:
        raise ProvisionError("Неизвестный тип ноды")

    script = active_script(db_session, node_type)
    if script is None:
        raise ProvisionError(
            "Для этого типа ноды не задан скрипт установки — задай скрипт "
            "на вкладке «Установка нод»."
        )

    if source_node is None or not source_node.config_profile_uuid:
        raise ProvisionError(
            "У ноды-образца нет конфиг-профиля — выбери другую ноду"
        )

    token = generate_token()
    provision_request = NodeProvisionRequest(
        node_name=node_name,
        node_type=node_type,
        script_id=script.id,
        token_hash=hash_token(token),
        status=NodeProvisionStatus.CREATED,
        expires_at=datetime.now() + timedelta(minutes=ttl_minutes),
        ssh_port=ssh_port,
        country_code=(country_code or None),
        config_profile_uuid=source_node.config_profile_uuid,
        inbound_uuids=list(source_node.active_inbound_uuids),
        created_by=created_by,
    )
    db_session.add(provision_request)
    db_session.flush()
    return provision_request, token


def find_request_by_token(db_session, token):
    if not token:
        raise ProvisionError("Нет токена", http_status=401)
    provision_request = (
        db_session.query(NodeProvisionRequest)
        .filter(NodeProvisionRequest.token_hash == hash_token(token))
        .first()
    )
    if provision_request is None:
        raise ProvisionError("Неизвестный токен", http_status=403)
    if provision_request.status not in TOKEN_ALIVE_STATUSES:
        raise ProvisionError(
            f"Заявка в статусе {provision_request.status.value}, токен погашен",
            http_status=403,
        )
    return provision_request


def check_claimed_ip(provision_request, client_ip):
    """После claim токен привязан к IP первого сервера."""
    if provision_request.claimed_ip and provision_request.claimed_ip != client_ip:
        logger.warning(
            "node bootstrap: request %s called from foreign ip %s (claimed %s)",
            provision_request.id,
            client_ip,
            provision_request.claimed_ip,
        )
        raise ProvisionError("Токен привязан к другому серверу", http_status=403)


def set_stage(db_session, provision_request, stage, status, message=None):
    row = (
        db_session.query(NodeProvisionStage)
        .filter(
            NodeProvisionStage.request_id == provision_request.id,
            NodeProvisionStage.stage == stage,
        )
        .first()
    )
    if row is None:
        row = NodeProvisionStage(
            request_id=provision_request.id, stage=stage, status=status
        )
        db_session.add(row)
    row.status = status
    if message is not None:
        row.message = message[:4096]
    return row


def claim_request(db_session, provision_request, client_ip, rwms_client):
    """Первый вызов с сервера: фиксация IP, выдача SECRET_KEY, создание ноды.

    Идемпотентен: повторный claim с того же IP заново собирает payload
    (CreateNode на стороне RWMS дубликатов не создаёт).
    """
    if provision_request.status == NodeProvisionStatus.CREATED:
        if datetime.now() > provision_request.expires_at:
            raise ProvisionError("Токен истёк — создай новую заявку", http_status=410)
    else:
        check_claimed_ip(provision_request, client_ip)

    secret_response = rwms_client.get_node_secret()
    if secret_response is None or not secret_response.secret_key:
        raise ProvisionError("RWMS недоступен (secret)", http_status=502)

    node = rwms_client.create_node(
        rw_proto.CreateNodeRequest(
            name=provision_request.node_name,
            address=client_ip,
            port=REMNANODE_PORT,
            country_code=provision_request.country_code,
            config_profile_uuid=provision_request.config_profile_uuid,
            inbound_uuids=list(provision_request.inbound_uuids or []),
        )
    )
    if node is None or not node.uuid:
        raise ProvisionError("RWMS недоступен (create node)", http_status=502)

    provision_request.status = (
        NodeProvisionStatus.CLAIMED
        if provision_request.status == NodeProvisionStatus.CREATED
        else provision_request.status
    )
    provision_request.claimed_ip = client_ip
    provision_request.claimed_at = provision_request.claimed_at or datetime.now()
    provision_request.remnawave_node_uuid = node.uuid
    set_stage(db_session, provision_request, "claim", "ok")

    logger.info(
        "node bootstrap: claim ok request=%s node=%s ip=%s rw_uuid=%s",
        provision_request.id,
        provision_request.node_name,
        client_ip,
        node.uuid,
    )
    script = db_session.query(NodeInstallScript).get(provision_request.script_id)
    return {
        "node_name": provision_request.node_name,
        "node_type": provision_request.node_type,
        "ssh_port": provision_request.ssh_port,
        "secret_key": secret_response.secret_key,
        "script_sha256": script_sha256(script.content),
        "remnawave_node_uuid": node.uuid,
    }


def check_stage_allowed(provision_request):
    """Эндпоинты после claim работают только по уже склеймленной заявке."""
    if provision_request.status == NodeProvisionStatus.CREATED:
        raise ProvisionError("Сначала claim", http_status=409)


def record_progress(db_session, provision_request, stage, status, message, log_tail):
    check_stage_allowed(provision_request)
    if stage not in STAGES:
        raise ProvisionError("Неизвестная стадия")
    if status not in ("running", "ok", "failed"):
        raise ProvisionError("Неизвестный статус стадии")

    set_stage(db_session, provision_request, stage, status, message)
    if stage == "script" and status == "running":
        if provision_request.status == NodeProvisionStatus.CLAIMED:
            provision_request.status = NodeProvisionStatus.PROVISIONING
    if status == "failed":
        provision_request.status = NodeProvisionStatus.FAILED
        provision_request.error = (message or f"стадия {stage} упала")[:4096]
        provision_request.finished_at = datetime.now()
    if log_tail:
        provision_request.install_log = log_tail[-INSTALL_LOG_MAX_BYTES:]


def complete_request(db_session, provision_request, exit_code):
    check_stage_allowed(provision_request)
    if provision_request.status == NodeProvisionStatus.INSTALLED:
        return  # повторный complete — идемпотентно
    if exit_code == 0:
        provision_request.status = NodeProvisionStatus.INSTALLED
        provision_request.error = None
        set_stage(db_session, provision_request, "script", "ok")
        set_stage(db_session, provision_request, "connect", "running")
    else:
        provision_request.status = NodeProvisionStatus.FAILED
        provision_request.error = f"install-скрипт завершился с кодом {exit_code}"
        set_stage(
            db_session, provision_request, "script", "failed", provision_request.error
        )
    provision_request.finished_at = datetime.now()
    logger.info(
        "node bootstrap: complete request=%s exit_code=%s",
        provision_request.id,
        exit_code,
    )


def refresh_connect_status(db_session, provision_request, rwms_client):
    """INSTALLED -> READY, когда панель увидела коннект remnanode.

    Вызывается из детальной admin-ручки (polling страницы заявки).
    """
    if provision_request.status != NodeProvisionStatus.INSTALLED:
        return
    nodes_response = rwms_client.get_nodes()
    if nodes_response is None:
        return
    for node in nodes_response.nodes:
        if node.uuid == provision_request.remnawave_node_uuid and node.is_connected:
            provision_request.status = NodeProvisionStatus.READY
            set_stage(db_session, provision_request, "connect", "ok")
            logger.info(
                "node bootstrap: node connected, request=%s ready",
                provision_request.id,
            )
            return


def revoke_request(provision_request):
    if provision_request.status in (
        NodeProvisionStatus.READY,
        NodeProvisionStatus.REVOKED,
    ):
        raise ProvisionError("Заявку в этом статусе отозвать нельзя")
    provision_request.status = NodeProvisionStatus.REVOKED
    provision_request.finished_at = datetime.now()
