# TUI Traffic Viewer Design — `uterm watch`

## Context

`uterm inspect` runs a local HTTP proxy and logs traffic to stderr. The browser inspect view at `/app/inspect/{id}` shows live traffic in the browser. But there's no terminal-native interactive viewer. `uterm watch` fills this gap — a Textual TUI that connects to an existing tunnel and shows HTTP traffic in real time.

## Command

```
uterm watch <tunnel-id-or-url> [--server URL] [--layout horizontal|vertical|modal]
```

**Arguments:**

- `tunnel-id-or-url`: Either a tunnel ID (`tunnel-abc123`) or a full URL (`https://worker.dev/app/inspect/tunnel-abc123`). If a URL, extracts the tunnel ID.
- `--server / -s`: Server URL (required if passing bare tunnel ID)
- `--layout`: Initial layout mode (default: `horizontal`)
- `--token`: Bearer token for auth
- `--token-file`: Path to token file

## Architecture

`uterm watch` is a pure consumer. It does NOT start a proxy or create a tunnel. It connects to an existing tunnel's browser WebSocket at `/ws/browser/{id}/term`, decodes control channel frames, filters for `_channel: "http"` messages, and renders them in a Textual app.

```
Existing tunnel agent (uterm inspect / uterm share)
       │
       ▼
Server (FastAPI or CF Worker)
       │ ch 0x03: http_req / http_res
       ▼
/ws/browser/{id}/term
       │
       ▼
uterm watch (Textual TUI)
  ├─ Request list table
  ├─ Detail pane (headers + body)
  ├─ Filter bar
  └─ Status bar
```

## TUI Layout

### Horizontal (default)

```
┌─────────────────────────────┬──────────────────────────────┐
│ Method URL          Status  │ POST /api/login              │
│ GET    /api/users   200 5ms │ 200 OK — 89ms                │
│ POST   /api/login   200 89m│                               │
│ GET    /favicon.ico 404 2ms│ Request Headers               │
│ DELETE /api/old     204 58m│ content-type: application/json│
│ POST   /api/crash   500 34m│ user-agent: demo/1.0          │
│ ▶ PATCH /api/users  200 47m│                               │
│                             │ Request Body                  │
│                             │ {"user":"admin"}              │
│                             │                               │
│                             │ Response Headers              │
│                             │ content-type: application/json│
│                             │                               │
│                             │ Response Body                 │
│                             │ {"ok":true,"items":8}         │
├─────────────────────────────┴──────────────────────────────┤
│ [/] Filter  [Tab] Layout  [q] Quit  ● Connected  42 reqs  │
└────────────────────────────────────────────────────────────┘
```

### Vertical

```
┌────────────────────────────────────────────────────────────┐
│ Method URL                    Status Duration  Size        │
│ GET    /api/users             200    5ms       3.2KB       │
│ POST   /api/login             200    89ms      24B         │
│ ▶ PATCH /api/users/42         200    47ms      24B         │
├────────────────────────────────────────────────────────────┤
│ PATCH /api/users/42  200 OK — 47ms                         │
│ Request Headers: content-type: application/json            │
│ Request Body: {"action":"patch"}                           │
│ Response Headers: x-request-id: r7                         │
│ Response Body: {"ok":true}                                 │
├────────────────────────────────────────────────────────────┤
│ [/] Filter  [Tab] Layout  [q] Quit  ● Connected  42 reqs  │
└────────────────────────────────────────────────────────────┘
```

### Modal

```
┌────────────────────────────────────────────────────────────┐
│ Method URL                    Status Duration  Size        │
│ GET    /api/users             200    5ms       3.2KB       │
│ POST   /api/login             200    89ms      24B         │
│ GET    /favicon.ico           404    2ms       0B          │
│ DELETE /api/sessions/old-1    204    58ms      0B          │
│ POST   /api/crash             500    34ms      26B         │
│ PATCH  /api/users/42          200    47ms      24B         │
│ GET    /api/health            200    146ms     24B         │
│ ...                                                        │
├────────────────────────────────────────────────────────────┤
│ [/] Filter  [Tab] Layout  [Enter] Detail  ● Connected     │
└────────────────────────────────────────────────────────────┘

(Enter opens modal overlay with detail, Esc closes)
```

## Key Bindings

| Key       | Action                                         | Status                         |
| --------- | ---------------------------------------------- | ------------------------------ |
| `q`       | Quit                                           | Implemented                    |
| `l`       | Cycle layout: horizontal → vertical → modal    | Implemented                    |
| `f`       | Cycle method filter (All → GET → POST → ...)   | Implemented                    |
| `Enter`   | Select row → show detail (split) or open modal | Implemented                    |
| `↑` / `↓` | Navigate request list                          | Implemented (DataTable cursor) |
| `Esc`     | Close modal overlay                            | Implemented (DetailScreen)     |
| `j` / `k` | Vim-style navigation                           | Planned                        |
| `/`       | Focus filter input                             | Planned                        |

## Data Flow

1. Connect WebSocket to `/ws/browser/{id}/term` (with share token if provided)
1. Receive DLE/STX encoded control channel frames
1. Parse JSON, filter for `_channel: "http"`
1. For `http_req`: create new exchange entry in list
1. For `http_res`: match by `id`, update exchange, refresh display
1. Terminal frames (`type: "term"`) ignored in TUI mode

## Dependencies

Add `textual>=0.50` to the `[cli]` extra in `packages/provide-terminal/pyproject.toml`.

## Files

| File              | Purpose                             |
| ----------------- | ----------------------------------- |
| `cli/watch.py`    | CLI entry point + Textual app class |
| `cli/__init__.py` | Register `watch` subcommand         |

The Textual app, widgets, and styles all live in `watch.py` (single file — Textual CSS is inline). If it grows past 500 LOC, split into `cli/watch/` package.

## Testing

- Arg parsing tests (tunnel ID extraction from URL, --layout validation)
- WebSocket message parsing (mock WS, feed http_req/http_res frames)
- Layout cycling logic
- Filter application
- No E2E TUI tests (Textual has its own test framework but it's complex — defer)

## Verification

1. Start `uterm inspect 3000 --server URL` in one terminal
1. Run `uterm watch tunnel-abc --server URL` in another
1. Make HTTP requests to the inspect proxy
1. Watch them appear in the TUI in real time
1. Navigate with keyboard, switch layouts, filter by method
