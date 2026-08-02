#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for OutputCollector — adaptive EventBus output accumulator."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from provide.uterm.server.bridge.fanout._collector import OutputCollector
from provide.uterm.server.bridge.hub import EventBus, TermHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_hub_with_worker(worker_id: str) -> TermHub:
    """Create a TermHub with EventBus and register a worker."""
    hub = TermHub(event_bus=EventBus())
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    await hub.register_worker(worker_id, ws)
    return hub


# ---------------------------------------------------------------------------
# No EventBus → fast return
# ---------------------------------------------------------------------------


async def test_collector_returns_empty_when_no_event_bus() -> None:
    """Collector must return ("", 0) immediately if hub has no EventBus."""
    hub = TermHub()  # no EventBus
    collector = OutputCollector()
    result = await collector.collect(hub, "w1", quiesce_ms=100, max_ms=1_000)
    assert result == ("", 0)


async def test_capture_without_any_event_infrastructure_collects_nothing() -> None:
    """A hub double exposing neither private stream nor EventBus opens no watch.

    ``OutputCapture.open`` degrades to a no-op subscription rather than raising,
    so a supervised operation driven against a narrow test double still runs;
    it just reports no captured output.
    """
    hub = SimpleNamespace(event_bus=None)

    capture = await OutputCollector().open(hub, "w1")  # type: ignore[arg-type]
    try:
        assert capture._watch is None
        assert capture._subscription is None
        assert await capture.collect(quiesce_ms=1, max_ms=50) == ("", 0)
    finally:
        await capture.close()


async def test_capture_close_unsubscribes_exactly_once() -> None:
    class Watch:
        exits = 0

        async def __aenter__(self):
            return SimpleNamespace(queue=asyncio.Queue())

        async def __aexit__(self, *args: object) -> None:
            self.exits += 1

    watch = Watch()
    bus = SimpleNamespace(watch=lambda *args, **kwargs: watch)
    hub = SimpleNamespace(event_bus=bus)
    capture = await OutputCollector().open(hub, "w1")  # type: ignore[arg-type]

    await capture.close()
    await capture.close()

    assert watch.exits == 1


async def test_event_bus_replacement_wakes_public_and_private_subscribers() -> None:
    hub = await _make_hub_with_worker("w1")
    old_public = hub.event_bus
    old_private = hub._operation_event_bus
    assert old_public is not None
    assert old_private is not None
    capture = await OutputCollector().open(hub, "w1")

    async with old_public.watch("w1") as public_sub:
        public_wait = asyncio.create_task(public_sub.queue.get())
        private_wait = asyncio.create_task(capture._subscription.queue.get())
        replacement = EventBus()

        hub.event_bus = replacement

        assert await asyncio.wait_for(public_wait, timeout=0.1) is None
        assert await asyncio.wait_for(private_wait, timeout=0.1) is None
        assert old_public.subscriber_count("w1") == 0
        assert old_private.subscriber_count("w1") == 0
        async with replacement.watch("w1") as replacement_sub:
            await hub.append_event("w1", "term", {"data": "new-generation"})
            delivered = await asyncio.wait_for(replacement_sub.queue.get(), timeout=0.1)
            assert delivered is not None
            assert delivered["data"]["data"] == "new-generation"

    await capture.close()


async def test_reassigning_the_same_event_bus_keeps_live_subscribers() -> None:
    """Assigning the bus a hub already owns must be a true no-op.

    The setter tears down both the public and the private bus before adopting a
    new one. Without the identity guard, an idempotent re-assignment (a config
    reload that resolves to the same bus, say) would sentinel every live
    subscriber and silently swap in a fresh private bus, orphaning any
    in-flight supervised operation.
    """
    hub = await _make_hub_with_worker("w1")
    public_bus = hub.event_bus
    private_bus = hub._operation_event_bus
    assert public_bus is not None
    capture = await OutputCollector().open(hub, "w1")

    async with public_bus.watch("w1") as public_sub:
        hub.event_bus = public_bus

        assert hub.event_bus is public_bus
        assert hub._operation_event_bus is private_bus
        await hub.append_event("w1", "term", {"data": "still-live"})
        public_event = await asyncio.wait_for(public_sub.queue.get(), timeout=0.1)
        private_event = await asyncio.wait_for(capture._subscription.queue.get(), timeout=0.1)

    assert public_event is not None
    assert private_event is not None
    assert public_event["data"]["data"] == "still-live"
    assert private_event["data"]["data"] == "still-live"

    await capture.close()


