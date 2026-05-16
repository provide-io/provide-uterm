#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import asyncio
import contextlib
import socket
import struct
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from provide.uterm.pty.capture import CHANNEL_CONNECT, CHANNEL_STDIN, CHANNEL_STDOUT
from provide.uterm.pty.capture_connector import CaptureConnector


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
    return CaptureConnector("test-cap-1", "Test Capture", {"socket_path": path, **kwargs})


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
        assert Path(conn._socket_path).exists()
        await conn.stop()


async def test_stop_removes_socket_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        path = conn._socket_path
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
        await _send_frames(conn._socket_path, [_make_frame(CHANNEL_STDOUT, b"hello")])
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
        await _send_frames(conn._socket_path, [_make_frame(CHANNEL_STDIN, b"x")])
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
            conn._socket_path,
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
            conn._socket_path,
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
        await _send_frames(conn._socket_path, [_make_frame(CHANNEL_STDOUT, b"data")])
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
        assert conn._socket_path in analysis


async def test_custom_cols_rows() -> None:
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td, cols=120, rows=40)
        await conn.start()
        assert conn._cols == 120
        assert conn._rows == 40
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
        await _send_frames(conn._socket_path, [_make_frame(CHANNEL_STDIN, b"k")])
        await asyncio.sleep(0.05)
        msgs = await conn.poll_messages()
        assert msgs == []
        await conn.stop()


def test_capture_register_import_error_silently_returns() -> None:
    """_register() returns silently when server package absent."""
    from provide.uterm.pty.capture_connector import _register

    with patch.dict(sys.modules, {"provide.uterm.server.connectors.registry": None}):
        _register()  # must not raise


def test_capture_register_success() -> None:
    """_register() calls register_connector when server package is available."""
    from types import ModuleType

    from provide.uterm.pty.capture_connector import CaptureConnector, _register

    fake_registry = ModuleType("provide.uterm.server.connectors.registry")
    registered: dict[str, object] = {}

    def _fake_register(name: str, cls: object) -> None:
        registered[name] = cls

    fake_registry.register_connector = _fake_register  # type: ignore[attr-defined]

    with patch.dict(
        sys.modules,
        {"provide.uterm.server.connectors.registry": fake_registry},
    ):
        _register()

    assert registered.get("pty_capture") is CaptureConnector


async def test_stop_closes_stdin_sock() -> None:
    """stop() closes _stdin_sock when it is set."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        mock_sock = MagicMock()
        conn._stdin_sock = mock_sock
        await conn.stop()
        mock_sock.close.assert_called_once()
        assert conn._stdin_sock is None


async def test_stop_stdin_sock_close_oserror_ignored() -> None:
    """stop() ignores OSError when closing _stdin_sock."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError("boom")
        conn._stdin_sock = mock_sock
        await conn.stop()  # must not raise


async def test_connect_frame_followed_by_more_frames_loops() -> None:
    """CONNECT frame followed by more frames covers the loop-continue branch."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        await _send_frames(
            conn._socket_path,
            [
                _make_frame(CHANNEL_CONNECT, b"10.0.0.1:80"),
                _make_frame(CHANNEL_STDOUT, b"hello"),
            ],
        )
        await asyncio.sleep(0.05)
        msgs = await conn.poll_messages()
        assert any(m.get("type") == "term" for m in msgs)
        analysis = await conn.get_analysis()
        assert "10.0.0.1:80" in analysis
        await conn.stop()


async def test_connect_log_truncated_at_100() -> None:
    """connect_log is truncated to last 100 entries when it exceeds 100."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        # Send 102 CONNECT frames via separate connections to avoid overwhelming it
        for i in range(102):
            await _send_frames(
                conn._socket_path,
                [_make_frame(CHANNEL_CONNECT, f"1.2.3.4:{i}".encode())],
            )
        await asyncio.sleep(0.15)
        await conn.poll_messages()
        assert len(conn._connect_log) <= 100
        await conn.stop()


