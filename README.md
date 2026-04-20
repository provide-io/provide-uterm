# provide-terminal

Shared terminal I/O primitives and WebSocket proxy infrastructure for the provide ecosystem.

**Highlights:** WebSocket ↔ telnet/SSH proxy · hijack/observe control plane · browser role system (viewer/operator/admin) · open/shared input mode · WS session resumption (role + hijack survive reconnect) · quick-connect ephemeral sessions (`GET /app/connect`, `POST /api/connect`) · `ShellSessionConnector` for in-process shell sessions · JWT auth · 2000+ tests at 100% branch coverage

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

```bash
pip install 'provide-terminal-server[server]'
uterm-server --config server.toml
# Dashboard: http://localhost:27780/app/
```

The browser connects to `ws://yourhost/ws/terminal`; the proxy opens a raw TCP
connection to the BBS for each session.

```bash
pip install 'provide-terminal-server[cli]'
uterm inspect 3000 --server https://your-server.example.com --intercept
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

| Connector | What it does |
|-----------|-------------|
| `shell` | Local shell process |
| `telnet` | Remote telnet (RFC 854) |
| `ssh` | Remote SSH (asyncssh) |
| `websocket` | WebSocket upstream |
| `ushell` | Built-in Python REPL (shell module in `provide-terminal`) |
| `pty` | Local PTY with PAM auth and LD_PRELOAD capture |

WebSocket session resumption is opt-in on raw `TermHub` instances. Resume tokens
are opaque session handles that restore the prior browser role unless the
consumer supplies stricter validation via `on_resume`.

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

See [HTTP Inspection & Interception](https://github.com/provide-io/provide-terminal/blob/main/docs/inspect.md) for the full protocol reference.

### Collaborative Presence (DeckMux)

Real-time collaborative features on any terminal session:

- **Avatar bar** — colored circles with initials, role badges, idle/typing indicators
- **Edge indicators** — minimap-style viewport bars showing where each user is scrolled
- **Pinned cursors** — click a line to pin your position, visible to all watchers
- **Control transfer** — request/handover/auto-transfer with keystroke queue buffering

Enable per-session with `presence: true`. Works on both FastAPI and CF backends at parity.

### AI/MCP Integration

21 tools for AI agents to control terminal sessions via the [Model Context Protocol](https://modelcontextprotocol.io/):

```bash
uv run python scripts/example_server.py
```

Tools include `session_create`, `session_read`, `session_subscribe`, `hijack_begin`, `hijack_send`, `hijack_step`, `hijack_release`, and more. See [provide-terminal-client](https://github.com/provide-io/provide-terminal/tree/main/packages/provide-terminal-client).

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

Process lifecycle, heartbeat monitoring, auto-respawn, fleet pause/resume, timeseries metrics, and WebSocket status broadcasting. See [provide-terminal-platform](https://github.com/provide-io/provide-terminal/tree/main/packages/provide-terminal-platform).

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

**Cloudflare Workers** — edge deployment on [Durable Objects](https://github.com/provide-io/provide-terminal/tree/main/packages/provide-terminal-cloudflare) with CF Access JWT, KV session registry, WebSocket hibernation.

```bash
docker compose -f docker/docker-compose.yml up
```

---

## Package Ecosystem

| Package | Role | Tests |
|---------|------|-------|
| `provide-terminal` | Core: ansi, screen, emulator, protocols, detection, deckmux, shell, render, replay | ~3600 |
| `provide-terminal-server` | Server: bridge hub, FastAPI, CLI, tunnel, gateway | ~2800 |
| `provide-terminal-client` | Client: HTTP/WS client, transports, AI/MCP | ~690 |
| `provide-terminal-platform` | Platform: PTY, PAM, capture, fleet manager | ~780 |
| `provide-terminal-cloudflare` | CF Worker + Durable Object | ~890 |
| `provide-terminal-frontend` | Browser UI (TypeScript, xterm.js) | — |
| `provide-terminal-app` | App shell | — |

All packages at 100% branch+line coverage. 8760+ tests total.

---

## Quality Guarantees

- Test gate runs at **100% branch coverage** (`--cov-branch`), enforced via `addopts` in `pyproject.toml`.
- Memory regressions caught in **nightly CI** via memray profiling (stress tests for hot paths).
- Pre-commit hooks enforce ruff, mypy strict, ty, bandit, and biome on every commit.
- Security audit via `pip-audit` and `bandit`; timing-safe token comparison in auth paths.
- All input size limits enforced at boundaries; fail-closed auth on misconfiguration.

## Documentation Ownership

- README: installation, quick-start, and API overview.
- Operations: runbook, SLOs, and production readiness gates.
- Protocol: backend capability matrix and client contract.
- Release: governance, tagging, and publishing workflow.

## Docs

- [HTTP Inspection & Interception](https://github.com/provide-io/provide-terminal/blob/main/docs/inspect.md)
- [Protocol Matrix](https://github.com/provide-io/provide-terminal/blob/main/docs/protocol-matrix.md) — backend capability contract
- [Testing Guide](https://github.com/provide-io/provide-terminal/blob/main/docs/TESTING.md)
- [Operations Runbook](https://github.com/provide-io/provide-terminal/blob/main/docs/operations/runbook.md)
- [Service SLOs](https://github.com/provide-io/provide-terminal/blob/main/docs/operations/slo.md)
- [Production Readiness Gates](https://github.com/provide-io/provide-terminal/blob/main/docs/production-readiness-pass2.md)
- [Release Governance](https://github.com/provide-io/provide-terminal/blob/main/docs/release-governance.md)
- [Architecture Diagrams](https://github.com/provide-io/provide-terminal/tree/main/docs/diagrams) (PlantUML)
- [Cloudflare Workers](https://github.com/provide-io/provide-terminal/blob/main/packages/provide-terminal-cloudflare/README.md)

---

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 MindTenet LLC.
