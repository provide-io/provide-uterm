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

# No window manager. A WM (fluxbox toolbar / decorations) left black margins
# around Chromium and made the nested desktop look padded. Chromium is
# launched fullscreen against raw Xvfb so the RFB framebuffer is edge-to-edge.
echo "vnc-lab: no window manager (Chromium owns the full framebuffer)"
xsetroot -solid "#0b0f14" 2>/dev/null || true

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
# -noxdamage: Chromium/canvas often skips XDAMAGE events
# -fs 1: force a full-screen RFB refresh every second so nested xterm.js
#        canvas repaints actually reach viewers (incremental damage is flaky)
# -nonap: don't sleep between polls
# -wait/-defer: short poll latency
COMMON_ARGS=(
  -display "${DISPLAY}"
  -listen 0.0.0.0
  -forever
  -shared
  -xkb
  -ncache 0
  -noxdamage
  -fs 1
  -nonap
  -wait 5
  -defer 5
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

# Docker-friendly Chromium under raw Xvfb (no WM):
# - no sandbox / test-type (suppresses the --no-sandbox infobar)
# - software rasterizer left ON so xterm.js canvas repaints hit X11
# - explicit window size/position + xdotool pin to 0,0 so the nested browser
#   owns the full RFB framebuffer (no top/side black margins)
export LIBGL_ALWAYS_SOFTWARE=1
# Parse geometry WxH for window size (default 1280x720).
_geom_wh="${GEOMETRY%%x*}"
_rest="${GEOMETRY#*x}"
_geom_h="${_rest%%x*}"
_geom_w="${_geom_wh:-1280}"
_geom_h="${_geom_h:-720}"

# --- Scene mode: play an animated clip natively instead of Chromium. ----------
# Chromium's web-content compositor does not continuously present animation
# frames to the Xvfb framebuffer (proven: a CSS/rAF animation never reaches
# x11vnc), so a VNC mirror of a nested browser freezes on the first paint. mpv
# blits each decoded frame to X whole, so x11vnc streams clean motion to an
# already-connected viewer — no per-cell tearing like a software terminal.
# Enabled when SCENE_MEDIA points at an animated file (docker cp'd after boot).
if [[ -n "${SCENE_MEDIA:-}" ]]; then
  echo "browser_binary=mpv-scene" >> "${NAV_LOG}"
  echo "scene_media=${SCENE_MEDIA}" >> "${NAV_LOG}"
  # The media file is copied in shortly after boot; wait for it to land.
  for _ in $(seq 1 100); do
    [ -s "${SCENE_MEDIA}" ] && break
    sleep 0.1
  done
  # Software X11 output (no GPU under Xvfb); loop forever, fill the framebuffer,
  # no UI/OSD/input. --keepaspect=no stretches the clip edge-to-edge.
  mpv \
    --vo=x11 \
    --loop-file=inf \
    --no-audio \
    --no-config \
    --really-quiet \
    --no-input-default-bindings \
    --no-input-terminal \
    --no-osc \
    --no-border \
    --cursor-autohide=always \
    --geometry="${_geom_w}x${_geom_h}+0+0" \
    --autofit="${_geom_w}x${_geom_h}" \
    --keepaspect=no \
    "${SCENE_MEDIA}" \
    >>"${LOG_DIR}/chromium.log" 2>&1 &
  CHROME_PID=$!
  echo "browser_pid=${CHROME_PID}" >> "${NAV_LOG}"
  echo "browser_nav_status=running" >> "${NAV_LOG}"
  sleep 2
  if ! kill -0 "${CHROME_PID}" 2>/dev/null; then
    echo "browser_nav_status=exited_early" >> "${NAV_LOG}"
    echo "vnc-lab: scene mpv exited early; see ${LOG_DIR}/chromium.log" >&2
    cat "${LOG_DIR}/chromium.log" >&2 || true
    exit 1
  fi
  # Pin the mpv window edge-to-edge (best-effort).
  for _ in $(seq 1 40); do
    if xdotool search --onlyvisible --class mpv windowmove 0 0 windowsize "${_geom_w}" "${_geom_h}" 2>/dev/null; then
      break
    fi
    sleep 0.15
  done
  echo "vnc-lab: scene player running ${SCENE_MEDIA} (pid ${CHROME_PID})"
  wait "${XVFB_PID}"
  exit 0
fi

# Anti-throttle flags: without a window manager Chromium treats the sole Xvfb
# window as occluded/backgrounded and throttles the renderer, so an
# xterm.js canvas driven by live WebSocket frames stops repainting — x11vnc then
# sees no pixel change and the VNC mirror freezes. These keep the renderer hot so
# streamed animation actually paints (and thus streams through RFB → noVNC).
"${CHROME_BIN}" \
  --no-sandbox \
  --test-type \
  --disable-gpu \
  --disable-gpu-compositing \
  --disable-dev-shm-usage \
  --no-first-run \
  --no-default-browser-check \
  --disable-session-crashed-bubble \
  --disable-infobars \
  --hide-crash-restore-bubble \
  --disable-renderer-backgrounding \
  --disable-backgrounding-occluded-windows \
  --disable-background-timer-throttling \
  --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling \
  --disable-frame-rate-limit \
  --disable-gpu-vsync \
  --window-size="${_geom_w},${_geom_h}" \
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

# Pin every Chromium window to the full framebuffer. Without a WM, Chromium
# often maps with a top offset; xdotool forces edge-to-edge (no black margins).
_pinned=0
for _ in $(seq 1 50); do
  # Class names vary: Chromium / chromium / Google-chrome / Chrome
  for _pat in Chromium chromium Chrome chrome; do
    if xdotool search --onlyvisible --class "${_pat}" \
        windowmove 0 0 windowsize "${_geom_w}" "${_geom_h}" 2>/dev/null; then
      _pinned=1
    fi
  done
  # Fallback: any window whose name contains Terminal / provide / chrom
  if [[ "${_pinned}" -eq 0 ]]; then
    if xdotool search --onlyvisible --name '.*' windowmove 0 0 \
        windowsize "${_geom_w}" "${_geom_h}" 2>/dev/null; then
      _pinned=1
    fi
  fi
  if [[ "${_pinned}" -eq 1 ]]; then
    echo "browser_geometry=${_geom_w}x${_geom_h}+0+0" >> "${NAV_LOG}"
    break
  fi
  sleep 0.15
done
if [[ "${_pinned}" -eq 0 ]]; then
  echo "browser_geometry=unpinned" >> "${NAV_LOG}"
  echo "vnc-lab: warning: could not xdotool-pin Chromium window" >&2
fi

echo "vnc-lab: browser navigated to ${DEMO_URL} (pid ${CHROME_PID})"

# Keep the container alive while Xvfb (and children) run.
wait "${XVFB_PID}"
