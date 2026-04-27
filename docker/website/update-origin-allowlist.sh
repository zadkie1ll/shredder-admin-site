#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
OUTPUT_DIR="${SCRIPT_DIR}/origin-allowlist"
OUTPUT_FILE="${OUTPUT_DIR}/allowlist.conf"
TMP_FILE="$(mktemp)"

cleanup() {
    rm -f "${TMP_FILE}"
}
trap cleanup EXIT

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

ORIGIN_ALLOWED_PROXY_CIDRS="$(read_env_value "ORIGIN_ALLOWED_PROXY_CIDRS" || true)"

if [[ -z "${ORIGIN_ALLOWED_PROXY_CIDRS}" ]]; then
    echo "ORIGIN_ALLOWED_PROXY_CIDRS is required in ${ENV_FILE}" >&2
    exit 1
fi

mkdir -p "${OUTPUT_DIR}"

{
    echo "# Generated from ORIGIN_ALLOWED_PROXY_CIDRS"
    IFS=',' read -r -a cidrs <<< "${ORIGIN_ALLOWED_PROXY_CIDRS}"
    for cidr in "${cidrs[@]}"; do
        trimmed="$(echo "${cidr}" | xargs)"
        [[ -n "${trimmed}" ]] || continue
        echo "allow ${trimmed};"
    done
    echo "deny all;"
} > "${TMP_FILE}"

mv "${TMP_FILE}" "${OUTPUT_FILE}"
echo "Updated ${OUTPUT_FILE}"
