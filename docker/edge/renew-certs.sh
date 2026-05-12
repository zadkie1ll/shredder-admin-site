#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

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

CF_DNS_API_TOKEN="$(read_env_value "CF_DNS_API_TOKEN" || true)"
CF_DNS_PROPAGATION_SECONDS="$(read_env_value "CF_DNS_PROPAGATION_SECONDS" || true)"
CF_DNS_PROPAGATION_SECONDS="${CF_DNS_PROPAGATION_SECONDS:-30}"

if [[ -z "${CF_DNS_API_TOKEN}" ]]; then
    echo "CF_DNS_API_TOKEN is required in ${ENV_FILE}" >&2
    exit 1
fi

mkdir -p "${SCRIPT_DIR}/letsencrypt" "${SCRIPT_DIR}/certbot-work" "${SCRIPT_DIR}/certbot-logs"

"${SCRIPT_DIR}/issue-certs.sh"

CREDENTIALS_FILE="$(mktemp)"
cleanup() {
    rm -f "${CREDENTIALS_FILE}"
}
trap cleanup EXIT

chmod 600 "${CREDENTIALS_FILE}"
cat > "${CREDENTIALS_FILE}" <<EOF
dns_cloudflare_api_token = ${CF_DNS_API_TOKEN}
EOF

docker run --rm \
    -v "${SCRIPT_DIR}/letsencrypt:/etc/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-work:/var/lib/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-logs:/var/log/letsencrypt" \
    -v "${CREDENTIALS_FILE}:/cloudflare.ini:ro" \
    certbot/dns-cloudflare:latest \
    renew \
    --dns-cloudflare \
    --dns-cloudflare-credentials /cloudflare.ini \
    --dns-cloudflare-propagation-seconds "${CF_DNS_PROPAGATION_SECONDS}"

docker compose -f "${SCRIPT_DIR}/docker-compose.yml" exec nginx nginx -t
docker compose -f "${SCRIPT_DIR}/docker-compose.yml" exec nginx nginx -s reload
echo "Renewal check finished and nginx config reloaded."
