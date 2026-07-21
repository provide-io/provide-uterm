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
