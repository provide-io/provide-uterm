#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for EventBus."""

from __future__ import annotations

import asyncio
import re

import pytest

from provide.terminal.hijack.hub.event_bus import EventBus, _compile_pattern

# ---------------------------------------------------------------------------
# subscribe + _enqueue: basic delivery
# ---------------------------------------------------------------------------


async def test_subscribe_receives_enqueued_event() -> None:
    bus = EventBus()
    event = {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {"screen": "hello"}}
    async with bus.watch("w1") as sub:
        bus._enqueue("w1", event)
        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert item == {"worker_id": "w1", **event}


async def test_enqueue_unknown_worker_does_nothing() -> None:
    bus = EventBus()
    bus._enqueue("no-such-worker", {"seq": 1, "ts": 1.0, "type": "x", "data": {}})
    # No subscribers → no-op, no exception


async def test_multiple_subscribers_all_receive() -> None:
    bus = EventBus()
    event = {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {}}
    async with bus.watch("w1") as sub1, bus.watch("w1") as sub2:
        bus._enqueue("w1", event)
        item1 = await asyncio.wait_for(sub1.queue.get(), timeout=1.0)
        item2 = await asyncio.wait_for(sub2.queue.get(), timeout=1.0)
    assert item1["seq"] == 1
    assert item2["seq"] == 1


# ---------------------------------------------------------------------------
# event_types filter
# ---------------------------------------------------------------------------


async def test_event_types_filter_passes_matching() -> None:
    bus = EventBus()
    event = {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {}}
    async with bus.watch("w1", event_types=["snapshot"]) as sub:
        bus._enqueue("w1", event)
        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert item["type"] == "snapshot"


async def test_event_types_filter_blocks_non_matching() -> None:
    bus = EventBus()
    snapshot_event = {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {}}
    input_event = {"seq": 2, "ts": 1.0, "type": "input_send", "data": {}}
    async with bus.watch("w1", event_types=["snapshot"]) as sub:
        bus._enqueue("w1", input_event)
        bus._enqueue("w1", snapshot_event)
        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert item["type"] == "snapshot"
    assert sub.queue.empty()


# ---------------------------------------------------------------------------
# pattern filter
# ---------------------------------------------------------------------------


async def test_pattern_filter_passes_matching_screen() -> None:
    bus = EventBus()
    event = {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {"screen": "$ ls"}}
    async with bus.watch("w1", pattern=r"\$") as sub:
        bus._enqueue("w1", event)
        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert item["seq"] == 1


async def test_pattern_filter_blocks_non_matching_screen() -> None:
    bus = EventBus()
    event = {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {"screen": "hello"}}
    async with bus.watch("w1", pattern=r"\$") as sub:
        bus._enqueue("w1", event)
    # Nothing should be in the queue
    assert sub.queue.empty()


async def test_pattern_filter_no_screen_field_blocked() -> None:
    bus = EventBus()
    event = {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {}}
    async with bus.watch("w1", pattern=r"prompt") as sub:
        bus._enqueue("w1", event)
    assert sub.queue.empty()


# ---------------------------------------------------------------------------
# Queue overflow — ring buffer semantics
# ---------------------------------------------------------------------------


async def test_queue_overflow_drops_oldest() -> None:
    bus = EventBus(max_queue_depth=2)
    async with bus.watch("w1") as sub:
        for i in range(4):
            bus._enqueue("w1", {"seq": i, "ts": 1.0, "type": "x", "data": {}})
        # Only 2 items fit; oldest two dropped
        assert sub.queue.qsize() == 2
        item1 = sub.queue.get_nowait()
        item2 = sub.queue.get_nowait()
    # Sequences 2 and 3 remain (0 and 1 were dropped)
    assert item1["seq"] == 2
    assert item2["seq"] == 3


async def test_queue_overflow_increments_dropped() -> None:
    bus = EventBus(max_queue_depth=1)
    async with bus.watch("w1") as sub:
        bus._enqueue("w1", {"seq": 1, "ts": 1.0, "type": "x", "data": {}})
        bus._enqueue("w1", {"seq": 2, "ts": 1.0, "type": "x", "data": {}})
    assert sub.dropped >= 1


