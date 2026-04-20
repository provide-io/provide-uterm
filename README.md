# provide-terminal

A terminal access and control platform. Creates, transports, secures, shares, records, replays, and arbitrates terminal sessions across browsers, WebSockets, telnet, SSH, local PTYs, and remote workers.

> xterm.js is the screen. Provide Terminal is the whole system around the screen.

```
Terminal UI         Session Control       Collaborative Presence
HTTP Inspection     AI/MCP Tools          Tunnel Sharing
Session Replay      Multi-Backend         Agent Management
```

---

## Architecture

```mermaid
graph TB
    subgraph Browser
        UI["Terminal UI<br/>Operator Dashboard<br/>Inspect View"]
    end

    subgraph Server ["Server (FastAPI or CF Worker)"]
        Hub["TermHub Bridge<br/><i>roles, leases, presence</i>"]
        Runtime["Session Host<br/><i>lifecycle, recording, policy</i>"]
        Connectors["Connectors<br/><i>shell, telnet, ssh, pty, ushell</i>"]
    end

    subgraph Workers
        Agent["Worker / Agent"]
        AI["AI Tools (MCP)<br/><i>21 session control tools</i>"]
    end

    subgraph CLI
        Proxy["uterm proxy / listen"]
        Inspect["uterm inspect"]
        Share["uterm share / tunnel"]
    end

    UI <-->|"WebSocket<br/>(control + terminal)"| Hub
    Hub <--> Runtime
    Runtime <--> Connectors
    Agent <-->|"Bridge WS"| Hub
    AI -.->|"tool calls"| Agent
    Inspect <-->|"CHANNEL_HTTP"| Hub
    Share <-->|"binary tunnel"| Hub
    Proxy <-->|"gateway"| Hub
```

**Control channel** — JSON control frames (snapshots, hijack state, presence, analysis) are mixed inline with raw terminal bytes in the same WebSocket stream. This makes the system a session orchestration platform, not just a proxy.

**Session model** — Named sessions with pluggable connectors, lifecycle management, JSONL recording, and policy enforcement.

**Bridge** — TermHub coordinates workers and browsers, enforces viewer/operator/admin roles, manages hijack ownership leases, and supports reconnect/resume tokens.

---

## Quick Start

### Embed a terminal in FastAPI

```python
from provide.terminal.fastapi import mount_terminal_ui
app = FastAPI()
mount_terminal_ui(app)  # serves at /terminal
```

### Run the reference server

```bash
pip install 'provide-terminal-server[server]'
uterm-server --config server.toml
# Dashboard: http://localhost:27780/app/
```

### Inspect HTTP traffic with interception

```bash
pip install 'provide-terminal-server[cli]'
uterm inspect 3000 --server https://your-server.example.com --intercept
```

---

## Core Capabilities

### Session Control (Bridge)

The bridge system lets operators observe and take over terminal sessions in real time.

- **Roles** — `viewer` (observe only), `operator` (input in shared mode), `admin` (full hijack control)
- **Hijack leases** — acquire/heartbeat/release with configurable TTL, auto-expire on disconnect
- **Input modes** — `hijack` (exclusive, one owner) or `open` (shared, all operators can type)
- **Session resumption** — browser reconnect restores role and hijack ownership via opaque tokens

```mermaid
sequenceDiagram
    participant B as Browser (admin)
    participant H as TermHub
    participant W as Worker

    B->>H: POST /hijack/acquire
    H->>W: control: pause
    H-->>B: {ok: true, hijack_id}
    B->>H: POST /hijack/send {keys: "ls\r"}
    H->>W: input: "ls\r"
    W-->>H: terminal output
    H-->>B: terminal output
    B->>H: POST /hijack/release
    H->>W: control: resume
```

### Terminal Transports

Pluggable connectors behind a unified session model:

| Connector | What it does |
|-----------|-------------|
| `shell` | Local shell process |
| `telnet` | Remote telnet (RFC 854) |
| `ssh` | Remote SSH (asyncssh) |
| `websocket` | WebSocket upstream |
| `ushell` | Built-in Python REPL (shell module in `provide-terminal`) |
| `pty` | Local PTY with PAM auth and LD_PRELOAD capture |

The **gateway** converts between protocols: browser WebSocket ↔ telnet/SSH backends with ANSI color mode negotiation.

### Tunnel Sharing & HTTP Inspection

Share terminals and inspect HTTP traffic through multiplexed binary tunnels.

```mermaid
graph LR
    subgraph "Tunnel Protocol (one WebSocket)"
        C0["0x00 Control"]
        C1["0x01 Terminal"]
        C2["0x02 TCP"]
        C3["0x03 HTTP"]
    end

    CLI["uterm share<br/>uterm inspect"] --> C0 & C1 & C2 & C3
    C0 & C1 & C2 & C3 --> Server["TermHub"]
    Server --> Browser["Browser UI"]
```

