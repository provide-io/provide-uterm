#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Lab image for graphical VNC (RFB) testing — mirrors the spirit of the
# Alpine OpenSSH fixture used by fan-out SSH e2e (uterm-test-ssh).
#
# Provides:
#   - Xvfb virtual display + lightweight window manager
#   - x11vnc RFB server on port 5900 (no password; lab-only)
#   - Chromium graphical browser auto-navigating to DEMO_URL
#
# Build (from repo root):
#   docker build -t uterm-test-vnc -f docker/vnc-lab/Dockerfile docker/vnc-lab
#
# Run:
#   docker run --rm -d -p 5900:5900 --name uterm-test-vnc --shm-size=256m uterm-test-vnc
#
# Env:
#   DEMO_URL   URL Chromium opens on startup (default: https://example.com)
#   GEOMETRY   Xvfb geometry (default: 1280x720x24)
#   RFB_PORT   VNC listen port (default: 5900)
#

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DEMO_URL=https://example.com \
    GEOMETRY=1280x720x24 \
    RFB_PORT=5900 \
    DISPLAY=:99 \
    HOME=/root

# X stack + VNC + WM + Chromium (graphical, not headless-only package)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fluxbox \
        fonts-liberation \
        procps \
        x11vnc \
        xauth \
        xterm \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/log/vnc-lab /root/.fluxbox

COPY entrypoint.sh /usr/local/bin/vnc-lab-entrypoint.sh
RUN chmod 755 /usr/local/bin/vnc-lab-entrypoint.sh

EXPOSE 5900

ENTRYPOINT ["/usr/local/bin/vnc-lab-entrypoint.sh"]
