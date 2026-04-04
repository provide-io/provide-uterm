#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests: EventBus wired into TermHub."""

from __future__ import annotations

import asyncio
import time

from provide.terminal.bridge.hub import EventBus, TermHub


def _make_hub() -> TermHub:
    return TermHub(event_bus=EventBus())


# ---------------------------------------------------------------------------
# append_event → EventBus delivery
# ---------------------------------------------------------------------------


async def test_append_event_delivers_to_subscriber() -> None:
    hub = _make_hub()
    await hub._get("w1")
    event_bus = hub.event_bus
    assert event_bus is not None
    async with event_bus.watch("w1") as sub:
        evt = await hub.append_event("w1", "snapshot", {"screen": "hello"})
        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert item["seq"] == evt["seq"]
    assert item["type"] == "snapshot"
    assert item["worker_id"] == "w1"


async def test_append_event_no_bus_works_normally() -> None:
    hub = TermHub()  # No event_bus
    await hub._get("w1")
    evt = await hub.append_event("w1", "snapshot", {"screen": "hi"})
    assert evt["type"] == "snapshot"


async def test_append_event_unknown_worker_no_bus_error() -> None:
    hub = _make_hub()
    # Worker not registered — append_event returns stub, no subscriber → no error
    evt = await hub.append_event("no-worker", "snapshot", {})
    assert evt["seq"] == 0


async def test_append_event_delivers_to_filtered_subscriber() -> None:
    hub = _make_hub()
    await hub._get("w1")
    event_bus = hub.event_bus
    assert event_bus is not None
    async with event_bus.watch("w1", event_types=["hijack_acquired"]) as sub:
        await hub.append_event("w1", "snapshot", {"screen": "x"})
        await hub.append_event("w1", "hijack_acquired", {"hijack_id": "abc"})
        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert item["type"] == "hijack_acquired"
    assert sub.queue.empty()


# ---------------------------------------------------------------------------
# deregister_worker → close_worker → sentinel
# ---------------------------------------------------------------------------


async def test_deregister_worker_closes_subscriptions() -> None:
    from unittest.mock import AsyncMock

    hub = _make_hub()
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()

    await hub.register_worker("w1", ws)
    event_bus = hub.event_bus
    assert event_bus is not None
    async with event_bus.watch("w1") as sub:
        await hub.deregister_worker("w1", ws)
        sentinel = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert sentinel is None


async def test_deregister_wrong_ws_does_not_close() -> None:
    from unittest.mock import AsyncMock

    hub = _make_hub()
    ws_real = AsyncMock()
    ws_other = AsyncMock()

    await hub.register_worker("w1", ws_real)
    event_bus = hub.event_bus
    assert event_bus is not None
    async with event_bus.watch("w1") as sub:
        # Deregister with the wrong ws — should not close subscriptions
        should_broadcast, _ = await hub.deregister_worker("w1", ws_other)
        assert not should_broadcast
        # No sentinel delivered
    assert sub.queue.empty()


# ---------------------------------------------------------------------------
# disconnect_worker → close_worker → sentinel
# ---------------------------------------------------------------------------


async def test_disconnect_worker_closes_subscriptions() -> None:
    from unittest.mock import AsyncMock

    hub = _make_hub()
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()

    await hub.register_worker("w1", ws)
    event_bus = hub.event_bus
    assert event_bus is not None
    async with event_bus.watch("w1") as sub:
        await hub.disconnect_worker("w1")
        sentinel = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert sentinel is None


async def test_disconnect_nonexistent_worker_noop() -> None:
    hub = _make_hub()
    result = await hub.disconnect_worker("no-such-worker")
    assert not result


# ---------------------------------------------------------------------------
# Latency: broadcast with multiple subscribers stays fast
# ---------------------------------------------------------------------------


async def test_broadcast_latency_with_subscribers() -> None:
    """1000 append_event calls with 5 active subscribers should complete quickly."""
    hub = _make_hub()
    await hub._get("w1")
    event_bus = hub.event_bus
    assert event_bus is not None

    n = 1000
    subs = []
    ctxs = []
    for _ in range(5):
        ctx = event_bus.watch("w1")
        sub = await ctx.__aenter__()
        subs.append(sub)
        ctxs.append(ctx)

    start = time.monotonic()
    for i in range(n):
        await hub.append_event("w1", "snapshot", {"screen": f"line {i}"})
    elapsed_ms = (time.monotonic() - start) * 1000

    for ctx in ctxs:
        await ctx.__aexit__(None, None, None)

    # 1000 events with 5 subscribers: should be well under 2 ms overhead per call
    # (total < 2000 ms for the batch, but typically < 100 ms)
    assert elapsed_ms < 2000, f"Too slow: {elapsed_ms:.1f} ms for {n} appends"


async def test_slow_subscriber_does_not_block_append() -> None:
    """A full subscriber queue must not block append_event."""
    hub = TermHub(event_bus=EventBus(max_queue_depth=2))
    await hub._get("w1")
    event_bus = hub.event_bus
    assert event_bus is not None

    async with event_bus.watch("w1") as sub:
        # Fill the queue first
        for i in range(10):
            await hub.append_event("w1", "snapshot", {"screen": f"{i}"})
        # Queue should be capped at max_queue_depth; no hang
        assert sub.queue.qsize() <= 2
        assert sub.dropped > 0
