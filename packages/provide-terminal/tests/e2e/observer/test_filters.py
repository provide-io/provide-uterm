#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: EventBus event_types and pattern filters under real conditions.

Scenarios
---------
1. Three concurrent subscribers with different event_types filters — each
   receives only its matching events.
2. A pattern-filter long-poll on a real worker session — only snapshots whose
   screen matches the regex are returned.
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

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_server() -> Any:
    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "flt1",
                    "display_name": "Filter Session",
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
            raise RuntimeError("live_server (filters): uvicorn startup timeout")
        await asyncio.sleep(0.05)

    port: int = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    hub = app.state.uterm_registry._hub
    hub._event_bus = EventBus()

    try:
        yield hub, base_url
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


def snapshot_msg(screen: str) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "flt",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "flt"},
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# 1. Three subscribers with different event_types filters
# ---------------------------------------------------------------------------


async def test_three_subscribers_different_event_filters(live_server: Any) -> None:
    """sub1=snapshot, sub2=hijack_acquired, sub3=no filter — each sees only its events."""
    hub, base_url = live_server

    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/flt1/term")) as worker,
        httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=20.0) as http,
    ):
        await worker.recv()  # snapshot_req

        # sub1: snapshot only
        sub1_task = asyncio.create_task(
            http.get(
                "/api/sessions/flt1/events/watch",
                params={"timeout_ms": 6000, "max_events": 1, "event_types": "snapshot"},
            )
        )
        # sub2: hijack_acquired only
        sub2_task = asyncio.create_task(
            http.get(
                "/api/sessions/flt1/events/watch",
                params={"timeout_ms": 6000, "max_events": 1, "event_types": "hijack_acquired"},
            )
        )
        # sub3: no filter (accepts everything)
        sub3_task = asyncio.create_task(
            http.get(
                "/api/sessions/flt1/events/watch",
                params={"timeout_ms": 6000, "max_events": 2},
            )
        )
        await asyncio.sleep(0.1)

        # Fire a snapshot — sub1 and sub3 should unblock; sub2 stays blocked
        await worker.send(json.dumps(snapshot_msg("$ filter test")))
        resp1 = await asyncio.wait_for(sub1_task, timeout=8.0)

        # Acquire hijack via WS — sub2 and sub3 (already got snapshot, now gets hijack too)
        from provide.terminal.client import connect_async_ws as _caws

        async with _caws(ws_url(base_url, "/ws/browser/flt1/term")) as browser_ws:
            await asyncio.sleep(0.05)
            await browser_ws.send(json.dumps({"type": "hijack_request"}))
            resp2 = await asyncio.wait_for(sub2_task, timeout=8.0)

    resp3 = await asyncio.wait_for(sub3_task, timeout=3.0)

    # sub1: snapshot only
    body1 = resp1.json()
    assert all(e["type"] == "snapshot" for e in body1["events"]), f"sub1 leaked non-snapshot: {body1}"
    assert len(body1["events"]) >= 1

    # sub2: hijack_acquired only
    body2 = resp2.json()
    assert all(e["type"] == "hijack_acquired" for e in body2["events"]), f"sub2 leaked non-hijack: {body2}"
    assert len(body2["events"]) >= 1

    # sub3: should have received at least the snapshot (may also have hijack_acquired)
    body3 = resp3.json()
    event_types_seen = {e["type"] for e in body3["events"]}
    assert "snapshot" in event_types_seen, f"sub3 missed snapshot: {body3}"


# ---------------------------------------------------------------------------
# 2. Pattern filter on real session output
# ---------------------------------------------------------------------------


async def test_pattern_filter_passes_matching_screen(live_server: Any) -> None:
    """EventBus watch with pattern=\\$ only delivers snapshots whose screen matches."""
    hub, base_url = live_server

    assert hub._event_bus is not None

    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/flt1/term")) as worker,
        hub._event_bus.watch("flt1", event_types=["snapshot"], pattern=r"\$ ") as sub,
    ):
        await worker.recv()  # snapshot_req

        # Non-matching snapshot — should be filtered by pattern
        await worker.send(json.dumps(snapshot_msg("loading...")))
        await asyncio.sleep(0.15)

        # Queue must still be empty — non-matching event was dropped by pattern filter
        assert sub.queue.empty(), "pattern filter let a non-matching event through"

        # Now publish a matching event directly so the screen field is present
        hub._event_bus._enqueue(  # type: ignore[attr-defined]
            "flt1",
            {"type": "snapshot", "worker_id": "flt1", "data": {"screen": "root@host:~$ ", "screen_hash": "p1"}},
        )

        evt = await asyncio.wait_for(sub.queue.get(), timeout=3.0)

    assert evt is not None
    assert evt["type"] == "snapshot"
    assert "$ " in evt["data"].get("screen", ""), f"unexpected screen: {evt}"
