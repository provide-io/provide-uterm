#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

# Docker images

Each **language server** has its own image. Cloudflare remains a separate
product line.

| Service | Dockerfile | Host port | Notes |
|---------|------------|-----------|-------|
| `server` | `Dockerfile.server` | **27780** | Python FastAPI reference (SPA baked) |
| `server-go` | `Dockerfile.go` | **27781** | Go `uterm server` (SPA at `/frontend`) |
| `server-csharp` | `Dockerfile.csharp` | **27782** | C# `uterm server` (SPA at `/app/frontend`; **no MCP**) |
| `cf` | `Dockerfile.cf` | **27788** | Cloudflare Worker |
| `vnc-lab` | `vnc-lab/Dockerfile` | **5900** | Lab-only Xvfb + x11vnc + Chromium (see `docker/vnc-lab/`) |

```bash
# All language servers + CF
docker compose -f docker/docker-compose.yml up --build

# One language only
docker compose -f docker/docker-compose.yml up --build server-go

# CI/local health smoke (build + curl /healthz for python/go/csharp)
bash ci/docker_language_smoke.sh
# Subset:
bash ci/docker_language_smoke.sh go csharp
```

## Config: always prefer a **directory** mount

Every language image reads **`/etc/uterm/server.toml`**.

Compose mounts **`docker/etc-uterm/`** (a directory that contains `server.toml`)
onto `/etc/uterm`. That pattern is reliable on Linux CI and Docker Desktop.

**Avoid** file-on-file binds like:

```bash
# Fragile on Docker Desktop — often fails with OCI "not a directory"
-v ./dev-smoke.toml:/etc/uterm/server.toml:ro
```

**Prefer:**

```bash
# Directory mount (compose default)
docker compose -f docker/docker-compose.yml up --build server

# Manual directory mount
mkdir -p /tmp/uterm-etc
cp docker/etc-uterm/server.toml /tmp/uterm-etc/   # JWT smoke config
# or: cp path/to/my.toml /tmp/uterm-etc/server.toml
docker run --rm -p 27780:27780 \
  -v /tmp/uterm-etc:/etc/uterm:ro \
  provide-uterm-server:local
# curl http://127.0.0.1:27780/healthz  →  {"status":"ok"}
```

Notes:

- Default **baked** image config (`docker/server.toml`) is **fail-closed** JWT
  placeholders — the process may exit until you mount a real/smoke config.
- Smoke config lives at `docker/etc-uterm/server.toml` (JWT + worker bearer for
  `0.0.0.0` binds; Python rejects `dev_token` on non-loopback).
- On macOS Docker Desktop, mount sources under `$HOME` or the project tree if
  `/var/folders` paths are not shared into the VM.

## Vulnerability scanning

CI workflow **Container Scan** (`container-scan.yml`) builds and Trivy-scans:

| Image | Script |
|-------|--------|
| Python | `ci/build_server_image.sh` |
| Go | `ci/build_go_image.sh` |
| C# | `ci/build_csharp_image.sh` |
| Cloudflare | `ci/build_cf_image.sh` |

HIGH/CRITICAL with available fixes fail the gate (`ignore-unfixed: true`).

## Surface parity (not packaging)

Containers make each language runnable the same way. **API/behavior gaps** are
tracked in `.provide/HANDOFF.md` (parity matrix). MCP remains **out of scope for C#**.
