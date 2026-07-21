#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Start Xvfb + fluxbox + dual x11vnc listeners, then launch Chromium at DEMO_URL.
#
# Dual RFB listeners (same X display — max client compatibility):
#   RFB_PORT     (default 5900) — classic unencrypted RFB (Security None / VNC Auth)
#   RFB_SSL_PORT (default 5901) — TLS-encrypted RFB with VeNCrypt + ANONTLS (x11vnc -ssl)
#
# Protocol: LibVNCServer negotiates RFB 3.3 / 3.7 / 3.8 with the usual encodings
# (raw, copyrect, hextile, zlib, tight, zrle, …). We leave version selection to
# the client handshake rather than pinning a single protocol revision.
#
# Writes navigation evidence under /var/log/vnc-lab/ for proof scripts.
#
set -euo pipefail

DEMO_URL="${DEMO_URL:-https://example.com}"
GEOMETRY="${GEOMETRY:-1280x720x24}"
RFB_PORT="${RFB_PORT:-5900}"
RFB_SSL_PORT="${RFB_SSL_PORT:-5901}"
# Optional shared VNC password for both listeners. Empty = -nopw (lab default).
VNC_PASSWORD="${VNC_PASSWORD:-}"
DISPLAY_NUM="${DISPLAY#:}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"

LOG_DIR=/var/log/vnc-lab
mkdir -p "${LOG_DIR}"
NAV_LOG="${LOG_DIR}/browser-nav.log"
READY_FILE="${LOG_DIR}/vnc-ready"
PASS_FILE="${LOG_DIR}/vnc.passwd"
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

# Shared auth args for both listeners.
AUTH_ARGS=()
if [[ -n "${VNC_PASSWORD}" ]]; then
  # -storepasswd writes a binary rfbauth file; -passwdfile is plain text first line.
  printf '%s\n' "${VNC_PASSWORD}" > "${PASS_FILE}"
  chmod 600 "${PASS_FILE}"
  AUTH_ARGS=(-passwdfile "${PASS_FILE}")
  echo "vnc-lab: VNC password auth enabled (both ports)"
else
  AUTH_ARGS=(-nopw)
  echo "vnc-lab: VNC auth disabled (-nopw; lab default)"
fi

# Common compatibility flags (clients negotiate RFB 3.3–3.8; no pinned revision).
# -shared: multiple concurrent viewers
# -forever: re-accept after client disconnect
# -xkb: better keyboard mapping across clients
# -ncache 0: avoid client-side pixel cache quirks with older viewers
COMMON_ARGS=(
  -display "${DISPLAY}"
  -listen 0.0.0.0
  -forever
  -shared
  -xkb
  -ncache 0
  "${AUTH_ARGS[@]}"
)

echo "vnc-lab: starting plain (unencrypted) x11vnc on 0.0.0.0:${RFB_PORT}"
x11vnc \
  "${COMMON_ARGS[@]}" \
  -rfbport "${RFB_PORT}" \
  -bg \
  -o "${LOG_DIR}/x11vnc-plain.log"

echo "vnc-lab: starting TLS/VeNCrypt/ANONTLS x11vnc on 0.0.0.0:${RFB_SSL_PORT}"
# -ssl SAVE: generate+persist self-signed cert (lab-only)
# -vencrypt support: VeNCrypt (modern TLS VNC) alongside x11vnc SSL
# -anontls support: older vino-style TLS security type
x11vnc \
  "${COMMON_ARGS[@]}" \
  -rfbport "${RFB_SSL_PORT}" \
  -ssl SAVE \
  -vencrypt support \
  -anontls support \
  -bg \
  -o "${LOG_DIR}/x11vnc-ssl.log"

wait_listener_log() {
  local log=$1
  local label=$2
  for _ in $(seq 1 80); do
    if grep -qE 'PORT=[0-9]+|Listening for VNC connections|The VNC desktop is' "${log}" 2>/dev/null; then
      return 0
    fi
    if grep -qiE 'vnc desktop|listening|sslport=' "${log}" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  echo "vnc-lab: ${label} listener did not become ready; log:" >&2
  cat "${log}" >&2 || true
  return 1
}

wait_listener_log "${LOG_DIR}/x11vnc-plain.log" "plain" || exit 1
wait_listener_log "${LOG_DIR}/x11vnc-ssl.log" "ssl" || exit 1

{
  date -u +%Y-%m-%dT%H:%M:%SZ
  echo "rfb_plain_port=${RFB_PORT}"
  echo "rfb_ssl_port=${RFB_SSL_PORT}"
  echo "rfb_auth=$([ -n "${VNC_PASSWORD}" ] && echo password || echo none)"
  echo "rfb_modes=plain,ssl,vencrypt,anontls"
  echo "rfb_versions=negotiated_3.3_3.7_3.8"
} > "${READY_FILE}"

echo "vnc-lab: RFB ready plain=:${RFB_PORT} ssl=:${RFB_SSL_PORT} (VeNCrypt+ANONTLS)"

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
