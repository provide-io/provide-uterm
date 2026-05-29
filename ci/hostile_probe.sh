#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Drive the hostile-client resilience suite (scripts/hostile_profile.py) against
# a locally-running uterm server. Each subcommand runs one tuned probe; the
# base URL is defined once here so the workflow never hardcodes a host/port.
#
# Usage: ci/hostile_probe.sh <start|wait-health|burst|oversized|slowloris>
set -euo pipefail

# Single source of truth for where the hostile suite reaches the server.
# Override with HOSTILE_BASE_URL=... when running against a different bind;
# the `start` subcommand derives the bind host/port from this same value.
HOSTILE_BASE_URL="${HOSTILE_BASE_URL:-http://127.0.0.1:8400}"
WORKER_ID="${HOSTILE_WORKER_ID:-provide-shell}"
SERVER_LOG="/tmp/uterm-hostile.log"
SERVER_PID_FILE="/tmp/uterm-hostile.pid"

probe() {
  uv run python scripts/hostile_profile.py --base-url "${HOSTILE_BASE_URL}" "$@"
}

case "${1:?usage: hostile_probe.sh <start|wait-health|burst|oversized|slowloris>}" in
  start)
    hostport="${HOSTILE_BASE_URL#*://}"
    host="${hostport%%:*}"
    port="${hostport##*:}"
    nohup uv run uterm server --host "${host}" --port "${port}" >"${SERVER_LOG}" 2>&1 &
    echo $! >"${SERVER_PID_FILE}"
    ;;
  wait-health)
    for _ in $(seq 1 60); do
      if curl -fsS "${HOSTILE_BASE_URL}/api/health" >/dev/null; then
        exit 0
      fi
      sleep 1
    done
    echo "server failed to become healthy" >&2
    cat "${SERVER_LOG}" || true
    exit 1
    ;;
  burst)
    probe \
      --worker-id "${WORKER_ID}" \
      --mode burst \
      --iterations 200 \
      --concurrency 40 \
      --min-success-rate 0.95 \
      --max-failure-rate 0.05
    ;;
  oversized)
    probe \
      --worker-id "${WORKER_ID}" \
      --mode oversized \
      --iterations 120 \
      --concurrency 20 \
      --payload-bytes 2000000 \
      --min-success-rate 0.90 \
      --max-failure-rate 0.10
    ;;
  slowloris)
    probe \
      --mode slowloris \
      --iterations 80 \
      --concurrency 10 \
      --header-bytes-per-chunk 8 \
      --delay-s 0.2 \
      --min-success-rate 0.80 \
      --max-failure-rate 0.20
    ;;
  *)
    echo "unknown probe: ${1}" >&2
    exit 2
    ;;
esac
