#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Lab image for graphical VNC (RFB) testing — mirrors the spirit of the
# Alpine OpenSSH fixture used by fan-out SSH e2e (uterm-test-ssh).
#
# Provides:
#   - Xvfb virtual display + lightweight window manager
#   - Dual x11vnc listeners on the same display:
#       :5900  classic unencrypted RFB (Security None / optional VNC Auth)
#       :5901  TLS-encrypted RFB (x11vnc -ssl + VeNCrypt + ANONTLS)
#   - RFB version negotiation (3.3 / 3.7 / 3.8) — not pinned to one revision
#   - Chromium graphical browser auto-navigating to DEMO_URL
#
# Build (from repo root):
#   docker build -t uterm-test-vnc -f docker/vnc-lab/Dockerfile docker/vnc-lab
#
# Run:
#   docker run --rm -d -p 5900:5900 -p 5901:5901 \
#     --name uterm-test-vnc --shm-size=256m uterm-test-vnc
#
# Env:
#   DEMO_URL       URL Chromium opens on startup (default: https://example.com)
#   GEOMETRY       Xvfb geometry (default: 1280x720x24)
#   RFB_PORT       Plain (unencrypted) VNC listen port (default: 5900)
#   RFB_SSL_PORT   TLS/VeNCrypt VNC listen port (default: 5901)
#   VNC_PASSWORD   Optional shared password for both ports (empty = -nopw)
#

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DEMO_URL=https://example.com \
    GEOMETRY=1280x720x24 \
    RFB_PORT=5900 \
    RFB_SSL_PORT=5901 \
    VNC_PASSWORD= \
    DISPLAY=:99 \
    HOME=/root

# X stack + VNC + WM + Chromium (graphical, not headless-only package).
# openssl is used by x11vnc -ssl SAVE for lab self-signed certs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-liberation \
        mpv \
        openssl \
        procps \
        x11-xserver-utils \
        x11vnc \
        xauth \
        xdotool \
        xterm \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/log/vnc-lab /root/.fluxbox /root/.vnc

COPY entrypoint.sh /usr/local/bin/vnc-lab-entrypoint.sh
RUN chmod 755 /usr/local/bin/vnc-lab-entrypoint.sh

EXPOSE 5900 5901

ENTRYPOINT ["/usr/local/bin/vnc-lab-entrypoint.sh"]
