#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

docker compose -f "${COMPOSE_FILE}" down || true

docker run --rm \
    -p 80:80 \
    -v "${SCRIPT_DIR}/letsencrypt:/etc/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-work:/var/lib/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-logs:/var/log/letsencrypt" \
    certbot/certbot:latest \
    renew \
    --standalone

docker compose -f "${COMPOSE_FILE}" up -d
docker compose -f "${COMPOSE_FILE}" exec nginx nginx -t
docker compose -f "${COMPOSE_FILE}" exec nginx nginx -s reload
echo "Renewal check finished and origin nginx config reloaded."
