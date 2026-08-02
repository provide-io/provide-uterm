#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for EventBus."""

from __future__ import annotations

import asyncio

import pytest

from provide.uterm.server.bridge.hub.event_bus import EventBus, _compile_pattern

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


async def test_enqueue_swallows_deliver_exception() -> None:
    """Defensive catch in ``_enqueue``: ``_deliver`` raising must not propagate to callers."""
    bus = EventBus()
    async with bus.watch("w1"):

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic deliver failure")

        # Replace _deliver with a raiser. _enqueue must catch and log, not raise.
        # ``setattr`` avoids the mypy ``method-assign`` complaint about replacing
        # bound methods on instances.
        object.__setattr__(bus, "_deliver", _boom)
        bus._enqueue("w1", {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {}})


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


async def test_subscription_byte_cap_keeps_newest_utf8_suffix() -> None:
    cap = 256
    bus = EventBus(max_queue_depth=50)
    async with bus.watch("w1", max_queue_bytes=cap) as sub:
        bus._enqueue(
            "w1",
            {"seq": 1, "ts": 1.0, "type": "term", "data": {"data": "é" * 2_000 + "NEWEST"}},
        )

        assert sub.queued_bytes <= cap
        item = sub.queue.get_nowait()

    assert item is not None
    assert item["data"]["data"].endswith("NEWEST")
    assert len(item["data"]["data"].encode("utf-8")) <= cap


async def test_subscription_byte_cap_drops_old_events_before_newest() -> None:
    cap = 300
    bus = EventBus(max_queue_depth=50)
    async with bus.watch("w1", max_queue_bytes=cap) as sub:
        for seq in range(20):
            bus._enqueue(
                "w1",
                {"seq": seq, "ts": 1.0, "type": "term", "data": {"data": f"chunk-{seq}-" + "x" * 40}},
            )

        assert sub.queued_bytes <= cap
        items = []
        while not sub.queue.empty():
            items.append(sub.queue.get_nowait())

    assert items[-1]["seq"] == 19
    assert sub.dropped > 0


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
    with pytest.raises(ValueError, match="invalid watch pattern regex"):
        _compile_pattern(r"[invalid")


def test_compile_pattern_rejects_patterns_over_configured_length() -> None:
    with pytest.raises(ValueError, match="watch pattern is too long"):
        _compile_pattern("a" * 9, max_pattern_length=8)


def test_compile_pattern_rejects_nested_quantifier_patterns() -> None:
    with pytest.raises(ValueError, match="unsafe watch pattern"):
        _compile_pattern(r"(a+)+$")


@pytest.mark.parametrize(
    "pattern",
    [
        # Alternation inside a quantified group (non-capturing).
        r"(?:a|aa)+",
        # Alternation inside a quantified capturing group.
        r"(a|b)+x",
        # Lookahead wrapping a quantified subgroup, itself quantified.
        r"(?=(a+))+",
        # Counted-quantifier variant of the alternation case.
        r"(?:a|aa){2,}",
        # Nested quantified group (regression).
        r"(a+)+$",
        # Nested quantified subgroup inside a star-quantified group.
        r"((a+))*",
    ],
)
def test_compile_pattern_rejects_redos_shapes(pattern: str) -> None:
    with pytest.raises(ValueError, match="unsafe watch pattern"):
        _compile_pattern(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        # Character class with a quantifier is linear-time.
        r"[abc]+",
        # Simple literal.
        r"hello world",
        # Single non-nested quantified literal.
        r"a+",
        # Alternation without a quantifier on the group.
        r"(a|b)x",
        # Quantified group with no alternation and no inner quantifier.
        r"(ab)+",
        # Escaped pipe is a literal, not alternation.
        r"(a\|b)+",
        # Lookbehind prefix ``(?<=`` — exercises the lookbehind ``<=``/``<!`` prefix-skip branch.
        r"(?<=foo)bar",
        # Negative lookbehind ``(?<!``.
        r"(?<!foo)bar",
        # Named capture group ``(?P<name>...)`` — exercises the ``P`` prefix-skip branch.
        r"(?P<word>\w+)",
        # Named group with unterminated ``>`` is tolerated (``end == -1`` path).
        # Use a syntactically valid pattern that just doesn't close the named-group
        # marker on the same character via raw string trickery — re.compile must
        # still accept it.  ``(?P<a>b)`` works to also touch the ``end != -1`` path.
        r"(?P<a>b)",
        # Nested group: outer encloses an inner quantified group + literal so the
        # inner-quantifier propagation branch (``group_stack[-1][0] = True``) runs
        # when the inner group closes.
        r"((a+)b)",
        # Nested group with alternation propagation (``group_stack[-1][1] = True``).
        r"((a|b)c)",
        # Top-level (no enclosing group) alternation — exercises the ``if group_stack``
        # False branch (334->336) where alternation lives outside any group.
        r"foo|bar",
        # Inline-flag group ``(?i...)`` — the prefix character is neither ``<``,
        # ``P``, nor in ``":=!"`` so the validator falls through to the comment
        # branch (313->316) without advancing ``i``.
        r"(?i)abc",
    ],
)
def test_compile_pattern_allows_safe_patterns(pattern: str) -> None:
    # Must not raise.
    compiled = _compile_pattern(pattern)
    assert compiled is not None


def test_compile_pattern_named_group_without_close_advances_safely() -> None:
    """``(?P`` without closing ``>``: the validator must not advance ``i`` past EOF.

    Exercises the ``end == -1`` branch in ``_validate_pattern_safety``: the
    pattern-skip logic for ``(?P<name>`` finds no ``>``, leaves ``i`` alone, and
    keeps scanning.  ``re.compile`` later rejects the malformed regex; the
    validator must still complete without crashing first.
    """
    with pytest.raises(ValueError, match="invalid watch pattern regex"):
        _compile_pattern(r"(?P abc")


async def test_watch_rejects_patterns_over_configured_length() -> None:
    bus = EventBus(max_pattern_length=8)
    with pytest.raises(ValueError, match="watch pattern is too long"):
        async with bus.watch("w1", pattern="a" * 9):
            pass


async def test_pattern_filter_bounds_screen_text_before_matching() -> None:
    bus = EventBus(max_match_input_chars=8)
    event = {"seq": 1, "ts": 1.0, "type": "snapshot", "data": {"screen": "abcdefghZ"}}
    async with bus.watch("w1", pattern="Z") as sub:
        bus._enqueue("w1", event)
    assert sub.queue.empty()


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
