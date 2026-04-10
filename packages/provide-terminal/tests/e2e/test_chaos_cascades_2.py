#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Chaos cascade E2E tests round 2: cascading failures and protocol edge cases.

Scenarios
---------
14. Worker crash mid-snapshot-flood with active hijack + EventBus subscriber.
15. Lease cleanup during browser reconnect with competing hijack.
16. Malformed control frames mixed with valid ones.
17. Binary-like data through hijack input path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import websockets

from provide.terminal.client import connect_async_ws
from provide.terminal.control_channel import DLE, STX, encode_control

from .conftest import _drain_all, _drain_until, _snapshot_msg, _ws_url

# ---------------------------------------------------------------------------
# 14. Worker crash mid-snapshot-flood with active hijack + EventBus
# ---------------------------------------------------------------------------


async def test_worker_crash_mid_snapshot_flood_with_active_hijack() -> None:
    """Worker floods snapshots while browser holds hijack; worker crash cleans up everything."""
    from tests.e2e._live_server import live_server_with_bus

    sessions = [
        {"session_id": "flood-crash", "display_name": "Flood Crash", "connector_type": "shell", "auto_start": False},
    ]
    async with live_server_with_bus(sessions, label="flood_crash") as (hub, base_url):
        event_bus = hub._event_bus

        async with connect_async_ws(_ws_url(base_url, "/ws/worker/flood-crash/term")) as worker:
            await worker.recv()  # snapshot_req

            async with connect_async_ws(_ws_url(base_url, "/ws/browser/flood-crash/term")) as browser:
                await _drain_all(browser)

                # Browser acquires hijack
                await browser.send(json.dumps({"type": "hijack_request"}))
                hijack_msg = await _drain_until(browser, "hijack_state", timeout=3.0)
                assert hijack_msg is not None, "Browser should receive hijack_state"
                await _drain_all(worker)  # drain pause

                async with event_bus.watch("flood-crash") as sub:
                    # Worker floods 50 snapshots rapidly
                    for i in range(50):
                        await worker.send(json.dumps(_snapshot_msg(f"flood-{i}")))

                    # Give some snapshots time to propagate
                    await asyncio.sleep(0.3)

                    # Forcibly close worker WS
                    await worker.close()

                # Worker is now disconnected — give hub time to process
                await asyncio.sleep(0.5)

                # Browser should receive worker_disconnected
                browser_msgs = await _drain_all(browser, timeout=2.0)
                disconnected = any(m.get("type") == "worker_disconnected" for m in browser_msgs)
                assert disconnected, f"Browser should get worker_disconnected: {browser_msgs}"

                # Hijack must be cleared
                st = hub._workers.get("flood-crash")
                if st is not None:
                    assert st.hijack_owner is None, f"Hijack owner should be None, got {st.hijack_owner}"

                # EventBus subscriber should have received some events
                items: list[dict[str, Any]] = []
                while not sub.queue.empty():
                    item = sub.queue.get_nowait()
                    if item is None:
                        break
                    items.append(item)
                assert len(items) >= 1, "EventBus subscriber should have received ≥1 snapshot"


# ---------------------------------------------------------------------------
# 15. Lease cleanup during browser reconnect with competing hijack
# ---------------------------------------------------------------------------


async def test_lease_cleanup_during_reconnect_with_competing_hijack(live_hub: Any) -> None:
    """Lease expires during browser reconnect race with competing hijack_request."""
    hub, base_url = live_hub

    async with connect_async_ws(_ws_url(base_url, "/ws/worker/lease-race/term")) as worker:
        await worker.recv()  # snapshot_req

        # B1 acquires hijack
        async with connect_async_ws(_ws_url(base_url, "/ws/browser/lease-race/term")) as b1:
            await _drain_all(b1)
            await b1.send(json.dumps({"type": "hijack_request"}))
            state = await _drain_until(b1, "hijack_state", timeout=3.0)
            assert state is not None
            await _drain_all(worker)  # drain pause

            # Shorten lease to expire fast
            st = hub._workers["lease-race"]
            st.hijack_owner_expires_at = time.monotonic() + 0.3

        # B1 disconnected — lease still ticking
        await asyncio.sleep(0.2)

        # Concurrently: B2 tries hijack while cleanup may fire
        async with connect_async_ws(_ws_url(base_url, "/ws/browser/lease-race/term")) as b2:
            await _drain_all(b2)

            # Force cleanup + B2 hijack request simultaneously
            async def do_cleanup() -> None:
                await asyncio.sleep(0.15)  # Let lease expire
                if hasattr(hub, "cleanup_expired_hijack"):
                    await hub.cleanup_expired_hijack("lease-race")

            await asyncio.gather(
                do_cleanup(),
                b2.send(json.dumps({"type": "hijack_request"})),
            )

            await asyncio.sleep(0.5)
            await _drain_all(b2, timeout=1.0)

            # Check final state: at most one owner, no zombie
            st = hub._workers.get("lease-race")
            if st is not None:
                now = time.monotonic()
                if st.hijack_owner is not None:
                    assert st.hijack_owner_expires_at is not None and st.hijack_owner_expires_at > now, (
                        f"Zombie hijack: owner={st.hijack_owner}, expires_at={st.hijack_owner_expires_at}, now={now}"
                    )


