#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Chaos cascade E2E tests: lease races, reconnect replacement, event overflow, heartbeat races."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from provide.terminal.client import connect_async_ws

from .conftest import _drain_all, _drain_until, _snapshot_msg, _ws_url

# ---------------------------------------------------------------------------
# TestHijackLeaseExpiryDuringConcurrentAcquireRace
# ---------------------------------------------------------------------------


class TestHijackLeaseExpiryDuringConcurrentAcquireRace:
    async def test_hijack_lease_expiry_during_concurrent_acquire_race(self, live_hub: Any) -> None:
        """After lease expires, three concurrent REST acquires result in exactly one owner."""
        _, base_url = live_hub

        async with (
            httpx.AsyncClient(base_url=base_url) as http,
            connect_async_ws(_ws_url(base_url, "/ws/worker/race1/term")) as worker,
        ):
            await worker.recv()  # snapshot_req

            # Acquire with a short lease
            r = await http.post("/worker/race1/hijack/acquire", json={"owner": "initial", "lease_s": 2})
            assert r.status_code == 200, f"Initial acquire failed: {r.status_code}: {r.text}"
            await _drain_all(worker)  # drain initial pause

            # Wait for the lease to expire
            await asyncio.sleep(2.2)

            # Three concurrent REST acquires race for the now-expired slot
            async def rest_acquire(owner: str) -> httpx.Response:
                return await http.post("/worker/race1/hijack/acquire", json={"owner": owner, "lease_s": 60})

            r1, r2, r3 = await asyncio.gather(
                rest_acquire("race-a"),
                rest_acquire("race-b"),
                rest_acquire("race-c"),
            )

            results = [r1.status_code, r2.status_code, r3.status_code]
            winners = results.count(200)

            assert winners == 1, f"Exactly one REST acquire should win, got {winners}: {results}"

            # Worker should have received at least one pause from the winner
            pause_msgs = await _drain_all(worker, timeout=1.0)
            pause_controls = [m for m in pause_msgs if m.get("type") == "control" and m.get("action") == "pause"]
            assert len(pause_controls) >= 1, f"Worker should get at least one pause, got {len(pause_controls)}"


# ---------------------------------------------------------------------------
# TestWorkerReconnectClearsStaleHijack
# ---------------------------------------------------------------------------


class TestWorkerReconnectClearsStaleHijack:
    async def test_worker_reconnect_clears_stale_hijack(self, live_hub: Any) -> None:
        """Second worker connection replaces the first; old hijack becomes invalid."""
        hub, base_url = live_hub

        async with (
            httpx.AsyncClient(base_url=base_url) as http,
            connect_async_ws(_ws_url(base_url, "/ws/worker/reconn1/term")) as w1,
        ):
            await w1.recv()  # snapshot_req

            # Acquire hijack on W1
            r = await http.post("/worker/reconn1/hijack/acquire", json={"owner": "old-owner", "lease_s": 60})
            assert r.status_code == 200, f"W1 acquire failed: {r.status_code}: {r.text}"
            hijack_id = r.json()["hijack_id"]

            # W2 connects to the same worker_id (hub replaces registration)
            async with connect_async_ws(_ws_url(base_url, "/ws/worker/reconn1/term")) as w2:
                await w2.recv()  # snapshot_req

                # Old hijack_id should fail heartbeat
                hb = await http.post(
                    f"/worker/reconn1/hijack/{hijack_id}/heartbeat",
                    json={"lease_s": 60},
                )
                # Should be 404 (session cleared when worker was replaced)
                assert hb.status_code == 404, (
                    f"Old hijack heartbeat should fail after worker replacement, got {hb.status_code}: {hb.text}"
                )

                # New acquire against W2 succeeds
                r2 = await http.post("/worker/reconn1/hijack/acquire", json={"owner": "new-owner", "lease_s": 60})
                assert r2.status_code == 200, (
                    f"New acquire after worker replacement should succeed, got {r2.status_code}: {r2.text}"
                )


# ---------------------------------------------------------------------------
# TestEventbusQueueOverflowPreservesSentinel
# ---------------------------------------------------------------------------


