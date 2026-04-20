# Delivery Layer: SSE Streaming, `session_subscribe` MCP Tool, Webhook Delivery

**Date:** 2026-03-27 **Status:** Approved

## Context and Problem Statement

The EventBus + long-poll observable core (merged 2026-03-27) provides a solid real-time event infrastructure. The long-poll endpoint is useful for one-shot queries, but production use cases need persistent delivery:

- **Browsers** want push without polling — SSE (`EventSource`) is the standard answer
- **AI agents** want to subscribe for a long duration and react when a pattern (e.g., a shell prompt) appears — `session_watch` is too short-lived and lacks `matched_pattern` metadata
- **External systems** (CI, webhooks, integrations) need events POSTed to a URL

All three delivery modes should be available in the FastAPI server and in the Cloudflare Durable Objects deployment.

______________________________________________________________________

## Feature 1: SSE Streaming Endpoint

### FastAPI

A true-streaming `GET /api/sessions/{id}/events/stream` endpoint backed by the EventBus.

**Registry method** (`stream_session_events`):

- Async generator that yields SSE-formatted strings
- `data: {json}\n\n` per event
- `data: {"type":"heartbeat"}\n\n` when idle for `heartbeat_s` (default 15 s)
- `data: {"type":"worker_disconnected"}\n\n` + return on disconnect sentinel
- Falls back to empty generator (no events, no error) when EventBus not configured

**Route** (`GET /api/sessions/{session_id}/events/stream`):

- Same auth/policy check as `/events/watch`
- Returns `StreamingResponse(..., media_type="text/event-stream")`
- Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`
- Query params: `event_types` (CSV), `pattern` (max 200 chars)

**No new dependencies** — `StreamingResponse` is in FastAPI/Starlette.

### Cloudflare Durable Objects

CF DOs cannot maintain long-lived HTTP connections across hibernation. Use **polling-SSE**: the endpoint returns a batch of events in SSE format + `retry: 3000` to instruct the browser's `EventSource` to reconnect every 3 s. Reconnect sends `Last-Event-ID: {seq}`.

**Handler** reads `after_seq` from `Last-Event-ID` header or query param, loads events from SQLite store, returns SSE-formatted response.

______________________________________________________________________

## Feature 2: `session_subscribe` MCP Tool

Designed for agent loops that need to watch a session for a long time (e.g., until a prompt appears). Differs from `session_watch`:

|                           | `session_watch` | `session_subscribe` |
| ------------------------- | --------------- | ------------------- |
| `duration_s` max          | 30 s            | 120 s               |
| `max_events` max          | 50              | 500                 |
| Returns `matched_pattern` | no              | yes                 |

`matched_pattern: bool` — True when a `pattern` was given and at least one delivered event matched it. Lets the caller distinguish "got data because pattern fired" from "got data from other events".

______________________________________________________________________

## Feature 3: Webhook Delivery

### FastAPI

In-memory `WebhookManager` with a background EventBus subscriber per registered webhook.

**`WebhookConfig`** fields: `webhook_id` (UUID), `session_id`, `url`, `event_types` (frozenset | None), `pattern` (str | None), `secret` (HMAC-SHA256 key | None).

**Delivery**:

- `POST {url}` with JSON body `{webhook_id, session_id, event, timestamp}`
- `X-Uterm-Signature: sha256={hex}` header when `secret` is set
- 3 retries with exponential backoff (0.5 s, 1 s, 2 s), 5 s timeout per attempt
- After 3 failures, delivery loop logs and continues (no crash)

**REST API**:

- `POST /api/sessions/{id}/webhooks` → register → `{webhook_id, ...}`
- `GET /api/sessions/{id}/webhooks` → list
- `DELETE /api/sessions/{id}/webhooks/{webhook_id}` → unregister

**`WebhookManager`** stored at `app.state.uterm_webhooks`, started/stopped in lifespan.

### Cloudflare Durable Objects

Webhook config stored in DO SQLite (one JSON blob per webhook, keyed `webhook:{id}`). When an event arrives from the worker WS, `_fire_webhooks()` loads configs and fires outbound `fetch()` calls. No background tasks — delivery is synchronous within `webSocketMessage()`.

**REST API**: same shape as FastAPI (register/list/unregister at `/api/sessions/{id}/webhooks`).

______________________________________________________________________

## File Layout

```
packages/provide-terminal/src/provide/terminal/
  server/
    routes/sse.py              NEW — SSE route
    routes/webhooks.py         NEW — webhook CRUD routes
    webhooks.py                NEW — WebhookManager + WebhookConfig
    registry.py                MOD — stream_session_events()
    app.py                     MOD — WebhookManager in lifespan
    routes/api.py              MOD — include SSE + webhook routers
  mcp/server.py                MOD — session_subscribe tool (TOOL_COUNT=18)

packages/provide-terminal/tests/
  server/
    test_sse_registry.py       NEW
    test_webhooks.py           NEW
  e2e/observer/
    test_sse.py                NEW
    test_webhooks_e2e.py       NEW
  mcp/test_mcp_watch.py        MOD — TOOL_COUNT=18, subscribe tests

packages/provide-terminal-cloudflare/src/provide/terminal/cloudflare/
  do/_sse.py                   NEW
  do/_webhooks.py              NEW
  do/session_runtime.py        MOD — SSE + webhook mixins
  api/http_routes/_session.py  MOD — /events/stream + webhook CRUD
  state/store.py               MOD — webhook CRUD methods
  contracts.py                 MOD — new TypedDicts

packages/provide-terminal-cloudflare/tests/
  test_sse.py                  NEW
  test_webhooks.py             NEW
```

______________________________________________________________________

## Verification

```bash
# FastAPI package — 100% coverage
uv run pytest packages/provide-terminal/tests/ packages/provide-terminal/tests/shell/ \
  -q --cov=provide.terminal --cov=provide.terminal.shell --cov-fail-under=100 \
  --ignore=packages/provide-terminal/tests/memray \
  --ignore=packages/provide-terminal/tests/playwright

# CF package — 100% coverage
uv run pytest packages/provide-terminal-cloudflare/tests/ \
  --cov=provide.terminal.cloudflare --cov-fail-under=100

# File size guard — no file over 500 LOC
find packages/*/src packages/*/tests -name "*.py" \
  | xargs wc -l | awk '$1 > 500 {print}' | grep -v total
```
