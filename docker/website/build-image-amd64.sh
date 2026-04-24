#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

docker buildx build \
  --platform linux/amd64 \
  -t monkey-island-website:v0.1 \
  -f "${SCRIPT_DIR}/Dockerfile" \
  --output "type=docker,dest=${SCRIPT_DIR}/monkey-island-website-amd64.tar" \
  "${REPO_ROOT}"
