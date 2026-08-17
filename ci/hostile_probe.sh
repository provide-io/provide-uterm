#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Drive the hostile-client resilience suite (scripts/hostile_profile.py) against
# a locally-running uterm server started in its DEFAULT fail-closed posture (no
# --config). Each subcommand runs one tuned probe and asserts the server
# SURVIVES the hostile traffic (stays healthy, refuses/bounds every attempt) —
# not that hostile connections succeed. The base URL is defined once here so the
# workflow never hardcodes a host/port.
#
# Usage: ci/hostile_probe.sh <start|wait-health|burst|worker|oversized|slowloris|availability|stop>
set -euo pipefail

# Single source of truth for where the suite reaches the server. The default
# host/port come from TerminalDefaults (no hardcoded port — CLAUDE.md). Override
# with HOSTILE_BASE_URL=... ; `start` derives the bind from this same value.
HOSTILE_BASE_URL="${HOSTILE_BASE_URL:-$(uv run python -c 'from provide.uterm.defaults import TerminalDefaults as T; print(f"http://{T.SERVER_HOST}:{T.SERVER_PORT}")')}"
WORKER_ID="${HOSTILE_WORKER_ID:-provide-shell}"
SERVER_LOG="/tmp/uterm-hostile.log"
SERVER_PID_FILE="/tmp/uterm-hostile.pid"

# The availability lane authenticates legit sessions with the dev JWT the server
# mints at startup. Pin its path so `start` (the server process) and
# `availability` (the probe process) agree on where it lives. This is a file
# PATH, not a secret (CLAUDE.md: no hardcoded secrets); the JWT itself is
# generated per-run and written 0600 by the server's dev IdP.
export UTERM_DEV_TOKEN_PATH="${UTERM_DEV_TOKEN_PATH:-/tmp/uterm-hostile-dev-token}"

probe() {
  uv run python scripts/hostile_profile.py --base-url "${HOSTILE_BASE_URL}" "$@"
}

case "${1:?usage: hostile_probe.sh <start|wait-health|burst|worker|oversized|slowloris|availability|stop>}" in
  start)
    hostport="${HOSTILE_BASE_URL#*://}"
    host="${hostport%%:*}"
    port="${hostport##*:}"
    
    server_impl="${SERVER_IMPL:-python}"
    if [ "$server_impl" = "python" ]; then
      nohup uv run uterm server --host "${host}" --port "${port}" >"${SERVER_LOG}" 2>&1 &
    elif [ "$server_impl" = "go" ]; then
      (cd packages/provide-uterm-go && nohup go run cmd/uterm/main.go server --host "${host}" --port "${port}" >"${SERVER_LOG}" 2>&1) &
    elif [ "$server_impl" = "csharp" ]; then
      # --project rather than `cd`: the CLI moved to cmd/Uterm, and a `cd` that
      # fails inside `( ... ) &` takes the subshell down without failing this
      # step -- the start reported success and the breakage only surfaced as a
      # health timeout sixty seconds later, in a different step. The giveaway is
      # that SERVER_LOG does not exist at all, because the failing `cd` short
      # circuits the `&&` before the redirect that would create it.
      nohup dotnet run --project packages/provide-uterm-csharp/cmd/Uterm/Uterm.csproj -c Release \
        -- server --host "${host}" --port "${port}" >"${SERVER_LOG}" 2>&1 &
    else
      echo "unknown SERVER_IMPL: $server_impl" >&2
      exit 1
    fi
    server_pid=$!
    echo "${server_pid}" >"${SERVER_PID_FILE}"

    # Fail HERE if the launch did not take, rather than sixty seconds later in
    # wait-health. A backgrounded launch reports success no matter what goes
    # wrong -- a path that does not exist, a missing toolchain, a port already
    # bound, a crash on boot -- and the health timeout that follows names none
    # of them. That cost four days of a red `hostile-probes (csharp)` whose only
    # symptom was a curl that could not connect.
    #
    # Two seconds is enough to catch a launch that died immediately; a server
    # that is merely slow to bind is what wait-health is for.
    sleep 2
    if ! kill -0 "${server_pid}" 2>/dev/null; then
      echo "start: ${server_impl} server exited immediately (pid ${server_pid})" >&2
      cat "${SERVER_LOG}" 2>/dev/null || echo "start: ${SERVER_LOG} was never created" >&2
      exit 1
    fi
    if [ ! -s "${SERVER_LOG}" ]; then
      # An empty log is not fatal on its own (a quiet server may not have
      # written yet), but a MISSING one means the redirect never ran.
      [ -e "${SERVER_LOG}" ] || {
        echo "start: ${SERVER_LOG} was never created -- the launch never ran" >&2
        exit 1
      }
    fi
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
    # Connection-burst flood. Auth-gated WS endpoint: every unauthenticated
    # connect must be refused (a completed connect would be an auth bypass).
    probe \
      --worker-id "${WORKER_ID}" \
      --mode burst \
      --iterations 200 \
      --concurrency 40 \
      --require-refused
    ;;
  worker)
    # The WORKER side of the bridge, which nothing probed until now. It is the
    # more privileged socket — a worker feeds terminal output to every viewer
    # and drives session state — so an unauthenticated registration would be a
    # worse bypass than the browser one.
    #
    # Sequential, and only three of them: a session has exactly ONE worker,
    # so concurrent attempts collide over that slot and report as errors
    # rather than as the auth answer being asked for. This is a correctness
    # assertion about the gate, not a flood; the flood lanes are above.
    probe \
      --worker-id "${WORKER_ID}" \
      --mode worker \
      --iterations 3 \
      --concurrency 1 \
      --require-refused
    ;;
  oversized)
    # Oversized-frame flood. Same auth-gated endpoint, so under the fail-closed
    # posture this also asserts clean refusal (--require-refused).
    probe \
      --worker-id "${WORKER_ID}" \
      --mode oversized \
      --iterations 120 \
      --concurrency 20 \
      --payload-bytes 2000000 \
      --require-refused
    ;;
  slowloris)
    # Slow header-drip lives below the WS/auth layer (raw TCP), so it is
    # posture-agnostic: the server must bound the drip without hanging/crashing.
    probe \
      --mode slowloris \
      --iterations 80 \
      --concurrency 10 \
      --header-bytes-per-chunk 8 \
      --delay-s 0.2
    ;;
  availability)
    # Availability under attack: legitimate AUTHENTICATED sessions must keep
    # getting served (receive their hello frame within budget) WHILE an
    # unauthenticated flood is refused. Proves DoS-starvation resistance, not
    # merely survival. Reads the dev JWT from UTERM_DEV_TOKEN_PATH (above).
    probe \
      --worker-id "${WORKER_ID}" \
      --mode availability \
      --iterations 200 \
      --concurrency 40 \
      --auth-sessions 10 \
      --latency-budget-s 5.0
    ;;
  stop)
    if [ -f "${SERVER_PID_FILE}" ]; then
      kill "$(cat "${SERVER_PID_FILE}")" 2>/dev/null || true
      rm -f "${SERVER_PID_FILE}"
    fi
    ;;
  *)
    echo "unknown probe: ${1}" >&2
    exit 2
    ;;
esac
