#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the shared TransportSession base.

Drives the base directly via a tiny fake :class:`ConnectionTransport` that
yields scripted bytes, plus a concrete subclass whose ``_connect_transport``
hook records that it was invoked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import pytest
from provide.uterm.transport_session import TransportSession

from provide.uterm import transport_session

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Scripted ConnectionTransport: ``receive`` replays a list of chunks/errors."""

    def __init__(self, script: Sequence[bytes | BaseException] | None = None) -> None:
        self.script: list[bytes | BaseException] = list(script or [])
        self.sent: list[bytes] = []
        self.connect_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.disconnect_count = 0
        self._idx = 0

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        self.connect_calls.append((args, kwargs))

    async def disconnect(self) -> None:
        self.disconnect_count += 1

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        if self._idx < len(self.script):
            item = self.script[self._idx]
            self._idx += 1
            if isinstance(item, BaseException):
                raise item
            return item
        # Exhausted: behave like an idle wire to keep the loop spinning.
        await asyncio.sleep(0.01)
        return b""

    def is_connected(self) -> bool:
        return True


class _ConcreteSession(TransportSession):
    """Minimal subclass that records the ``_connect_transport`` hook call."""

    def __init__(self, transport: _FakeTransport, **kwargs: Any) -> None:
        super().__init__(transport, **kwargs)
        self.connect_hook_calls = 0

    async def _connect_transport(self) -> None:
        self.connect_hook_calls += 1
        await self._transport.connect("h", 1, cols=self._cols, rows=self._rows)


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_constructor_defaults() -> None:
    transport = _FakeTransport()
    session = TransportSession(transport)
    assert session._transport is transport
    assert session._cols == 80
    assert session._rows == 25
    assert session._send_encoding == "utf-8"
    assert session._change_seq == 0
    assert session._read_task is None
    assert session._watchers == []
    assert not session.is_connected()
    # Emulator built with the configured geometry.
    assert session._emulator.cols == 80
    assert session._emulator.rows == 25


def test_constructor_custom_params() -> None:
    session = TransportSession(_FakeTransport(), cols=120, rows=40, send_encoding="cp437")
    assert session._cols == 120
    assert session._rows == 40
    assert session._send_encoding == "cp437"
    assert session._emulator.cols == 120
    assert session._emulator.rows == 40


# ---------------------------------------------------------------------------
# _connect_transport hook
# ---------------------------------------------------------------------------


async def test_base_connect_transport_not_implemented() -> None:
    """The base hook must be abstract-by-convention (NotImplementedError)."""
    session = TransportSession(_FakeTransport())
    with pytest.raises(NotImplementedError):
        await session._connect_transport()


async def test_connect_invokes_hook_and_starts_reader() -> None:
    transport = _FakeTransport([])
    session = _ConcreteSession(transport)

    await session.connect()

    assert session.connect_hook_calls == 1
    assert transport.connect_calls == [(("h", 1), {"cols": 80, "rows": 25})]
    assert session.is_connected()
    assert session._read_task is not None

    await session.close()


# ---------------------------------------------------------------------------
# reader loop: feeds emulator, bumps seq, sets event, fans out to watchers
# ---------------------------------------------------------------------------


async def test_reader_feeds_emulator_and_bumps_seq() -> None:
    transport = _FakeTransport([b"\x1b[31mhi", ConnectionResetError("done")])
    session = _ConcreteSession(transport)

    await session.connect()
    await asyncio.sleep(0.1)

    assert session.screen_change_seq() >= 1
    assert "hi" in session.snapshot()["screen"]
    await session.close()


async def test_reader_fires_watchers_before_emulator_in_order() -> None:
    """Watchers see the raw bytes, and they fire before the emulator consumes."""
    order: list[str] = []

    class _SpyEmulatorSession(_ConcreteSession):
        async def _connect_transport(self) -> None:  # keep hook identical
            await super()._connect_transport()

    transport = _FakeTransport([b"RAW", ConnectionResetError("done")])
    session = _SpyEmulatorSession(transport)

    # Wrap the emulator's process to record relative order.
    real_process = session._emulator.process

    def _spy_process(data: bytes) -> None:
        order.append(f"emulator:{data!r}")
        real_process(data)

    session._emulator.process = _spy_process  # type: ignore[method-assign]

    seen: list[bytes] = []

    def watcher_a(_state: dict[str, Any], raw: bytes) -> None:
        order.append("watcher_a")
        seen.append(raw)

    def watcher_b(_state: dict[str, Any], raw: bytes) -> None:
        order.append("watcher_b")

    session.add_watch(watcher_a)
    session.add_watch(watcher_b)

    await session.connect()
    await asyncio.sleep(0.1)
    await session.close()

    assert seen == [b"RAW"]
    # Watchers fire in registration order, both before the emulator.
    assert order == ["watcher_a", "watcher_b", "emulator:b'RAW'"]


async def test_reader_swallows_watcher_exceptions() -> None:
    transport = _FakeTransport([b"d1", b"d2", ConnectionResetError("done")])
    session = _ConcreteSession(transport)
    calls = 0

    def bad_watcher(_state: dict[str, Any], _raw: bytes) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    session.add_watch(bad_watcher)
    await session.connect()
    await asyncio.sleep(0.1)
    await session.close()

    assert calls >= 1
    # Emulator still advanced despite the watcher raising.
    assert session.screen_change_seq() >= 1


async def test_reader_skips_empty_reads() -> None:
    """Empty receive (b'') keeps looping but does not feed the emulator."""
    call_count = 0

    class _Script(_FakeTransport):
        async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return b""
            if call_count == 2:
                return b"data"
            raise ConnectionResetError("done")

    session = _ConcreteSession(_Script())
    await session.connect()
    await asyncio.sleep(0.1)
    await session.close()

    assert session.snapshot()["screen"].startswith("data")
    # Only the non-empty read advanced the sequence.
    assert session.screen_change_seq() == 1


async def test_reader_loop_exits_when_not_connected() -> None:
    """Reader loop exits immediately when ``_connected`` is False."""
    session = _ConcreteSession(_FakeTransport([b"never-read"]))
    session._connected = False
    await session._reader_loop()  # must not raise, must not process
    assert session.screen_change_seq() == 0


async def test_reader_loop_connection_error_clears_connected() -> None:
    transport = _FakeTransport([OSError("lost")])
    session = _ConcreteSession(transport)
    await session.connect()
    await asyncio.sleep(0.05)
    assert not session.is_connected()
    await session.close()


# ---------------------------------------------------------------------------
# send: encodes via the configured encoding
# ---------------------------------------------------------------------------


async def test_send_encodes_utf8() -> None:
    transport = _FakeTransport()
    session = TransportSession(transport, send_encoding="utf-8")
    await session.send("héllo")
    assert transport.sent == ["héllo".encode()]


async def test_send_encodes_cp437() -> None:
    transport = _FakeTransport()
    session = TransportSession(transport, send_encoding="cp437")
    await session.send("Hello\r")
    assert transport.sent == [b"Hello\r"]


async def test_send_expect_delegates_to_shared_helper() -> None:
    transport = _FakeTransport()
    session = TransportSession(transport)
    result = await session.send_expect("Hello\r", expect_text="Hello", timeout_ms=0, sanitize=False)

    assert result.timed_out is True
    assert transport.sent == [b"Hello\r"]


async def test_send_encoding_errors_replace() -> None:
    """Characters not representable in the encoding are replaced, never raise."""
    transport = _FakeTransport()
    session = TransportSession(transport, send_encoding="cp437")
    # U+1F600 has no cp437 representation → errors="replace".
    await session.send("\U0001f600")
    assert transport.sent == ["\U0001f600".encode("cp437", errors="replace")]


# ---------------------------------------------------------------------------
# snapshot / ansi_screen delegate to the emulator
# ---------------------------------------------------------------------------


async def test_snapshot_and_ansi_screen_delegate() -> None:
    session = TransportSession(_FakeTransport())
    session._emulator.process(b"\x1b[31mred")
    snap = session.snapshot()
    assert "red" in snap["screen"]
    assert "red" in session.ansi_screen()


# ---------------------------------------------------------------------------
# wait_for_update
# ---------------------------------------------------------------------------


async def test_wait_for_update_timeout() -> None:
    session = TransportSession(_FakeTransport())
    assert await session.wait_for_update(timeout_ms=20) is False


async def test_wait_for_update_signaled() -> None:
    session = TransportSession(_FakeTransport())

    async def signal_soon() -> None:
        await asyncio.sleep(0.02)
        session._update_event.set()

    asyncio.create_task(signal_soon())  # noqa: RUF006
    assert await session.wait_for_update(timeout_ms=2000) is True


# ---------------------------------------------------------------------------
# screen_change_seq / update_seq alias
# ---------------------------------------------------------------------------


def test_screen_change_seq_and_update_seq_alias() -> None:
    session = TransportSession(_FakeTransport())
    assert session.screen_change_seq() == 0
    assert session.update_seq() == 0
    session._change_seq = 7
    assert session.screen_change_seq() == 7
    assert session.update_seq() == 7


# ---------------------------------------------------------------------------
# wait_for_screen_change
# ---------------------------------------------------------------------------


async def test_wait_for_screen_change_already_advanced() -> None:
    session = TransportSession(_FakeTransport())
    session._change_seq = 5
    assert await session.wait_for_screen_change(timeout_ms=1000, since=3) is True


async def test_wait_for_screen_change_timeout() -> None:
    session = TransportSession(_FakeTransport())
    assert await session.wait_for_screen_change(timeout_ms=20, since=0) is False


async def test_wait_for_screen_change_none_since() -> None:
    session = TransportSession(_FakeTransport())

    async def bump() -> None:
        await asyncio.sleep(0.02)
        session._change_seq = 1
        session._update_event.set()

    asyncio.create_task(bump())  # noqa: RUF006
    # since=None: after the bump the wait times out, then recheck 1 > 0 → True.
    assert await session.wait_for_screen_change(timeout_ms=2000, since=None) is True


async def test_wait_for_screen_change_signaled_then_advanced() -> None:
    session = TransportSession(_FakeTransport())

    async def bump() -> None:
        await asyncio.sleep(0.02)
        session._change_seq = 9
        session._update_event.set()

    asyncio.create_task(bump())  # noqa: RUF006
    assert await session.wait_for_screen_change(timeout_ms=2000, since=0) is True


async def test_wait_for_screen_change_expired_deadline() -> None:
    """remaining <= 0 on the first loop returns False (deadline already passed)."""
    session = TransportSession(_FakeTransport())
    loop = asyncio.get_event_loop()
    original_time = loop.time
    call_count = 0

    def mock_time() -> float:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 1000.0
        return 2000.0

    loop.time = mock_time  # type: ignore[assignment]
    try:
        assert await session.wait_for_screen_change(timeout_ms=100, since=0) is False
    finally:
        loop.time = original_time  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# close: idempotent, cancels reader, disconnects
# ---------------------------------------------------------------------------


async def test_close_cancels_reader_and_disconnects() -> None:
    transport = _FakeTransport([])
    session = _ConcreteSession(transport)
    await session.connect()
    assert session._read_task is not None

    await session.close()
    assert not session.is_connected()
    assert session._read_task is None
    assert transport.disconnect_count == 1

    # Idempotent: a second close does not error and disconnects again.
    await session.close()
    assert transport.disconnect_count == 2


async def test_close_without_connect() -> None:
    transport = _FakeTransport()
    session = _ConcreteSession(transport)
    await session.close()
    assert not session.is_connected()
    assert transport.disconnect_count == 1


# ---------------------------------------------------------------------------
# async context manager
# ---------------------------------------------------------------------------


async def test_async_context_manager() -> None:
    transport = _FakeTransport([])
    session = _ConcreteSession(transport)
    async with session as entered:
        assert entered is session
        assert session.is_connected()
    assert not session.is_connected()


# ---------------------------------------------------------------------------
# add_watch
# ---------------------------------------------------------------------------


def test_add_watch_appends_callback() -> None:
    session = TransportSession(_FakeTransport())

    def cb(_state: dict[str, Any], _raw: bytes) -> None:
        return None

    session.add_watch(cb, interval_s=0.5)  # interval_s reserved/ignored
    assert cb in session._watchers


# ---------------------------------------------------------------------------
# is_connected
# ---------------------------------------------------------------------------


async def test_is_connected_reflects_lifecycle() -> None:
    session = _ConcreteSession(_FakeTransport([]))
    assert session.is_connected() is False
    await session.connect()
    assert session.is_connected() is True
    await session.close()
    assert session.is_connected() is False


# ---------------------------------------------------------------------------
# Slow-chunk warning
# ---------------------------------------------------------------------------


class _StepClock:
    """Stand-in for the ``time`` module whose monotonic advances a fixed step.

    The reader loop measures its per-chunk work with ``time.monotonic()``, so a
    real clock cannot cross the slow threshold here without actually sleeping
    that long. Stepping the clock a fixed amount per reading makes the measured
    elapsed exact and the test instant.
    """

    def __init__(self, step: float) -> None:
        self._step = step
        self._now = 0.0

    def monotonic(self) -> float:
        self._now += self._step
        return self._now

    def time(self) -> float:
        return 1_700_000_000.0


async def test_reader_loop_warns_when_a_chunk_is_slow(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A chunk whose processing crosses the threshold reports itself."""
    monkeypatch.setattr(transport_session, "time", _StepClock(0.3))
    session = _ConcreteSession(_FakeTransport([b"slow-chunk", ConnectionResetError("done")]))
    session._connected = True

    with caplog.at_level(logging.WARNING):
        await session._reader_loop()

    slow = [r.getMessage() for r in caplog.records if "reader_chunk_slow" in r.getMessage()]
    assert len(slow) == 1
    # The measured elapsed and the chunk it belongs to both reach the log — a
    # warning that says only "something was slow" is not actionable.
    assert "elapsed_s=0.300" in slow[0]
    assert f"chunk_bytes={len(b'slow-chunk')}" in slow[0]


async def test_reader_loop_stays_quiet_for_a_fast_chunk(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Below the threshold nothing is logged.

    Pairs with the test above deliberately: without it, a threshold that always
    fired would pass just as well, and this warning exists to be rare.
    """
    monkeypatch.setattr(transport_session, "time", _StepClock(0.01))
    session = _ConcreteSession(_FakeTransport([b"fast", ConnectionResetError("done")]))
    session._connected = True

    with caplog.at_level(logging.WARNING):
        await session._reader_loop()

    assert not [r for r in caplog.records if "reader_chunk_slow" in r.getMessage()]
