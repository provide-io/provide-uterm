# Tunnel & Terminal Sharing Design

## Context

provide-terminal currently operates as a server-side terminal proxy: the FastAPI server (or CF Worker) hosts sessions where connectors (telnet/ssh/shell) connect to remote hosts, and browsers observe/control via WebSocket. The connection always originates server-side.

This design adds the reverse direction: a **local CLI agent** connects outbound to the CF edge, sharing a local terminal session (or later, any TCP/HTTP service) with remote viewers. This enables tmate-style terminal sharing and ngrok-style tunneling through the same infrastructure.

**Goals:**
- Share a local terminal session via a URL (tmate-like)
- Protocol-agnostic tunnel primitive that supports terminal now, HTTP/TCP later
- Layered auth: secret URL for quick sharing, CF Access for elevated roles
- Reuse existing browser multiplexing, hijack, recording, and xterm.js UI

## Tunnel Primitive

A **tunnel** is a bidirectional byte stream between a local agent and the CF edge, multiplexed over a single WebSocket connection. Everything above (terminal rendering, HTTP inspection, TCP forwarding) is a **tunnel type** that interprets those bytes.

### Wire Format

Binary WebSocket frames with a minimal multiplexing header:

```
[1 byte: channel_id] [1 byte: flags] [N bytes: payload]
```

**Channels:**
- `0x00` — control channel (JSON messages: open/close channel, metadata, errors, hijack commands)
- `0x01` — primary data stream (raw bytes, e.g., PTY I/O)
- `0x02+` — additional streams (stderr, secondary ports — future)

**Flags:**
- `0x00` — data
- `0x01` — EOF (half-close this direction)

The tunnel layer is protocol-agnostic. It moves bytes by channel ID. Terminal framing, hijack semantics, and recording are layers above.

### Tunnel Types

Declared at connection time via a control channel message:

| Type | Channel 1 carries | CF-side handler |
|------|-------------------|-----------------|
| `terminal` | Raw PTY I/O bytes | Feeds to xterm state, browser multiplexing, hijack, recording |
| `tcp` (future) | Raw TCP bytes | Relays to/from browser or external connection |
| `http` (future) | HTTP request/response bytes | Reverse proxy with inspection UI |

### Control Channel Messages (channel 0)

```json
// Agent → CF: open a data channel
{"type": "open", "channel": 1, "tunnel_type": "terminal", "term_size": [80, 24]}

// Agent → CF: terminal resize
{"type": "resize", "channel": 1, "cols": 120, "rows": 40}

// CF → Agent: input from browser
{"type": "input", "channel": 1, "data": "ls\n"}

// CF → Agent: hijack control
{"type": "hijack", "action": "pause"|"resume"|"step", "owner": "..."}

// Either direction: close channel
{"type": "close", "channel": 1}

// Either direction: error
{"type": "error", "message": "...", "channel": 1}
```

## Local CLI Agent

Python CLI in the existing `provide-terminal` package. Two modes:

### `provide share <cmd>` (new session)

1. Spawns `<cmd>` (default: `$SHELL`) in a new PTY via `pty.openpty()`
2. `POST /api/tunnels` with `{tunnel_type: "terminal", display_name: "user@host"}` → returns `tunnel_id`, `share_url`, `control_url`, `ws_endpoint`, `worker_token`
3. Opens WSS to `/tunnel/{tunnel_id}` with `Bearer: worker_token`
4. Sends control: `{"type": "open", "channel": 1, "tunnel_type": "terminal", "term_size": [cols, rows]}`
5. Event loop:
   - PTY stdout → channel 1 binary frames → CF
   - Channel 1 input frames from CF → PTY stdin
   - SIGWINCH → control resize frame
6. On exit (Ctrl+C or PTY close): sends EOF on channel 1, closes WebSocket

### `provide share` (attach current TTY)

1. Puts current TTY in raw mode
2. Same tunnel connection as above
3. Interposes as proxy: local keystrokes → real shell AND channel 1; shell output → local terminal AND channel 1
4. On disconnect: restores TTY settings, local session continues normally

### CLI Output

```
Sharing terminal session...
  View:    https://your-worker.dev/s/abc123
  Control: https://your-worker.dev/s/abc123?token=secret456

Connected. Press Ctrl+C to stop sharing.
```

### Package Location

| File | Purpose |
|------|---------|
| `src/provide/terminal/tunnel/protocol.py` | Binary frame encode/decode |
| `src/provide/terminal/tunnel/client.py` | Async WebSocket tunnel client |
| `src/provide/terminal/tunnel/pty_capture.py` | PTY spawn + attach-to-TTY |
| `src/provide/terminal/tunnel/fastapi_routes.py` | FastAPI `/tunnel/{id}` WebSocket route |
| `src/provide/terminal/cli/share.py` | `uterm share` CLI entry point |

## CF Side: Extended SessionRuntime

The existing `SessionRuntime` DO gains a **tunnel mode** alongside the existing worker mode. When a tunnel-protocol WebSocket connects, the DO:

