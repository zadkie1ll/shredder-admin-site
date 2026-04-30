#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH_HOST="${SSH_HOST:-mi.edge1}"
REMOTE_DIR="${REMOTE_DIR:-/root/edge}"

DRY_RUN=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Upload an edge nginx stack to a remote server and start it there.

Options:
  --dry-run       Show upload plan without changing the server
  -h, --help      Show this help

Required env:
  SSH_HOST        SSH config host or user@host for the edge server

Optional env:
  REMOTE_DIR      Remote edge directory (default: /root/edge)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -z "${SSH_HOST}" ]]; then
    echo "SSH_HOST is required" >&2
    exit 1
fi

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Required command not found: $1" >&2
        exit 1
    fi
}

require_command rsync
require_command ssh

RUNTIME_FILES=(
    "docker-compose.yml"
    "nginx.conf.template"
    ".env.example"
    "issue-certs.sh"
    "renew-certs.sh"
    "install-renew-cron.sh"
)

RSYNC_ARGS=(
    --archive
    --compress
    --human-readable
    --progress
)

if [[ "${DRY_RUN}" -eq 1 ]]; then
    RSYNC_ARGS+=(--dry-run --itemize-changes)
fi

echo "Preparing remote directory ${SSH_HOST}:${REMOTE_DIR}"
if [[ "${DRY_RUN}" -eq 0 ]]; then
    ssh "${SSH_HOST}" "mkdir -p '${REMOTE_DIR}/letsencrypt' '${REMOTE_DIR}/certbot-work' '${REMOTE_DIR}/certbot-logs' '${REMOTE_DIR}/certbot-www'"
fi

for relative_path in "${RUNTIME_FILES[@]}"; do
    echo "Uploading ${relative_path}"
    rsync "${RSYNC_ARGS[@]}" \
        "${SCRIPT_DIR}/${relative_path}" \
        "${SSH_HOST}:${REMOTE_DIR}/${relative_path}"
done

if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "Dry run complete."
    exit 0
fi

REMOTE_SCRIPT=$(cat <<EOF
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker not found on server. Installing..."
    curl -fsSL https://get.docker.com | sh
fi

cd '${REMOTE_DIR}'
if [ ! -f '.env' ]; then
    echo "Remote .env file not found at ${REMOTE_DIR}/.env" >&2
    echo "Use .env.example as a template and create ${REMOTE_DIR}/.env first." >&2
    exit 1
fi

chmod +x issue-certs.sh renew-certs.sh install-renew-cron.sh
./issue-certs.sh
./install-renew-cron.sh
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml ps
EOF
)

echo "Starting edge stack on the server..."
ssh "${SSH_HOST}" "${REMOTE_SCRIPT}"

echo "Edge deploy complete."
