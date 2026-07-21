# Goal — Language containers + remaining server surface parity

**Status:** in progress (Docker images landed; surface backlog open)  
**Out of scope:** **C# MCP** (permanent)  
**Head:** see `main` tip after docker commits

## Objective

1. **Every language server has its own Docker image** and compose service (Python / Go / C#), plus CF as today.
2. **Close remaining C# (and any Go) host-API surface gaps** vs the Python/Go oracle until only permanent de-scopes remain (embed FastAPI helper, C# MCP).

## Docker (phase D)

- [x] `docker/Dockerfile.go` + `docker/Dockerfile.csharp`
- [x] `docker/docker-compose.yml` services `server` / `server-go` / `server-csharp` / `cf`
- [x] `docker/README.md`
- [ ] Prove images build (`docker build` / compose) and `/healthz` responds with mounted dev config

## Surface parity (phase S) — sequential

1. C# **profiles** CRUD + connect  
2. C# **API keys**  
3. C# **approvals**  
4. C# **metrics** + security-posture  
5. C# **events/stream** + **events/watch**  
6. C# session **PATCH** + bulk DELETE + **POST /api/connect**  
7. Optional: SPA static hosting on Go/C# (fewer page.routes)  
8. Optional: dual-OS cover floor raise with ≥0.2pt headroom  

Each step: real tests + csharp-quality green + HANDOFF table update. No empty multi-backend passes.

## Permanent non-goals

| Item | Reason |
|------|--------|
| C# MCP | Operator de-scope |
| FastAPI `mount_terminal_ui` on Go/C# | Embed helper; use `uterm proxy` |
| Cloudflare full port to Go/C# | Separate product |

## One-liner

> Ship Docker per language server; close remaining C# host REST/UI gaps vs Go/Python. **No C# MCP.**
