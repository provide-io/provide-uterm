#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: EventBus fan-out correctness with multiple concurrent subscribers.

Scenarios
---------
1. Five concurrent long-poll subscribers all receive the same set of events.
2. Worker disconnects mid-subscription; new worker connects; a fresh
   subscription on the same session receives events from the new worker.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from tests.e2e._live_server import live_server_with_bus
from provide.terminal.client import connect_async_ws

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}
_N_SUBSCRIBERS = 5
_N_EVENTS = 5


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_server() -> Any:
    sessions = [
        {"session_id": "obs1", "display_name": "Observer Session", "connector_type": "shell", "auto_start": False}
    ]
    async with live_server_with_bus(sessions, label="live_server (fanout)") as result:
        yield result


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def snapshot_msg(screen: str) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "fanout",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "fanout"},
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# 1. Five concurrent subscribers all receive all events
# ---------------------------------------------------------------------------


async def test_five_concurrent_subscribers_all_receive(live_server: Any) -> None:
    """Five long-poll subscribers each receive at least one event from the same worker."""
    hub, base_url = live_server

    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/obs1/term")) as worker,
        httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=20.0) as http,
    ):
        await worker.recv()  # snapshot_req

        # Launch N subscribers concurrently — each wants max_events=_N_EVENTS
        poll_tasks = [
            asyncio.create_task(
                http.get(
                    "/api/sessions/obs1/events/watch",
                    params={
                        "timeout_ms": 8000,
                        "max_events": _N_EVENTS,
                        "event_types": "snapshot",
                    },
                )
            )
            for _ in range(_N_SUBSCRIBERS)
        ]
        await asyncio.sleep(0.1)

        # Worker fires _N_EVENTS snapshots
        for i in range(_N_EVENTS):
            await worker.send(json.dumps(snapshot_msg(f"$ fanout-{i}")))

        responses = await asyncio.gather(*[asyncio.wait_for(t, timeout=12.0) for t in poll_tasks])

    for idx, response in enumerate(responses):
        assert response.status_code == 200, f"subscriber {idx} got {response.status_code}"
        body = response.json()
        assert len(body["events"]) == _N_EVENTS, (
            f"subscriber {idx} expected {_N_EVENTS} events, got {len(body['events'])}"
        )
        assert body["timed_out"] is False
        assert body["dropped_count"] == 0


# ---------------------------------------------------------------------------
# 2. Worker reconnect — new subscription works after reconnect
# ---------------------------------------------------------------------------


async def test_worker_reconnect_new_subscription_works(live_server: Any) -> None:
    """Worker disconnects; after reconnect, a new long-poll subscription receives events."""
    hub, base_url = live_server

    # First worker connects and disconnects
    async with connect_async_ws(ws_url(base_url, "/ws/worker/obs1/term")) as worker:
        await worker.recv()
    # worker WS is now closed

    # Second worker connects
    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/obs1/term")) as worker2,
        httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=15.0) as http,
    ):
        await worker2.recv()  # snapshot_req

        poll_task = asyncio.create_task(
            http.get(
                "/api/sessions/obs1/events/watch",
                params={"timeout_ms": 5000, "max_events": 1, "event_types": "snapshot"},
            )
        )
        await asyncio.sleep(0.1)

        await worker2.send(json.dumps(snapshot_msg("$ after reconnect")))
        response = await asyncio.wait_for(poll_task, timeout=8.0)

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "snapshot"
    assert body["timed_out"] is False
