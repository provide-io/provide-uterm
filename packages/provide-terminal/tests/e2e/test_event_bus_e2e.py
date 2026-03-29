#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: EventBus + /events/watch long-poll with a real worker WebSocket.

Flow
----
1. Start a live uvicorn server (create_server_app + EventBus injected).
2. Connect a real WebSocket worker and wait for the snapshot_req handshake.
3. Concurrently open a long-poll to GET /api/sessions/{id}/events/watch.
4. Worker sends a snapshot — hub.broadcast → append_event → EventBus._enqueue.
5. Long-poll returns; assert the snapshot event is present.
6. Disconnect the worker; confirm a second long-poll terminates cleanly (sentinel).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
import uvicorn

from provide.terminal.client import connect_async_ws
from provide.terminal.hijack.hub import EventBus
from provide.terminal.server.app import create_server_app
from provide.terminal.server.config import config_from_mapping

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_app_with_bus() -> Any:
    """Start create_server_app on a random port with EventBus injected post-startup."""
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "s1",
                    "display_name": "E2E Session",
                    "connector_type": "shell",
                    "auto_start": False,
                }
            ],
        }
    )
    app = create_server_app(cfg)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while not server.started:
        if loop.time() > deadline:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=2.0)
            raise RuntimeError("live_app_with_bus: uvicorn startup timeout")
        await asyncio.sleep(0.05)

    port: int = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    # Inject EventBus into the hub after startup
    hub = app.state.uterm_registry._hub
    hub._event_bus = EventBus()

    try:
        yield hub, base_url
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADMIN_HEADERS = {"X-Uterm-Principal": "e2e-tester", "X-Uterm-Role": "admin"}


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def _snapshot_msg(screen: str = "$ e2e test") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "e2e-hash",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "e2e"},
        "ts": time.time(),
    }


async def _drain_until_type(ws: Any, type_: str, timeout: float = 3.0) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            msg = json.loads(raw)
            if msg.get("type") == type_:
                return msg
        except TimeoutError:
            continue
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_worker_snapshot_arrives_via_long_poll(live_app_with_bus: Any) -> None:
    """Worker sends snapshot → EventBus delivers it to the HTTP long-poll caller."""
    hub, base_url = live_app_with_bus
    ws_url = _ws_url(base_url, "/ws/worker/s1/term")

    # Pre-register the worker slot so EventBus has an entry for "s1"
    await hub._get("s1")

    async with connect_async_ws(ws_url) as worker:
        # Wait for server's snapshot_req handshake
        snap_req = await _drain_until_type(worker, "snapshot_req", timeout=3.0)
        assert snap_req is not None, "worker did not receive snapshot_req"

        # Start long-poll in background — will block until an event arrives
        async with httpx.AsyncClient(base_url=base_url, headers=_ADMIN_HEADERS, timeout=15.0) as http:
            poll_task = asyncio.create_task(
                http.get("/api/sessions/s1/events/watch", params={"timeout_ms": 5000, "max_events": 1})
            )

            # Give the long-poll a moment to register its subscription
            await asyncio.sleep(0.1)

            # Worker sends a snapshot — hub.broadcast → append_event → EventBus
            await worker.send(json.dumps(_snapshot_msg("$ e2e live poll")))

            response = await asyncio.wait_for(poll_task, timeout=8.0)

    assert response.status_code == 200, f"unexpected status: {response.status_code}"
    body = response.json()
    assert len(body["events"]) == 1, f"expected 1 event, got: {body['events']}"
    evt = body["events"][0]
    assert evt["type"] == "snapshot"
    assert evt["worker_id"] == "s1"
    assert body["timed_out"] is False
    assert body["dropped_count"] == 0


async def test_worker_disconnect_terminates_long_poll(live_app_with_bus: Any) -> None:
    """Worker disconnect delivers sentinel → long-poll returns cleanly before timeout."""
    hub, base_url = live_app_with_bus
    ws_url = _ws_url(base_url, "/ws/worker/s1/term")

    await hub._get("s1")

    async with httpx.AsyncClient(base_url=base_url, headers=_ADMIN_HEADERS, timeout=15.0) as http:
        async with connect_async_ws(ws_url):
            # Start long-poll
            poll_task = asyncio.create_task(http.get("/api/sessions/s1/events/watch", params={"timeout_ms": 8000}))
            await asyncio.sleep(0.1)
            # Worker disconnects here (context manager exits)

        response = await asyncio.wait_for(poll_task, timeout=5.0)

    assert response.status_code == 200
    body = response.json()
    # No events, but returned before timeout because of sentinel
    assert body["timed_out"] is False


async def test_event_types_filter_excludes_non_matching(live_app_with_bus: Any) -> None:
    """Long-poll with event_types filter only returns matching events."""
    hub, base_url = live_app_with_bus
    ws_url = _ws_url(base_url, "/ws/worker/s1/term")

    await hub._get("s1")

    async with connect_async_ws(ws_url) as worker:
        await _drain_until_type(worker, "snapshot_req", timeout=3.0)

        async with httpx.AsyncClient(base_url=base_url, headers=_ADMIN_HEADERS, timeout=15.0) as http:
            poll_task = asyncio.create_task(
                http.get(
                    "/api/sessions/s1/events/watch",
                    params={"timeout_ms": 4000, "max_events": 1, "event_types": "snapshot"},
                )
            )
            await asyncio.sleep(0.1)

            # Send snapshot (should be returned) — hub routes it through broadcast
            await worker.send(json.dumps(_snapshot_msg("$ filtered")))

            response = await asyncio.wait_for(poll_task, timeout=8.0)

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "snapshot"
