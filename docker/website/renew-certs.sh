#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

mkdir -p \
    "${SCRIPT_DIR}/letsencrypt" \
    "${SCRIPT_DIR}/certbot-work" \
    "${SCRIPT_DIR}/certbot-logs" \
    "${SCRIPT_DIR}/certbot-www"

docker compose -f "${COMPOSE_FILE}" up -d nginx

docker run --rm \
    -v "${SCRIPT_DIR}/letsencrypt:/etc/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-work:/var/lib/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-logs:/var/log/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-www:/var/www/certbot" \
    certbot/certbot:latest \
    renew \
    --webroot \
    --webroot-path /var/www/certbot

docker compose -f "${COMPOSE_FILE}" exec nginx nginx -t
docker compose -f "${COMPOSE_FILE}" exec nginx nginx -s reload
echo "Renewal check finished and origin nginx config reloaded."
