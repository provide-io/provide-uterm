#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Multi-session stress E2E tests: isolation, concurrent ops, ephemeral races.

Scenarios
---------
21. Cross-session routing strict isolation (2 sessions × 20 snapshots × 3 browsers).
22. 5 sessions concurrent worker disconnect with active browsers.
23. Ephemeral session double-delete race.
24. Worker ID reuse after cleanup — no stale state.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from provide.terminal.client import connect_async_ws

from ..conftest import _drain_all, _drain_until, _snapshot_msg, _ws_url
from .conftest import snapshot_msg, ws_url

from tests.e2e._live_server import live_server_with_bus


# ---------------------------------------------------------------------------
# 21. Cross-session routing strict isolation
# ---------------------------------------------------------------------------


async def test_cross_session_routing_strict_isolation() -> None:
    """2 sessions × 20 snapshots × 3 browsers each — zero cross-contamination."""
    sessions = [
        {"session_id": "iso-a", "display_name": "Iso A", "connector_type": "shell", "auto_start": False},
        {"session_id": "iso-b", "display_name": "Iso B", "connector_type": "shell", "auto_start": False},
    ]
    async with live_server_with_bus(sessions, label="strict_isolation") as (hub, base_url):
        # Connect workers
        async with (
            connect_async_ws(ws_url(base_url, "/ws/worker/iso-a/term")) as wa,
            connect_async_ws(ws_url(base_url, "/ws/worker/iso-b/term")) as wb,
        ):
            await wa.recv()  # snapshot_req
            await wb.recv()

            # Connect 3 browsers per session (6 total)
            browsers_a: list[Any] = []
            browsers_b: list[Any] = []
            contexts: list[Any] = []

            for _ in range(3):
                ctx_a = connect_async_ws(ws_url(base_url, "/ws/browser/iso-a/term"))
                ba = await ctx_a.__aenter__()
                contexts.append(ctx_a)
                browsers_a.append(ba)

                ctx_b = connect_async_ws(ws_url(base_url, "/ws/browser/iso-b/term"))
                bb = await ctx_b.__aenter__()
                contexts.append(ctx_b)
                browsers_b.append(bb)

            try:
                # Drain initial messages
                for b in [*browsers_a, *browsers_b]:
                    await _drain_all(b, timeout=0.5)

                # Both workers send 20 snapshots concurrently with distinctive markers
                async def send_batch(ws: Any, marker: str) -> None:
                    for i in range(20):
                        await ws.send(json.dumps(_snapshot_msg(f"{marker}-snap-{i}")))

                await asyncio.gather(
                    send_batch(wa, "SESSION-A"),
                    send_batch(wb, "SESSION-B"),
                )

                await asyncio.sleep(1.0)

                # Drain all browsers
                for ba in browsers_a:
                    msgs = await _drain_all(ba, timeout=2.0)
                    snapshots = [m for m in msgs if m.get("type") == "snapshot"]
                    for s in snapshots:
                        screen = s.get("screen", "")
                        assert "SESSION-B" not in screen, (
                            f"Session A browser received Session B snapshot: {screen}"
                        )
                    a_snaps = [s for s in snapshots if "SESSION-A" in s.get("screen", "")]
                    assert len(a_snaps) >= 10, (
                        f"Session A browser got only {len(a_snaps)} A-snapshots, expected ≥10"
                    )

                for bb in browsers_b:
                    msgs = await _drain_all(bb, timeout=2.0)
                    snapshots = [m for m in msgs if m.get("type") == "snapshot"]
                    for s in snapshots:
                        screen = s.get("screen", "")
                        assert "SESSION-A" not in screen, (
                            f"Session B browser received Session A snapshot: {screen}"
                        )
                    b_snaps = [s for s in snapshots if "SESSION-B" in s.get("screen", "")]
                    assert len(b_snaps) >= 10, (
                        f"Session B browser got only {len(b_snaps)} B-snapshots, expected ≥10"
                    )

            finally:
                for ctx in contexts:
                    try:
                        await ctx.__aexit__(None, None, None)
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# 22. 5 sessions concurrent worker disconnect
# ---------------------------------------------------------------------------