async def test_handle_input_forwards_to_stdin_socket() -> None:
    """handle_input() forwards data to stdin socket when configured."""
    with tempfile.TemporaryDirectory() as td:
        stdin_sock_path = str(Path(td) / "stdin.sock")
        received: list[bytes] = []

        # Create a simple Unix socket server to receive stdin data
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(stdin_sock_path)
        srv.listen(1)
        srv.settimeout(2.0)

        conn = _make_connector(td, stdin_socket_path=stdin_sock_path)
        await conn.start()
        await conn.handle_input("hello\n")

        # Accept connection and read data
        with contextlib.suppress(Exception):
            client, _ = srv.accept()
            data = client.recv(1024)
            received.append(data)
            client.close()
        srv.close()

        await conn.stop()
        assert received and b"hello\n" in received[0]


async def test_forward_stdin_reconnects_on_send_error() -> None:
    """_forward_stdin() clears broken socket when sendall raises OSError."""
    with tempfile.TemporaryDirectory() as td:
        stdin_sock_path = str(Path(td) / "stdin.sock")

        conn = _make_connector(td, stdin_socket_path=stdin_sock_path)
        await conn.start()

        # Pre-set a broken sock that will fail on sendall
        broken_sock = MagicMock()
        broken_sock.sendall.side_effect = OSError("broken pipe")
        broken_sock.close = MagicMock()
        conn._stdin_sock = broken_sock

        # _forward_stdin should clear the broken sock; since no real server is listening
        # the reconnect attempt will also fail (OSError on connect) — that's OK
        conn._forward_stdin(b"test")

        assert conn._stdin_sock is None  # cleared after failure
        await conn.stop()


async def test_forward_stdin_close_oserror_on_broken_sock() -> None:
    """_forward_stdin() handles OSError from close() on a broken socket."""
    with tempfile.TemporaryDirectory() as td:
        stdin_sock_path = str(Path(td) / "stdin.sock")

        conn = _make_connector(td, stdin_socket_path=stdin_sock_path)
        await conn.start()

        # Pre-set a sock that fails on both sendall AND close
        broken_sock = MagicMock()
        broken_sock.sendall.side_effect = OSError("broken pipe")
        broken_sock.close.side_effect = OSError("close failed")
        conn._stdin_sock = broken_sock

        # Must not raise — OSError from close() is swallowed
        conn._forward_stdin(b"test")

        assert conn._stdin_sock is None
        await conn.stop()


async def test_forward_stdin_both_attempts_fail_exhausts_loop() -> None:
    """_forward_stdin() exits loop when reconnect succeeds but sendall fails twice."""
    with tempfile.TemporaryDirectory() as td:
        stdin_sock_path = str(Path(td) / "stdin.sock")

        # Create a server that accepts connections but immediately drops them
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(stdin_sock_path)
        srv.listen(5)
        srv.setblocking(False)

        conn = _make_connector(td, stdin_socket_path=stdin_sock_path)
        await conn.start()

        # Attempt 0: broken sock → sendall fails → close → sock = None
        # Attempt 1: None → real connect succeeds → sendall fails → sock = None → done
        call_count = [0]

        def _patched_socket(*args: object, **kwargs: object) -> MagicMock:
            call_count[0] += 1
            s = MagicMock()
            s.connect = MagicMock()  # succeed
            s.sendall.side_effect = OSError("send failed attempt 2")
            s.close = MagicMock()
            return s

        broken_sock = MagicMock()
        broken_sock.sendall.side_effect = OSError("send failed attempt 1")
        broken_sock.close = MagicMock()
        conn._stdin_sock = broken_sock

        with patch(
            "provide.uterm.pty.capture_connector.socket.socket",
            side_effect=_patched_socket,
        ):
            conn._forward_stdin(b"test")

        # The loop should have exhausted both attempts
        assert conn._stdin_sock is None
        assert call_count[0] == 1  # one new socket was created for attempt 1

        srv.close()
        await conn.stop()


async def test_poll_messages_unknown_channel_loops() -> None:
    """Unknown channel frames are silently ignored and the loop continues."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()

        # Send an unknown channel (0xFF) followed by a STDOUT frame
        unknown_frame = _make_frame(0xFF, b"ignored")
        stdout_frame = _make_frame(CHANNEL_STDOUT, b"visible")
        await _send_frames(
            conn._socket_path,
            [unknown_frame, stdout_frame],
        )
        await asyncio.sleep(0.05)
        msgs = await conn.poll_messages()
        assert any(m.get("type") == "term" for m in msgs)
        await conn.stop()
