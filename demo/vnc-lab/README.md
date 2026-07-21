#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

# VNC lab demo proof

Live proof that the `uterm-test-vnc` lab image serves a real RFB desktop and
that a **web console** (noVNC) can connect and show the in-session graphical
browser navigated to `https://example.com`.

## Artifacts

| File | What it shows |
|------|----------------|
| `screenshots/01-desktop-example-com.png` | X11 root grab from inside the lab (Chromium → example.com) |
| `screenshots/02-novnc-web-console.png` | Host browser noVNC session connected to the lab RFB port |
| `screenshots/03-novnc-connected-final.png` | Final frame at teardown (still connected) |
| `novnc-console.webm` / `novnc-console.mp4` | Recorded web-console session |
| `connection-status.txt` | noVNC status line / canvas size at connect time |

## Reproduce

```bash
# 1) Lab container — plain RFB :5900 + encrypted TLS/VeNCrypt :5901
docker build -t uterm-test-vnc -f docker/vnc-lab/Dockerfile docker/vnc-lab
docker run --rm -d --name uterm-test-vnc-demo --shm-size=256m \
  -p 5900:5900 -p 5901:5901 \
  -e DEMO_URL=https://example.com uterm-test-vnc

# 2) RFB proof (plain 3.3/3.7/3.8 + TLS handshake + browser nav)
uv run python scripts/prove_vnc_lab.py --skip-build --runs 1

# 3) Web console (noVNC + websockify) against the *plain* port:
#    websockify --web=/path/to/noVNC 6080 127.0.0.1:5900
#    open http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale
# Encrypted viewers use host:5901 (accept lab self-signed cert).

# 4) Desktop grab from inside the lab
docker exec uterm-test-vnc-demo bash -c \
  'apt-get update -qq && apt-get install -y -qq imagemagick && \
   DISPLAY=:99 import -window root /tmp/desk.png'
docker cp uterm-test-vnc-demo:/tmp/desk.png ./desktop.png
```

See also `docker/vnc-lab/README.md` and `scripts/prove_vnc_lab.py`.

## First-party uterm VNC console (preferred)

The external noVNC + websockify path above is a **contrast** demo only.
The product path is provide-uterm’s own page + human VNC relay:

| File | What it shows |
|------|----------------|
| `screenshots/uterm-vnc-console.png` | Full product chrome: brand, status, CTAs, remote desktop |
| `screenshots/uterm-vnc-aesthetic-full.png` | Hi-DPI aesthetic capture (same page) |
| `screenshots/uterm-vnc-aesthetic-desktop.png` | Desktop crop — example.com inside the relay canvas |

### Aesthetic / integration markers (asserted by capture script)

- Eyebrow brand **provide-uterm** + mark (matches hijack palette)
- Primary Connect CTA (accent green, same language as hijack console)
- Status chip **Connected · lab-vnc-plain** with live pulse
- Viewport chrome: **REMOTE DESKTOP** + live `1280×720 · lab-vnc-plain`
- Real RFB framebuffer (example.com Chromium in the lab), not a mock

```bash
# Functional proof (2× plain + TLS + denied + screenshot)
uv run python scripts/prove_uterm_vnc_console.py --runs 2

# Aesthetic / integration pack (full page + desktop crop + webm)
uv run python scripts/capture_uterm_vnc_aesthetic.py

# Nested proof: first-party VNC console → lab Chromium → text-based uterm terminal demo
uv run python scripts/record_uterm_vnc_demo_video.py --seconds 16
```

### Nested video (text demos inside VNC)

| File | What it shows |
|------|----------------|
| `uterm-vnc-text-demos.webm` | **~20s** recording of provide-uterm VNC chrome with remote Chromium open on `/_terminal/terminal.html?worker_id=provide-shell` |
| `screenshots/uterm-vnc-text-demos-full.png` | Still of the same stack (Connected · lab-vnc-plain + nested terminal UI) |
| `screenshots/uterm-vnc-text-demos-desktop.png` | Desktop crop |
| `video-metrics.json` | Asserted canvas size, status, demo URL |

Stack under test:

1. Host Playwright records **first-party** `vnc.html`
2. Lab RFB desktop runs Chromium at the host’s text console  
   `http://host.docker.internal:<port>/_terminal/terminal.html?worker_id=provide-shell&role=browser`
3. Host injects live shell demo keystrokes into `provide-shell` while recording

See `.provide/goals/uterm-vnc-web-console.md` and `scripts/uterm-server.vnc-lab.example.toml`.
