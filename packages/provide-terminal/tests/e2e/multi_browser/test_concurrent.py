#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: Multiple concurrent browsers + EventBus fan-out.

Scenarios
---------
1. Three role-differentiated browsers all connected; worker snapshot is
   broadcast to all three WS connections AND the EventBus long-poll.
2. Five browsers open concurrently; worker sends snapshots; browsers disconnect
   one by one; EventBus long-poll subscriber stays healthy throughout.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from provide.terminal.client import connect_async_ws

from .conftest import (
    ADMIN_H,
    connect_browser,
    drain_all,
    drain_until,
    snapshot_msg,
    ws_url,
)

# ---------------------------------------------------------------------------
# 1. Three-role browsers + EventBus all receive the same snapshot
# ---------------------------------------------------------------------------


async def test_three_role_browsers_all_receive_snapshot_eventbus_delivers(live_server: Any) -> None:
    """viewer + operator + admin all connected; snapshot broadcast + EventBus delivery confirmed."""
    hub, base_url = live_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as worker:
        await worker.recv()  # snapshot_req

        async with (
            connect_browser(base_url, "s1", role="viewer") as viewer,
            connect_browser(base_url, "s1", role="operator") as op,
            connect_browser(base_url, "s1", role="admin") as admin,
        ):
            for ws in (viewer, op, admin):
                await drain_all(ws)

            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
                poll_task = asyncio.create_task(
                    http.get(
                        "/api/sessions/s1/events/watch",
                        params={"timeout_ms": 5000, "max_events": 1, "event_types": "snapshot"},
                    )
                )
                await asyncio.sleep(0.1)

                await worker.send(json.dumps(snapshot_msg("$ three-browser test")))

                # All three WS connections receive the snapshot
                results = await asyncio.gather(
                    drain_until(viewer, "snapshot"),
                    drain_until(op, "snapshot"),
                    drain_until(admin, "snapshot"),
                )
                for msg in results:
                    assert msg is not None, "A browser did not receive the snapshot"
                    assert msg["screen"] == "$ three-browser test"

                # EventBus long-poll also delivers it
                response = await asyncio.wait_for(poll_task, timeout=8.0)

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "snapshot"
    assert body["timed_out"] is False


# ---------------------------------------------------------------------------
# 2. Five browsers join, get snapshots, disconnect progressively
# ---------------------------------------------------------------------------


async def test_five_browsers_join_leave_eventbus_stable(live_server: Any) -> None:
    """Five browsers open concurrently; disconnect one by one; EventBus subscriber unaffected."""
    hub, base_url = live_server

    async with connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as worker:
        await worker.recv()  # snapshot_req

        # Start the EventBus long-poll first (it will watch for 3 snapshots)
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=20.0) as http:
            poll_task = asyncio.create_task(
                http.get(
                    "/api/sessions/s1/events/watch",
                    params={"timeout_ms": 10000, "max_events": 3, "event_types": "snapshot"},
                )
            )
            await asyncio.sleep(0.1)

            # Open 5 browsers simultaneously
            browser_cms = [connect_browser(base_url, "s1", role="admin") for _ in range(5)]
            browsers = []
            for cm in browser_cms:
                ws = await cm.__aenter__()
                browsers.append((cm, ws))

            for _, ws in browsers:
                await drain_all(ws)

            # Worker sends 3 snapshots while all 5 browsers are connected
            for i in range(3):
                await worker.send(json.dumps(snapshot_msg(f"$ snapshot-{i}")))
                await asyncio.sleep(0.05)

            # Disconnect browsers one by one
            for cm, _ws in browsers:
                await cm.__aexit__(None, None, None)
                await asyncio.sleep(0.02)

            response = await asyncio.wait_for(poll_task, timeout=12.0)

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 3, f"expected 3 snapshot events, got: {body['events']}"
    assert body["timed_out"] is False
    assert body["dropped_count"] == 0
