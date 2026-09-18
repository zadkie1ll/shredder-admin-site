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
import ipaddress
import logging
import secrets
from datetime import datetime
from datetime import timedelta

from django.conf import settings

from common.models.db import NodeInstallScript
from common.models.db import NodeProvisionRequest
from common.models.db import NodeProvisionStage
from common.models.db import NodeProvisionStatus
from engine.request_ip import is_trusted_proxy_address

import proto.rwmanager_pb2 as rw_proto

logger = logging.getLogger("engine")

LEGACY_SCRIPT_NAMES = {
    "self_steal": "Self-steal (Reality + haproxy/nginx)",
    "hysteria": "Hysteria2",
    "whitelist": "Whitelist (чистый Reality)",
    "whitelist_self_steal": "Whitelist self-steal",
}

STAGES = ["claim", "script", "connect"]

DEFAULT_SSH_PORT = 40022
REMNANODE_PORT = 2222
INSTALL_LOG_MAX_BYTES = 64 * 1024
# Статусы, в которых токен ещё принимается bootstrap-эндпоинтами.
# FAILED здесь намеренно: упавшую установку перезапускают тем же
# one-liner'ом (токен уже привязан к IP сервера, это безопасно).
TOKEN_ALIVE_STATUSES = {
    NodeProvisionStatus.CREATED,
    NodeProvisionStatus.CLAIMED,
    NodeProvisionStatus.PROVISIONING,
    NodeProvisionStatus.INSTALLED,
    NodeProvisionStatus.FAILED,
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


def normalize_script_name(value: str) -> str:
    """Имя динамической группы версий в пределах существующей колонки БД."""
    name = " ".join((value or "").split())
    if not name:
        raise ProvisionError("Задай название скрипта")
    if len(name) > 32:
        raise ProvisionError("Название скрипта: не больше 32 символов")
    if any(ord(character) < 32 for character in name):
        raise ProvisionError("Название скрипта содержит недопустимые символы")
    return name


def script_display_name(name: str) -> str:
    """Красивые подписи для четырёх старых ключей до их переименования."""
    return LEGACY_SCRIPT_NAMES.get(name, name)


def _script_name_exists(db_session, name, *, except_name=None):
    normalized = name.casefold()
    return any(
        stored_name.casefold() == normalized and stored_name != except_name
        for stored_name, in db_session.query(NodeInstallScript.node_type).distinct()
    )


def create_script_group(db_session, name, created_by):
    """Создать сохраняемый черновик, который при первом save станет v1."""
    name = normalize_script_name(name)
    if _script_name_exists(db_session, name):
        raise ProvisionError("Скрипт с таким названием уже существует")
    draft = NodeInstallScript(
        node_type=name,
        version=0,
        content="",
        is_active=False,
        comment="Черновик",
        created_by=created_by,
    )
    db_session.add(draft)
    db_session.flush()
    return draft


def rename_script_group(db_session, old_name, new_name):
    """Переименовать группу версий, не меняя script_id существующих заявок."""
    old_name = (old_name or "").strip()
    new_name = normalize_script_name(new_name)
    scripts = (
        db_session.query(NodeInstallScript)
        .filter(NodeInstallScript.node_type == old_name)
        .all()
    )
    if not scripts:
        raise ProvisionError("Скрипт не найден", http_status=404)
    if new_name == old_name:
        return scripts[0]
    if _script_name_exists(db_session, new_name, except_name=old_name):
        raise ProvisionError("Скрипт с таким названием уже существует")
    for script in scripts:
        script.node_type = new_name
    db_session.flush()
    return scripts[0]


def delete_script_group(db_session, name):
    """Удалить только группу, версии которой не зафиксированы в заявках."""
    name = (name or "").strip()
    scripts = (
        db_session.query(NodeInstallScript)
        .filter(NodeInstallScript.node_type == name)
        .all()
    )
    if not scripts:
        raise ProvisionError("Скрипт не найден", http_status=404)
    script_ids = [script.id for script in scripts]
    used_count = (
        db_session.query(NodeProvisionRequest)
        .filter(NodeProvisionRequest.script_id.in_(script_ids))
        .count()
    )
    if used_count:
        raise ProvisionError(
            f"Скрипт используется в {used_count} заявках и хранит их историю; "
            "его нельзя удалить"
        )
    for script in scripts:
        db_session.delete(script)
    db_session.flush()
    return len(scripts)


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


def _read_has_own_input(line: str) -> bool:
    """У этого read есть собственный источник ввода, а не stdin установки.

    Такой read безопасен при закрытом stdin: `read -ra x <<<"$(cmd)"` берёт
    here-string, `read x < file` — файл, `read -u 3 x` — отдельный дескриптор.
    Без этой проверки блоки вроде torrent-block.sh (разбор аргументов через
    here-string) дают ложную тревогу, а привычка отмахиваться от ложных
    предупреждений ровно тогда и прячет настоящие.
    """
    return "<<<" in line or "<<" in line or "<" in line or " -u" in line


def script_warnings(content: str) -> list[str]:
    """Мягкие проверки скрипта при сохранении: только предупреждения.

    Скрипт исполняется без stdin (раннер зовёт его с `</dev/null`), поэтому
    интерактивный read либо повесит установку, либо уронит её через set -e.
    Heredoc'и это НЕ затрагивает: `install /dev/stdin <<'EOF'` переопределяет
    stdin для своей команды.
    """
    warnings = []
    if not content.startswith("#!"):
        warnings.append("Нет shebang в первой строке (ожидается #!/usr/bin/env bash).")
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("read ") or " read " in f" {stripped} ":
            if _read_has_own_input(stripped):
                continue
            warnings.append(
                f"Строка {line_number}: интерактивный read — под автоматической "
                "установкой stdin закрыт, скрипт повиснет или упадёт."
            )
    return warnings


def save_script_version(db_session, node_type, content, comment, created_by):
    """Новая версия скрипта; становится активной, старая остаётся в истории."""
    node_type = normalize_script_name(node_type)
    if not content.strip():
        raise ProvisionError("Пустой скрипт")

    # Браузерная textarea отправляет форму с CRLF-переводами строк; bash на
    # ноде падает уже на "set -euo pipefail\r" (инцидент 2026-08-19).
    # Храним канонический LF.
    content = content.replace("\r\n", "\n").replace("\r", "\n")

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

    if last is not None and last.version == 0 and not last.content:
        # Неактивный технический черновик ещё никем не мог быть выбран.
        script = last
        script.version = 1
        script.content = content
        script.is_active = True
        script.comment = comment or None
        script.created_by = created_by
    else:
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
    node_type = normalize_script_name(node_type)

    script = active_script(db_session, node_type)
    if script is None:
        raise ProvisionError(
            "Для выбранного скрипта нет активной версии — сохрани её "
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


def find_request_by_token(db_session, token, *, for_update=False):
    if not token:
        raise ProvisionError("Нет токена", http_status=401)
    query = db_session.query(NodeProvisionRequest).filter(
        NodeProvisionRequest.token_hash == hash_token(token)
    )
    if for_update:
        query = query.with_for_update()
    provision_request = query.first()
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


def node_matches_request(node, provision_request, client_ip):
    """A CreateNode retry is safe only for the exact immutable request identity."""
    return (
        bool(getattr(node, "uuid", ""))
        and getattr(node, "name", "") == provision_request.node_name
        and getattr(node, "address", "") == client_ip
        and getattr(node, "config_profile_uuid", "")
        == (provision_request.config_profile_uuid or "")
        and sorted(getattr(node, "active_inbound_uuids", []) or [])
        == sorted(provision_request.inbound_uuids or [])
    )


def ensure_node_matches_request(node, provision_request, client_ip):
    if node_matches_request(node, provision_request, client_ip):
        return
    logger.error(
        "node bootstrap: CreateNode identity conflict request=%s returned_uuid=%s "
        "returned_name=%s returned_address=%s",
        provision_request.id,
        getattr(node, "uuid", ""),
        getattr(node, "name", ""),
        getattr(node, "address", ""),
    )
    raise ProvisionError(
        "В панели уже есть нода с совпадающим именем или адресом, но другими "
        "параметрами. Существующая нода не изменена.",
        http_status=409,
    )


def node_address_unusable(client_ip):
    """Адрес не годится как публичный адрес ноды.

    Пустой или не IP, приватный, loopback, link-local (а также unspecified,
    multicast, reserved) либо входящий в TRUSTED_PROXY_NETWORKS — то есть
    адрес edge/nginx/docker, а не сервера. Такой IP означает, что настоящий
    адрес ноды за прокси определить не удалось.
    """
    try:
        address = ipaddress.ip_address((client_ip or "").strip())
    except ValueError:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    ):
        return True
    return is_trusted_proxy_address(str(address)) or is_trusted_proxy_address(
        client_ip
    )


def ensure_claim_ip_usable(provision_request, client_ip):
    """Не создавать в Remnawave ноду с адресом прокси или приватной сети."""
    if not node_address_unusable(client_ip):
        return
    logger.error(
        "node bootstrap: claim rejected request=%s: client ip %r is empty, "
        "non-public or a trusted proxy; check TRUSTED_PROXY_NETWORKS and "
        "ORIGIN_ALLOWED_PROXY_CIDRS",
        provision_request.id,
        (client_ip or "")[:64],
    )
    raise ProvisionError(
        "Не удалось определить публичный IP сервера: запрос пришёл с адреса "
        "прокси или из приватной сети. Нода не создана. Проверьте "
        "TRUSTED_PROXY_NETWORKS и ORIGIN_ALLOWED_PROXY_CIDRS на сайте.",
        http_status=409,
    )


def claim_request(db_session, provision_request, client_ip, rwms_client):
    """Первый вызов с сервера: фиксация IP, выдача SECRET_KEY, создание ноды.

    Идемпотентен: повторный claim с того же IP заново собирает payload
    (CreateNode на стороне RWMS дубликатов не создаёт).
    """
    # До любых изменений заявки и обращений к RWMS: адрес прокси в CreateNode
    # дал бы в панели ноду, к которой Remnawave не подключится.
    ensure_claim_ip_usable(provision_request, client_ip)

    if provision_request.status == NodeProvisionStatus.CREATED:
        if datetime.now() > provision_request.expires_at:
            raise ProvisionError("Токен истёк — создай новую заявку", http_status=410)
    else:
        check_claimed_ip(provision_request, client_ip)

    # Перезапуск после падения: тот же one-liner с того же сервера начинает
    # установку заново, прошлая ошибка сбрасывается.
    if provision_request.status == NodeProvisionStatus.FAILED:
        provision_request.status = NodeProvisionStatus.CLAIMED
        provision_request.error = None
        provision_request.finished_at = None

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
        ),
        # CreateNode на стороне RWMS — выгрузка нод панели и создание ноды
        # двумя запросами, поэтому массовый дедлайн, а не точечный.
        timeout=settings.RWMS_BULK_RPC_TIMEOUT_SECONDS,
    )
    if node is None or not node.uuid:
        raise ProvisionError("RWMS недоступен (create node)", http_status=502)
    ensure_node_matches_request(node, provision_request, client_ip)

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
        if node.uuid != provision_request.remnawave_node_uuid:
            continue
        if not node_matches_request(node, provision_request, provision_request.claimed_ip):
            provision_request.status = NodeProvisionStatus.FAILED
            provision_request.error = (
                "Нода в панели больше не соответствует параметрам заявки; READY не выставлен"
            )
            set_stage(
                db_session,
                provision_request,
                "connect",
                "failed",
                provision_request.error,
            )
            logger.error(
                "node bootstrap: refusing READY for identity mismatch request=%s node=%s",
                provision_request.id,
                node.uuid,
            )
            return
        if node.is_connected:
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
