#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PTYConnector: I/O, poll, handle, snapshot, and buffer tests."""

from __future__ import annotations

import asyncio
import hashlib
import os
from unittest.mock import patch

import pytest

from ._connector_helpers import make_connector


async def test_poll_messages_returns_list() -> None:
    conn = make_connector("/bin/echo", ["hi"])
    await conn.start()
    await asyncio.sleep(0.1)
    msgs = await conn.poll_messages()
    await conn.stop()
    assert isinstance(msgs, list)


async def test_handle_input_returns_snapshot() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs = await conn.handle_input("hello\n")
    await conn.stop()
    assert any(m.get("type") == "snapshot" for m in msgs)


async def test_get_snapshot_returns_dict() -> None:
    conn = make_connector("/bin/echo", ["snap"])
    await conn.start()
    snap = await conn.get_snapshot()
    await conn.stop()
    assert snap["type"] == "snapshot"
    assert "screen" in snap
    assert "cols" in snap
    assert "rows" in snap


async def test_set_mode_returns_hello_and_snapshot() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs = await conn.set_mode("hijack")
    await conn.stop()
    types = [m.get("type") for m in msgs]
    assert "worker_hello" in types
    assert "snapshot" in types
    hellos = [m for m in msgs if m.get("type") == "worker_hello"]
    assert hellos[0]["input_mode"] == "hijack"


async def test_set_mode_invalid_raises() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    with pytest.raises(ValueError, match="invalid mode"):
        await conn.set_mode("superuser")
    await conn.stop()


async def test_clear_returns_empty_snapshot() -> None:
    conn = make_connector("/bin/echo", ["clear-me"])
    await conn.start()
    await asyncio.sleep(0.1)
    msgs = await conn.clear()
    await conn.stop()
    assert any(m.get("type") == "snapshot" for m in msgs)
    screens = [m["screen"] for m in msgs if m.get("type") == "snapshot"]
    assert all(s == "" for s in screens)


async def test_handle_control_pause_resume() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs_pause = await conn.handle_control("pause")
    assert conn._paused is True
    msgs_resume = await conn.handle_control("resume")
    assert conn._paused is False
    await conn.stop()
    assert all(m.get("type") == "snapshot" for m in msgs_pause)
    assert all(m.get("type") == "snapshot" for m in msgs_resume)


async def test_get_analysis_returns_string() -> None:
    conn = make_connector("/bin/echo", ["analysis"])
    await conn.start()
    analysis = await conn.get_analysis()
    await conn.stop()
    assert isinstance(analysis, str)
    assert "/bin/echo" in analysis


async def test_paused_connector_drops_input() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    await conn.handle_control("pause")
    msgs = await conn.poll_messages()
    await conn.stop()
    assert msgs == []


def test_read_master_returns_empty_before_start() -> None:
    conn = make_connector()
    assert conn._read_master() == b""


async def test_handle_control_step_resumes() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    await conn.handle_control("pause")
    assert conn._paused
    msgs = await conn.handle_control("step")
    await conn.stop()
    assert not conn._paused
    assert all(m.get("type") == "snapshot" for m in msgs)


async def test_handle_input_noop_when_paused() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    await conn.handle_control("pause")
    msgs = await conn.handle_input("ignored\n")
    await conn.stop()
    assert any(m.get("type") == "snapshot" for m in msgs)


async def test_poll_messages_empty_when_no_output() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs = await conn.poll_messages()
    await conn.stop()
    assert isinstance(msgs, list)


async def test_handle_control_unknown_action_returns_snapshot() -> None:
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs = await conn.handle_control("unknown_action")
    await conn.stop()
    assert all(m.get("type") == "snapshot" for m in msgs)


async def test_read_master_oserror_marks_disconnected() -> None:
    """_read_master() catches OSError and sets _connected to exactly False."""
    conn = make_connector("/bin/echo", ["done"])
    await conn.start()
    assert conn.is_connected()
    if conn._master_fd is not None:
        os.close(conn._master_fd)
    result = conn._read_master()
    assert result == b""
    assert conn._connected is False
    conn._master_fd = None
    await conn.stop()


def test_read_master_blocking_io_error_returns_empty_bytes() -> None:
    """_read_master() returns exactly b'' on BlockingIOError — kills return b'XXXX' mutation.

    Keep write-end open so os.read raises BlockingIOError (EAGAIN) rather than EOF.
    """
    import fcntl as _fcntl

    conn = make_connector("/bin/echo")
    r_fd, w_fd = os.pipe()
    conn._master_fd = r_fd
    conn._connected = True
    fl = _fcntl.fcntl(r_fd, _fcntl.F_GETFL)
    _fcntl.fcntl(r_fd, _fcntl.F_SETFL, fl | os.O_NONBLOCK)
    result = conn._read_master()
    os.close(r_fd)
    os.close(w_fd)
    conn._master_fd = None
    assert result == b""


async def test_poll_messages_buffer_truncated() -> None:
    """poll_messages() truncates buffer to exactly 32768 chars."""
    conn = make_connector("/bin/cat")
    await conn.start()
    conn._buffer = "a" * 32764
    conn._read_master = lambda: b"b" * 10  # type: ignore[method-assign]
    msgs = await conn.poll_messages()
    await conn.stop()
    assert len(conn._buffer) == 32768
    assert any(m.get("type") == "snapshot" for m in msgs)


async def test_start_buffer_truncation_at_32769() -> None:
    """Buffer of 32769 chars is truncated — kills the > 32769 mutation."""
    conn = make_connector("/bin/cat")
    await conn.start()
    conn._buffer = "a" * 32759
    conn._read_master = lambda: b"b" * 10  # type: ignore[method-assign]
    msgs = await conn.poll_messages()
    await conn.stop()
    assert len(conn._buffer) == 32768
    assert any(m.get("type") == "snapshot" for m in msgs)


