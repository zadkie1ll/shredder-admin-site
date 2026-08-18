#!/usr/bin/env bash
# Обёртка автоматической установки ноды Monkey Island.
# Запускается одной командой с панельного домена:
#   curl -fsSL {{ base_url }}/node-bootstrap/runner | bash -s -- <TOKEN>
# Сама установка выполняется скриптом из админки (вкладка «Установка нод»),
# обёртка отвечает за claim/сертификаты/доставку скрипта/отчёт о прогрессе.
set -euo pipefail

BASE_URL="{{ base_url }}"
TOKEN="${1:-}"

if [[ -z "$TOKEN" ]]; then
    echo "Usage: curl -fsSL ${BASE_URL}/node-bootstrap/runner | bash -s -- <TOKEN>"
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "Run as root"
    exit 1
fi

WORKDIR="/var/lib/monkeyisland/bootstrap"
LOG_FILE="$WORKDIR/install.log"
mkdir -p "$WORKDIR"
chmod 700 "$WORKDIR"

api() { # api <method> <path> [curl args...]
    local method="$1" path="$2"
    shift 2
    curl -fsS -X "$method" \
        -H "Authorization: Bearer $TOKEN" \
        "$@" \
        "${BASE_URL}/node-bootstrap/${path}/"
}

# Прогресс не должен ронять установку: API может быть временно недоступен.
report() { # report <stage> <status> [message]
    local stage="$1" status="$2" message="${3:-}"
    api POST progress \
        -H 'Content-Type: application/json' \
        --data "$(python3 - "$stage" "$status" "$message" <<'PYEOF'
import json, sys
print(json.dumps({"stage": sys.argv[1], "status": sys.argv[2], "message": sys.argv[3]}))
PYEOF
)" >/dev/null 2>&1 || echo "WARN: progress report failed (${stage}/${status})" >&2
}

# Хвост лога установки — отдельным вызовом, чтобы не раздувать report().
push_log() {
    api POST progress \
        -H 'Content-Type: application/json' \
        --data "$(python3 - "$LOG_FILE" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 16384))
        tail = f.read().decode("utf-8", "replace")
except OSError:
    tail = ""
print(json.dumps({"stage": "script", "status": "running", "log_tail": tail}))
PYEOF
)" >/dev/null 2>&1 || true
}

echo "== Monkey Island node bootstrap =="

# 1. Claim: представляемся, получаем конфиг и SECRET_KEY.
CLAIM_JSON=$(api POST claim) || { echo "ОШИБКА: claim не прошёл (токен истёк/использован?)"; exit 1; }

SECRET_KEY=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["secret_key"])' <<<"$CLAIM_JSON")
NODE_TYPE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["node_type"])' <<<"$CLAIM_JSON")
SCRIPT_SHA256=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["script_sha256"])' <<<"$CLAIM_JSON")
echo "claim ok: node_type=${NODE_TYPE}"

# Сертификаты в bootstrap не входят: их деплоишь вручную
# (manage-node-certificates.sh deploy --host <ip> из devops-репозитория).
# Install-скрипт сам скажет в конце, если сертов ещё нет.

# 2. Скрипт установки: зафиксированная в заявке версия, сверяем sha256.
api GET script -o "$WORKDIR/install.sh"
ACTUAL_SHA=$(sha256sum "$WORKDIR/install.sh" | cut -d' ' -f1)
if [[ "$ACTUAL_SHA" != "$SCRIPT_SHA256" ]]; then
    report script failed "sha256 mismatch"
    echo "ОШИБКА: sha256 скрипта не совпал (${ACTUAL_SHA} != ${SCRIPT_SHA256})"
    exit 1
fi
chmod 700 "$WORKDIR/install.sh"

# 3. Запуск ровно как руками: bash install.sh <SECRET_KEY>, stdin закрыт.
report script running
echo "-- запускаю скрипт установки, лог: $LOG_FILE --"

( while sleep 15; do push_log; done ) &
LOG_PUSHER_PID=$!
trap 'kill "$LOG_PUSHER_PID" 2>/dev/null || true' EXIT

set +e
bash "$WORKDIR/install.sh" "$SECRET_KEY" </dev/null 2>&1 | tee "$LOG_FILE"
SCRIPT_EXIT=${PIPESTATUS[0]}
set -e

kill "$LOG_PUSHER_PID" 2>/dev/null || true
push_log

# 4. Завершение: сайт переводит заявку в installed/failed; статус «нода
# подключилась» появится в админке, когда панель увидит remnanode.
api POST complete \
    -H 'Content-Type: application/json' \
    --data "{\"exit_code\": ${SCRIPT_EXIT}}" >/dev/null || true

if [[ "$SCRIPT_EXIT" -ne 0 ]]; then
    echo "ОШИБКА: скрипт установки завершился с кодом ${SCRIPT_EXIT}, лог: $LOG_FILE"
    exit "$SCRIPT_EXIT"
fi

echo "== Установка завершена, статус подключения смотри в админке на странице заявки =="
