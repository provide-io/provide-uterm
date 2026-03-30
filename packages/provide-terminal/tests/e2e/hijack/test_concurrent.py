#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: Hijack lifecycle events delivered through the EventBus long-poll.

Scenarios
---------
1. REST acquire → hijack_acquired event reaches a concurrent long-poll subscriber.
2. REST acquire then release → hijack_released event reaches a concurrent
   long-poll subscriber.
3. Worker WS closes while a long-poll subscriber is open → poll returns before
   its timeout (sentinel delivery).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.e2e._live_server import live_server_with_bus
from provide.terminal.client import connect_async_ws

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_server() -> Any:
    sessions = [{"session_id": "h1", "display_name": "Hijack Session", "connector_type": "shell", "auto_start": False}]
    async with live_server_with_bus(sessions, label="live_server (hijack)") as result:
        yield result


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


# ---------------------------------------------------------------------------
# 1. REST acquire → EventBus delivers hijack_acquired
# ---------------------------------------------------------------------------


async def test_hijack_acquired_event_in_long_poll(live_server: Any) -> None:
    """REST acquire → EventBus delivers hijack_acquired to concurrent long-poll subscriber."""
    hub, base_url = live_server

    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/h1/term")) as worker,
        httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http,
    ):
        await worker.recv()  # snapshot_req

        poll_task = asyncio.create_task(
            http.get(
                "/api/sessions/h1/events/watch",
                params={"timeout_ms": 5000, "max_events": 1, "event_types": "hijack_acquired"},
            )
        )
        await asyncio.sleep(0.1)

        r = await http.post("/worker/h1/hijack/acquire", json={"owner": "e2e-test", "lease_s": 60})
        assert r.status_code == 200, f"acquire failed: {r.text}"

        response = await asyncio.wait_for(poll_task, timeout=8.0)

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 1
    evt = body["events"][0]
    assert evt["type"] == "hijack_acquired"
    assert evt["worker_id"] == "h1"
    assert body["timed_out"] is False


# ---------------------------------------------------------------------------
# 2. REST release → EventBus delivers hijack_released
# ---------------------------------------------------------------------------


async def test_hijack_released_event_in_long_poll(live_server: Any) -> None:
    """REST acquire then release → EventBus delivers hijack_released."""
    hub, base_url = live_server

    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/h1/term")) as worker,
        httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http,
    ):
        await worker.recv()  # snapshot_req

        # Acquire hijack first
        r = await http.post("/worker/h1/hijack/acquire", json={"owner": "e2e-test", "lease_s": 60})
        assert r.status_code == 200
        hijack_id = r.json()["hijack_id"]

        # Subscribe to hijack_released
        poll_task = asyncio.create_task(
            http.get(
                "/api/sessions/h1/events/watch",
                params={"timeout_ms": 5000, "max_events": 1, "event_types": "hijack_released"},
            )
        )
        await asyncio.sleep(0.1)

        # Release
        r2 = await http.post(f"/worker/h1/hijack/{hijack_id}/release")
        assert r2.status_code == 200, f"release failed: {r2.text}"

        response = await asyncio.wait_for(poll_task, timeout=8.0)

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 1
    evt = body["events"][0]
    assert evt["type"] == "hijack_released"
    assert body["timed_out"] is False


# ---------------------------------------------------------------------------
# 3. Worker disconnect → long-poll terminates via sentinel
# ---------------------------------------------------------------------------


async def test_worker_disconnect_terminates_long_poll(live_server: Any) -> None:
    """Worker WS closes; long-poll returns before its timeout via sentinel."""
    hub, base_url = live_server

    async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http:
        async with connect_async_ws(ws_url(base_url, "/ws/worker/h1/term")):
            poll_task = asyncio.create_task(
                http.get(
                    "/api/sessions/h1/events/watch",
                    params={"timeout_ms": 8000},
                )
            )
            await asyncio.sleep(0.1)
            # Worker disconnects (context exit)

        response = await asyncio.wait_for(poll_task, timeout=5.0)

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is False
