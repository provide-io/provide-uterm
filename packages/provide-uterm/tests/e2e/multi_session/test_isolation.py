#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: Two concurrent sessions with isolated EventBus subscribers.

Scenarios
---------
1. Sessions s1 and s2 each have their own WS worker sending snapshots.
   EventBus subscribers for each session receive only their session's events —
   no cross-contamination.
2. Two long-poll callers blocking on s1 and s2 simultaneously both unblock
   with exactly the right event when their respective worker fires.
"""

from __future__ import annotations

import asyncio
from typing import Any

from provide.uterm.client import connect_async_ws

from tests.e2e._live_server import wait_for_subscribers

from .conftest import (
    snapshot_msg,
    watch_events,
    ws_url,
)

# ---------------------------------------------------------------------------
# 1. EventBus subscriber per session — no cross-contamination
# ---------------------------------------------------------------------------


async def test_two_sessions_eventbus_isolated(two_session_server: Any) -> None:
    """s1 events reach s1 subscriber; s2 events reach s2 subscriber; no cross-talk."""
    hub, base_url = two_session_server

    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as w1,
        connect_async_ws(ws_url(base_url, "/ws/worker/s2/term")) as w2,
    ):
        await w1.recv()  # snapshot_req for s1
        await w2.recv()  # snapshot_req for s2

        # Subscribe to each session independently
        event_bus = hub.event_bus
        assert event_bus is not None
        async with (
            event_bus.watch("s1") as sub1,
            event_bus.watch("s2") as sub2,
        ):
            # s1 worker fires; s2 worker stays silent
            await w1.send_json(snapshot_msg("$ s1 event", "s1"))
            s1_event = await asyncio.wait_for(sub1.queue.get(), timeout=3.0)
            assert s1_event is not None
            assert s1_event["worker_id"] == "s1"
            assert s1_event["data"].get("screen_hash") == "hash-s1"

            # s2 queue must remain empty
            assert sub2.queue.empty(), "s2 subscriber received an s1 event — isolation broken"

            # Now s2 fires
            await w2.send_json(snapshot_msg("$ s2 event", "s2"))
            s2_event = await asyncio.wait_for(sub2.queue.get(), timeout=3.0)
            assert s2_event is not None
            assert s2_event["worker_id"] == "s2"
            assert s2_event["data"].get("screen_hash") == "hash-s2"

            # s1 queue must still be empty
            assert sub1.queue.empty(), "s1 subscriber received an s2 event — isolation broken"


# ---------------------------------------------------------------------------
# 2. Two concurrent long-polls — each unblocks from the right session
# ---------------------------------------------------------------------------


async def test_two_sessions_concurrent_long_polls(two_session_server: Any) -> None:
    """Two long-poll callers block concurrently; each unblocks from its own session only."""
    hub, base_url = two_session_server

    async with (
        connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as w1,
        connect_async_ws(ws_url(base_url, "/ws/worker/s2/term")) as w2,
    ):
        await w1.recv()
        await w2.recv()

        # Launch both long-polls simultaneously
        poll1 = asyncio.create_task(watch_events(base_url, "s1", timeout_ms=6000, max_events=1))
        poll2 = asyncio.create_task(watch_events(base_url, "s2", timeout_ms=6000, max_events=1))

        # Wait for the registrations, rather than guessing at how long they take.
        # A long-poll subscribes from inside its request handler, so an event
        # fired before that lands is missed and its caller returns zero events.
        await wait_for_subscribers(hub, "s1", 1)
        await wait_for_subscribers(hub, "s2", 1)

        # Both events fired BEFORE either poll is awaited. Firing s2, awaiting
        # poll2, then firing s1 left poll1's 6s window having to outlast poll2's
        # entire round trip; on a loaded runner it did not, and poll1 came back
        # empty -- `assert 0 == 1` on quality (3.11) in run 33563719610.
        # Widening the window would only move the threshold, and the dependency
        # between the two polls is what is actually wrong.
        #
        # Reverse order is kept, and the routing check is stronger for it: both
        # events are in flight at once, so a misrouted s2 event has a live poll1
        # to land in rather than one that has already returned.
        await w2.send_json(snapshot_msg("$ s2 concurrent", "s2"))
        await w1.send_json(snapshot_msg("$ s1 concurrent", "s1"))

        resp2 = await asyncio.wait_for(poll2, timeout=15.0)
        resp1 = await asyncio.wait_for(poll1, timeout=15.0)

    assert resp1.status_code == 200
    body1 = resp1.json()
    assert len(body1["events"]) == 1
    assert body1["events"][0]["worker_id"] == "s1"
    assert body1["timed_out"] is False

    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["events"]) == 1
    assert body2["events"][0]["worker_id"] == "s2"
    assert body2["timed_out"] is False
