# provide-terminal-cloudflare

Cloudflare Workers companion package for [`provide-terminal`](../../README.md). Runs the provide-terminal control plane on Cloudflare Workers using Durable Objects, with a fleet-wide session registry backed by Workers KV.

## What it does

Each terminal session gets its own Durable Object (`SessionRuntime`). The DO arbitrates WebSocket traffic between the runtime worker connector and browser clients, stores hijack leases and snapshots in SQLite, and publishes events to all connected browsers. A fleet-wide session list is maintained in Workers KV.

## Installation

```bash
pip install provide-terminal-cloudflare
```

Or install from the monorepo with `uv`:

```bash
uv pip install -e packages/provide-terminal-cloudflare
```

Deploy with `pywrangler` (wraps `wrangler` for Python Workers):

```bash
uv run pywrangler deploy
```

## Key features

- **Durable Object per session** — `SessionRuntime` DO holds all session state (leases, snapshots, event sequence) in SQLite.
- **Fleet-wide session registry** — `SESSION_REGISTRY` Workers KV namespace; `GET /api/sessions` returns all active sessions across the fleet.
- **CF Access JWT auth** — validates Cloudflare Access JWTs via JWKS; `JWT_DEFAULT_ROLE` env var assigns a role when the JWT carries no role claim.
- **Hijack REST API** — `POST /hijack/{id}/acquire`, `POST /hijack/{id}/send`, `POST /hijack/{id}/release`, `GET /hijack/{id}/snapshot`.
- **WebSocket proxy** — three WS endpoints per session:
  - `/ws/worker/{worker_id}/term` — runtime worker protocol (JSON frames)
  - `/ws/browser/{worker_id}/term` — browser/operator protocol (JSON frames)
  - `/ws/raw/{worker_id}/term` — raw stream mode for `uterm listen` telnet/SSH gateways
- **Hibernation-safe** — uses CF WebSocket Hibernation API; state survives DO sleep/wake cycles.
- **Quick-connect** — not currently exposed as a dedicated Cloudflare page or `POST /api/connect` flow in this package.

## Auth modes

Set `AUTH_MODE` in `wrangler.toml` or `.dev.vars`:

| Mode | Behavior |
|---|---|
| `dev` | No auth checks; all requests accepted. |
| `jwt` | Validates CF Access JWT; role from claim or `JWT_DEFAULT_ROLE`. |

## Current gaps

- There is no Cloudflare-hosted quick-connect page yet. The package serves the
  dashboard and hijack surfaces, but not the FastAPI-style `/app/connect` flow.
- The hijack REST surface is intended to match the FastAPI contract, but there
  are still backend-parity gaps; treat `docs/protocol-matrix.md` as the target
  contract, not a guarantee that every edge case is identical today.

## Commands

```bash
uv run pywrangler dev        # local dev server (sync deps + wrangler dev)
uv run pywrangler deploy     # deploy to Cloudflare
uterm-cf build               # build only
uterm-cf deploy --env production
```

### Docker alternative

```bash
# Build and run from repo root
docker build -f docker/Dockerfile.cf -t provide-terminal-cf .
docker run --rm -p 27788:27788 provide-terminal-cf

# JWT auth test
docker run --rm -p 27788:27788 \
  -e AUTH_MODE=jwt \
  -e JWT_JWKS_URL=https://<team>.cloudflareaccess.com/cdn-cgi/access/certs \
  -e JWT_ISSUER=https://<team>.cloudflareaccess.com \
  -e JWT_AUDIENCE=<aud-tag> \
  provide-terminal-cf
```

## Tests

Unit tests (no network required):

```bash
uv run pytest tests/ -v
```

E2E tests against a local `wrangler dev` instance or the live worker:

```bash
E2E=1 uv run pytest -m e2e -v
REAL_CF=1 REAL_CF_URL=https://provide-terminal-cloudflare.neurotic.workers.dev uv run pytest -m e2e -v
```

## Related

- Main package: [`provide-terminal`](../../README.md)
- Terraform for KV provisioning: `terraform/`
