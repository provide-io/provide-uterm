# provide-uterm-cloudflare

Cloudflare Workers companion package for [`provide-uterm`](../../README.md). Runs the provide-uterm control plane on Cloudflare Workers using Durable Objects, with a fleet-wide session registry backed by Workers KV.

## What it does

Each terminal session gets its own Durable Object (`SessionRuntime`). The DO arbitrates WebSocket traffic between the runtime worker connector and browser clients, stores hijack leases and snapshots in SQLite, and publishes events to all connected browsers. A fleet-wide session list is maintained in Workers KV.

## Installation

```bash
pip install provide-uterm-cloudflare
```

Or install from the monorepo with `uv`:

```bash
uv pip install -e packages/provide-uterm-cloudflare
```

### Deploy

The worker entrypoint is `src/worker_entry.py` (referenced by `main` in
`wrangler.toml`). It lives at the package `src/` root on purpose: wrangler bundles
the directory of the `main` file, so anchoring it there preserves the full
`provide/uterm/cloudflare/` tree in the bundle and the worker's qualified imports
resolve as-is.

The Pyodide runtime also needs a flat vendor tree of the pure-Python deps the
worker imports (`structlog`, `provide.telemetry`, and the `provide.uterm.*`
modules). `pywrangler sync` produces a layout the worker can't import, so build it
with the helper script, then deploy with `wrangler` directly (not `pywrangler
deploy`, which would re-sync and overwrite the tree):

```bash
bash .ci/vendor_cf_worker.sh                          # build python_modules/
cd packages/provide-uterm-cloudflare
CLOUDFLARE_API_TOKEN=… npx wrangler deploy            # publish

# Required secrets (AUTH_MODE is jwt-only; the worker 500s without these):
npx wrangler secret put WORKER_BEARER_TOKEN           # >=32 high-entropy chars
npx wrangler secret put WEBHOOK_SECRET_KEY            # base64 AES-256 key
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
- **WS session resumption** — browser reconnects reclaim their role and hijack ownership via one-time tokens stored in DO SQLite; see `docs/cf-do-architecture.md`.
- **Quick-connect** — `POST /api/connect` creates sessions in KV; SPA serves the connect form at `/app/connect`. Supports shell, websocket, and ushell connector types.

## Auth modes

Set `AUTH_MODE` in `wrangler.toml` or `.dev.vars`. `jwt` is the **only**
supported value — the worker is always internet-facing, so any other mode
(`dev`/`none` are removed) raises a `ValueError` at config load.

| Mode | Behavior |
|---|---|
| `jwt` | Validates CF Access JWT; role from claim or `JWT_DEFAULT_ROLE`. CF Access service-token JWTs (with a `common_name` claim and no human `email` claim) are accepted, but are only granted the admin role when `JWT_SERVICE_TOKEN_ADMIN=1` is set (defaults off); otherwise they get their roles from the normal claim/scope/default-role path. |

`WORKER_BEARER_TOKEN` is also required and must clear a 32-character /
non-placeholder entropy floor.

## Current gaps

- The quick-connect form creates sessions in KV but the CF worker cannot run
  shell/SSH/telnet connectors itself — a worker process must bridge in via WS.
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
docker build -f docker/Dockerfile.cf -t provide-uterm-cf .
docker run --rm -p 27788:27788 provide-uterm-cf

# JWT auth test
docker run --rm -p 27788:27788 \
  -e AUTH_MODE=jwt \
  -e JWT_JWKS_URL=https://<team>.cloudflareaccess.com/cdn-cgi/access/certs \
  -e JWT_ISSUER=https://<team>.cloudflareaccess.com \
  -e JWT_AUDIENCE=<aud-tag> \
  provide-uterm-cf
```

## Tests

Unit tests (no network required):

```bash
uv run pytest tests/ -v
```

E2E tests against a local `wrangler dev` instance or the live worker:

```bash
E2E=1 uv run pytest -m e2e -v
REAL_CF=1 REAL_CF_URL=https://provide-uterm-cloudflare.neurotic.workers.dev uv run pytest -m e2e -v
```

## Related

- Main package: [`provide-uterm`](../../README.md)
- Terraform for KV provisioning: `terraform/`
