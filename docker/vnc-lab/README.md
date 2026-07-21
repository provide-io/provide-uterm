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
| **Plain VNC** | x11vnc on **TCP 5900** — unencrypted classic RFB |
| **Encrypted VNC** | x11vnc on **TCP 5901** — TLS (`-ssl SAVE`) + **VeNCrypt** + **ANONTLS** |
| RFB versions | Negotiated **3.3 / 3.7 / 3.8** (not pinned to one revision) |
| Encodings | LibVNCServer defaults: raw, copyrect, hextile, zlib, tight, zrle, … |
| Browser | Chromium (graphical) opens `DEMO_URL` on startup |
| Auth | Optional `VNC_PASSWORD` (both ports); empty = `-nopw` (lab default) |

### Why two ports?

A single TCP port cannot cleanly be both “raw RFB” and “TLS-first RFB” for all
clients. Dual listeners on the same X display give maximum client coverage:

| Client style | Port | Notes |
|--------------|------|--------|
| RealVNC / TigerVNC / noVNC / most legacy viewers (no TLS) | **5900** | Security None (or VNC Auth if password set) |
| x11vnc `vncs://`, SSL-capable viewers (TLS-first) | **5901** | OpenSSL tunnel, then RFB |
| VeNCrypt-capable (TigerVNC, some RealVNC) | **5901** | RFB then TLS (enabled with `-vencrypt support`) |
| Older vino-style “TLS” (ANONTLS) | **5901** | Enabled with `-anontls support` |

Lab certs are **self-signed** (`-ssl SAVE`) — clients must allow insecure / skip
verify (expected for a local fixture).

## Build / run

```bash
# From repo root
docker build -t uterm-test-vnc -f docker/vnc-lab/Dockerfile docker/vnc-lab

docker run --rm -d \
  --name uterm-test-vnc \
  --shm-size=256m \
  -p 5900:5900 \
  -p 5901:5901 \
  -e DEMO_URL=https://example.com \
  uterm-test-vnc

# Optional password on both ports:
#   -e VNC_PASSWORD=labpass
```

Connect:

- Unencrypted: any VNC viewer → `127.0.0.1:5900`
- Encrypted (TLS-first): SSL-capable viewer → `127.0.0.1:5901` (accept lab cert)
- Web console: websockify/noVNC → plain port 5900 (or stunnel/TLS path to 5901)

## Proof path

```bash
# Full build + run + plain RFB (3.3/3.7/3.8) + TLS RFB + navigation evidence
uv run python scripts/prove_vnc_lab.py --runs 2

# Or pytest (docker marker for the live path):
uv run pytest packages/provide-uterm/tests/e2e/test_docker_vnc_lab.py -m docker -v --no-cov
```

## Navigation evidence (inside the container)

- `/var/log/vnc-lab/browser-nav.log` — `browser_nav_url=<DEMO_URL>` + launch status
- `/var/log/vnc-lab/vnc-ready` — ports + modes (`plain,ssl,vencrypt,anontls`)
- `/var/log/vnc-lab/x11vnc-plain.log` / `x11vnc-ssl.log` — listener logs
- Chromium process cmdline includes the demo URL

## Env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `DEMO_URL` | `https://example.com` | URL Chromium opens |
| `GEOMETRY` | `1280x720x24` | Xvfb screen geometry |
| `RFB_PORT` | `5900` | Plain (unencrypted) listen port |
| `RFB_SSL_PORT` | `5901` | TLS / VeNCrypt / ANONTLS listen port |
| `VNC_PASSWORD` | _(empty)_ | Shared password for both ports; empty = `-nopw` |

## First-party provide-uterm VNC console

The lab is the proof backend for the **uterm** VNC web console (not bare noVNC):

```bash
# Lab
docker build -t uterm-test-vnc -f docker/vnc-lab/Dockerfile docker/vnc-lab
docker run --rm -d --name uterm-test-vnc --shm-size 256m -p 5900:5900 -p 5901:5901 uterm-test-vnc

# Server with seeded graphical targets (see scripts/uterm-server.vnc-lab.example.toml)
uv run uterm server --config scripts/uterm-server.vnc-lab.example.toml

# Full proof (plain×2 + TLS + denied + uterm UI screenshot)
uv run python scripts/prove_uterm_vnc_console.py --runs 2
```

Browser page (after hijack acquire):  
`http://127.0.0.1:8780/_terminal/vnc.html?worker_id=vnc-shell&hijack_id=…&target_id=lab-vnc-plain`

Wire path: binary WebSocket  
`/worker/{id}/hijack/{hijack_id}/gui/vnc?target_id=…`  
Server dials RFB upstream (plain or TLS with `tls` / `tls_insecure` in target config).
