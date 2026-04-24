#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/var/log/monkey-island-cert-renew.log"
CRON_FILE="/etc/cron.d/monkey-island-cert-renew"

cat > "${CRON_FILE}" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

17 3,15 * * * root cd '${SCRIPT_DIR}' && ./update-cloudflare-allowlist.sh && ./renew-certs.sh >> '${LOG_FILE}' 2>&1
EOF

chmod 644 "${CRON_FILE}"
touch "${LOG_FILE}"
echo "Installed ${CRON_FILE}"
