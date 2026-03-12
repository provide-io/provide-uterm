# provide-terminal

Shared terminal I/O primitives and WebSocket proxy infrastructure for the provide ecosystem.

**Highlights:** WebSocket ↔ telnet/SSH proxy · hijack/observe control plane · browser role system (viewer/operator/admin) · open/shared input mode · quick-connect ephemeral sessions (`GET /connect`) · `ShellSessionConnector` for in-process shell sessions · JWT auth · 1225+ tests at 100% branch coverage

For Cloudflare Workers deployment, see [`provide-terminal-cloudflare`](packages/provide-terminal-cloudflare/README.md) — a companion package that runs the control plane on Durable Objects with CF Access JWT support.

## Installation

```bash
pip install provide-terminal
```

### Extras

| Extra | Installs | Required for |
|---|---|---|
| `[websocket]` | `fastapi`, `websockets` | `WsTerminalProxy`, `create_ws_terminal_router`, hijack hub |
| `[emulator]` | `pyte` | `TerminalEmulator` (screen state tracking) |
| `[ssh]` | `asyncssh` | SSH transport, `uterm proxy --transport ssh` |
| `[server]` | `fastapi`, `uvicorn`, `pyjwt` | `uterm-server` hosted reference server |
| `[cli]` | `fastapi`, `uvicorn`, `websockets` | `uterm` command-line tool |
| `[all]` | everything above | Full feature set |

```bash
pip install 'provide-terminal[all]'
```

---

## Quick Start

### Serve the built-in terminal UI

Mount the bundled `terminal.html` + `terminal.js` frontend into any FastAPI app:

```python
from fastapi import FastAPI
from provide.terminal.fastapi import mount_terminal_ui

app = FastAPI()
mount_terminal_ui(app)           # serves ProvideTerminal at /terminal
mount_terminal_ui(app, path="/t")  # custom path
```

### Browser WebSocket → remote telnet proxy

Accept browser WebSocket connections and proxy them to a remote BBS:

```python
from provide.terminal.fastapi import WsTerminalProxy

proxy = WsTerminalProxy("bbs.example.com", 23)
app.include_router(proxy.create_router("/ws/terminal"))
```

The browser connects to `ws://yourhost/ws/terminal`; the proxy opens a raw TCP
connection to the BBS for each session.

### In-process session handler

Handle terminal sessions in your own async code:

```python
from provide.terminal.fastapi import create_ws_terminal_router

async def my_handler(reader, writer, ws):
    writer.write(b"Welcome!\r\n")
    await writer.drain()
    async for line in reader:
        writer.write(line)
        await writer.drain()

app.include_router(create_ws_terminal_router(my_handler))
```

---

## Hijack Widget

The hijack system lets a human operator observe and take over a worker's terminal
session in real time.

### Backend — TermHub

```python
from provide.terminal.hijack.hub import TermHub

def resolve_browser_role(ws, worker_id):
    user = getattr(ws.state, "user", None)
    if getattr(user, "is_admin", False):
        return "admin"
    if getattr(user, "can_operate_terminals", False):
        return "operator"
    return "viewer"

hub = TermHub(
    on_hijack_changed=lambda worker_id, enabled, owner: print(worker_id, enabled),
    resolve_browser_role=resolve_browser_role,
)
app.include_router(hub.create_router())
```

This adds:
- `GET  /ws/browser/{worker_id}/term` — browser observer/hijack WebSocket
- `GET  /ws/worker/{worker_id}/term` — worker WebSocket
- REST endpoints for session management

Browser roles are resolved on the server. The browser WebSocket does not accept
a client-selected role parameter; without a resolver, browser sessions default
to read-only (`viewer`).

If `resolve_browser_role` raises an exception, the browser WebSocket is rejected
and closed. Resolver failures do not fall back to `viewer`.

### Frontend — ProvideHijack

Embed the hijack control widget in any HTML page:

```html
<div id="hijack-container"></div>
<script src="/static/hijack.js"></script>
<script>
  new ProvideHijack(document.getElementById('hijack-container'), {
    workerId: 'myworker',     // connects to /ws/browser/myworker/term
    mobileKeys: true,         // show collapsible special-key toolbar when hijacked
    heartbeatInterval: 5000,  // ms between heartbeats while owner
  });
</script>
```

Mount the bundled frontend files via FastAPI's `StaticFiles` or use
`mount_terminal_ui()` which includes `hijack.html`, `hijack.js`, and `hijack.css`.

