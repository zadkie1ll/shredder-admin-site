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
LETSENCRYPT_EMAIL="$(read_env_value "LETSENCRYPT_EMAIL" || true)"
EDGE_CERT_NAME="$(read_env_value "EDGE_CERT_NAME" || true)"
EDGE_CERTBOT_DOMAINS="$(read_env_value "EDGE_CERTBOT_DOMAINS" || true)"
CF_DNS_PROPAGATION_SECONDS="$(read_env_value "CF_DNS_PROPAGATION_SECONDS" || true)"

EDGE_CERT_NAME="${EDGE_CERT_NAME:-edge-site}"
CF_DNS_PROPAGATION_SECONDS="${CF_DNS_PROPAGATION_SECONDS:-30}"

if [[ -z "${CF_DNS_API_TOKEN}" || -z "${LETSENCRYPT_EMAIL}" || -z "${EDGE_CERTBOT_DOMAINS}" ]]; then
    echo "CF_DNS_API_TOKEN, LETSENCRYPT_EMAIL and EDGE_CERTBOT_DOMAINS are required in ${ENV_FILE}" >&2
    exit 1
fi

mkdir -p "${SCRIPT_DIR}/letsencrypt" "${SCRIPT_DIR}/certbot-work" "${SCRIPT_DIR}/certbot-logs"

CERT_PATH="${SCRIPT_DIR}/letsencrypt/live/${EDGE_CERT_NAME}/fullchain.pem"
if [[ -f "${CERT_PATH}" ]] && openssl x509 -checkend 0 -noout -in "${CERT_PATH}" >/dev/null 2>&1; then
    echo "Certificate ${EDGE_CERT_NAME} already exists and is not expired. Skipping issue."
    exit 0
fi

if [[ -f "${CERT_PATH}" ]]; then
    echo "Certificate ${EDGE_CERT_NAME} exists but is expired. Issuing a new certificate."
else
    echo "Certificate ${EDGE_CERT_NAME} not found. Issuing a new certificate."
fi

CREDENTIALS_FILE="$(mktemp)"
cleanup() {
    rm -f "${CREDENTIALS_FILE}"
}
trap cleanup EXIT

chmod 600 "${CREDENTIALS_FILE}"
cat > "${CREDENTIALS_FILE}" <<EOF
dns_cloudflare_api_token = ${CF_DNS_API_TOKEN}
EOF

domain_args=()
IFS=',' read -r -a domains <<< "${EDGE_CERTBOT_DOMAINS}"
for domain in "${domains[@]}"; do
    trimmed="$(echo "${domain}" | xargs)"
    [[ -n "${trimmed}" ]] || continue
    domain_args+=(-d "${trimmed}")
done

docker run --rm \
    -v "${SCRIPT_DIR}/letsencrypt:/etc/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-work:/var/lib/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot-logs:/var/log/letsencrypt" \
    -v "${CREDENTIALS_FILE}:/cloudflare.ini:ro" \
    certbot/dns-cloudflare:latest \
    certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /cloudflare.ini \
    --dns-cloudflare-propagation-seconds "${CF_DNS_PROPAGATION_SECONDS}" \
    --non-interactive \
    --agree-tos \
    --keep-until-expiring \
    --preferred-challenges dns-01 \
    --key-type ecdsa \
    --email "${LETSENCRYPT_EMAIL}" \
    --cert-name "${EDGE_CERT_NAME}" \
    "${domain_args[@]}"

echo "Certificate ready: ${EDGE_CERT_NAME}"