# ---------------------------------------------------------------------------
# 16. Malformed control frames mixed with valid ones
# ---------------------------------------------------------------------------


async def test_malformed_control_frames_close_worker_cleanly(live_hub: Any) -> None:
    """Malformed DLE STX frame causes hub to close worker WS with code 1003."""
    hub, base_url = live_hub
    ws_base = base_url.replace("http://", "ws://")

    # Connect browser first (using control-channel-aware client)
    async with connect_async_ws(f"{ws_base}/ws/browser/malform/term") as browser:
        await _drain_all(browser)

        # Worker uses raw websockets (not connect_async_ws) to send malformed frames
        async with websockets.connect(f"{ws_base}/ws/worker/malform/term") as raw_worker:
            # First: send a valid snapshot via raw control channel encoding
            valid_snapshot = encode_control(_snapshot_msg("valid-first"))
            await raw_worker.send(valid_snapshot)
            await asyncio.sleep(0.3)

            # Browser should have received the valid snapshot
            browser_msgs = await _drain_all(browser, timeout=1.0)
            got_snapshot = any(
                m.get("type") == "snapshot" and "valid-first" in m.get("screen", "")
                for m in browser_msgs
            )
            assert got_snapshot, f"Browser should receive valid snapshot: {browser_msgs}"

            # Now send a malformed frame: DLE STX + garbage hex length
            malformed = f"{DLE}{STX}ZZZZZZZZ:not-a-json"
            await raw_worker.send(malformed)

            # Worker WS should be closed by hub
            with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(raw_worker.recv(), timeout=5.0)

        # Hub should remain healthy — new worker can connect
        await asyncio.sleep(0.3)
        async with connect_async_ws(f"{ws_base}/ws/worker/malform/term") as new_worker:
            await new_worker.recv()  # snapshot_req

            # New worker sends valid snapshot
            await new_worker.send(json.dumps(_snapshot_msg("valid-second")))
            await asyncio.sleep(0.3)

            browser_msgs2 = await _drain_all(browser, timeout=1.0)
            got_second = any(
                m.get("type") == "snapshot" and "valid-second" in m.get("screen", "")
                for m in browser_msgs2
            )
            assert got_second, f"Browser should receive second valid snapshot: {browser_msgs2}"


# ---------------------------------------------------------------------------
# 17. Binary-like data through hijack input path
# ---------------------------------------------------------------------------


async def test_binary_like_data_through_hijack_input(live_hub: Any) -> None:
    """Exotic characters in hijack input are delivered intact to worker."""
    hub, base_url = live_hub

    async with connect_async_ws(_ws_url(base_url, "/ws/worker/bindata/term")) as worker:
        await worker.recv()  # snapshot_req

        async with connect_async_ws(_ws_url(base_url, "/ws/browser/bindata/term")) as browser:
            await _drain_all(browser)

            # Acquire hijack
            await browser.send(json.dumps({"type": "hijack_request"}))
            await _drain_until(browser, "hijack_state", timeout=3.0)
            await _drain_all(worker)  # drain pause

            # Send exotic input strings
            test_inputs = [
                "hello\x1b[31mred\x1b[0m",         # ANSI escape sequences
                "\u2603\u2764\U0001F600",            # Snowman, heart, emoji
                "\u4e16\u754c",                      # CJK characters (世界)
                f"dle{DLE}embedded",                 # Embedded DLE character
                "tabs\there\nnewlines",              # Whitespace chars
            ]

            for inp in test_inputs:
                await browser.send(json.dumps({"type": "input", "data": inp}))
                await asyncio.sleep(0.05)

            # Drain worker and check all inputs arrived
            await asyncio.sleep(0.5)
            worker_msgs = await _drain_all(worker, timeout=1.0)

            # Collect all data from input/data messages
            received_data: list[str] = [str(m["data"]) for m in worker_msgs if "data" in m]

            for inp in test_inputs:
                found = any(inp in d for d in received_data)
                assert found, f"Worker missing input {inp!r}: received={received_data}"
