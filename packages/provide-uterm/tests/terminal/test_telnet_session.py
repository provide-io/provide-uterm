#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for TelnetSession with mocked transport and emulator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.telnet_session import TelnetSession, connect_telnet

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_transport() -> MagicMock:
    """Create a mock TelnetTransport."""
    transport = MagicMock()
    transport.connect = AsyncMock()
    transport.disconnect = AsyncMock()
    transport.send = AsyncMock()
    # receive returns data once then empty (simulates idle)
    transport.receive = AsyncMock(side_effect=[b"Hello\r\n", b"", b"", b""])
    return transport


def _mock_emulator() -> MagicMock:
    """Create a mock TerminalEmulator."""
    emu = MagicMock()
    emu.get_snapshot.return_value = {
        "screen": "Hello",
        "cursor": {"x": 5, "y": 0},
        "cols": 80,
        "rows": 25,
    }
    return emu


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_constructor_defaults() -> None:
    session = TelnetSession("localhost", 23)
    assert session.host == "localhost"
    assert session.port == 23
    assert session._cols == 80
    assert session._rows == 25
    assert session._term == "ANSI"
    assert session._connect_timeout == 30.0
    assert not session.is_connected()


def test_constructor_custom_params() -> None:
    session = TelnetSession("example.com", 2102, cols=120, rows=40, term="VT100", connect_timeout=10.0)
    assert session._cols == 120
    assert session._rows == 40
    assert session._term == "VT100"
    assert session._connect_timeout == 10.0


def test_constructor_control_frames_off_by_default() -> None:
    assert TelnetSession("localhost", 23)._control_decoder is None


def test_constructor_control_frames_propagates() -> None:
    session = TelnetSession("localhost", 23, control_frames=True)
    assert session._control_decoder is not None


# ---------------------------------------------------------------------------
# connect / close lifecycle
# ---------------------------------------------------------------------------


async def test_connect_starts_reader() -> None:
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    session._transport = transport
    session._emulator = _mock_emulator()

    await session.connect()

    transport.connect.assert_awaited_once_with("localhost", 23, cols=80, rows=25, term="ANSI", timeout=30.0)
    assert session.is_connected()
    assert session._read_task is not None

    await session.close()
    assert not session.is_connected()
    assert session._read_task is None
    transport.disconnect.assert_awaited_once()


async def test_close_without_connect() -> None:
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    session._transport = transport
    # close when not connected — should not error
    await session.close()
    assert not session.is_connected()


async def test_async_context_manager() -> None:
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    session._transport = transport
    session._emulator = _mock_emulator()

    async with session:
        assert session.is_connected()
    assert not session.is_connected()


# ---------------------------------------------------------------------------
# connect_telnet factory
# ---------------------------------------------------------------------------


async def test_connect_telnet_factory() -> None:
    with patch("provide.uterm.telnet_session.TelnetTransport") as mock_transport_cls:
        mock_t = _mock_transport()
        mock_transport_cls.return_value = mock_t

        session = await connect_telnet("bbs.example.com", 2102, cols=120, rows=40, term="VT100")

        assert session.is_connected()
        assert session._control_decoder is None
        mock_t.connect.assert_awaited_once()

        await session.close()


async def test_connect_telnet_factory_control_frames() -> None:
    with patch("provide.uterm.telnet_session.TelnetTransport") as mock_transport_cls:
        mock_transport_cls.return_value = _mock_transport()

        session = await connect_telnet("bbs.example.com", 2102, control_frames=True)

        assert session._control_decoder is not None
        await session.close()


# ---------------------------------------------------------------------------
# snapshot / send
# ---------------------------------------------------------------------------


async def test_snapshot_delegates_to_emulator() -> None:
    session = TelnetSession("localhost", 23)
    emu = _mock_emulator()
    session._emulator = emu
    snap = session.snapshot()
    emu.get_snapshot.assert_called_once()
    assert snap["screen"] == "Hello"


async def test_send_encodes_cp437() -> None:
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    session._transport = transport
    await session.send("Hello\r")
    transport.send.assert_awaited_once_with(b"Hello\r")


