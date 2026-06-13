#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: Fleet-scale multi-session scenarios.

Scenarios
---------
8. Three sessions with 10 concurrent snapshots each — EventBus isolation holds
   under load with zero cross-contamination.
9. Ephemeral session survives browser disconnect and reconnect within the grace
   period.
10. Broadcast storm across 3 sessions with 15 browsers; abrupt socket closures
    mid-flight don't poison surviving browsers or leak across sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
from provide.uterm.client import connect_async_ws

from .._live_server import live_server_with_bus  # noqa: TID252
from ..conftest import _drain_all, _snapshot_msg  # noqa: TID252
from .conftest import snapshot_msg, ws_url

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

THREE_SESSIONS = [
    {"session_id": "s1", "display_name": "Fleet S1", "connector_type": "shell", "auto_start": False},
    {"session_id": "s2", "display_name": "Fleet S2", "connector_type": "shell", "auto_start": False},
    {"session_id": "s3", "display_name": "Fleet S3", "connector_type": "shell", "auto_start": False},
]


# ---------------------------------------------------------------------------
# 8. EventBus isolation under load — 3 sessions × 10 snapshots
# ---------------------------------------------------------------------------


async def test_cross_session_eventbus_isolation_under_load() -> None:
    """3 sessions x 10 concurrent snapshots -- each subscriber gets exactly its own events."""
    async with (
        live_server_with_bus(THREE_SESSIONS, label="fleet_isolation") as (hub, base_url),
        connect_async_ws(ws_url(base_url, "/ws/worker/s1/term")) as w1,
        connect_async_ws(ws_url(base_url, "/ws/worker/s2/term")) as w2,
        connect_async_ws(ws_url(base_url, "/ws/worker/s3/term")) as w3,
    ):
        # Drain initial snapshot_req messages
        await w1.recv()
        await w2.recv()
        await w3.recv()

        event_bus = hub._event_bus
        assert event_bus is not None

        async with (
            event_bus.watch("s1") as sub1,
            event_bus.watch("s2") as sub2,
            event_bus.watch("s3") as sub3,
        ):
            # All 3 workers send 10 snapshots concurrently
            async def _send_batch(ws: Any, sid: str) -> None:
                for i in range(10):
                    await ws.send_json(snapshot_msg(f"$ {sid}-snap-{i}"))

            await asyncio.gather(
                _send_batch(w1, "s1"),
                _send_batch(w2, "s2"),
                _send_batch(w3, "s3"),
            )

            # Drain each subscriber with a timeout
            async def _collect(sub: Any, count: int, timeout: float = 5.0) -> list[dict[str, Any]]:
                events: list[dict[str, Any]] = []
                deadline = asyncio.get_running_loop().time() + timeout
                while len(events) < count and asyncio.get_running_loop().time() < deadline:
                    try:
                        ev = await asyncio.wait_for(sub.queue.get(), timeout=0.5)
                        events.append(ev)
                    except TimeoutError:
                        continue
                return events

            events_s1 = await _collect(sub1, 10)
            events_s2 = await _collect(sub2, 10)
            events_s3 = await _collect(sub3, 10)

        # Each subscriber got exactly 10 events
        assert len(events_s1) == 10, f"s1 expected 10, got {len(events_s1)}"
        assert len(events_s2) == 10, f"s2 expected 10, got {len(events_s2)}"
        assert len(events_s3) == 10, f"s3 expected 10, got {len(events_s3)}"

        # Every event has the correct worker_id — zero cross-contamination
        for ev in events_s1:
            assert ev["worker_id"] == "s1", f"s1 subscriber got event from {ev['worker_id']}"
        for ev in events_s2:
            assert ev["worker_id"] == "s2", f"s2 subscriber got event from {ev['worker_id']}"
        for ev in events_s3:
            assert ev["worker_id"] == "s3", f"s3 subscriber got event from {ev['worker_id']}"


# ---------------------------------------------------------------------------
# 9. Ephemeral session reconnect during grace period
# ---------------------------------------------------------------------------


async def test_ephemeral_session_reconnect_during_grace_period() -> None:
    """Session survives browser disconnect and reconnect within the grace period."""
    sessions = [
        {"session_id": "eph1", "display_name": "Ephemeral", "connector_type": "shell", "auto_start": True},
    ]
    async with live_server_with_bus(sessions, label="ephemeral_reconnect") as (hub, base_url):
        # Wait for the shell worker to auto-connect (poll /api/sessions until eph1 is running)
        async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
            deadline = asyncio.get_running_loop().time() + 10.0
            while asyncio.get_running_loop().time() < deadline:
                resp = await http.get("/api/sessions")
                if resp.status_code == 200:
                    body = resp.json()
                    sessions_data = body if isinstance(body, list) else body.get("sessions", [])
                    for s in sessions_data:
                        sid = s.get("session_id", s.get("id", ""))
                        if sid == "eph1" and s.get("status") in ("running", "connected", "active"):
                            break
                    else:
                        await asyncio.sleep(0.2)
                        continue
                    break
                await asyncio.sleep(0.2)

        # Connect a browser to the session via WS
        async with connect_async_ws(ws_url(base_url, "/ws/browser/eph1/term")) as browser1:
            # Drain any initial messages
            await _drain_all(browser1, timeout=1.0)

        # browser1 is now disconnected (exited context manager)
        # After 1 second (within grace period), connect a new browser
        await asyncio.sleep(1.0)

        async with connect_async_ws(ws_url(base_url, "/ws/browser/eph1/term")) as browser2:
            # Wait 5 more seconds to verify session persists
            await asyncio.sleep(5.0)

            # Verify session still exists via REST
            async with httpx.AsyncClient(base_url=base_url, headers=ADMIN_H, timeout=10.0) as http:
                resp = await http.get("/api/sessions/eph1")
                # Session should still be accessible (200 or similar)
                assert resp.status_code == 200, f"Session eph1 disappeared after reconnect: status={resp.status_code}"

            await _drain_all(browser2, timeout=0.5)


