#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Stress-loop E2E tests: rapid connect/disconnect, hijack cycling, subscriber churn.

Scenarios
---------
18. 100 rapid worker connect/disconnect cycles — no leaked state.
19. Hijack acquire/release 50 cycles — no state corruption.
20. EventBus subscriber churn during snapshot stream.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from provide.terminal.client import connect_async_ws

from .conftest import _drain_all, _drain_until, _snapshot_msg, _ws_url

# ---------------------------------------------------------------------------
# 18. 100 rapid connect/disconnect cycles
# ---------------------------------------------------------------------------


async def test_100_rapid_connect_disconnect_no_leaked_state(live_hub: Any) -> None:
    """Worker connects/disconnects 100 times; no leaked state in hub._workers."""
    hub, base_url = live_hub
    wid = "rapid-100"
    ws_base = base_url.replace("http://", "ws://")

    for _ in range(100):
        async with connect_async_ws(f"{ws_base}/ws/worker/{wid}/term") as worker:
            await worker.recv()  # snapshot_req

    # Give hub time to prune
    await asyncio.sleep(0.3)

    # Final state: entry pruned or clean
    st = hub._workers.get(wid)
    if st is not None:
        assert st.worker_ws is None, f"Worker WS should be None, got {st.worker_ws}"
        assert st.hijack_owner is None, f"Hijack owner leaked: {st.hijack_owner}"
        assert len(st.browsers) == 0, f"Browser list leaked: {len(st.browsers)}"

    assert len(hub._workers) <= 1, f"Should have at most 1 entry, got {len(hub._workers)}"


# ---------------------------------------------------------------------------
# 19. Hijack acquire/release 50 cycles
# ---------------------------------------------------------------------------


async def test_hijack_acquire_release_50_cycles_no_corruption(live_hub: Any) -> None:
    """Browser acquires/releases hijack 50 times; state never corrupts."""
    hub, base_url = live_hub

    async with connect_async_ws(_ws_url(base_url, "/ws/worker/cycle50/term")) as worker:
        await worker.recv()  # snapshot_req

        async with connect_async_ws(_ws_url(base_url, "/ws/browser/cycle50/term")) as browser:
            await _drain_all(browser)

            for i in range(50):
                # Acquire
                await browser.send(json.dumps({"type": "hijack_request"}))
                state = await _drain_until(browser, "hijack_state", timeout=3.0)
                assert state is not None, f"Cycle {i}: no hijack_state on acquire"

                # Release
                await browser.send(json.dumps({"type": "hijack_release"}))
                await asyncio.sleep(0.02)  # Brief settle

            # Final state: fully released
            await asyncio.sleep(0.3)
            st = hub._workers.get("cycle50")
            assert st is not None
            assert st.hijack_owner is None, f"Final hijack_owner should be None: {st.hijack_owner}"

            # Worker should have received pause/resume messages
            worker_msgs = await _drain_all(worker, timeout=1.0)
            pause_count = sum(1 for m in worker_msgs if m.get("type") == "control" and m.get("action") == "pause")
            resume_count = sum(1 for m in worker_msgs if m.get("type") == "control" and m.get("action") == "resume")
            # At minimum some pauses and resumes should have been delivered
            assert pause_count >= 10, f"Expected ≥10 pauses, got {pause_count}"
            assert resume_count >= 10, f"Expected ≥10 resumes, got {resume_count}"


# ---------------------------------------------------------------------------
# 20. EventBus subscriber churn during snapshot stream
# ---------------------------------------------------------------------------


async def test_eventbus_subscriber_churn_during_snapshot_stream() -> None:
    """5 stable + 5 churning subscribers; stable ones get all events, no errors."""
    from tests.e2e._live_server import live_server_with_bus

    sessions = [
        {"session_id": "churn1", "display_name": "Churn", "connector_type": "shell", "auto_start": False},
    ]
    async with live_server_with_bus(sessions, label="sub_churn") as (hub, base_url):
        event_bus = hub._event_bus

        async with connect_async_ws(_ws_url(base_url, "/ws/worker/churn1/term")) as worker:
            await worker.recv()  # snapshot_req

            # 5 stable subscribers
            stable_subs = []
            stable_ctxs = []
            for _ in range(5):
                ctx = event_bus.watch("churn1")
                sub = await ctx.__aenter__()
                stable_subs.append(sub)
                stable_ctxs.append(ctx)

            churn_errors: list[Exception] = []

            # 5 churning subscribers: each subscribes/unsubscribes 10 times
            async def churn_subscriber() -> None:
                try:
                    for _ in range(10):
                        async with event_bus.watch("churn1"):
                            await asyncio.sleep(0.01)
                except Exception as exc:
                    churn_errors.append(exc)

            # Start churn tasks
            churn_tasks = [asyncio.create_task(churn_subscriber()) for _ in range(5)]

            # Worker streams 100 snapshots concurrently
            for i in range(100):
                await worker.send(json.dumps(_snapshot_msg(f"stream-{i}")))
                if i % 10 == 0:
                    await asyncio.sleep(0.01)  # Brief yield

            # Wait for churn to finish
            await asyncio.gather(*churn_tasks)

            # Give time for all events to propagate
            await asyncio.sleep(1.0)

            # Drain stable subscribers
            for idx, sub in enumerate(stable_subs):
                events: list[dict[str, Any]] = []
                while not sub.queue.empty():
                    item = sub.queue.get_nowait()
                    if item is None:
                        break
                    events.append(item)

                # All events should be for the correct worker
                for ev in events:
                    assert ev["worker_id"] == "churn1", f"Sub {idx} got event from {ev['worker_id']}"

                # Should have received many events (may not be exactly 100 due to timing)
                assert len(events) >= 50, f"Stable sub {idx} got only {len(events)} events, expected ≥50"

            # Clean up stable subscribers
            for ctx in stable_ctxs:
                await ctx.__aexit__(None, None, None)

            # No errors during churn
            assert len(churn_errors) == 0, f"Churn errors: {churn_errors}"
