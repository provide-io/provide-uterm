#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.pty.pam_listener import PamEvent, PamNotifyListener, _parse_event

# ── _parse_event ─────────────────────────────────────────────────────────────


def test_parse_open_event() -> None:
    line = b'{"event":"open","username":"alice","tty":"/dev/pts/3","pid":12345}\n'
    ev = _parse_event(line)
    assert ev is not None
    assert ev.event == "open"
    assert ev.username == "alice"
    assert ev.tty == "/dev/pts/3"
    assert ev.pid == 12345


def test_parse_close_event() -> None:
    line = b'{"event":"close","username":"bob","tty":"/dev/pts/7","pid":99}\n'
    ev = _parse_event(line)
    assert ev is not None
    assert ev.event == "close"
    assert ev.pid == 99


def test_parse_bad_json_returns_none() -> None:
    assert _parse_event(b"not-json\n") is None


def test_parse_unknown_event_returns_none() -> None:
    assert _parse_event(b'{"event":"reboot","username":"root","tty":"","pid":1}\n') is None


def test_parse_missing_username_returns_none() -> None:
    assert _parse_event(b'{"event":"open","username":"","tty":"/dev/pts/1","pid":5}\n') is None


def test_parse_missing_pid_defaults_zero() -> None:
    line = b'{"event":"open","username":"alice","tty":"/dev/pts/0"}\n'
    ev = _parse_event(line)
    assert ev is not None
    assert ev.pid == 0


def test_parse_event_timestamp_set() -> None:
    import time

    t0 = time.time()
    ev = _parse_event(b'{"event":"open","username":"u","tty":"","pid":1}\n')
    assert ev is not None
    assert ev.timestamp >= t0


# ── PamNotifyListener ────────────────────────────────────────────────────────


def test_invalid_socket_path_null_byte() -> None:
    with pytest.raises(ValueError, match="null byte"):
        PamNotifyListener("/run/\x00bad.sock")


def test_invalid_socket_path_relative() -> None:
    with pytest.raises(ValueError, match="absolute"):
        PamNotifyListener("relative/path.sock")


async def test_start_stop_creates_and_removes_socket() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))
        assert Path(path).exists()
        await listener.stop()
        assert not Path(path).exists()


async def test_receives_open_event() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        await _send_line(
            path,
            {"event": "open", "username": "alice", "tty": "/dev/pts/3", "pid": 111},
        )
        await asyncio.sleep(0.05)

        await listener.stop()
        assert len(events) == 1
        assert events[0].event == "open"
        assert events[0].username == "alice"


async def test_receives_close_event() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        await _send_line(path, {"event": "close", "username": "bob", "tty": "/dev/pts/5", "pid": 222})
        await asyncio.sleep(0.05)

        await listener.stop()
        assert events[0].event == "close"


async def test_multiple_events_on_one_connection() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"event":"open","username":"u1","tty":"/dev/pts/1","pid":1}\n')
        writer.write(b'{"event":"close","username":"u1","tty":"/dev/pts/1","pid":1}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert len(events) == 2
        assert events[0].event == "open"
        assert events[1].event == "close"


async def test_bad_json_line_skipped_gracefully() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b"not-json\n")
        writer.write(b'{"event":"open","username":"alice","tty":"","pid":9}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert len(events) == 1  # bad line skipped, good one still received


async def test_handler_exception_does_not_kill_listener() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        call_count = [0]

        async def bad_handler(e: PamEvent) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("handler exploded")

        await listener.start(bad_handler)

        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"event":"open","username":"a","tty":"","pid":1}\n')
        writer.write(b'{"event":"open","username":"b","tty":"","pid":2}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert call_count[0] == 2  # second event still delivered


async def test_stop_without_start_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        listener = PamNotifyListener(str(Path(td) / "notify.sock"))
        await listener.stop()  # must not raise


async def test_double_start_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        await listener.start(lambda e: _collect([], e))
        with pytest.raises(RuntimeError, match="already started"):
            await listener.start(lambda e: _collect([], e))
        await listener.stop()


async def test_multiple_concurrent_connections() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        async def send(username: str, pid: int) -> None:
            await _send_line(path, {"event": "open", "username": username, "tty": "", "pid": pid})

        await asyncio.gather(send("u1", 1), send("u2", 2), send("u3", 3))
        await asyncio.sleep(0.1)

        await listener.stop()
        assert len(events) == 3


def test_socket_path_property() -> None:
    """socket_path property returns the configured path."""
    listener = PamNotifyListener("/tmp/test.sock")
    assert listener.socket_path == "/tmp/test.sock"


async def test_handle_connection_readline_timeout_drops_connection() -> None:
    """readline TimeoutError causes the connection to be dropped gracefully."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        reader = AsyncMock()
        reader.readline.side_effect = TimeoutError("mock timeout")
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await listener._handle_connection(reader, writer)
        writer.close.assert_called_once()
        await listener.stop()


async def test_handle_connection_exception_in_readline_drops_connection() -> None:
    """Non-timeout exception during readline breaks the loop."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        reader = AsyncMock()
        reader.readline.side_effect = ConnectionResetError("reset")
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await listener._handle_connection(reader, writer)
        writer.close.assert_called_once()
        await listener.stop()


async def test_handle_connection_oversized_line_skipped() -> None:
    """Lines longer than _MAX_LINE are skipped with a warning."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        big_line = b"x" * 5000 + b"\n"
        good_line = b'{"event":"open","username":"alice","tty":"","pid":1}\n'

        _reader, writer = await asyncio.open_unix_connection(path)
        writer.write(big_line + good_line)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert len(events) == 1  # big line skipped, good line received


async def test_handle_connection_null_handler_skips_dispatch() -> None:
    """Events are not dispatched when handler is None."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))
        listener._handler = None

        _reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"event":"open","username":"alice","tty":"","pid":1}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert events == []


def test_parse_event_non_numeric_pid_defaults_to_zero() -> None:
    """_parse_event() handles non-numeric pid gracefully."""
    line = b'{"event":"open","username":"alice","tty":"/dev/pts/0","pid":"bad"}\n'
    ev = _parse_event(line)
    assert ev is not None
    assert ev.pid == 0


# ── helpers ──────────────────────────────────────────────────────────────────


async def _collect(events: list[PamEvent], ev: PamEvent) -> None:
    events.append(ev)


async def _send_line(path: str, data: dict) -> None:
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps(data) + "\n").encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def test_notify_socket_is_owner_only(tmp_path) -> None:
    import stat

    sock = tmp_path / "notify.sock"
    listener = PamNotifyListener(str(sock))
    await listener.start(AsyncMock())
    try:
        mode = stat.S_IMODE(sock.stat().st_mode)
        assert mode == 0o600
    finally:
        await listener.stop()
