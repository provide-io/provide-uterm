# Design Spec: Observable Core

**Date:** 2026-03-26
**Status:** Approved
**Branch:** feat/observable-core

---

## Problem

provide-terminal receives every byte of terminal output and records all significant events (`append_event`), but consumers — AI agents, safety monitors, collaboration tools — must poll to learn what happened. Polling adds latency, wastes resources, and prevents reactive architectures.

The hub already has a rich event ring buffer, a well-structured `HubEvent` format, and existing callback hooks. What's missing is a fanout layer that lets async subscribers receive events in real time.

---

## Goals

- AI agents and internal subscribers receive terminal events as they arrive, not via polling.
- Zero impact on the broadcast hot path (fanout must be non-blocking, synchronous enqueue only).
- Pure asyncio, zero framework dependencies in the core.
- Provide a hybrid MCP tool: streaming where supported, long-poll batch fallback otherwise.
- Establish the foundation for all planned ARDs (anomaly detection, command approval, presence, fan-out).

---

## Non-Goals

- Replacing the existing event ring buffer (EventBus is additive).
- Modifying the terminal output stream.
- Persistent subscriptions across restarts.
- Replacing the FastAPI dependency in TermHub (deferred to follow-up).

---

## Architecture

### `EventBus` (`provide.terminal.hijack.hub.event_bus`)

Pure asyncio, zero framework deps. Lives alongside `TermHub` in the hub package.

**Subscription lifecycle:**
```
hub._event_bus.watch(worker_id, event_types=["snapshot"], pattern=r"\$\s*$")
    → asynccontextmanager yields _Subscription
    → caller drains sub.queue with asyncio.wait_for
    → finally: subscription removed from registry
```

**Hot path integration:**
```
append_event(worker_id, "snapshot", {...})
    → lock released
    → event_bus._enqueue(worker_id, evt)   # put_nowait, never blocks
        → per-subscriber filter (event_types, pattern)
        → sub.queue.put_nowait(item)
           QueueFull? → drop oldest, enqueue new (ring buffer semantics)
```

**Worker disconnect:**
```
deregister_worker() or disconnect_worker()
    → event_bus.close_worker(worker_id)
        → put_nowait(None) sentinel into all subscriber queues
        → remove all subscriptions for worker_id
```

### `HubEvent` shape

Same as `append_event` output, plus `worker_id`:
```python
{
    "worker_id": str,
    "seq": int,
    "ts": float,
    "type": str,   # "snapshot" | "input_send" | "hijack_acquired" | ...
    "data": dict,
}
```

### Long-poll REST endpoint

`GET /api/sessions/{session_id}/events/watch`

Query params: `timeout_ms` (100–30000, default 5000), `event_types` (comma-separated), `pattern` (regex), `max_events` (1–200, default 50).

Response:
```json
{
    "events": [...],
    "dropped_count": 0,
    "timed_out": true
}
```

Falls back gracefully when EventBus is not configured (returns recent events from ring buffer).

### `session_watch` MCP tool (17th tool)

```python
session_watch(
    session_id: str,
    event_types: str | None = None,   # comma-separated
    pattern: str | None = None,
    timeout_s: float = 10.0,
    max_events: int = 50,
) -> dict[str, Any]
```

Calls `GET /api/sessions/{session_id}/events/watch` via `HijackClient.watch_session_events()`. Works with any MCP host (no streaming runtime required).

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Queue full | Drop oldest event, enqueue new. `dropped_count` incremented. Never blocks. |
| Slow subscriber | Accumulates drops. `dropped_count` in response. Hot path unaffected. |
| Subscriber crash / abandoned `async with` | `finally` in `watch()` calls `unsubscribe`. No leak. |
| Worker disconnect mid-watch | `None` sentinel delivered; consumer loop exits cleanly. |
| EventBus raises in `_enqueue` | Caught, logged via `provide.telemetry`. Event skipped. Never propagates. |
| EventBus not configured | Watch endpoint returns recent ring-buffer events + `timed_out: false`. |

---

## Future: ARD Subscribers

Once EventBus is in place, planned ARDs subscribe without touching core:

```python
hub = TermHub(..., event_bus=EventBus())
hub.event_bus.register_handler("detector", TerminalDetector(rules=RULES, sink=WebhookSink(...)))
```

- `TerminalDetector` → subscribes to `snapshot` + `input_send`
- `CommandApprovalGate` → subscribes to `input_send`
- `PresenceManager` → subscribes to browser lifecycle events
- `FanOutController` → subscribes to `snapshot` during response-window collection

---

## Files

**New:**
- `packages/provide-terminal/src/provide/terminal/hijack/hub/event_bus.py`
- `packages/provide-terminal/tests/hijack/test_event_bus.py`
- `packages/provide-terminal/tests/hijack/test_event_bus_integration.py`
- `packages/provide-terminal/tests/mcp/test_mcp_watch.py`

**Modified:**
- `packages/provide-terminal/src/provide/terminal/hijack/hub/core.py` — EventBus wiring
- `packages/provide-terminal/src/provide/terminal/hijack/hub/__init__.py` — export EventBus
- `packages/provide-terminal/src/provide/terminal/server/registry.py` — `watch_session_events()`
- `packages/provide-terminal/src/provide/terminal/server/routes/api.py` — watch endpoint
- `packages/provide-terminal/src/provide/terminal/client/hijack.py` — `watch_session_events()`
- `packages/provide-terminal/src/provide/terminal/mcp/server.py` — `session_watch` tool
- `pyproject.toml` — add `event_bus.py` to `paths_to_mutate`