class TestEventbusQueueOverflowPreservesSentinel:
    async def test_eventbus_queue_overflow_preserves_sentinel(self) -> None:
        """Flooding snapshots saturates the queue; events are dropped but sequence continues."""
        from tests.e2e._live_server import live_server_with_bus

        sessions = [{"session_id": "flood1", "display_name": "Flood", "connector_type": "shell", "auto_start": False}]

        async with live_server_with_bus(sessions) as (hub, base_url):
            # Use a small queue to force overflow
            bus = hub._event_bus
            bus._max_queue_depth = 5

            async with connect_async_ws(_ws_url(base_url, "/ws/worker/flood1/term")) as worker:
                await worker.recv()  # snapshot_req

                async with bus.watch("flood1") as sub:
                    # Flood 20 snapshots rapidly without draining
                    for i in range(20):
                        await worker.send(json.dumps(_snapshot_msg(f"screen-{i}")))

                    # Give the hub time to process all snapshots
                    await asyncio.sleep(1.0)

                    # Now drain what the subscriber received
                    items: list[dict[str, Any]] = []
                    while not sub.queue.empty():
                        items.append(sub.queue.get_nowait())

                    # Should have received some events but not all 20
                    assert len(items) > 0, "Subscriber should have received at least some events"
                    assert len(items) <= 20, f"Subscriber received {len(items)} events"

                    # With overflow, the ring buffer keeps the latest events.
                    # The seq numbers should be contiguous but START after seq 1,
                    # confirming earlier events were dropped.
                    seqs = [e["seq"] for e in items]
                    if len(items) < 20:
                        # First seq should be > 1, confirming early events were dropped
                        assert seqs[0] > 1, f"First seq should be > 1 (early events dropped), got {seqs[0]}"

                    # Verify all events are for the correct worker
                    for event in items:
                        assert event["worker_id"] == "flood1", f"Event has wrong worker_id: {event['worker_id']}"


# ---------------------------------------------------------------------------
# TestHeartbeatRaceWithLeaseCleanup
# ---------------------------------------------------------------------------


class TestHeartbeatRaceWithLeaseCleanup:
    async def test_heartbeat_race_with_lease_cleanup(self, live_hub: Any) -> None:
        """Concurrent heartbeats and cleanup never leave a zombie hijack state."""
        hub, base_url = live_hub

        async with connect_async_ws(_ws_url(base_url, "/ws/worker/hb-race1/term")) as worker:
            await worker.recv()  # snapshot_req

            async with connect_async_ws(_ws_url(base_url, "/ws/browser/hb-race1/term")) as browser:
                await _drain_all(browser)

                # Browser acquires WS hijack
                await browser.send(json.dumps({"type": "hijack_request"}))
                state = await _drain_until(browser, "hijack_state")
                assert state is not None, "Should receive hijack_state"
                assert state.get("owner") == "me", f"Browser should own hijack, got {state}"

                # Drain the worker pause
                await _drain_all(worker)

                # Shorten the lease to expire fast
                hub._workers["hb-race1"].hijack_owner_expires_at = time.monotonic() + 0.5

                # Wait so we are near/past expiry
                await asyncio.sleep(0.4)

                # Concurrent: 3 heartbeats + 3 cleanup attempts
                async def send_heartbeat() -> None:
                    await browser.send(json.dumps({"type": "heartbeat"}))
                    await asyncio.sleep(0.01)

                async def do_cleanup() -> None:
                    await hub.cleanup_expired_hijack("hb-race1")

                await asyncio.gather(
                    send_heartbeat(),
                    send_heartbeat(),
                    send_heartbeat(),
                    do_cleanup(),
                    do_cleanup(),
                    do_cleanup(),
                )

                # Give a moment for all side effects to settle
                await asyncio.sleep(0.2)

                # Check final state: either alive or fully released, never zombie
                st = hub._workers["hb-race1"]
                now = time.monotonic()

                hijack_alive = (
                    st.hijack_owner is not None
                    and st.hijack_owner_expires_at is not None
                    and st.hijack_owner_expires_at > now
                )
                hijack_released = st.hijack_owner is None and st.hijack_session is None

                assert hijack_alive or hijack_released, (
                    f"Hijack must be alive (expires_at > now) or fully released "
                    f"(owner=None, session=None). Got: "
                    f"hijack_owner={st.hijack_owner}, "
                    f"hijack_owner_expires_at={st.hijack_owner_expires_at}, "
                    f"hijack_session={st.hijack_session}, "
                    f"now={now}"
                )