# ---------------------------------------------------------------------------
# 10. Broadcast storm with dead socket cleanup
# ---------------------------------------------------------------------------


async def test_fleet_broadcast_storm_with_dead_socket_cleanup() -> None:
    """15 browsers across 3 sessions; abrupt close mid-storm doesn't leak or cross-contaminate."""
    bs_sessions = [
        {"session_id": "bs1", "display_name": "Storm BS1", "connector_type": "shell", "auto_start": False},
        {"session_id": "bs2", "display_name": "Storm BS2", "connector_type": "shell", "auto_start": False},
        {"session_id": "bs3", "display_name": "Storm BS3", "connector_type": "shell", "auto_start": False},
    ]
    async with (
        live_server_with_bus(bs_sessions, label="broadcast_storm") as (hub, base_url),
        connect_async_ws(ws_url(base_url, "/ws/worker/bs1/term")) as w1,
        connect_async_ws(ws_url(base_url, "/ws/worker/bs2/term")) as w2,
        connect_async_ws(ws_url(base_url, "/ws/worker/bs3/term")) as w3,
    ):
        await w1.recv()
        await w2.recv()
        await w3.recv()

        # Connect 5 browsers per session (15 total)
        browsers: dict[str, list[Any]] = {"bs1": [], "bs2": [], "bs3": []}
        ws_contexts: list[Any] = []

        for sid in ("bs1", "bs2", "bs3"):
            for _ in range(5):
                ctx = connect_async_ws(ws_url(base_url, f"/ws/browser/{sid}/term"))
                ws = await ctx.__aenter__()
                ws_contexts.append((ctx, ws))
                browsers[sid].append(ws)

        try:
            # Drain initial messages from all browsers
            for sid in browsers:
                for b in browsers[sid]:
                    await _drain_all(b, timeout=0.5)

            # First batch: each worker sends 5 snapshots concurrently
            async def _send_snapshots(ws: Any, sid: str, batch: int, count: int = 5) -> None:
                for i in range(count):
                    await ws.send_json(_snapshot_msg(f"{sid}-screen-batch{batch}-{i}"))

            await asyncio.gather(
                _send_snapshots(w1, "bs1", 1),
                _send_snapshots(w2, "bs2", 1),
                _send_snapshots(w3, "bs3", 1),
            )

            # While snapshots may still be propagating, abruptly close 1 browser per session
            killed: list[Any] = []
            for sid in ("bs1", "bs2", "bs3"):
                victim = browsers[sid].pop(0)  # remove first browser
                killed.append(victim)
                await victim.close()

            # Give the server a moment to process the disconnections
            await asyncio.sleep(0.5)

            # Second batch: 5 more snapshots per worker
            await asyncio.gather(
                _send_snapshots(w1, "bs1", 2),
                _send_snapshots(w2, "bs2", 2),
                _send_snapshots(w3, "bs3", 2),
            )

            # Drain all 12 surviving browsers (4 per session)
            results: dict[str, list[list[dict[str, Any]]]] = {"bs1": [], "bs2": [], "bs3": []}
            for sid in ("bs1", "bs2", "bs3"):
                for b in browsers[sid]:
                    msgs = await _drain_all(b, timeout=3.0)
                    results[sid].append(msgs)

            # Assert: all 12 surviving browsers received at least the second batch of 5 snapshots
            for sid in ("bs1", "bs2", "bs3"):
                assert len(browsers[sid]) == 4, f"{sid} should have 4 surviving browsers"
                for idx, msgs in enumerate(results[sid]):
                    snapshot_msgs = [m for m in msgs if m.get("type") == "snapshot"]
                    batch2_msgs = [m for m in snapshot_msgs if f"{sid}-screen-batch2" in m.get("screen", "")]
                    assert len(batch2_msgs) >= 5, (
                        f"{sid} browser[{idx}] got {len(batch2_msgs)} batch-2 snapshots, expected >= 5"
                    )

                    # No browser in session X received a snapshot from session Y
                    for m in snapshot_msgs:
                        screen = m.get("screen", "")
                        for other_sid in ("bs1", "bs2", "bs3"):
                            if other_sid != sid:
                                assert f"{other_sid}-screen-" not in screen, (
                                    f"{sid} browser received snapshot from {other_sid}: {screen}"
                                )

        finally:
            # Clean up all remaining browser connections
            for ctx, _ws in ws_contexts:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)