# ---------------------------------------------------------------------------
# close_worker — sentinel delivery and cleanup
# ---------------------------------------------------------------------------


async def test_close_worker_delivers_sentinel() -> None:
    bus = EventBus()
    async with bus.watch("w1") as sub:
        bus.close_worker("w1")
        sentinel = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert sentinel is None


async def test_close_worker_removes_subscriptions() -> None:
    bus = EventBus()
    async with bus.watch("w1") as _sub:
        assert bus.subscriber_count("w1") == 1
        bus.close_worker("w1")
        # After close, subscriptions are removed from registry
        assert bus.subscriber_count("w1") == 0
    # After context exit (which also calls _remove), still zero
    assert bus.subscriber_count("w1") == 0


async def test_close_worker_unknown_noop() -> None:
    bus = EventBus()
    bus.close_worker("no-such-worker")  # Should not raise


async def test_close_worker_full_queue_puts_sentinel_anyway() -> None:
    """Sentinel must fit even when queue is full (drops oldest to make room)."""
    bus = EventBus(max_queue_depth=1)
    async with bus.watch("w1") as sub:
        bus._enqueue("w1", {"seq": 1, "ts": 1.0, "type": "x", "data": {}})
        assert sub.queue.full()
        bus.close_worker("w1")
        # Queue should contain the sentinel (oldest event dropped)
        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert item is None


# ---------------------------------------------------------------------------
# unsubscribe — context manager cleanup
# ---------------------------------------------------------------------------


async def test_context_exit_removes_subscription() -> None:
    bus = EventBus()
    async with bus.watch("w1") as _sub:
        assert bus.subscriber_count("w1") == 1
    assert bus.subscriber_count("w1") == 0


async def test_events_not_delivered_after_context_exit() -> None:
    bus = EventBus()
    async with bus.watch("w1") as sub:
        pass
    # After exit, enqueue should silently do nothing
    bus._enqueue("w1", {"seq": 1, "ts": 1.0, "type": "x", "data": {}})
    assert sub.queue.empty()


# ---------------------------------------------------------------------------
# subscriber_count
# ---------------------------------------------------------------------------


async def test_subscriber_count_tracks_correctly() -> None:
    bus = EventBus()
    assert bus.subscriber_count("w1") == 0
    async with bus.watch("w1") as _s1:
        assert bus.subscriber_count("w1") == 1
        async with bus.watch("w1") as _s2:
            assert bus.subscriber_count("w1") == 2
        assert bus.subscriber_count("w1") == 1
    assert bus.subscriber_count("w1") == 0


# ---------------------------------------------------------------------------
# _compile_pattern helper
# ---------------------------------------------------------------------------


def test_compile_pattern_none_returns_none() -> None:
    assert _compile_pattern(None) is None


def test_compile_pattern_returns_compiled() -> None:
    p = _compile_pattern(r"\d+")
    assert p is not None
    assert p.search("abc123") is not None


def test_compile_pattern_invalid_raises() -> None:
    with pytest.raises(re.error):
        _compile_pattern(r"[invalid")


# ---------------------------------------------------------------------------
# watch: multiple workers isolated
# ---------------------------------------------------------------------------


async def test_workers_isolated() -> None:
    bus = EventBus()
    event_w1 = {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {}}
    event_w2 = {"seq": 2, "ts": 1.0, "type": "snapshot", "data": {}}
    async with bus.watch("w1") as sub1, bus.watch("w2") as sub2:
        bus._enqueue("w1", event_w1)
        bus._enqueue("w2", event_w2)
        item1 = await asyncio.wait_for(sub1.queue.get(), timeout=1.0)
        item2 = await asyncio.wait_for(sub2.queue.get(), timeout=1.0)
    assert item1["worker_id"] == "w1"
    assert item2["worker_id"] == "w2"
    assert sub1.queue.empty()
    assert sub2.queue.empty()