async def test_hub_shutdown_wakes_public_and_private_subscribers_idempotently() -> None:
    hub = await _make_hub_with_worker("w1")
    public_bus = hub.event_bus
    private_bus = hub._operation_event_bus
    assert public_bus is not None
    assert private_bus is not None
    capture = await OutputCollector().open(hub, "w1")

    async with public_bus.watch("w1") as public_sub:
        public_wait = asyncio.create_task(public_sub.queue.get())
        private_wait = asyncio.create_task(capture._subscription.queue.get())

        await hub.shutdown()
        await hub.shutdown()

        assert await asyncio.wait_for(public_wait, timeout=0.1) is None
        assert await asyncio.wait_for(private_wait, timeout=0.1) is None
        assert public_bus.subscriber_count("w1") == 0
        assert private_bus.subscriber_count("w1") == 0

    await capture.close()


# ---------------------------------------------------------------------------
# Captures output events and returns delta string
# ---------------------------------------------------------------------------


async def test_collector_captures_term_events() -> None:
    """Collector accumulates text from term events."""
    hub = await _make_hub_with_worker("w1")
    collector = OutputCollector()

    async def _emit() -> None:
        await asyncio.sleep(0.05)
        await hub.append_event("w1", "term", {"data": "hello "})
        await asyncio.sleep(0.05)
        await hub.append_event("w1", "term", {"data": "world"})

    task = asyncio.create_task(_emit())
    delta, elapsed_ms = await collector.collect(hub, "w1", quiesce_ms=300, max_ms=5_000)
    await task

    assert delta == "hello world"
    assert elapsed_ms >= 0


async def test_capture_bounds_multimegabyte_raw_output_by_utf8_bytes() -> None:
    hub = await _make_hub_with_worker("w1")
    cap = 64 * 1024
    capture = await OutputCollector().open(hub, "w1", max_output_bytes=cap)
    marker = "NEWEST-✅"

    try:
        await hub.append_event("w1", "term", {"data": "é" * (2 * 1024 * 1024) + marker})
        assert capture.queued_bytes <= cap
        delta, _elapsed_ms = await capture.collect(quiesce_ms=1, max_ms=50)
    finally:
        await capture.close()

    assert len(delta.encode("utf-8")) <= cap
    assert delta.endswith(marker)
    assert "�" not in delta


async def test_capture_truncates_accumulated_output_to_the_newest_whole_characters() -> None:
    """Accumulation across events is bounded by bytes, tail-first.

    Each individual event already fits the per-subscription budget, so the
    overflow only appears once the collector concatenates them. The retained
    text must be the *newest* suffix — an operation's final prompt is what the
    caller acts on — and the cut must land on a character boundary rather than
    slicing a multi-byte codepoint into a replacement character.
    """
    hub = await _make_hub_with_worker("w1")
    cap = 4095
    capture = await OutputCollector().open(hub, "w1", max_output_bytes=cap)
    first = "é" * 1900  # 3800 UTF-8 bytes
    second = "x" * 400
    assert len(first.encode("utf-8")) + len(second.encode("utf-8")) > cap

    async def _emit() -> None:
        await hub.append_event("w1", "term", {"data": first})
        for _ in range(1000):
            if not capture.queued_bytes:
                break
            await asyncio.sleep(0)
        await hub.append_event("w1", "term", {"data": second})

    emitter = asyncio.create_task(_emit())
    try:
        delta, _elapsed_ms = await capture.collect(quiesce_ms=100, max_ms=5_000)
    finally:
        await emitter
        await capture.close()

    encoded = delta.encode("utf-8")
    assert delta.endswith(second)
    assert delta.startswith("é")
    assert "�" not in delta
    # 4200 total bytes trimmed to the newest 4095; the byte at that offset is a
    # continuation byte, so one further byte is given up to keep the character
    # whole rather than emitting U+FFFD.
    assert len(encoded) == cap - 1


