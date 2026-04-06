#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: Role-differentiated browsers + EventBus observable layer.

Scenarios
---------
1. Viewer sends input while a long-poll subscriber is open — input is blocked,
   subscriber still receives the next snapshot from the worker normally.
2. Operator sends input in open mode — worker receives it AND the EventBus
   delivers the input_send event to a concurrent long-poll subscriber.
3. Admin performs a WS hijack — EventBus delivers hijack_acquired to a
   concurrent long-poll subscriber with event_types=hijack_acquired.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from provide.terminal.client import connect_async_ws

from .conftest import (
    ADMIN_H,
    connect_browser,
    drain_all,
    drain_until,
    long_poll,
    snapshot_msg,
    ws_url,
)

# ---------------------------------------------------------------------------
# 1. Viewer cannot send input — EventBus subscriber unaffected
# ---------------------------------------------------------------------------


async def test_viewer_cannot_send_input_eventbus_stable(live_server: Any) -> None:
    """Viewer input is silently dropped; EventBus still delivers the next snapshot."""
    hub, base_url = live_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as worker:
        await worker.recv()  # snapshot_req

        async with connect_browser(base_url, "s1", role="viewer") as viewer_ws:
            await drain_all(viewer_ws)  # hello + hijack_state

            # Viewer tries to send input (should be silently dropped)
            await viewer_ws.send(json.dumps({"type": "input", "data": "x"}))

            # Start long-poll subscriber
            poll_task = asyncio.create_task(long_poll(base_url, "s1", timeout_ms=5000, max_events=1))
            await asyncio.sleep(0.1)

            # Worker sends a snapshot — EventBus delivers it
            await worker.send(json.dumps(snapshot_msg("$ after viewer input")))

            response = await asyncio.wait_for(poll_task, timeout=8.0)

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) >= 1
    evt = body["events"][0]
    assert evt["type"] == "snapshot"
    assert body["timed_out"] is False

    # Worker must NOT have received any input from the viewer
    # (verified by the poll having received snapshot only, not input_send)
    input_events = [e for e in body["events"] if e.get("type") == "input_send"]
    assert len(input_events) == 0, f"viewer input leaked to EventBus: {input_events}"


# ---------------------------------------------------------------------------
# 2. Operator sends input in open mode — EventBus delivers input_send
# ---------------------------------------------------------------------------


async def test_operator_open_mode_input_eventbus_delivers(live_server: Any) -> None:
    """Operator sends input in open mode; worker receives it; EventBus delivers input_send."""
    hub, base_url = live_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as worker:
        await worker.recv()  # snapshot_req

        # Switch session to open input mode
        await worker.send(json.dumps({"type": "worker_hello", "input_mode": "open", "ts": time.time()}))

        async with connect_browser(base_url, "s1", role="operator") as op_ws:
            await drain_all(op_ws)
            # Wait for mode propagation
            await drain_until(op_ws, "hijack_state", timeout=2.0)

            # Subscribe to input_send events
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
                poll_task = asyncio.create_task(
                    http.get(
                        "/api/sessions/s1/events/watch",
                        params={"timeout_ms": 5000, "max_events": 1, "event_types": "input_send"},
                    )
                )
                await asyncio.sleep(0.1)

                # Operator sends input
                await op_ws.send(json.dumps({"type": "input", "data": "hello\r"}))

                response = await asyncio.wait_for(poll_task, timeout=8.0)

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "input_send"
    assert body["timed_out"] is False


# ---------------------------------------------------------------------------
# 3. Admin WS hijack — EventBus delivers hijack_acquired
# ---------------------------------------------------------------------------


async def test_admin_hijack_eventbus_delivers_hijack_acquired(live_server: Any) -> None:
    """Admin browser performs WS hijack; EventBus delivers hijack_acquired to subscriber."""
    hub, base_url = live_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as worker:
        await worker.recv()  # snapshot_req

        async with connect_browser(base_url, "s1", role="admin") as admin_ws:
            await drain_all(admin_ws)

            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
                poll_task = asyncio.create_task(
                    http.get(
                        "/api/sessions/s1/events/watch",
                        params={"timeout_ms": 5000, "max_events": 1, "event_types": "hijack_acquired"},
                    )
                )
                await asyncio.sleep(0.1)

                # Admin browser acquires WS hijack
                await admin_ws.send(json.dumps({"type": "hijack_request"}))

                response = await asyncio.wait_for(poll_task, timeout=8.0)

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "hijack_acquired"
    assert body["timed_out"] is False
