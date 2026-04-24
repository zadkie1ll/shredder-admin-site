#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/cloudflare-ips"
REALIP_FILE="${OUTPUT_DIR}/realip.conf"
GEO_FILE="${OUTPUT_DIR}/geo.conf"
REALIP_TMP_FILE="$(mktemp)"
GEO_TMP_FILE="$(mktemp)"

cleanup() {
    rm -f "${REALIP_TMP_FILE}" "${GEO_TMP_FILE}"
}
trap cleanup EXIT

mkdir -p "${OUTPUT_DIR}"

{
    echo "# Generated from Cloudflare published proxy IP ranges"
    echo "# Sources:"
    echo "# https://www.cloudflare.com/ips-v4"
    echo "# https://www.cloudflare.com/ips-v6"
    echo "real_ip_header CF-Connecting-IP;"
    echo "real_ip_recursive on;"
} > "${REALIP_TMP_FILE}"

{
    echo "# Generated from Cloudflare published proxy IP ranges"
    echo "# Used by nginx geo{} against \$realip_remote_addr"
} > "${GEO_TMP_FILE}"

curl -fsSL https://www.cloudflare.com/ips-v4 | while IFS= read -r ip; do
    [[ -n "${ip}" ]] || continue
    echo "set_real_ip_from ${ip};" >> "${REALIP_TMP_FILE}"
    echo "${ip} 1;" >> "${GEO_TMP_FILE}"
done

curl -fsSL https://www.cloudflare.com/ips-v6 | while IFS= read -r ip; do
    [[ -n "${ip}" ]] || continue
    echo "set_real_ip_from ${ip};" >> "${REALIP_TMP_FILE}"
    echo "${ip} 1;" >> "${GEO_TMP_FILE}"
done

mv "${REALIP_TMP_FILE}" "${REALIP_FILE}"
mv "${GEO_TMP_FILE}" "${GEO_FILE}"
echo "Updated ${REALIP_FILE}"
echo "Updated ${GEO_FILE}"
