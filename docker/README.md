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

Default `/config/server.toml` is **fail-closed** (JWT placeholders). Mount a
real config for local/dev:

```bash
docker run --rm -p 27781:27780 \
  -v "$PWD/scripts/uterm-server.example.toml:/config/server.toml:ro" \
  provide-uterm-server-go:local
```

## Surface parity (not packaging)

Containers make each language runnable the same way. **API/behavior gaps** are
tracked in `.provide/HANDOFF.md` (C# REST residual vs Go, UI hosting, etc.).
MCP remains **out of scope for C#**.
