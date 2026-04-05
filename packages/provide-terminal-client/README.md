# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# provide-terminal-client

Async HTTP and WebSocket client library for the provide-terminal control plane.
Provides `HijackClient` for REST-based session and hijack management, plus
inline WebSocket clients (`AsyncInlineWebSocketClient`,
`SyncInlineWebSocketClient`) for real-time terminal data streams. Every method
returns `tuple[bool, dict]` for straightforward success/error handling.

## Installation

```bash
pip install provide-terminal-client[http]        # HijackClient (httpx)
pip install provide-terminal-client[websocket]   # WS clients (websockets)
```

Requires Python 3.11+.

## HijackClient API

| Method | Description |
|---|---|
| `acquire(worker_id, owner, lease_s)` | Acquire a lease-based hijack session |
| `heartbeat(worker_id, hijack_id, lease_s)` | Extend a hijack lease |
| `send(worker_id, hijack_id, keys, ...)` | Send input with optional prompt guard |
| `snapshot(worker_id, hijack_id, wait_ms)` | Read terminal snapshot |
| `events(worker_id, hijack_id, after_seq)` | Read event log |
| `step(worker_id, hijack_id)` | Single-step the worker loop |
| `release(worker_id, hijack_id)` | Release hijack, resume automation |
| `list_sessions()` | List all sessions |
| `get_session(session_id)` | Get session details |
| `session_snapshot(session_id)` | Get session terminal snapshot |
| `watch_session_events(session_id, ...)` | Long-poll session event stream |
| `quick_connect(connector_type, ...)` | Create an ephemeral session |
| `health()` | Server health check |

## Usage

```python
from provide.terminal.client import HijackClient

async with HijackClient("http://localhost:8780") as c:
    ok, data = await c.acquire("worker-1", owner="agent")
    if ok:
        ok, snap = await c.snapshot("worker-1", data["hijack_id"])
        print(snap["snapshot"]["screen"])
        await c.release("worker-1", data["hijack_id"])
```

## Key modules

- `provide.terminal.client.hijack` -- `HijackClient` async REST client
- `provide.terminal.client.control_ws` -- `AsyncInlineWebSocketClient`, `SyncInlineWebSocketClient`, `LogicalFrameDecoder`
- `provide.terminal.client.mcp_tools` -- shared helpers used by the AI package

## Links

- [Main repository README](../../README.md)