async def test_5_sessions_concurrent_worker_disconnect() -> None:
    """5 workers + 5 browsers; 3 workers crash concurrently; 2 survive and keep sending."""
    sessions = [
        {"session_id": f"s5-{i}", "display_name": f"S5-{i}", "connector_type": "shell", "auto_start": False}
        for i in range(5)
    ]
    async with live_server_with_bus(sessions, label="5_session_disconnect") as (hub, base_url):
        workers: list[Any] = []
        worker_ctxs: list[Any] = []
        browser_ctxs: list[Any] = []
        browsers: list[Any] = []

        for i in range(5):
            wctx = connect_async_ws(ws_url(base_url, f"/ws/worker/s5-{i}/term"))
            w = await wctx.__aenter__()
            await w.recv()  # snapshot_req
            worker_ctxs.append(wctx)
            workers.append(w)

            bctx = connect_async_ws(ws_url(base_url, f"/ws/browser/s5-{i}/term"))
            b = await bctx.__aenter__()
            await _drain_all(b, timeout=0.5)
            browser_ctxs.append(bctx)
            browsers.append(b)

        try:
            # Workers 0,1,2 disconnect concurrently
            await asyncio.gather(
                workers[0].close(),
                workers[1].close(),
                workers[2].close(),
            )

            await asyncio.sleep(0.5)

            # Workers 3,4 send snapshots after the crash
            for i in (3, 4):
                await workers[i].send(json.dumps(_snapshot_msg(f"s5-{i}-post-crash")))

            await asyncio.sleep(0.5)

            # Browsers 0,1,2 should have received worker_disconnected
            for i in (0, 1, 2):
                msgs = await _drain_all(browsers[i], timeout=2.0)
                disconnected = any(m.get("type") == "worker_disconnected" for m in msgs)
                assert disconnected, f"Browser s5-{i} should get worker_disconnected"

            # Browsers 3,4 should have received the post-crash snapshots
            for i in (3, 4):
                msgs = await _drain_all(browsers[i], timeout=2.0)
                snapshots = [m for m in msgs if m.get("type") == "snapshot"]
                found = any(f"s5-{i}-post-crash" in m.get("screen", "") for m in snapshots)
                assert found, f"Browser s5-{i} should get post-crash snapshot: {snapshots}"

        finally:
            for ctx in reversed(browser_ctxs):
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass
            for i, ctx in enumerate(reversed(worker_ctxs)):
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 23. Ephemeral session double-delete race
# ---------------------------------------------------------------------------


async def test_ephemeral_double_disconnect_race(live_hub: Any) -> None:
    """Two browsers disconnect simultaneously on same worker; no errors, state clean."""
    hub, base_url = live_hub

    async with connect_async_ws(_ws_url(base_url, "/ws/worker/eph-race/term")) as worker:
        await worker.recv()  # snapshot_req

        # Connect two browsers
        ctx1 = connect_async_ws(_ws_url(base_url, "/ws/browser/eph-race/term"))
        ctx2 = connect_async_ws(_ws_url(base_url, "/ws/browser/eph-race/term"))
        b1 = await ctx1.__aenter__()
        b2 = await ctx2.__aenter__()
        await _drain_all(b1)
        await _drain_all(b2)

        # Both disconnect simultaneously
        await asyncio.gather(
            ctx1.__aexit__(None, None, None),
            ctx2.__aexit__(None, None, None),
        )

        await asyncio.sleep(0.3)

        # Check state: no browsers, no errors
        st = hub._workers.get("eph-race")
        if st is not None:
            assert len(st.browsers) == 0, f"Browser list should be empty: {len(st.browsers)}"


# ---------------------------------------------------------------------------
# 24. Worker ID reuse after cleanup — no stale state
# ---------------------------------------------------------------------------


async def test_worker_id_reuse_after_cleanup_no_stale_state() -> None:
    """Second worker with same ID starts clean after first worker + hijack + disconnect."""
    from tests.e2e._live_server import live_server_with_bus

    sessions = [
        {"session_id": "reuse-1", "display_name": "Reuse", "connector_type": "shell", "auto_start": False},
    ]
    async with live_server_with_bus(sessions, label="worker_reuse") as (hub, base_url):
        event_bus = hub._event_bus

        # First generation: worker connects, browser acquires hijack
        async with connect_async_ws(ws_url(base_url, "/ws/worker/reuse-1/term")) as w1:
            await w1.recv()  # snapshot_req

            async with connect_async_ws(ws_url(base_url, "/ws/browser/reuse-1/term")) as b1:
                await _drain_all(b1)
                await b1.send(json.dumps({"type": "hijack_request"}))
                await _drain_until(b1, "hijack_state", timeout=3.0)
                await _drain_all(w1)

                # Worker sends gen-1 snapshot
                await w1.send(json.dumps(_snapshot_msg("gen-1-data")))
                await asyncio.sleep(0.3)

        # Both disconnected — give cleanup time
        await asyncio.sleep(0.5)

        # Second generation: new worker, new browser
        async with connect_async_ws(ws_url(base_url, "/ws/worker/reuse-1/term")) as w2:
            await w2.recv()  # snapshot_req

            # Hijack should be clean
            st = hub._workers.get("reuse-1")
            assert st is not None
            assert st.hijack_owner is None, f"Stale hijack owner: {st.hijack_owner}"

            async with connect_async_ws(ws_url(base_url, "/ws/browser/reuse-1/term")) as b2:
                b2_msgs = await _drain_all(b2, timeout=1.0)

                # No gen-1 hijack state in hello
                for m in b2_msgs:
                    if m.get("type") == "hijack_state":
                        assert not m.get("hijacked_by_me"), "Gen-2 browser should not inherit gen-1 hijack"

                # Subscribe to EventBus and send gen-2 snapshot
                async with event_bus.watch("reuse-1") as sub:
                    await w2.send(json.dumps(_snapshot_msg("gen-2-data")))
                    await asyncio.sleep(0.5)

                    events: list[dict[str, Any]] = []
                    while not sub.queue.empty():
                        item = sub.queue.get_nowait()
                        if item is None:
                            break
                        events.append(item)

                    # Should only see gen-2 events
                    for ev in events:
                        assert ev["worker_id"] == "reuse-1"
                    assert len(events) >= 1, "Should have ≥1 gen-2 event"
