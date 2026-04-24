#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/var/log/monkey-island-edge-renew.log"
CRON_FILE="/etc/cron.d/monkey-island-edge-renew"

cat > "${CRON_FILE}" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

11 4,16 * * * root cd '${SCRIPT_DIR}' && ./renew-certs.sh >> '${LOG_FILE}' 2>&1
EOF

chmod 644 "${CRON_FILE}"
touch "${LOG_FILE}"
echo "Installed ${CRON_FILE}"