### Interactive Example Server

The repo also includes an interactive example server for manual testing:

```bash
uv run python scripts/example_server.py
```

Then open:

- `http://127.0.0.1:8742/hijack/hijack.html?worker=demo-session`

The built-in demo session is a general-purpose interactive worker rather than a
static screen. It supports:

- exclusive hijack mode (one browser owns input)
- shared input mode (multiple browsers can type)
- free-form text that appends to a live transcript
- built-in commands: `/help`, `/mode open`, `/mode hijack`, `/clear`, `/status`, `/nick <name>`, `/say <text>`, `/demo`, `/reset`

The demo page includes mode and reset controls backed by example-only HTTP
endpoints:

- `GET /demo/session/{worker_id}`
- `POST /demo/session/{worker_id}/mode`
- `POST /demo/session/{worker_id}/reset`

These demo endpoints exist only for the example server and are not part of the
library's public API.

### Reference Server

The repo now also includes a standalone reference server application:

```bash
uterm-server --config scripts/uterm-server.example.toml
```

This is the canonical hosted-app example for the library. It demonstrates:

- named sessions above `TermHub`
- browser session pages and operator pages
- server-side role resolution and policy
- hosted connectors (`shell`, `telnet`, `ssh`)
- session APIs, mode switching, and optional file-backed recording

Key endpoints:

- `GET /api/health`
- `GET /api/sessions`
- `GET /app/` (operator dashboard)
- `GET /app/session/{session_id}` (end-user page)
- `GET /app/operator/{session_id}` (operator console)
- `GET /connect` (quick-connect: create ephemeral sessions on demand)

The example TOML config in [`scripts/uterm-server.example.toml`](scripts/uterm-server.example.toml)
shows the intended reference-implementation structure for server config.
For production JWT deployments, start from
[`scripts/uterm-server.jwt.example.toml`](scripts/uterm-server.jwt.example.toml).

### Auth Runtime Posture

- `default_server_config()` is intentionally local-friendly and uses `auth.mode = "dev"`.
- Production should run `auth.mode = "jwt"` with:
  - `jwt_issuer`, `jwt_audience`
  - `jwt_public_key_pem` or `jwt_jwks_url`
  - `jwt_algorithms` (for example `["RS256"]`)
  - `worker_bearer_token` for hosted runtime worker WebSocket authentication

When `auth.mode = "jwt"`, the server fails fast at startup unless:
- `worker_bearer_token` is set
- `jwt_algorithms` is non-empty
- at least one key source is configured (`jwt_public_key_pem` or `jwt_jwks_url`)

### JWT Deployment Runbook

1. Configure JWT trust:
   - set `jwt_issuer`, `jwt_audience`, `jwt_algorithms`
   - prefer `jwt_jwks_url` for key rotation without restarts
2. Configure runtime worker auth:
   - mint a dedicated service JWT (admin role) for `worker_bearer_token`
   - scope/TTL this token to server runtime usage only
3. Set session ownership/visibility:
   - use `owner` + `visibility` to enforce role + ownership constraints
4. Validate startup:
   - `uterm-server --config scripts/uterm-server.jwt.example.toml`
   - run smoke tests against `/api/health`, `/api/sessions`, and browser WS connect

### Key Rotation

- `jwt_jwks_url` mode: rotate signing keys at the IdP, publish new JWKs, then retire old keys after token TTL.
- `jwt_public_key_pem` mode: deploy new config and restart server(s) in rolling fashion.
- Rotate `worker_bearer_token` independently from user-facing tokens; keep overlap window short.

### Failure Behavior

- Missing/invalid/expired JWT:
  - HTTP routes: `401`
  - WebSocket routes: close with policy violation (`1008`)
- Authenticated but unauthorized action:
  - HTTP routes: `403`
  - Browser WS hijack attempts: explicit error event; no privilege escalation
- Invalid JWT runtime config:
  - app startup raises `ValueError` (fail-fast)

For `connector_type = "ssh"`, the session entry can use these auth fields:

- `password` for password authentication
- `client_key_path` for a private key file path
- `client_key_data` for inline PEM private key text
- `client_key` for a single AsyncSSH-compatible key value
- `client_keys` for multiple keys
- `known_hosts` to override host-key verification behavior (`null` disables checks for local/dev use)

