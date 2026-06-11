#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH_HOST="${SSH_HOST:-mi.fornex.website}"
REMOTE_DIR="${REMOTE_DIR:-/root/website/postgres}"
DOCKER_NETWORK="${DOCKER_NETWORK:-monkey-island-network}"

DRY_RUN=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Upload postgres compose files to ${SSH_HOST}:${REMOTE_DIR} and restart the postgres stack.

Options:
  --dry-run   Show upload plan without changing the server
  -h, --help  Show this help
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

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Required command not found: $1" >&2
        exit 1
    fi
}

require_command rsync
require_command ssh

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
    ssh "${SSH_HOST}" "mkdir -p '${REMOTE_DIR}'"
fi

for relative_path in docker-compose.yml; do
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

docker network inspect '${DOCKER_NETWORK}' >/dev/null 2>&1 || docker network create '${DOCKER_NETWORK}'

cd '${REMOTE_DIR}'
if [ ! -f '.env' ]; then
    echo "Remote .env file not found at ${REMOTE_DIR}/.env" >&2
    exit 1
fi
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml ps
EOF
)

echo "Starting postgres stack on the server..."
ssh "${SSH_HOST}" "${REMOTE_SCRIPT}"

echo "Postgres deploy complete."