async def test_snapshot_keys_and_static_values() -> None:
    """_snapshot() returns dict with exact key names and correct static values."""
    conn = make_connector("/bin/echo", cols=132, rows=50)
    await conn.start()
    snap = await conn.get_snapshot()
    await conn.stop()
    assert snap["type"] == "snapshot"
    assert snap["cols"] == 132
    assert snap["rows"] == 50
    assert "screen" in snap
    assert "screen_hash" in snap
    assert "ts" in snap
    assert snap["cursor"] == {"row": 0, "col": 0}
    assert snap["cursor"]["row"] == 0
    assert snap["cursor"]["col"] == 0
    assert snap["cursor_at_end"] is True
    assert snap["has_trailing_space"] is False
    assert snap["prompt_detected"] is False
    expected_hash = hashlib.md5(snap["screen"].encode()).hexdigest()  # noqa: S324
    assert snap["screen_hash"] == expected_hash


async def test_snapshot_screen_hash_matches_buffer() -> None:
    """screen_hash is md5 of current screen content."""
    conn = make_connector("/bin/cat")
    await conn.start()
    conn._buffer = "hello world"
    snap = await conn.get_snapshot()
    await conn.stop()
    assert snap["screen"] == "hello world"
    assert snap["screen_hash"] == hashlib.md5(b"hello world").hexdigest()  # noqa: S324


async def test_hello_message_keys_and_value() -> None:
    """_hello() uses exact key 'input_mode' with the correct value."""
    conn = make_connector("/bin/cat")
    await conn.start()
    msgs_open = await conn.set_mode("open")
    hellos_open = [m for m in msgs_open if m.get("type") == "worker_hello"]
    assert len(hellos_open) == 1
    assert "input_mode" in hellos_open[0]
    assert hellos_open[0]["input_mode"] == "open"
    msgs_hijack = await conn.set_mode("hijack")
    hellos_hijack = [m for m in msgs_hijack if m.get("type") == "worker_hello"]
    assert hellos_hijack[0]["input_mode"] == "hijack"
    await conn.stop()


async def test_poll_messages_returns_empty_when_paused_with_data() -> None:
    """poll_messages() returns [] when paused even with data available.

    Kills the 'or' → 'and' mutation in the guard condition.
    """
    conn = make_connector("/bin/cat")
    await conn.start()
    await conn.handle_control("pause")
    conn._read_master = lambda: b"data that should be suppressed"  # type: ignore[method-assign]
    msgs = await conn.poll_messages()
    await conn.stop()
    assert msgs == []


async def test_handle_input_writes_to_pty() -> None:
    """handle_input() writes encoded data to master_fd — kills condition mutations."""
    conn = make_connector("/bin/cat")
    await conn.start()
    written: list[bytes] = []
    original_write = os.write

    def _spy_write(fd: int, data: bytes) -> int:
        if fd == conn._master_fd:
            written.append(data)
        return original_write(fd, data)

    with patch("provide.uterm.pty.connector.os.write", side_effect=_spy_write):
        await conn.handle_input("ping\n")
    await conn.stop()
    assert written == [b"ping\n"]


async def test_handle_input_no_write_when_not_connected() -> None:
    """handle_input() does not write when not connected."""
    conn = make_connector("/bin/cat")
    written: list[bytes] = []

    def _spy_write(fd: int, data: bytes) -> int:
        written.append(data)
        return len(data)

    with patch("provide.uterm.pty.connector.os.write", side_effect=_spy_write):
        await conn.handle_input("ping\n")
    assert written == []


async def test_handle_input_no_write_when_disconnected_but_fd_set() -> None:
    """handle_input() does not write when _connected=False even if _master_fd is set.

    Kills the 'and' → 'or' mutation in: is_connected() and _master_fd is not None.
    With 'or', a disconnected connector with a valid fd would still write.
    """
    conn = make_connector("/bin/cat")
    await conn.start()
    # Force disconnected state but keep the fd
    conn._connected = False
    written: list[bytes] = []
    original_write = os.write

    def _spy_write(fd: int, data: bytes) -> int:
        if fd == conn._master_fd:
            written.append(data)
        return original_write(fd, data)

    with patch("provide.uterm.pty.connector.os.write", side_effect=_spy_write):
        await conn.handle_input("should not be written\n")
    conn._connected = True
    await conn.stop()
    assert written == []


async def test_poll_messages_invalid_utf8_replaced() -> None:
    """poll_messages() decodes bytes with errors='replace'."""
    conn = make_connector("/bin/cat")
    await conn.start()
    conn._read_master = lambda: b"\xff\xfe"  # type: ignore[method-assign]
    msgs = await conn.poll_messages()
    await conn.stop()
    assert any(m.get("type") == "snapshot" for m in msgs)
    assert "\ufffd" in conn._buffer


async def test_handle_control_step_sets_paused_false() -> None:
    """handle_control('step') sets _paused to exactly False."""
    conn = make_connector("/bin/cat")
    await conn.start()
    await conn.handle_control("pause")
    assert conn._paused is True
    await conn.handle_control("step")
    assert conn._paused is False
    await conn.stop()


async def test_buffer_capped_at_32768() -> None:
    """Buffer is truncated to last 32768 chars when it exceeds the limit."""
    conn = make_connector("/bin/cat")
    await conn.start()
    conn._buffer = "a" * 32764
    if conn._master_fd is not None:
        os.write(conn._master_fd, b"b" * 10)
    await asyncio.sleep(0.05)
    await conn.poll_messages()
    await conn.stop()
    assert len(conn._buffer) <= 32768