The SSH connector intentionally skips user SSH config discovery so startup stays
predictable and fast in the hosted server. If you need key-based auth in config
without a file path, prefer `client_key_data`.

### Frontend — ProvideTerminal

Standalone terminal widget (no hijack controls):

```html
<div id="term"></div>
<script src="/static/terminal.js"></script>
<script>
  new ProvideTerminal(document.getElementById('term'), {
    wsUrl: '/ws/terminal',
    theme: 'crt',             // 'crt' | 'bbs' | 'glass'
    heartbeatMs: 25000,       // keepalive ping interval (ms). 0 disables.
  });
</script>
```

---

## CLI

Install the `[cli]` extra, then:

### `uterm proxy` — browser WS → telnet/SSH

Accepts browser WebSocket connections and proxies to a remote BBS.

```bash
# Basic telnet proxy
uterm proxy bbs.example.com 23

# Custom port and WS path
uterm proxy bbs.example.com 23 --port 9000 --path /ws/term

# SSH proxy (requires [ssh] extra)
uterm proxy bbs.example.com 22 --transport ssh
```

### `uterm listen` — telnet/SSH client → WebSocket server

Accepts traditional telnet and/or SSH clients and proxies to a remote WebSocket
terminal endpoint.

```bash
# Telnet listener
uterm listen wss://warp.provide.io/ws/terminal

# With custom ports
uterm listen wss://warp.provide.io/ws/terminal --port 2112 --ssh-port 2222

# With host key (SSH)
uterm listen wss://warp.provide.io/ws/terminal --server-key /etc/host_key
```

---

## Docker

Pre-built Docker targets are provided for local testing of both backends.

### FastAPI reference server

```bash
# Build (from repo root)
docker build -f docker/Dockerfile.server -t provide-terminal-server .

# Run — dashboard at http://localhost:27780/app/
docker run --rm -p 27780:27780 provide-terminal-server

# Custom config
docker run --rm -p 27780:27780 \
  -v /path/to/my.toml:/config/server.toml:ro \
  provide-terminal-server
```

The default config (`docker/server.toml`) starts in `dev` auth mode with one pre-configured shell session. Mount a custom TOML to add JWT, real connectors, or additional sessions — see `scripts/uterm-server.jwt.example.toml` for a full JWT example.

### Cloudflare Worker (pywrangler dev)

```bash
# Build (requires Docker Buildx; Node 20 + Python 3.11 image)
docker build -f docker/Dockerfile.cf -t provide-terminal-cf .

# Run — worker at http://localhost:27788/api/health
docker run --rm -p 27788:27788 provide-terminal-cf
```

Runs `pywrangler dev` inside the container with `AUTH_MODE=dev`. Pass `-e AUTH_MODE=jwt -e JWT_JWKS_URL=...` etc. to test JWT auth. KV/DO state is local (SQLite in `/tmp`) — not written to Cloudflare.

### Both backends together

```bash
docker compose -f docker/docker-compose.yml up
```

FastAPI on `:27780`, CF worker on `:27788`.

---

## Quality Guarantees

- Test gate runs at **100% branch coverage** (`--cov-branch`), enforced via `addopts` in `pyproject.toml`.
- Pre-commit hooks enforce ruff, mypy strict, ty, bandit, and biome on every commit.
- Security audit via `pip-audit` and `bandit`; timing-safe token comparison in auth paths.
- All input size limits enforced at boundaries; fail-closed auth on misconfiguration.

## Documentation Ownership

- README: installation, quick-start, and API overview.
- Operations: runbook, SLOs, and production readiness gates.
- Protocol: backend capability matrix and client contract.
- Release: governance, tagging, and publishing workflow.

## Docs

- [Operations Runbook](https://github.com/provide-io/provide-terminal/blob/main/docs/operations/runbook.md)
- [Service SLOs](https://github.com/provide-io/provide-terminal/blob/main/docs/operations/slo.md)
- [Protocol Matrix](https://github.com/provide-io/provide-terminal/blob/main/docs/protocol-matrix.md)
- [Production Readiness Gates](https://github.com/provide-io/provide-terminal/blob/main/docs/production-readiness-pass2.md)
- [Release Governance](https://github.com/provide-io/provide-terminal/blob/main/docs/release-governance.md)
- [Cloudflare Companion Package](https://github.com/provide-io/provide-terminal/blob/main/packages/provide-terminal-cloudflare/README.md)

---

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 MindTenet LLC.
