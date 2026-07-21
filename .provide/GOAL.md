# Goal — Language containers + remaining server surface parity

**Status:** complete  
**Out of scope:** **C# MCP** (permanent)  
**Head:** `main` @ residual closeout (`f9fd3159` and later)

## Objective

1. **Every language server has its own Docker image** and compose service (Python / Go / C#), plus CF as today.
2. **Close remaining C# (and any Go) host-API surface gaps** vs the Python/Go oracle until only permanent de-scopes remain (embed FastAPI helper, C# MCP).

## Docker (phase D)

- [x] `docker/Dockerfile.go` + `docker/Dockerfile.csharp` (+ SPA bake)
- [x] `docker/docker-compose.yml` services `server` / `server-go` / `server-csharp` / `cf`
- [x] Directory mounts `docker/etc-uterm` → `/etc/uterm`
- [x] `ci/docker_language_smoke.sh` + CI `docker-smoke` job
- [x] `/healthz` → `{"status":"ok"}` on 27780 / 27781 / 27782

## Surface parity (phase S)

Closed: profiles, keys, approvals, metrics/posture, EventBus watch/SSE, session
patch/bulk + connect depth, SPA bake. **Canonical matrix:** `.provide/HANDOFF.md`
§ “Parity matrix (Python / Go / C#)”.

## Permanent non-goals

| Item | Reason |
|------|--------|
| C# MCP | Operator de-scope |
| FastAPI `mount_terminal_ui` on Go/C# | Embed helper; use `uterm proxy` |
| Cloudflare full port to Go/C# | Separate product |

## One-liner

> Ship Docker per language server; close remaining C# host REST/UI gaps vs Go/Python. **No C# MCP.**
