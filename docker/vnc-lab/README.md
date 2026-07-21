#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

# uterm-test-vnc lab image

Graphical VNC (RFB) lab target for local demos and docker-marked e2e, in the
same spirit as the Alpine OpenSSH fixture (`uterm-test-ssh`) used by fan-out
SSH tests.

## What it provides

| Piece | Detail |
|-------|--------|
| Display | Xvfb `:99` (default `1280x720x24`) |
| Desktop | fluxbox |
| VNC | x11vnc on **TCP 5900**, no password (lab only) |
| Browser | Chromium (graphical) opens `DEMO_URL` on startup |

## Build / run

```bash
# From repo root
docker build -t uterm-test-vnc -f docker/vnc-lab/Dockerfile docker/vnc-lab

docker run --rm -d \
  --name uterm-test-vnc \
  --shm-size=256m \
  -p 5900:5900 \
  -e DEMO_URL=https://example.com \
  uterm-test-vnc
```

Connect any VNC viewer to `127.0.0.1:5900`.

## Proof path

```bash
# Full build + run + RFB handshake + navigation evidence (writes under SCRATCH
# or --evidence-dir). Safe to run twice; cleans up the container.
uv run python scripts/prove_vnc_lab.py
uv run python scripts/prove_vnc_lab.py --skip-build   # reuse image

# Or pytest (docker marker):
uv run pytest packages/provide-uterm/tests/e2e/test_docker_vnc_lab.py -m docker -v --no-cov
```

## Navigation evidence (inside the container)

- `/var/log/vnc-lab/browser-nav.log` — records `browser_nav_url=<DEMO_URL>` and launch status
- `/var/log/vnc-lab/vnc-ready` — written when x11vnc is up
- Chromium process cmdline includes the demo URL (`ps` / `docker exec`)

## Env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `DEMO_URL` | `https://example.com` | URL Chromium opens |
| `GEOMETRY` | `1280x720x24` | Xvfb screen geometry |
| `RFB_PORT` | `5900` | x11vnc listen port |
