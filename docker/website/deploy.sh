#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH_HOST="${SSH_HOST:-mi.website}"
REMOTE_DIR="${REMOTE_DIR:-/root/website/website}"
DOCKER_NETWORK="${DOCKER_NETWORK:-monkey-island-network}"

LOCAL_IMAGE_TAG="${LOCAL_IMAGE_TAG:-monkey-island-website:v0.1}"
REMOTE_IMAGE_TAG="${REMOTE_IMAGE_TAG:-monkey-island-website:prod}"
IMAGE_TAR_NAME="${IMAGE_TAR_NAME:-monkey-island-website-amd64.tar}"
IMAGE_TAR_PATH="${SCRIPT_DIR}/${IMAGE_TAR_NAME}"

DRY_RUN=0
SKIP_BUILD=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Build the amd64 website image locally, upload runtime files to ${SSH_HOST}:${REMOTE_DIR},
then load the image and restart the website stack on the server.

Options:
  --dry-run     Show upload plan without changing the server
  --skip-build  Do not rebuild the local image tar before upload
  -h, --help    Show this help

Environment overrides:
  SSH_HOST          SSH config host or user@host (default: mi.website)
  REMOTE_DIR        Remote website directory (default: /root/website/website)
  DOCKER_NETWORK    Shared docker network for website/postgres (default: monkey-island-network)
  LOCAL_IMAGE_TAG   Tag embedded in the built tar (default: monkey-island-website:v0.1)
  REMOTE_IMAGE_TAG  Tag used by docker-compose on the server (default: monkey-island-website:prod)
  IMAGE_TAR_NAME    Tar file name in this directory (default: monkey-island-website-amd64.tar)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --skip-build)
            SKIP_BUILD=1
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

require_command docker
require_command rsync
require_command ssh

if [[ "${SKIP_BUILD}" -eq 0 ]]; then
    echo "Building amd64 image tar..."
    "${SCRIPT_DIR}/build-image-amd64.sh"
fi

if [[ ! -f "${IMAGE_TAR_PATH}" ]]; then
    echo "Image tar not found: ${IMAGE_TAR_PATH}" >&2
    exit 1
fi

RUNTIME_FILES=(
    "docker-compose.yml"
    "nginx.conf"
    "PRODUCTION.md"
    "update-cloudflare-allowlist.sh"
    "issue-certs.sh"
    "renew-certs.sh"
    "install-renew-cron.sh"
    "${IMAGE_TAR_NAME}"
)

echo "Preparing remote directory ${SSH_HOST}:${REMOTE_DIR}"

if [[ "${DRY_RUN}" -eq 0 ]]; then
    ssh "${SSH_HOST}" "mkdir -p \
        '${REMOTE_DIR}/certbot-www' \
        '${REMOTE_DIR}/letsencrypt' \
        '${REMOTE_DIR}/certbot-work' \
        '${REMOTE_DIR}/certbot-logs' \
        '${REMOTE_DIR}/cloudflare-ips'"
fi

RSYNC_ARGS=(
    --archive
    --compress
    --human-readable
    --progress
)

if [[ "${DRY_RUN}" -eq 1 ]]; then
    RSYNC_ARGS+=(--dry-run --itemize-changes)
fi

if [[ "${DRY_RUN}" -eq 0 ]]; then
    echo "Creating remote backup for existing image tar if present..."
    ssh "${SSH_HOST}" "\
        if [ -f '${REMOTE_DIR}/${IMAGE_TAR_NAME}' ]; then \
            if [ -f '${REMOTE_DIR}/${IMAGE_TAR_NAME}.bak' ]; then \
                ts=\$(date +%Y%m%d-%H%M%S); \
                mv '${REMOTE_DIR}/${IMAGE_TAR_NAME}.bak' '${REMOTE_DIR}/${IMAGE_TAR_NAME}.'\"\${ts}\"'.bak'; \
            fi; \
            mv '${REMOTE_DIR}/${IMAGE_TAR_NAME}' '${REMOTE_DIR}/${IMAGE_TAR_NAME}.bak'; \
        fi"
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

docker network inspect '${DOCKER_NETWORK}' >/dev/null 2>&1 || docker network create '${DOCKER_NETWORK}'

cd '${REMOTE_DIR}'
if [ ! -f '.env' ]; then
    echo "Remote .env file not found at ${REMOTE_DIR}/.env" >&2
    exit 1
fi
chmod +x update-cloudflare-allowlist.sh issue-certs.sh renew-certs.sh install-renew-cron.sh
./update-cloudflare-allowlist.sh
./issue-certs.sh
./install-renew-cron.sh
docker compose -f docker-compose.yml down || true
docker image rm '${REMOTE_IMAGE_TAG}' || true
docker load -i '${IMAGE_TAR_NAME}'
docker image tag '${LOCAL_IMAGE_TAG}' '${REMOTE_IMAGE_TAG}'
docker compose -f docker-compose.yml up -d --no-build
docker ps
EOF
)

echo "Loading image and starting website containers on the server..."
ssh "${SSH_HOST}" "${REMOTE_SCRIPT}"

echo "Website deploy complete."