1. Detects binary framing (tunnel) vs text framing (legacy worker)
2. Demuxes channel 0 (control) and channel 1+ (data)
3. For `terminal` tunnel type:
   - Channel 1 bytes → broadcast to connected browsers (same as existing worker output path)
   - Browser input → channel 0 `input` message → agent
   - Hijack commands → channel 0 `hijack` messages → agent
   - Recording: logs channel 1 bytes to session recording

**No new Durable Object class.** SessionRuntime handles both legacy workers and tunnel agents.

### New CF Routes

| Route | Handler | Auth |
|-------|---------|------|
| `POST /api/tunnels` | Create tunnel session, generate tokens | JWT or API key |
| `WSS /tunnel/{tunnel_id}` | Tunnel WebSocket endpoint | Bearer worker_token |
| `GET /s/{tunnel_id}` | Public viewer (share token required) | Share token |
| `GET /s/{tunnel_id}?token={control}` | Public operator | Control token |

### CF File Changes

| File | Change |
|------|--------|
| `entry.py` | Add `/s/` route, `/tunnel/` pattern, `/api/tunnels` route |
| `api/_tunnel_api.py` (new) | `POST /api/tunnels` + `GET /s/{id}` handlers |
| `api/tunnel_routes.py` (new) | Binary frame handler (`handle_tunnel_message`) |
| `do/session_runtime.py` | Detect binary frames in `webSocketMessage`, route to tunnel handler; `/tunnel/` in worker ID extraction and socket role detection |

## Auth Model

### Layered Authentication

**Layer 1 — Secret URLs (no account needed):**

Three tokens generated at tunnel creation, stored in KV session metadata:

| Token | Purpose | Grants |
|-------|---------|--------|
| `worker_token` | Agent WSS auth (Bearer header) | Tunnel connection |
| `share_token` | Embedded in view URL | `viewer` role |
| `control_token` | Embedded in control URL | `operator` role |

- `/s/{tunnel_id}` validates `share_token` from query param or cookie
- `/s/{tunnel_id}?token={control_token}` grants operator role
- Tokens are 32-byte URL-safe random strings

**Layer 2 — CF Access (elevated access):**

- Valid CF Access JWT overrides share token role → role from JWT claims
- Admins see shared sessions in dashboard alongside hosted sessions
- Share-token-only users see only their specific `/s/` URL

### Token Lifecycle

- Created at `POST /api/tunnels`
- Valid while agent is connected + configurable grace period (default: 5 min)
- Rotatable: agent sends control frame, CF regenerates and returns new URLs
- Revoked on agent disconnect (after grace period)

## What Stays the Same

These existing systems are reused without modification:

- **Browser WebSocket multiplexing** — DO handles N browsers watching one session
- **Hijack/control plane** — works via tunnel control channel instead of existing framing
- **Recording/replay** — logs tunnel bytes same as worker output
- **xterm.js frontend** — renders terminal output identically
- **KV session registry** — new fields (tokens, tunnel_type) but same structure
- **JWT auth pipeline** — CF Access integration unchanged

## Frontend Changes

Minimal changes to support share URLs:

- `state.ts` — parse share/control token from URL query params, pass on WS handshake
- New or adjusted route: `/s/{id}` serves same SPA with role from token validation
- No new UI components — shared sessions look like regular sessions to the viewer

## Phased Delivery

**Phase 1 (this spec):** Terminal sharing via tunnel primitive
- Tunnel protocol (binary frames, channel mux)
- Local CLI agent (`provide share`)
- CF tunnel endpoint + extended SessionRuntime
- Share URLs with layered auth

**Phase 2 (future):** TCP tunneling
- `provide tunnel <port>` CLI command
- `tcp` tunnel type handler in CF
- Subdomain-per-tunnel or port mapping

**Phase 3 (future):** HTTP inspection
- `http` tunnel type with request/response parsing
- Inspection UI in frontend (request list, headers, body)
- Modify-in-flight capability (MITM)

## Verification

### Implemented Tests (Phase 1)

| Test Suite | Count | What |
|-----------|-------|------|
| `tests/tunnel/test_protocol.py` | 31 | Frame encode/decode, roundtrip, error handling |
| `tests/tunnel/test_protocol_stress.py` | 30 | Hypothesis fuzzing, 100k throughput, concurrent |
| `tests/tunnel/test_client.py` | 27 | WebSocket client, reconnect, auth headers |
| `tests/tunnel/test_pty_capture.py` | 21 | PTY spawn, TTY proxy, SIGWINCH, cleanup |
| `tests/tunnel/test_pty_stress.py` | 12 | Rapid spawn/close, high-throughput, concurrent |
| `tests/tunnel/test_share_cli.py` | 26 | CLI arg parsing, bridge loop, error cases |
| `tests/tunnel/test_fastapi_routes.py` | 11 | FastAPI tunnel WS route, browser coexistence |
| CF `tests/test_tunnel_routes.py` | 28 | DO binary frame handler, tunnel API, share routes |
| **Total** | **186** | 100% statement coverage on tunnel package |

### Manual Test Flow
1. Run `uterm share bash --server https://your-worker.dev`
2. Open view URL in browser — see terminal output
3. Open control URL — type commands, see them execute
4. Test hijack: CF Access admin acquires exclusive control
5. Disconnect agent — verify cleanup
