#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Start Xvfb + fluxbox + x11vnc, then launch Chromium at DEMO_URL.
# Writes navigation evidence under /var/log/vnc-lab/ for proof scripts.
#
set -euo pipefail

DEMO_URL="${DEMO_URL:-https://example.com}"
GEOMETRY="${GEOMETRY:-1280x720x24}"
RFB_PORT="${RFB_PORT:-5900}"
DISPLAY_NUM="${DISPLAY#:}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"

LOG_DIR=/var/log/vnc-lab
mkdir -p "${LOG_DIR}"
NAV_LOG="${LOG_DIR}/browser-nav.log"
READY_FILE="${LOG_DIR}/vnc-ready"
rm -f "${READY_FILE}"

cleanup() {
  local code=$?
  # Best-effort shutdown of children when the entrypoint is signalled.
  jobs -p | xargs -r kill 2>/dev/null || true
  exit "${code}"
}
trap cleanup EXIT INT TERM

echo "vnc-lab: starting Xvfb on ${DISPLAY} geometry=${GEOMETRY}"
Xvfb "${DISPLAY}" -screen 0 "${GEOMETRY}" -ac +extension RANDR -nolisten tcp &
XVFB_PID=$!

# Wait until the X socket exists before attaching clients.
for _ in $(seq 1 50); do
  if [[ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]]; then
    break
  fi
  if ! kill -0 "${XVFB_PID}" 2>/dev/null; then
    echo "vnc-lab: Xvfb exited early" >&2
    exit 1
  fi
  sleep 0.1
done

if [[ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]]; then
  echo "vnc-lab: X socket never appeared" >&2
  exit 1
fi

echo "vnc-lab: starting fluxbox"
fluxbox >/dev/null 2>&1 &
FLUX_PID=$!

echo "vnc-lab: starting x11vnc on 0.0.0.0:${RFB_PORT}"
# -nopw: lab fixture (no auth). -forever/-shared: keep accepting clients.
x11vnc \
  -display "${DISPLAY}" \
  -rfbport "${RFB_PORT}" \
  -listen 0.0.0.0 \
  -nopw \
  -forever \
  -shared \
  -xkb \
  -ncache 0 \
  -bg \
  -o "${LOG_DIR}/x11vnc.log"

# Confirm RFB is accepting connections before advertising ready.
for _ in $(seq 1 50); do
  if grep -qE 'PORT=[0-9]+|Listening for VNC connections' "${LOG_DIR}/x11vnc.log" 2>/dev/null; then
    break
  fi
  # Also accept "The VNC desktop is" which x11vnc prints when bound.
  if grep -qiE 'vnc desktop|listening' "${LOG_DIR}/x11vnc.log" 2>/dev/null; then
    break
  fi
  sleep 0.1
done

date -u +%Y-%m-%dT%H:%M:%SZ > "${READY_FILE}"
echo "vnc-lab: RFB ready on port ${RFB_PORT}"

# Record navigation intent + launch Chromium (graphical, not --headless).
{
  echo "browser_nav_url=${DEMO_URL}"
  echo "browser_nav_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "browser_binary=chromium"
} > "${NAV_LOG}"

# Prefer the Debian chromium wrapper; fall back to chromium-browser if renamed.
CHROME_BIN=""
for candidate in chromium chromium-browser google-chrome; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    CHROME_BIN="${candidate}"
    break
  fi
done
if [[ -z "${CHROME_BIN}" ]]; then
  echo "vnc-lab: no chromium binary found" >&2
  exit 1
fi

echo "browser_resolved_binary=${CHROME_BIN}" >> "${NAV_LOG}"

# Docker-friendly Chromium flags: no sandbox (container), small shm, no GPU.
"${CHROME_BIN}" \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --disable-software-rasterizer \
  --no-first-run \
  --no-default-browser-check \
  --disable-session-crashed-bubble \
  --disable-infobars \
  --window-size=1280,720 \
  --window-position=0,0 \
  "${DEMO_URL}" \
  >>"${LOG_DIR}/chromium.log" 2>&1 &
CHROME_PID=$!
echo "browser_pid=${CHROME_PID}" >> "${NAV_LOG}"
echo "browser_launch_cmd=${CHROME_BIN} ... ${DEMO_URL}" >> "${NAV_LOG}"
echo "browser_nav_status=launched" >> "${NAV_LOG}"

# Wait for Chromium process to stay up briefly (crash → fail the container).
sleep 2
if ! kill -0 "${CHROME_PID}" 2>/dev/null; then
  echo "browser_nav_status=exited_early" >> "${NAV_LOG}"
  echo "vnc-lab: Chromium exited early; see ${LOG_DIR}/chromium.log" >&2
  cat "${LOG_DIR}/chromium.log" >&2 || true
  exit 1
fi
echo "browser_nav_status=running" >> "${NAV_LOG}"
echo "vnc-lab: browser navigated to ${DEMO_URL} (pid ${CHROME_PID})"

# Keep the container alive while Xvfb (and children) run.
wait "${XVFB_PID}"
