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
CERTBOT_CERT_NAME="$(read_env_value "CERTBOT_CERT_NAME" || true)"
CERTBOT_DOMAINS="$(read_env_value "CERTBOT_DOMAINS" || true)"
CF_DNS_PROPAGATION_SECONDS="$(read_env_value "CF_DNS_PROPAGATION_SECONDS" || true)"

CERTBOT_CERT_NAME="${CERTBOT_CERT_NAME:-monkey-island-sites}"
CERTBOT_DOMAINS="${CERTBOT_DOMAINS:-monkey-island-vps.com,monkeyislandvpn.com,monkey-island-vpn.com,mnk-island.org}"
CF_DNS_PROPAGATION_SECONDS="${CF_DNS_PROPAGATION_SECONDS:-30}"

if [[ -z "${CF_DNS_API_TOKEN}" ]]; then
    echo "CF_DNS_API_TOKEN is required in ${ENV_FILE}" >&2
    exit 1
fi

if [[ -z "${LETSENCRYPT_EMAIL}" ]]; then
    echo "LETSENCRYPT_EMAIL is required in ${ENV_FILE}" >&2
    exit 1
fi

mkdir -p \
    "${SCRIPT_DIR}/letsencrypt" \
    "${SCRIPT_DIR}/certbot-work" \
    "${SCRIPT_DIR}/certbot-logs"

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
IFS=',' read -r -a domains <<< "${CERTBOT_DOMAINS}"
for domain in "${domains[@]}"; do
    trimmed="$(echo "${domain}" | xargs)"
    [[ -n "${trimmed}" ]] || continue
    domain_args+=(-d "${trimmed}")
done

if [[ ${#domain_args[@]} -eq 0 ]]; then
    echo "CERTBOT_DOMAINS is empty" >&2
    exit 1
fi

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
    --cert-name "${CERTBOT_CERT_NAME}" \
    "${domain_args[@]}"

echo "Certificate ready: ${CERTBOT_CERT_NAME}"
