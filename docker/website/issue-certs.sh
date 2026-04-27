#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "Missing env file: ${ENV_FILE}" >&2
    exit 1
fi

read_env_value() {
    local key="$1"
    local line
    line="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 || true)"
    if [[ -z "${line}" ]]; then
        return 1
    fi

    line="${line#*=}"

    if [[ "${line}" =~ ^\"(.*)\"$ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi

    if [[ "${line}" =~ ^\'(.*)\'$ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi

    printf '%s\n' "${line}"
}

LETSENCRYPT_EMAIL="$(read_env_value "LETSENCRYPT_EMAIL" || true)"
ORIGIN_CERT_NAME="$(read_env_value "ORIGIN_CERT_NAME" || true)"
ORIGIN_CERTBOT_DOMAINS="$(read_env_value "ORIGIN_CERTBOT_DOMAINS" || true)"

if [[ -z "${LETSENCRYPT_EMAIL}" || -z "${ORIGIN_CERT_NAME}" || -z "${ORIGIN_CERTBOT_DOMAINS}" ]]; then
    echo "LETSENCRYPT_EMAIL, ORIGIN_CERT_NAME and ORIGIN_CERTBOT_DOMAINS are required in ${ENV_FILE}" >&2
    exit 1
fi

mkdir -p "${SCRIPT_DIR}/letsencrypt" "${SCRIPT_DIR}/certbot-work" "${SCRIPT_DIR}/certbot-logs"

domain_args=()
IFS=',' read -r -a domains <<< "${ORIGIN_CERTBOT_DOMAINS}"
for domain in "${domains[@]}"; do
    trimmed="$(echo "${domain}" | xargs)"
    [[ -n "${trimmed}" ]] || continue
    domain_args+=(-d "${trimmed}")
done

if [[ ${#domain_args[@]} -eq 0 ]]; then
    echo "ORIGIN_CERTBOT_DOMAINS is empty" >&2
    exit 1
fi

was_running=0
if docker compose -f "${COMPOSE_FILE}" ps --status running 2>/dev/null | grep -q nginx; then
    was_running=1
fi

docker compose -f "${COMPOSE_FILE}" down || true

docker run --rm \
    -p 80:80 \
    -v "${SCRIPT_DIR}/letsencrypt:/etc/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-work:/var/lib/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-logs:/var/log/letsencrypt" \
    certbot/certbot:latest \
    certonly \
    --standalone \
    --preferred-challenges http-01 \
    --non-interactive \
    --agree-tos \
    --expand \
    --key-type ecdsa \
    --email "${LETSENCRYPT_EMAIL}" \
    --cert-name "${ORIGIN_CERT_NAME}" \
    "${domain_args[@]}"

if [[ "${was_running}" -eq 1 ]]; then
    docker compose -f "${COMPOSE_FILE}" up -d
fi

echo "Certificate ready: ${ORIGIN_CERT_NAME}"