- **`uterm share`** — share your local terminal through the tunnel server
- **`uterm tunnel`** — forward a local TCP port through the tunnel
- **`uterm inspect`** — HTTP reverse proxy with live traffic inspection
- **`uterm inspect --intercept`** — pause requests, forward/drop/modify from the browser

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
uterm-mcp  # starts MCP server for Claude, GPT, or any MCP-compatible agent
```

Tools include `session_create`, `session_read`, `session_subscribe`, `hijack_begin`, `hijack_send`, `hijack_step`, `hijack_release`, and more. See [provide-terminal-client](https://github.com/provide-io/provide-terminal/tree/main/packages/provide-terminal-client).

### Agent Management

Orchestrate fleets of terminal workers:

```bash
uterm-manager --config swarm.yaml
```

Process lifecycle, heartbeat monitoring, auto-respawn, fleet pause/resume, timeseries metrics, and WebSocket status broadcasting. See [provide-terminal-platform](https://github.com/provide-io/provide-terminal/tree/main/packages/provide-terminal-platform).

---

## CLI Tools

| Entry Point | Purpose |
|-------------|---------|
| `uterm` | Terminal proxy, sharing, tunneling, inspection |
| `uterm-server` | Hosted reference server with sessions, auth, UI |
| `uterm-mcp` | MCP server for AI agents |
| `uterm-manager` | Agent swarm orchestration |

### `uterm` commands

| Command | Description |
|---------|-------------|
| `proxy HOST PORT` | Browser WS → telnet/SSH proxy |
| `listen WS_URL` | Telnet/SSH client → WebSocket |
| `share [CMD]` | Share local terminal via tunnel |
| `tunnel PORT` | Forward TCP port via tunnel |
| `inspect PORT` | HTTP traffic inspection (add `--intercept` for pause/edit) |
| `watch` | TUI for watching HTTP tunnel traffic |

---

## Installation

```bash
pip install provide-terminal                  # core only
pip install 'provide-terminal[emulator]'      # + pyte screen emulation
pip install 'provide-terminal-server[cli]'    # CLI tools (uterm, uterm-server)
pip install 'provide-terminal-server[server]' # hosted server
pip install 'provide-terminal-client[all]'    # client + MCP tools
```

**provide-terminal extras:**

| Extra | Installs | Required for |
|-------|----------|-------------|
| `[emulator]` | pyte | Screen state tracking |
| `[ssh]` | asyncssh | SSH transport |
| `[client]` | httpx | HTTP client |
| `[all]` | everything above | Full core feature set |

**provide-terminal-server extras:**

| Extra | Installs | Required for |
|-------|----------|-------------|
| `[server]` | fastapi, uvicorn, pyjwt, websockets | Reference server |
| `[cli]` | fastapi, uvicorn, websockets, textual, httpx | CLI tools |
| `[tunnel]` | httpx, uvicorn, websockets, fastapi | Tunnel sharing |
| `[gateway]` | asyncssh, websockets | Telnet/SSH gateways |
| `[all]` | everything above | Full server feature set |

---

## Deployment

```mermaid
graph LR
    subgraph "Self-Hosted"
        FA["FastAPI Server<br/><code>uterm-server</code>"]
    end

    subgraph "Edge"
        CF["Cloudflare Workers<br/>Durable Objects"]
    end

    subgraph "Local"
        Docker["Docker Compose<br/>both backends"]
    end

    Browser["Browser"] --> FA & CF
    FA --- Docker
    CF --- Docker
```

**FastAPI** — full control, named sessions, auth, recording, policy. Deploy anywhere Python runs.

**Cloudflare Workers** — edge deployment on [Durable Objects](https://github.com/provide-io/provide-terminal/tree/main/packages/provide-terminal-cloudflare) with CF Access JWT, KV session registry, WebSocket hibernation.

**Docker** — both backends locally:
```bash
docker compose -f docker/docker-compose.yml up
# FastAPI: http://localhost:27780/app/
# CF Worker: http://localhost:27788/api/health
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

## Security & Quality

- **Auth modes** — `dev` (local), `jwt` (production), fail-closed on misconfiguration
- **Security headers** — CSP, HSTS, X-Frame-Options, SRI integrity hashes (configurable per-header)
- **100% branch coverage** — enforced via `--cov-fail-under=100` in every package
- **Pre-commit** — ruff, mypy strict, ty, bandit, biome (TS/JS) on every commit
- **Security audit** — `pip-audit`, `bandit`, timing-safe token comparison

---

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

AGPL-3.0-or-later. Copyright (c) 2025-2026 provide.io llc.
