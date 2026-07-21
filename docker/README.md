#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

# Docker images

Each **language server** has its own image. Cloudflare remains a separate
product line.

| Service | Dockerfile | Host port | Notes |
|---------|------------|-----------|-------|
| `server` | `Dockerfile.server` | **27780** | Python FastAPI reference |
| `server-go` | `Dockerfile.go` | **27781** | Go `uterm server` |
| `server-csharp` | `Dockerfile.csharp` | **27782** | C# `uterm server` (no MCP) |
| `cf` | `Dockerfile.cf` | **27788** | Cloudflare Worker |

```bash
# All language servers + CF
docker compose -f docker/docker-compose.yml up --build

# One language only
docker compose -f docker/docker-compose.yml up --build server-go
```

Default `/etc/uterm/server.toml` is **fail-closed** (JWT placeholders). Compose
mounts `docker/dev-smoke.toml` (throwaway smoke JWT public key + 32-char worker
bearer) so Python/Go/C# all boot on `0.0.0.0` — Python refuses `dev_token` on
non-loopback binds. Prefer a **directory** mount of `/etc/uterm` if Docker Desktop
rejects file-on-file binds:

```bash
mkdir -p /tmp/uterm-etc && cp docker/dev-smoke.toml /tmp/uterm-etc/server.toml
docker run --rm -p 27780:27780 \
  -v /tmp/uterm-etc:/etc/uterm:ro \
  provide-uterm-server:local
# curl http://127.0.0.1:27780/healthz  →  {"status":"ok"}
```

## Surface parity (not packaging)

Containers make each language runnable the same way. **API/behavior gaps** are
tracked in `.provide/HANDOFF.md` (C# REST residual vs Go, UI hosting, etc.).
MCP remains **out of scope for C#**.