# ---------------------------------------------------------------------------
# wait_for_update
# ---------------------------------------------------------------------------


async def test_wait_for_update_timeout() -> None:
    session = TelnetSession("localhost", 23)
    result = await session.wait_for_update(timeout_ms=50)
    assert result is False


async def test_wait_for_update_signaled() -> None:
    session = TelnetSession("localhost", 23)

    async def signal_soon() -> None:
        await asyncio.sleep(0.02)
        session._update_event.set()

    asyncio.create_task(signal_soon())  # noqa: RUF006
    result = await session.wait_for_update(timeout_ms=2000)
    assert result is True


# ---------------------------------------------------------------------------
# screen_change_seq / update_seq
# ---------------------------------------------------------------------------


def test_screen_change_seq_starts_at_zero() -> None:
    session = TelnetSession("localhost", 23)
    assert session.screen_change_seq() == 0
    assert session.update_seq() == 0


# ---------------------------------------------------------------------------
# wait_for_screen_change
# ---------------------------------------------------------------------------


async def test_wait_for_screen_change_timeout() -> None:
    session = TelnetSession("localhost", 23)
    result = await session.wait_for_screen_change(timeout_ms=50, since=0)
    assert result is False


async def test_wait_for_screen_change_already_changed() -> None:
    session = TelnetSession("localhost", 23)
    session._change_seq = 5
    result = await session.wait_for_screen_change(timeout_ms=1000, since=3)
    assert result is True


async def test_wait_for_screen_change_signaled() -> None:
    session = TelnetSession("localhost", 23)

    async def bump_seq() -> None:
        await asyncio.sleep(0.02)
        session._change_seq = 1
        session._update_event.set()

    asyncio.create_task(bump_seq())  # noqa: RUF006
    result = await session.wait_for_screen_change(timeout_ms=2000, since=0)
    assert result is True


async def test_wait_for_screen_change_none_since() -> None:
    """since=None waits for any update."""
    session = TelnetSession("localhost", 23)

    async def bump_seq() -> None:
        await asyncio.sleep(0.02)
        session._change_seq = 1
        session._update_event.set()

    asyncio.create_task(bump_seq())  # noqa: RUF006
    result = await session.wait_for_screen_change(timeout_ms=2000, since=None)
    # since is None, so loops until _change_seq > 0 OR timeout
    # After signal: _change_seq=1, since=None → times out, checks _change_seq > (since or 0) = 1 > 0 → True
    assert result is True


# ---------------------------------------------------------------------------
# _reader_loop
# ---------------------------------------------------------------------------


async def test_reader_loop_processes_data() -> None:
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    transport.receive = AsyncMock(side_effect=[b"Hello", b"World", ConnectionResetError("reset")])
    session._transport = transport
    emu = _mock_emulator()
    session._emulator = emu

    await session.connect()
    # Give the reader loop time to process
    await asyncio.sleep(0.2)

    # Emulator should have been called with the data
    assert emu.process.call_count >= 1

    await session.close()


async def test_reader_loop_handles_empty_data() -> None:
    """Empty receive (b'') should not feed emulator but keep looping."""
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    call_count = 0

    async def receive_fn(max_bytes: int, timeout_ms: int) -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return b""  # empty — no data
        if call_count == 2:
            return b"data"
        raise ConnectionResetError("done")

    transport.receive = receive_fn
    session._transport = transport
    emu = _mock_emulator()
    session._emulator = emu

    await session.connect()
    await asyncio.sleep(0.3)

    # Emulator only called for non-empty data
    assert emu.process.call_count == 1
    emu.process.assert_called_with(b"data")

    await session.close()


async def test_reader_loop_connection_error_disconnects() -> None:
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    transport.receive = AsyncMock(side_effect=OSError("connection lost"))
    session._transport = transport
    session._emulator = _mock_emulator()

    await session.connect()
    await asyncio.sleep(0.1)

    # Reader loop caught the OSError and set _connected = False
    assert not session.is_connected()
    await session.close()