async def test_capture_preserves_exact_unicode_output_below_cap() -> None:
    hub = await _make_hub_with_worker("w1")
    capture = await OutputCollector().open(hub, "w1", max_output_bytes=1024)

    try:
        await hub.append_event("w1", "term", {"data": "Trade → Sector 42 ✅"})
        delta, _elapsed_ms = await capture.collect(quiesce_ms=1, max_ms=50)
    finally:
        await capture.close()

    assert delta == "Trade → Sector 42 ✅"


# ---------------------------------------------------------------------------
# Quiesce — returns early after silence
# ---------------------------------------------------------------------------


async def test_collector_quiesces_after_silence() -> None:
    """Collector returns after quiesce_ms with no new events, not after max_ms."""
    hub = await _make_hub_with_worker("w1")
    collector = OutputCollector()

    async def _emit() -> None:
        await asyncio.sleep(0.05)
        await hub.append_event("w1", "term", {"data": "ping"})
        # No more events — collector should quiesce after quiesce_ms

    task = asyncio.create_task(_emit())
    quiesce_ms = 200
    max_ms = 10_000

    delta, elapsed_ms = await collector.collect(hub, "w1", quiesce_ms=quiesce_ms, max_ms=max_ms)
    await task

    assert delta == "ping"
    # Should return well before max_ms (e.g. within 2× quiesce_ms + emit delay)
    assert elapsed_ms < max_ms // 2


# ---------------------------------------------------------------------------
# Hard cap — returns at max_ms when events keep coming
# ---------------------------------------------------------------------------


async def test_collector_respects_hard_cap() -> None:
    """Collector returns at max_ms when a continuous stream of events is flowing."""
    hub = await _make_hub_with_worker("w1")
    collector = OutputCollector()

    stop_event = asyncio.Event()

    async def _continuous_emit() -> None:
        i = 0
        while not stop_event.is_set():
            await hub.append_event("w1", "term", {"data": f"chunk{i} "})
            await asyncio.sleep(0.01)
            i += 1

    task = asyncio.create_task(_continuous_emit())

    max_ms = 300
    quiesce_ms = 5_000  # large quiesce so it won't trigger

    delta, elapsed_ms = await collector.collect(hub, "w1", quiesce_ms=quiesce_ms, max_ms=max_ms)

    stop_event.set()
    await task

    # Should have been capped at max_ms
    assert elapsed_ms >= max_ms - 50  # within 50 ms tolerance
    assert elapsed_ms < max_ms + 500  # reasonable upper bound
    assert len(delta) > 0  # some output was collected


# ---------------------------------------------------------------------------
# Non-term events are ignored
# ---------------------------------------------------------------------------


async def test_collector_ignores_non_term_events() -> None:
    """Collector only accumulates 'term' events, not 'snapshot' or others."""
    hub = await _make_hub_with_worker("w1")
    collector = OutputCollector()

    async def _emit() -> None:
        await asyncio.sleep(0.05)
        await hub.append_event("w1", "snapshot", {"screen": "ignored"})
        await hub.append_event("w1", "term", {"data": "captured"})

    task = asyncio.create_task(_emit())
    delta, elapsed_ms = await collector.collect(hub, "w1", quiesce_ms=300, max_ms=5_000)
    await task

    assert delta == "captured"


async def test_collector_skips_empty_payload_events() -> None:
    """Term with empty data and snapshot with empty screen must not crash
    or accumulate; both fall through to the loop's next iteration.

    Exercises the falsy-branch paths in _run_collect: term with empty data
    is filtered by ``if text:`` and snapshot with empty screen is filtered
    by ``if screen:``.  Without this, the collector partial-branch coverage
    would sit at 96%.
    """
    hub = await _make_hub_with_worker("w1")
    collector = OutputCollector()

    async def _emit() -> None:
        await asyncio.sleep(0.05)
        # Empty payload variants — must be skipped without contributing output.
        await hub.append_event("w1", "term", {"data": ""})
        await hub.append_event("w1", "snapshot", {"screen": ""})
        # Then a real term event so the collector has something to return.
        await hub.append_event("w1", "term", {"data": "real"})

    task = asyncio.create_task(_emit())
    delta, _elapsed = await collector.collect(hub, "w1", quiesce_ms=300, max_ms=5_000)
    await task

    assert delta == "real"
