# Goal — Language containers + remaining server surface parity

**Status:** complete  
**Out of scope:** **C# MCP** (permanent)  
**Head:** see `main` tip after docker + REST parity commits

## Objective

1. **Every language server has its own Docker image** and compose service (Python / Go / C#), plus CF as today.
2. **Close remaining C# (and any Go) host-API surface gaps** vs the Python/Go oracle until only permanent de-scopes remain (embed FastAPI helper, C# MCP).

## Docker (phase D)

- [x] `docker/Dockerfile.go` + `docker/Dockerfile.csharp`
- [x] `docker/docker-compose.yml` services `server` / `server-go` / `server-csharp` / `cf`
- [x] `docker/README.md`
- [x] Prove images build and `/healthz` → `{"status":"ok"}` on 27780/27781/27782 with mounted `docker/dev-smoke.toml` (JWT smoke config; directory mount of `/etc/uterm`)

## Surface parity (phase S)

1–7 closed (profiles, keys, approvals, metrics/posture, events/watch, session patch/bulk + connect, SPA shell). See HANDOFF residual table. Dual-OS cover floor raise remains optional follow-on.

## Permanent non-goals

| Item | Reason |
|------|--------|
| C# MCP | Operator de-scope |
| FastAPI `mount_terminal_ui` on Go/C# | Embed helper; use `uterm proxy` |
| Cloudflare full port to Go/C# | Separate product |

## One-liner

> Ship Docker per language server; close remaining C# host REST/UI gaps vs Go/Python. **No C# MCP.**
