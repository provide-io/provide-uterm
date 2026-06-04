#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Per-send timeout in broadcast(): stalled browser is pruned from st.browsers."""

from __future__ import annotations

import asyncio

import provide.uterm.server.bridge.hub.router_impl as router_impl
from provide.uterm.server.bridge.hub import TermHub


class _HangingWS:
    """Fake browser WebSocket whose send_text blocks forever (simulates stalled TCP window)."""

    async def send_text(self, _payload: str) -> None:
        await asyncio.Event().wait()  # never completes


async def test_broadcast_prunes_browser_whose_send_stalls(monkeypatch: object) -> None:
    """A browser that stalls on send_text must be pruned from st.browsers after broadcast."""
    monkeypatch.setattr(router_impl, "_BROADCAST_SEND_TIMEOUT_S", 0.05)

    hub = TermHub()
    # Mirror the exact hub-registration harness used in test_hub.py:
    # _get() creates the WorkerTermState; direct dict assignment registers the browser.
    await hub._get("w1")
    ws = _HangingWS()
    hub.registry._workers["w1"].browsers[ws] = "viewer"  # type: ignore[arg-type]

    await hub.broadcast("w1", {"type": "term", "data": "x"})

    st = hub.registry.get("w1")
    # The stalled browser must be gone from the browsers map.
    assert ws not in (st.browsers if st is not None else {}), (
        "Expected stalled browser to be pruned from st.browsers after broadcast timeout"
    )


class _GatedWS:
    """Browser WS that bumps a shared in-flight counter, signals once every send
    is dispatched, then blocks until released.

    With a sequential broadcast loop only one send is ever in flight, so the
    ``arrived`` event never fires for N>1 and the awaiting test times out. The
    concurrent fan-out dispatches all N before any completes.
    """

    def __init__(self, *, in_flight: list[int], expected: int, arrived: asyncio.Event, release: asyncio.Event) -> None:
        self._in_flight = in_flight
        self._expected = expected
        self._arrived = arrived
        self._release = release

    async def send_text(self, _payload: str) -> None:
        self._in_flight[0] += 1
        if self._in_flight[0] >= self._expected:
            self._arrived.set()
        await self._release.wait()


async def test_broadcast_dispatches_all_sends_concurrently() -> None:
    """All browser sends are in flight together before any completes (no head-of-line block)."""
    hub = TermHub()
    await hub._get("w1")

    n = 4
    in_flight = [0]
    arrived = asyncio.Event()
    release = asyncio.Event()
    for _ in range(n):
        ws = _GatedWS(in_flight=in_flight, expected=n, arrived=arrived, release=release)
        hub.registry._workers["w1"].browsers[ws] = "viewer"  # type: ignore[arg-type]

    task = asyncio.create_task(hub.broadcast("w1", {"type": "term", "data": "x"}))
    try:
        # A sequential loop would block on the first send and never reach n.
        await asyncio.wait_for(arrived.wait(), timeout=1.0)
        assert in_flight[0] == n, f"expected all {n} sends dispatched concurrently, got {in_flight[0]}"
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1.0)

    # No browser raised, so none should be pruned.
    assert len(hub.registry.get("w1").browsers) == n


async def test_broadcast_slow_browser_does_not_serialize_a_fast_one() -> None:
    """A browser that completes quickly is not delayed by a slow co-broadcast peer."""
    hub = TermHub()
    await hub._get("w1")

    slow_release = asyncio.Event()
    fast_done = asyncio.Event()

    class _SlowWS:
        async def send_text(self, _payload: str) -> None:
            await slow_release.wait()

    class _FastWS:
        async def send_text(self, _payload: str) -> None:
            fast_done.set()

    hub.registry._workers["w1"].browsers[_SlowWS()] = "viewer"  # type: ignore[arg-type]
    hub.registry._workers["w1"].browsers[_FastWS()] = "viewer"  # type: ignore[arg-type]

    task = asyncio.create_task(hub.broadcast("w1", {"type": "term", "data": "x"}))
    try:
        # The fast browser finishes while the slow one is still blocked.
        await asyncio.wait_for(fast_done.wait(), timeout=1.0)
        assert not task.done()
    finally:
        slow_release.set()
        await asyncio.wait_for(task, timeout=1.0)


async def test_broadcast_prunes_raising_browser_among_concurrent_sends() -> None:
    """A browser whose concurrent send raises is mapped back and pruned; peers survive."""
    hub = TermHub()
    await hub._get("w1")

    class _RaisingWS:
        async def send_text(self, _payload: str) -> None:
            raise RuntimeError("boom")

    class _OkWS:
        async def send_text(self, _payload: str) -> None:
            return None

    bad = _RaisingWS()
    good = _OkWS()
    hub.registry._workers["w1"].browsers[good] = "viewer"  # type: ignore[arg-type]
    hub.registry._workers["w1"].browsers[bad] = "viewer"  # type: ignore[arg-type]

    await hub.broadcast("w1", {"type": "term", "data": "x"})

    st = hub.registry.get("w1")
    assert bad not in st.browsers, "raising browser must be collected into dead and pruned"
    assert good in st.browsers, "healthy peer must survive a co-broadcast failure"
