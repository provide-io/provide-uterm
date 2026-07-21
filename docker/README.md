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

```bash
# All language servers + CF
docker compose -f docker/docker-compose.yml up --build

# One language only
docker compose -f docker/docker-compose.yml up --build server-go

# CI/local health smoke (build + curl /healthz)
bash ci/docker_language_smoke.sh
```

Compose mounts **`docker/etc-uterm/`** as a **directory** at `/etc/uterm` (contains
`server.toml` from the JWT smoke config). Prefer directory mounts over
file-on-file binds — Docker Desktop often fails the latter with OCI
"not a directory".

```bash
# Manual directory mount
mkdir -p /tmp/uterm-etc && cp docker/etc-uterm/server.toml /tmp/uterm-etc/
docker run --rm -p 27780:27780 \
  -v /tmp/uterm-etc:/etc/uterm:ro \
  provide-uterm-server:local
# curl http://127.0.0.1:27780/healthz  →  {"status":"ok"}
```

## Surface parity (not packaging)

Containers make each language runnable the same way. **API/behavior gaps** are
tracked in `.provide/HANDOFF.md`. MCP remains **out of scope for C#**.
