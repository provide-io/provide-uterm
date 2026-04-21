# HTTP Inspection & Interception

## Overview

`uterm inspect` is an HTTP reverse proxy that forwards traffic to a local port while capturing structured request/response data for live viewing in the browser. With `--intercept`, requests pause before forwarding, letting you forward, drop, or modify them.

**Use cases:**
- API debugging — see exact payloads, headers, timing
- Security testing — intercept and tamper with auth flows
- AI agent supervision — pause and review agent API calls before they hit production

## Quick Start

```bash
# Observe HTTP traffic on port 3000
uterm inspect 3000 --server https://your-server.example.com

# With interception enabled
uterm inspect 3000 --server https://your-server.example.com --intercept

# Custom timeout and drop on timeout
uterm inspect 8080 --server URL --intercept --intercept-timeout 60 --intercept-timeout-action drop
```

The proxy binds to a local port (auto-assigned by default, or `--listen-port PORT`). Send your HTTP traffic to the proxy port instead of directly to the target.

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `PORT` | (required) | Target HTTP port to inspect |
| `--server URL` | (required) | Tunnel server URL |
| `--listen-port PORT` | 0 (auto) | Local proxy listen port |
| `--intercept` | off | Pause requests for browser action |
| `--intercept-timeout SEC` | 30 | Seconds to wait for browser action |
| `--intercept-timeout-action` | forward | Action on timeout: `forward` or `drop` |
| `--token TOKEN` | — | Bearer token for API auth |
| `--display-name NAME` | http:PORT | Override session display name |

## Browser UI

Open the inspect page in your browser (the share URL is printed at startup). The UI shows:

- **Request list** — method, URL, status, duration, size for each request
- **Detail panel** — click a request to see full headers and decoded body
- **Inspect toggle** — ON/OFF. When OFF, proxy forwards silently (no frames sent to browser)
- **Intercept toggle** — ON/OFF. When ON, requests pause with a PAUSED badge

### Intercept Workflow

1. Enable intercept (click toggle or start with `--intercept`)
2. Send HTTP requests to the proxy port
3. Requests appear in the list with **PAUSED** badge
4. Click a paused request to see details
5. Choose an action:
   - **Forward** — send request to target unchanged
   - **Drop** — block the request, client gets 502
   - **Modify & Forward** — edit headers/body, then send modified request

## Protocol Reference

All messages use CHANNEL_HTTP (0x03) in the tunnel binary protocol.

### Proxy → Browser

**`http_req`** — captured request:
```json
{"type": "http_req", "id": "r1", "ts": 1234567890.0, "method": "POST",
 "url": "/api/data", "headers": {"content-type": "application/json"},
 "body_size": 42, "body_b64": "eyJ0ZXN0IjogdHJ1ZX0=", "intercepted": true}
```

**`http_res`** — captured response:
```json
{"type": "http_res", "id": "r1", "ts": 1234567890.1, "status": 200,
 "status_text": "OK", "headers": {"content-type": "application/json"},
 "body_size": 15, "body_b64": "eyJvayI6IHRydWV9", "duration_ms": 12.3}
```

**`http_intercept_state`** — current intercept/inspect mode:
```json
{"type": "http_intercept_state", "enabled": true, "inspect_enabled": true,
 "timeout_s": 30, "timeout_action": "forward"}
```

### Browser → Proxy

**`http_action`** — resolve a paused request:
```json
{"type": "http_action", "id": "r1", "action": "forward"}
{"type": "http_action", "id": "r1", "action": "drop"}
{"type": "http_action", "id": "r1", "action": "modify",
 "headers": {"X-Injected": "value"}, "body_b64": "bmV3IGJvZHk="}
```

**`http_intercept_toggle`** / **`http_inspect_toggle`**:
```json
{"type": "http_intercept_toggle", "enabled": true}
{"type": "http_inspect_toggle", "enabled": false}
```

## Architecture

```
Client (curl/browser/agent)
  │
  ▼
┌──────────────────┐
│  uterm inspect   │  Local HTTP reverse proxy (ASGI/uvicorn)
│  proxy           │  Captures req/res, applies intercept gate
└──────┬───────────┘
       │ CHANNEL_HTTP frames (binary WS)
       ▼
┌──────────────────┐
│  Tunnel Server   │  FastAPI TermHub or CF Durable Object
│  (relay)         │  Routes frames between proxy and browsers
└──────┬───────────┘
       │ Control channel WS
       ▼
┌──────────────────┐
│  Browser UI      │  inspect-view.ts
│  (inspect page)  │  Shows traffic, intercept toggles, action buttons
└──────────────────┘
```

The `InterceptGate` class (`tunnel/intercept.py`) manages pending requests as `asyncio.Future` objects. When intercept is enabled, the proxy awaits a browser decision (forward/drop/modify) or times out.