async def test_reader_loop_increments_change_seq() -> None:
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    transport.receive = AsyncMock(side_effect=[b"a", b"b", ConnectionResetError("done")])
    session._transport = transport
    session._emulator = _mock_emulator()

    await session.connect()
    await asyncio.sleep(0.2)

    assert session.screen_change_seq() >= 2
    await session.close()


async def test_wait_for_screen_change_expired_deadline() -> None:
    """When remaining time is already <= 0, returns False immediately (line 189).

    We patch the event loop's time() so that the second call (for remaining)
    returns a value past the deadline, guaranteeing remaining <= 0.
    """
    session = TelnetSession("localhost", 23)
    loop = asyncio.get_event_loop()
    original_time = loop.time

    call_count = 0

    def mock_time() -> float:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 1000.0  # deadline = 1000.0 + timeout_ms/1000
        return 2000.0  # way past deadline

    loop.time = mock_time  # type: ignore[assignment]
    try:
        result = await session.wait_for_screen_change(timeout_ms=100, since=0)
        assert result is False
    finally:
        loop.time = original_time  # type: ignore[assignment]


async def test_reader_loop_not_connected_exits() -> None:
    """Reader loop exits immediately when _connected is False (branch 201->exit)."""
    session = TelnetSession("localhost", 23)
    session._connected = False
    # Run the reader loop directly — should exit immediately
    await session._reader_loop()
    # No crash, no data processed


# ---------------------------------------------------------------------------
# ansi_screen — delegates to emulator (line 157)
# ---------------------------------------------------------------------------


async def test_ansi_screen_delegates_to_emulator() -> None:
    """Covers line 157: TelnetSession.ansi_screen delegates to emulator."""
    session = TelnetSession("localhost", 23)
    emu = _mock_emulator()
    emu.ansi_screen.return_value = "\x1b[31mred\x1b[0m"
    session._emulator = emu
    out = session.ansi_screen()
    emu.ansi_screen.assert_called_once()
    assert out == "\x1b[31mred\x1b[0m"


# ---------------------------------------------------------------------------
# add_watch — watcher registration and reader-loop fan-out (lines 245-246, 258-260)
# ---------------------------------------------------------------------------


def test_add_watch_appends_callback() -> None:
    """Covers lines 245-246: add_watch stores the callback in _watchers."""
    session = TelnetSession("localhost", 23)

    def cb(_state: dict, _raw: bytes) -> None:
        return None

    session.add_watch(cb, interval_s=0.5)  # interval_s is reserved/ignored
    assert cb in session._watchers


async def test_reader_loop_fans_out_to_watchers() -> None:
    """Covers lines 257-260: reader loop calls each watcher with raw bytes
    before the emulator consumes them."""
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    transport.receive = AsyncMock(side_effect=[b"chunk-A", b"chunk-B", ConnectionResetError("done")])
    session._transport = transport
    session._emulator = _mock_emulator()

    received: list[bytes] = []

    def watcher(_state: dict[str, object], raw: bytes) -> None:
        received.append(raw)

    session.add_watch(watcher)

    await session.connect()
    await asyncio.sleep(0.2)
    await session.close()

    assert b"chunk-A" in received
    assert b"chunk-B" in received


async def test_reader_loop_swallows_watcher_exceptions() -> None:
    """Covers the contextlib.suppress branch around the watcher call (line 259):
    a misbehaving watcher must not break the reader loop."""
    session = TelnetSession("localhost", 23)
    transport = _mock_transport()
    transport.receive = AsyncMock(side_effect=[b"data-1", b"data-2", ConnectionResetError("done")])
    session._transport = transport
    emu = _mock_emulator()
    session._emulator = emu

    calls = 0

    def bad_watcher(_state: dict[str, object], _raw: bytes) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("watcher boom")

    session.add_watch(bad_watcher)

    await session.connect()
    await asyncio.sleep(0.2)
    await session.close()

    # Emulator still received data despite watcher exceptions.
    assert emu.process.call_count >= 1
    assert calls >= 1
