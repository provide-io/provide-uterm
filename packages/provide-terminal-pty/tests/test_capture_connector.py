# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import struct
import tempfile
from pathlib import Path

import pytest

from provide.terminal.pty.capture import CHANNEL_CONNECT, CHANNEL_STDIN, CHANNEL_STDOUT
from provide.terminal.pty.capture_connector import CaptureConnector


def _make_frame(channel: int, data: bytes) -> bytes:
    return struct.pack(">BI", channel, len(data)) + data


async def _send_frames(path: str, frames: list[bytes]) -> None:
    _reader, writer = await asyncio.open_unix_connection(path)
    for frame in frames:
        writer.write(frame)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def _make_connector(td: str, **kwargs: object) -> CaptureConnector:
    path = str(Path(td) / "cap.sock")
    return CaptureConnector(
        "test-cap-1", "Test Capture", {"socket_path": path, **kwargs}
    )


def test_unknown_config_key_rejected() -> None:
    with pytest.raises(ValueError, match="unknown config keys"):
        CaptureConnector("s1", "name", {"socket_path": "/tmp/x.sock", "bad_key": True})


def test_missing_socket_path_rejected() -> None:
    with pytest.raises(ValueError, match="socket_path"):
        CaptureConnector("s1", "name", {})


def test_is_connected_false_before_start() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        assert conn.is_connected() is False


async def test_start_creates_socket_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        assert Path(conn._socket_path).exists()  # noqa: SLF001
        await conn.stop()


async def test_stop_removes_socket_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        path = conn._socket_path  # noqa: SLF001
        await conn.stop()
        assert not Path(path).exists()


async def test_stop_without_start_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.stop()  # must not raise


async def test_is_connected_after_start() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        assert conn.is_connected() is True
        await conn.stop()


async def test_is_connected_false_after_stop() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        await conn.stop()
        assert conn.is_connected() is False


async def test_poll_messages_empty_when_not_connected() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        msgs = await conn.poll_messages()
        assert msgs == []


async def test_stdout_frame_updates_buffer() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        await _send_frames(conn._socket_path, [_make_frame(CHANNEL_STDOUT, b"hello")])  # noqa: SLF001
        await asyncio.sleep(0.05)
        msgs = await conn.poll_messages()
        assert len(msgs) == 1
        assert msgs[0]["type"] == "term"
        assert "hello" in msgs[0]["data"]
        await conn.stop()


async def test_stdin_frame_increments_counter() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        await _send_frames(conn._socket_path, [_make_frame(CHANNEL_STDIN, b"x")])  # noqa: SLF001
        await asyncio.sleep(0.05)
        await conn.poll_messages()
        analysis = await conn.get_analysis()
        assert "stdin_keystrokes=1" in analysis
        await conn.stop()


async def test_connect_frame_logs_address() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        await _send_frames(
            conn._socket_path,  # noqa: SLF001
            [_make_frame(CHANNEL_CONNECT, b"192.168.1.1:8080")],
        )
        await asyncio.sleep(0.05)
        await conn.poll_messages()
        analysis = await conn.get_analysis()
        assert "192.168.1.1:8080" in analysis
        await conn.stop()


async def test_buffer_truncated_at_65536_chars() -> None:
    """Internal scroll-back buffer is capped at 65536; streaming data is unaffected."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        await _send_frames(
            conn._socket_path,  # noqa: SLF001
            [_make_frame(CHANNEL_STDOUT, b"x" * 70000)],
        )
        await asyncio.sleep(0.05)
        await conn.poll_messages()
        # After draining, the internal buffer should be capped at 65536
        snap = await conn.get_snapshot()
        assert len(snap["screen"]) <= 65536
        await conn.stop()


async def test_handle_input_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        msgs = await conn.handle_input("ignored input")
        assert msgs == []
        await conn.stop()


async def test_get_snapshot_structure() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        snap = await conn.get_snapshot()
        assert snap["type"] == "snapshot"
        for key in ("screen", "cursor", "cols", "rows", "screen_hash"):
            assert key in snap, f"missing key: {key}"
        await conn.stop()


async def test_clear_resets_buffer() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        await _send_frames(conn._socket_path, [_make_frame(CHANNEL_STDOUT, b"data")])  # noqa: SLF001
        await asyncio.sleep(0.05)
        await conn.poll_messages()
        msgs = await conn.clear()
        assert msgs[0]["type"] == "term"
        assert msgs[0].get("data") == ""
        await conn.stop()


async def test_set_mode_returns_hello() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        msgs = await conn.set_mode("open")
        types = [m["type"] for m in msgs]
        assert "worker_hello" in types
        await conn.stop()


async def test_get_analysis_contains_socket_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        analysis = await conn.get_analysis()
        assert conn._socket_path in analysis  # noqa: SLF001


async def test_custom_cols_rows() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td, cols=120, rows=40)
        await conn.start()
        assert conn._cols == 120  # noqa: SLF001
        assert conn._rows == 40  # noqa: SLF001
        await conn.stop()


async def test_handle_control_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        msgs = await conn.handle_control("any")
        assert msgs == []
        await conn.stop()


async def test_no_snapshot_when_no_stdout_frames() -> None:
    """CHANNEL_STDIN frames do not trigger snapshot emission."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        await _send_frames(conn._socket_path, [_make_frame(CHANNEL_STDIN, b"k")])  # noqa: SLF001
        await asyncio.sleep(0.05)
        msgs = await conn.poll_messages()
        assert msgs == []
        await conn.stop()
