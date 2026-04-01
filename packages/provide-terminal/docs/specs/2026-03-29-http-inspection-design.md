# HTTP Tunnel Inspection Design

## Context

The tunnel system supports terminal sharing (`uterm share`) and TCP port forwarding (`uterm tunnel`). This spec adds HTTP-aware tunneling with real-time traffic inspection — the ngrok-style "see every request flowing through your tunnel" feature.

## Architecture

### Channel Multiplexing

Extends the existing binary tunnel protocol with a new channel:

| Channel | Purpose | Format |
|---------|---------|--------|
| 0x00 | Control (existing) | JSON |
| 0x01 | Terminal log (existing) | Raw PTY bytes |
| 0x02 | Raw TCP (existing) | Raw bytes |
| 0x03 | HTTP stream (new) | Structured JSON per request/response |

### Agent-Side HTTP Proxy

`uterm inspect <port> --server URL` starts a local HTTP reverse proxy:

1. Listens on a local port (random or `--listen-port`)
2. Proxies all requests to `localhost:<port>` (the target service)
3. For each request/response pair, sends structured JSON on channel 0x03
4. Logs compact summary to terminal (channel 0x01) and stderr

### HTTP Message Format (Channel 0x03)

```json
{"type": "http_req", "id": "r1", "ts": 1711000000.0,
 "method": "POST", "url": "/api/login",
 "headers": {"content-type": "application/json"},
 "body_size": 42, "body_b64": "eyJ1c2VyIjoiYWRtaW4ifQ=="}

{"type": "http_res", "id": "r1", "ts": 1711000000.089,
 "status": 200, "status_text": "OK",
 "headers": {"content-type": "application/json"},
 "body_size": 18, "body_b64": "eyJ0b2tlbiI6ImFiYyJ9",
 "duration_ms": 89}
```

**Body rules:**
- Under 256KB: included inline as base64 in `body_b64`
- Over 256KB: `body_b64` omitted, `body_truncated: true`
- Binary content types: `body_b64` omitted, `body_binary: true`

### Server-Side Handling

Channel 0x03 messages are:
- Stored in an event buffer (same pattern as terminal snapshots)
- Broadcast to connected browsers as control frames
- Available via REST: `GET /api/sessions/{id}/http` returns recent requests

No new Durable Object or server-side HTTP parsing needed — the agent does all parsing.

## CLI: `uterm inspect`

**Command:** `uterm inspect <port> --server URL [--listen-port PORT]`

**What it does:**
1. `POST /api/tunnels` with `tunnel_type: "http"`
2. Start local HTTP proxy on `--listen-port` (default: random)
3. Connect tunnel WebSocket
4. For each proxied request:
   - Send `http_req` on channel 0x03
   - Forward request to `localhost:<port>`
   - Send `http_res` on channel 0x03 with timing
   - Log compact line to stderr

**CLI output (stderr):**
```
[inspect] Proxying localhost:3000 via tunnel
  View:    https://worker.dev/app/inspect/tunnel-abc
  Listen:  http://127.0.0.1:9123 → localhost:3000

← 200 GET /api/users (142ms, 3.2KB)
→ POST /api/login (1.1KB)
← 200 POST /api/login (89ms, 256B)
← 500 POST /api/crash (34ms) ⚠
```

Color: green 2xx, yellow 3xx/4xx, red 5xx.

**File:** `packages/provide-terminal/src/provide/terminal/cli/inspect.py`

## Browser UI: SPA Inspect View

**Route:** `/app/inspect/{session_id}`

**Layout:** Split pane:
- **Left:** Request list table (method, URL, status, duration, size). Scrollable, auto-follows new requests. Click to select.
- **Right:** Selected request detail tabs: Headers, Request Body, Response Body, Timing.
- **Top bar:** Filter controls (method dropdown, status range, URL search), pause/resume toggle.
- **Bottom:** Collapsible terminal panel showing agent log output (channel 0x01).

**Data flow:**
- Connects via existing `/ws/browser/{id}/term` WebSocket
- Channel 0x03 control frames arrive as `http_req`/`http_res` JSON
- Stored in local array, rendered reactively
- Request/response pairs matched by `id` field

**Files:**
- `packages/provide-terminal-frontend/src/app/views/inspect-view.ts`
- `packages/provide-terminal-frontend/src/app/views/inspect-view.css`
- Entry point registered in `boot.ts` for `page_kind: "inspect"`

## Browser UI: Standalone Embeddable Page

**Route:** `/inspect.html` (or served via `/assets/inspect-page.js`)

Same functionality as the SPA view but self-contained:
- Single HTML page with inline xterm.js + inspect UI
- Connectable via `<script>` tag with config object
- Useful for embedding in other tools/dashboards

**File:** `packages/provide-terminal-frontend/src/inspect-page.ts`

## Server-Side Changes

### FastAPI

- `tunnel/fastapi_routes.py`: detect channel 0x03 frames, store in HTTP event buffer, broadcast to browsers
- `server/routes/api.py`: add `GET /api/sessions/{id}/http` — returns recent HTTP request/response pairs
- New page route: `/app/inspect/{id}` → serves SPA shell with `page_kind: "inspect"`

### CF Worker

- `api/tunnel_routes.py`: detect channel 0x03, broadcast as control frames
- `entry.py`: add inspect SPA route (`/app/inspect/{id}`)
- No new DO logic needed — HTTP events flow through existing broadcast path

## Design for Future Intercept

The `http_req` message includes an `id` field. Future intercept mode:
1. Agent sends `http_req` with `intercept: true`
2. Agent holds the request (doesn't forward yet)
3. Browser sends back `http_forward` or `http_drop` on channel 0x03 with the `id`
4. Agent forwards (possibly modified) or drops

This requires no architectural changes — just new message types on the same channel.

## Phased Delivery

**Phase 1 (this spec):** Agent HTTP proxy + CLI output + channel 0x03 protocol
**Phase 2:** SPA inspect view (request list + detail)
**Phase 3:** Standalone embeddable page
**Phase 4 (future):** Intercept/modify mode

## Verification

1. `uterm inspect 3000 --server URL` starts proxy, logs traffic to stderr
2. Browser at `/app/inspect/{id}` shows request list in real time
3. Request detail shows headers and body
4. Filters work (method, status, URL)
5. Channel multiplexing: terminal log (ch1) + HTTP stream (ch3) coexist
6. Body truncation at 256KB
7. Binary content detected and flagged
