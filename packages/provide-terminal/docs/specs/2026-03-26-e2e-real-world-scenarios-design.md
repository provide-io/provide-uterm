# E2E Real-World Scenario Suite

**Date:** 2026-03-26
**Status:** Approved

## Context

provide-terminal has unit and integration test coverage for the EventBus, hijack
lifecycle, and multi-browser broadcast individually. What was missing is proof
that these components work correctly *together* under real-world conditions:
multiple browsers with different roles, multiple concurrent sessions, hijack
events flowing through the observable layer, and fan-out correctness at scale.

This spec defines a layered e2e scenario suite built on top of the existing
`live_app_with_bus` pattern (real uvicorn, real WebSocket workers, real HTTP
long-polls) with three dimensions:

- **Realistic user flows** — role-differentiated browsers interacting as in production
- **Concurrency correctness** — N clients, N subscribers racing under one event loop
- **Protocol edge coverage** — hijack lifecycle events in the observable stream, filter
  isolation under load

## File Layout

All files stay under 500 LOC. Tests are organized by scenario domain:

```
packages/provide-terminal/tests/e2e/
  conftest.py                   # existing shared helpers (unchanged)
  test_event_bus_e2e.py         # existing 3-test baseline (unchanged)
  multi_browser/
    __init__.py
    conftest.py                 # browser WS helper, shared fixture
    test_roles.py               # viewer/operator/admin + EventBus
    test_concurrent.py          # 5-browser fan-out + EventBus
  multi_session/
    __init__.py
    conftest.py                 # two-session live_server fixture
    test_isolation.py           # s1 and s2 events isolated in EventBus
  hijack/
    __init__.py
    test_concurrent.py          # hijack lifecycle events in long-poll stream
  observer/
    __init__.py
    test_fanout.py              # 5 concurrent long-poll subscribers
    test_filters.py             # event_types + pattern filter isolation
```

## Fixture Architecture

### `live_server_with_bus(sessions=1|2)`
Extends the `live_app_with_bus` pattern from `test_event_bus_e2e.py`.
- Starts `create_server_app` on port 0 with `auth.mode=dev`
- Injects `EventBus()` into `hub._event_bus` post-startup
- Session configs: `auto_start=False` (WS workers connect manually)
- Yields `(hub, base_url)`

### `mock_tcp_echo_server`
Used by multi_session tests to simulate a telnet endpoint. `asyncio.start_server`
on port 0; echoes bytes back. Yields `(host, port)`.

### Browser helper
```python
@asynccontextmanager
async def connect_browser(base_url, session_id, *, role="admin"):
    url = ws_url(base_url, f"/ws/browser/{session_id}/term")
    async with connect_async_ws(url) as ws:
        yield ws
```

### Header constants
```python
_ADMIN    = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}
_OPERATOR = {"X-Uterm-Principal": "op-user",    "X-Uterm-Role": "operator"}
_VIEWER   = {"X-Uterm-Principal": "view-user",  "X-Uterm-Role": "viewer"}
```

## Scenario Summaries

### multi_browser/test_roles.py

1. `test_viewer_cannot_send_input_eventbus_stable` — viewer sends input while
   long-poll subscriber is open; worker never receives input; subscriber still
   receives snapshot from worker normally after the attempt.

2. `test_operator_open_mode_input_reaches_worker_eventbus_delivers` — operator
   sends input in open mode; worker receives it; EventBus delivers the
   `input_send` event to a concurrent long-poll subscriber.

3. `test_admin_hijack_blocks_operator_eventbus_delivers_hijack_acquired` — admin
   browser performs WS hijack_request; long-poll subscriber with
   `event_types=hijack_acquired` receives the event; operator input is blocked.

### multi_browser/test_concurrent.py

1. `test_three_role_browsers_all_receive_broadcast` — viewer + operator + admin
   browsers all connected; worker sends snapshot; all three WS connections
   receive it; EventBus long-poll also delivers.

2. `test_five_browsers_join_leave_eventbus_stable` — 5 browsers open
   concurrently via `asyncio.gather`; worker sends 3 snapshots; all 5 receive
   them; browsers disconnect one by one; no crash; long-poll subscriber remains
   healthy throughout.

### multi_session/test_isolation.py

1. `test_two_sessions_eventbus_isolated` — sessions s1 and s2, each with their
   own WS worker; EventBus subscriber on s1 receives s1 events; subscriber on
   s2 receives s2 events; cross-contamination asserted absent.

2. `test_two_sessions_concurrent_long_polls` — two long-poll callers
   simultaneously blocking on s1 and s2 respectively; workers fire one event
   each; each caller unblocks with exactly the right event.

### hijack/test_concurrent.py

1. `test_hijack_acquired_event_in_long_poll` — REST acquire on worker w1; long-
   poll subscriber with `event_types=hijack_acquired` receives the event.

2. `test_hijack_released_event_in_long_poll` — REST acquire then release; long-
   poll subscriber with `event_types=hijack_released` receives the event.

3. `test_worker_disconnect_during_active_long_poll` — worker WS closes while
   long-poll subscriber is open; poll returns before timeout (`timed_out=false`).

### observer/test_fanout.py

1. `test_five_concurrent_subscribers_all_receive` — 5 concurrent
   `asyncio.create_task` long-polls; worker sends 5 snapshots serially; all
   5 tasks return 200 with at least 1 event each.

2. `test_observer_survives_worker_reconnect` — subscriber open on s1; worker
   WS closes; new worker WS connects; subscriber receives sentinel from first
   worker, then new subscription can be opened cleanly.

### observer/test_filters.py

1. `test_three_subscribers_different_event_filters` — sub1: `event_types=snapshot`;
   sub2: `event_types=hijack_acquired`; sub3: no filter. Worker sends snapshot;
   admin browser acquires WS hijack. Each subscriber receives only its matching
   events.

2. `test_pattern_filter_passes_matching_screen` — long-poll with `pattern=\$\s`;
   worker sends snapshot with screen containing `"$ "` then one without; only
   the matching event is returned.

## Error Handling

- All fixtures use `asyncio.wait_for` with explicit timeouts
- No `asyncio.sleep` except the mandatory 0.1 s "let subscriber register" beat
- All tasks stored in variables and awaited (RUF006 compliant)

## Verification

```bash
# New scenarios only — fast
uv run pytest packages/provide-terminal/tests/e2e/multi_browser/ \
               packages/provide-terminal/tests/e2e/multi_session/ \
               packages/provide-terminal/tests/e2e/hijack/ \
               packages/provide-terminal/tests/e2e/observer/ -v

# Full suite — 100% coverage must hold
uv run pytest packages/provide-terminal/tests/ packages/provide-terminal-shell/tests/ \
  -q --cov=provide.terminal --cov=provide.terminal.shell --cov-fail-under=100 \
  --ignore=packages/provide-terminal/tests/memray \
  --ignore=packages/provide-terminal/tests/playwright

# File size check
wc -l packages/provide-terminal/tests/e2e/**/*.py
```
