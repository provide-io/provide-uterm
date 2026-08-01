#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for correlated terminal transcript and screen frames."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from provide.uterm.control_channel import encode_control_frame

from .test_transport_session import _ConcreteSession, _FakeTransport


class _GatedReadTransport(_FakeTransport):
    def __init__(self, chunk: bytes) -> None:
        super().__init__()
        self.chunk = chunk
        self.release = asyncio.Event()

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        del max_bytes, timeout_ms
        if self._idx == 0:
            self._idx += 1
            await self.release.wait()
            return self.chunk
        await asyncio.sleep(0.01)
        return b""


class _PausedWaitEvent(asyncio.Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_started = asyncio.Event()
        self.proceed = asyncio.Event()

    async def wait(self) -> bool:
        self.wait_started.set()
        await self.proceed.wait()
        return await super().wait()


class _TwoGatedReadTransport(_FakeTransport):
    def __init__(self, first: bytes, second: bytes) -> None:
        super().__init__()
        self.chunks = (first, second)
        self.release_second = asyncio.Event()

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        del max_bytes, timeout_ms
        if self._idx == 0:
            self._idx += 1
            return self.chunks[0]
        if self._idx == 1:
            self._idx += 1
            await self.release_second.wait()
            return self.chunks[1]
        await asyncio.sleep(0.01)
        return b""


async def test_wait_for_terminal_frame_correlates_delta_and_snapshot() -> None:
    prompt = b"Enter your choice [T] ?"
    session = _ConcreteSession(_FakeTransport([prompt]), receive_encoding="utf-8")
    since = session.screen_change_seq()

    await session.connect()
    frame = await session.wait_for_terminal_frame(since=since, timeout_ms=1000)
    await session.close()

    assert frame is not None
    assert frame.sequence == since + 1
    assert frame.transcript_delta == prompt.decode()
    assert frame.snapshot["screen"].rstrip().endswith(prompt.decode())
    assert frame.snapshot["cursor"] == frame.cursor


async def test_wait_for_terminal_frame_returns_queued_frames_in_order() -> None:
    session = _ConcreteSession(_FakeTransport([b"first", b" second"]), receive_encoding="utf-8")
    since = session.screen_change_seq()

    await session.connect()
    while session.screen_change_seq() < since + 2:
        await asyncio.sleep(0)

    first = await session.wait_for_terminal_frame(since=since, timeout_ms=1000)
    assert first is not None
    second = await session.wait_for_terminal_frame(since=first.sequence, timeout_ms=1000)
    await session.close()

    assert second is not None
    assert first.sequence == since + 1
    assert first.transcript_delta == "first"
    assert first.snapshot["screen"].rstrip().endswith("first")
    assert second.sequence == since + 2
    assert second.transcript_delta == " second"
    assert second.snapshot["screen"].rstrip().endswith("first second")


async def test_wait_for_terminal_frame_timeout_returns_none_within_budget() -> None:
    session = _ConcreteSession(_FakeTransport())
    loop = asyncio.get_running_loop()
    started = loop.time()

    frame = await session.wait_for_terminal_frame(since=0, timeout_ms=20)

    elapsed = loop.time() - started
    assert frame is None
    assert elapsed < 0.2


async def test_terminal_frame_excludes_control_protocol_bytes_from_delta() -> None:
    control = encode_control_frame({"type": "render_speed", "cps": 2400}).encode()
    session = _ConcreteSession(
        _FakeTransport([control + b"terminal text"]),
        receive_encoding="utf-8",
        control_frames=True,
    )

    await session.connect()
    frame = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    await session.close()

    assert frame is not None
    assert frame.transcript_delta == "terminal text"
    assert "render_speed" not in frame.transcript_delta


class _MutableSnapshotEmulator:
    def __init__(self) -> None:
        self.current: dict[str, Any] = {
            "screen": "",
            "cursor": {"x": 0, "y": 0},
            "metadata": {"nested": {"state": "original"}},
        }

    def process(self, data: bytes) -> None:
        self.current["screen"] = data.decode()
        self.current["cursor"]["x"] = len(data)

    def get_snapshot(self) -> dict[str, Any]:
        return self.current


class _SizedSnapshotEmulator(_MutableSnapshotEmulator):
    def __init__(self) -> None:
        super().__init__()
        self.current["raw_tail"] = "r" * 200
        self.current["metadata"] = {"nested": {"payload": "n" * 300}}


async def test_terminal_frame_owns_nested_snapshot_and_copies_cursor() -> None:
    session = _ConcreteSession(_FakeTransport([b"owned"]), receive_encoding="utf-8")
    emulator = _MutableSnapshotEmulator()
    session._emulator = emulator  # type: ignore[assignment]

    await session.connect()
    frame = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    await session.close()

    assert frame is not None
    emulator.current["cursor"]["x"] = 999
    emulator.current["metadata"]["nested"]["state"] = "mutated"
    copied_cursor = frame.cursor
    copied_cursor["x"] = 123

    assert frame.snapshot["cursor"] == {"x": 5, "y": 0}
    assert frame.snapshot["metadata"] == {"nested": {"state": "original"}}
    assert frame.cursor == {"x": 5, "y": 0}


async def test_terminal_frame_waiters_receive_independently_owned_snapshots() -> None:
    session = _ConcreteSession(_FakeTransport([b"shared"]), receive_encoding="utf-8")
    emulator = _MutableSnapshotEmulator()
    session._emulator = emulator  # type: ignore[assignment]

    await session.connect()
    consumer_a = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    assert consumer_a is not None
    consumer_a.snapshot["cursor"]["x"] = 999
    consumer_a.snapshot["metadata"]["nested"]["state"] = "consumer-a"

    consumer_b = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    retained = session._terminal_frames[0]
    await session.close()

    assert consumer_b is not None
    assert consumer_b is not retained
    assert consumer_b.snapshot["cursor"] == {"x": 6, "y": 0}
    assert consumer_b.snapshot["metadata"] == {"nested": {"state": "original"}}
    assert retained.snapshot["cursor"] == {"x": 6, "y": 0}
    assert retained.snapshot["metadata"] == {"nested": {"state": "original"}}


async def test_legacy_event_clear_cannot_lose_terminal_frame_wakeup() -> None:
    transport = _GatedReadTransport(b"ready")
    session = _ConcreteSession(transport, receive_encoding="utf-8")
    legacy_event = _PausedWaitEvent()
    session._update_event = legacy_event
    legacy_waiter: asyncio.Task[bool] | None = None

    await session.connect()
    frame_waiter = asyncio.create_task(session.wait_for_terminal_frame(since=0, timeout_ms=10_000))
    for _ in range(10):
        await asyncio.sleep(0)
        if legacy_event.wait_started.is_set():
            break

    try:
        transport.release.set()
        while session.screen_change_seq() == 0:
            await asyncio.sleep(0)

        legacy_waiter = asyncio.create_task(session.wait_for_update(timeout_ms=20))
        await asyncio.sleep(0)
        legacy_event.proceed.set()

        frame = await asyncio.wait_for(frame_waiter, timeout=0.2)
        assert frame is not None
        assert frame.transcript_delta == "ready"
    finally:
        legacy_event.proceed.set()
        if legacy_waiter is not None:
            await legacy_waiter
        if not frame_waiter.done():
            frame_waiter.cancel()
        await session.close()


async def test_simultaneous_terminal_frame_waiters_are_all_notified() -> None:
    transport = _GatedReadTransport(b"all")
    session = _ConcreteSession(transport, receive_encoding="utf-8")
    await session.connect()
    waiter_a = asyncio.create_task(session.wait_for_terminal_frame(since=0, timeout_ms=1000))
    waiter_b = asyncio.create_task(session.wait_for_terminal_frame(since=0, timeout_ms=1000))

    await asyncio.sleep(0)
    transport.release.set()
    frame_a, frame_b = await asyncio.wait_for(asyncio.gather(waiter_a, waiter_b), timeout=0.2)
    await session.close()

    assert frame_a is not None
    assert frame_b is not None
    assert frame_a.sequence == frame_b.sequence == 1
    assert frame_a is not frame_b


async def test_close_wakes_terminal_frame_waiter_promptly() -> None:
    session = _ConcreteSession(_GatedReadTransport(b"never"), receive_encoding="utf-8")
    await session.connect()
    waiter = asyncio.create_task(session.wait_for_terminal_frame(since=0, timeout_ms=10_000))
    await asyncio.sleep(0)

    started = asyncio.get_running_loop().time()
    await session.close()
    frame = await asyncio.wait_for(waiter, timeout=0.2)

    assert frame is None
    assert asyncio.get_running_loop().time() - started < 0.2


async def test_close_returns_retained_frame_before_closed_outcome() -> None:
    session = _ConcreteSession(_FakeTransport([b"retained"]), receive_encoding="utf-8")
    await session.connect()
    frame = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    assert frame is not None
    await session.close()

    retained = await session.wait_for_terminal_frame(since=0, timeout_ms=10_000)
    exhausted = await asyncio.wait_for(
        session.wait_for_terminal_frame(since=frame.sequence, timeout_ms=10_000),
        timeout=0.2,
    )

    assert retained is not None
    assert retained.sequence == frame.sequence
    assert exhausted is None


async def test_remote_disconnect_wakes_terminal_frame_waiter_promptly() -> None:
    session = _ConcreteSession(_FakeTransport([ConnectionResetError("remote closed")]))
    await session.connect()

    started = asyncio.get_running_loop().time()
    frame = await asyncio.wait_for(
        session.wait_for_terminal_frame(since=0, timeout_ms=10_000),
        timeout=0.2,
    )
    await session.close()

    assert frame is None
    assert asyncio.get_running_loop().time() - started < 0.2


async def test_terminal_frame_waiter_cancellation_propagates() -> None:
    session = _ConcreteSession(_GatedReadTransport(b"never"))
    await session.connect()
    waiter = asyncio.create_task(session.wait_for_terminal_frame(since=0, timeout_ms=10_000))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    await session.close()


async def test_terminal_frame_history_evicts_by_retained_byte_budget() -> None:
    chunk = b"x" * 64
    transport = _TwoGatedReadTransport(chunk, chunk)
    session = _ConcreteSession(transport, receive_encoding="utf-8")
    emulator = _SizedSnapshotEmulator()
    session._emulator = emulator  # type: ignore[assignment]

    await session.connect()
    first = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    assert first is not None
    minimum_payload_bytes = (
        len(chunk) * 2 + len(emulator.current["raw_tail"]) + len(emulator.current["metadata"]["nested"]["payload"])
    )
    assert session._terminal_frame_bytes >= minimum_payload_bytes
    session._terminal_frame_max_bytes = session._terminal_frame_bytes + 1

    transport.release_second.set()
    second = await session.wait_for_terminal_frame(since=first.sequence, timeout_ms=1000)
    await session.close()

    assert second is not None
    assert len(session._terminal_frames) == 1
    assert session._terminal_frames[0].sequence == second.sequence
    assert session._terminal_frame_bytes <= session._terminal_frame_max_bytes


async def test_terminal_frame_history_count_overflow_returns_oldest_retained_gap() -> None:
    chunks = [str(index % 10).encode() for index in range(129)]
    session = _ConcreteSession(_FakeTransport(chunks), receive_encoding="utf-8")

    await session.connect()
    while session.screen_change_seq() < len(chunks):
        await asyncio.sleep(0)
    oldest = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    await session.close()

    assert oldest is not None
    assert len(session._terminal_frames) == 128
    assert oldest.sequence == 2


async def test_terminal_frame_history_retains_oversized_newest_frame_complete() -> None:
    chunk = b"oversized newest"
    transport = _GatedReadTransport(chunk)
    session = _ConcreteSession(transport, receive_encoding="utf-8")
    session._emulator = _SizedSnapshotEmulator()  # type: ignore[assignment]
    session._terminal_frame_max_bytes = 1

    await session.connect()
    transport.release.set()
    frame = await session.wait_for_terminal_frame(since=0, timeout_ms=1000)
    await session.close()

    assert frame is not None
    assert frame.transcript_delta == chunk.decode()
    assert len(session._terminal_frames) == 1
    assert session._terminal_frames[0].transcript_delta == chunk.decode()
    assert session._terminal_frame_bytes > session._terminal_frame_max_bytes


async def test_terminal_frame_wait_nonpositive_timeout_returns_immediately() -> None:
    session = _ConcreteSession(_FakeTransport())
    started = asyncio.get_running_loop().time()

    zero = await session.wait_for_terminal_frame(since=0, timeout_ms=0)
    negative = await session.wait_for_terminal_frame(since=0, timeout_ms=-1)
    extreme_negative = await session.wait_for_terminal_frame(since=0, timeout_ms=-(10**1000))

    assert zero is None
    assert negative is None
    assert extreme_negative is None
    assert asyncio.get_running_loop().time() - started < 0.2


async def test_terminal_frame_wait_accepts_24_hour_timeout_boundary() -> None:
    session = _ConcreteSession(_FakeTransport())
    await session.close()

    assert await session.wait_for_terminal_frame(since=0, timeout_ms=86_400_000) is None


async def test_terminal_frame_wait_rejects_timeout_above_24_hours() -> None:
    session = _ConcreteSession(_FakeTransport())
    await session.close()

    with pytest.raises(ValueError, match="timeout_ms"):
        await session.wait_for_terminal_frame(since=0, timeout_ms=86_400_001)


async def test_terminal_frame_wait_rejects_extreme_integer_without_overflow() -> None:
    session = _ConcreteSession(_FakeTransport())

    with pytest.raises(ValueError, match="timeout_ms"):
        await session.wait_for_terminal_frame(since=0, timeout_ms=10**1000)
