#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import asyncio
import struct
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def _mock_writer(*, drain_error: bool = False, wait_closed_error: bool = False) -> MagicMock:
    """A stand-in asyncio.StreamWriter: sync write/close, awaitable drain/wait_closed."""
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock(side_effect=OSError("drain") if drain_error else None)
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock(side_effect=OSError("wait_closed") if wait_closed_error else None)
    return writer


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


async def test_stop_closes_stdin_writer() -> None:
    """stop() closes the stdin stream writer and clears it."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        writer = _mock_writer()
        conn._stdin_writer = writer
        await conn.stop()
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()
        assert conn._stdin_writer is None


async def test_stop_stdin_writer_wait_closed_oserror_ignored() -> None:
    """stop() ignores an OSError from the stdin writer's wait_closed()."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td)
        await conn.start()
        conn._stdin_writer = _mock_writer(wait_closed_error=True)
        await conn.stop()  # must not raise
        assert conn._stdin_writer is None


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
    """handle_input() forwards typed bytes to the stdin socket over an asyncio stream."""
    with tempfile.TemporaryDirectory() as td:
        stdin_sock_path = str(Path(td) / "stdin.sock")
        received: list[bytes] = []
        got = asyncio.Event()

        async def _on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received.append(await reader.read(64))
            writer.close()
            got.set()

        server = await asyncio.start_unix_server(_on_conn, path=stdin_sock_path)
        try:
            conn = _make_connector(td, stdin_socket_path=stdin_sock_path)
            await conn.start()
            assert await conn.handle_input("hello\n") == []
            await asyncio.wait_for(got.wait(), 2.0)
            await conn.stop()
        finally:
            server.close()
            await server.wait_closed()
        assert received and received[0] == b"hello\n"


async def test_forward_stdin_connect_failure_returns() -> None:
    """No listener at the stdin path → open_unix_connection raises → forward returns cleanly."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td, stdin_socket_path=str(Path(td) / "absent.sock"))
        await conn.start()
        await conn.handle_input("x")  # connect fails (no server) → OSError → return
        assert conn._stdin_writer is None
        await conn.stop()


async def test_forward_stdin_reuses_existing_writer() -> None:
    """A live writer is reused: no new connection is opened for the next keystroke."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td, stdin_socket_path=str(Path(td) / "s.sock"))
        writer = _mock_writer()
        conn._stdin_writer = writer
        with patch(
            "provide.uterm.pty.capture_connector.asyncio.open_unix_connection",
            new=AsyncMock(),
        ) as open_conn:
            await conn._forward_stdin(b"abc")
        open_conn.assert_not_called()  # existing writer reused
        writer.write.assert_called_once_with(b"abc")
        writer.drain.assert_awaited_once()
        assert conn._stdin_writer is writer


async def test_forward_stdin_reconnects_on_drain_error() -> None:
    """A drain failure tears down the writer and retries once on a fresh connection."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td, stdin_socket_path=str(Path(td) / "s.sock"))
        broken, fresh = _mock_writer(drain_error=True), _mock_writer()
        with patch(
            "provide.uterm.pty.capture_connector.asyncio.open_unix_connection",
            new=AsyncMock(side_effect=[(MagicMock(), broken), (MagicMock(), fresh)]),
        ):
            await conn._forward_stdin(b"data")
        broken.write.assert_called_once_with(b"data")
        broken.close.assert_called_once()  # broken writer torn down
        fresh.write.assert_called_once_with(b"data")  # retried on the new writer
        assert conn._stdin_writer is fresh


async def test_forward_stdin_both_attempts_fail_exhausts_loop() -> None:
    """Both attempts' drains fail → the retry loop exhausts and leaves no writer."""
    with tempfile.TemporaryDirectory() as td:
        conn = _make_connector(td, stdin_socket_path=str(Path(td) / "s.sock"))
        w1, w2 = _mock_writer(drain_error=True), _mock_writer(drain_error=True)
        with patch(
            "provide.uterm.pty.capture_connector.asyncio.open_unix_connection",
            new=AsyncMock(side_effect=[(MagicMock(), w1), (MagicMock(), w2)]),
        ):
            await conn._forward_stdin(b"x")
        w1.close.assert_called_once()
        w2.close.assert_called_once()
        assert conn._stdin_writer is None


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


def test_snapshot_survives_the_hub_frame_builder() -> None:
    """A snapshot the hub refuses is a hijacked terminal that never paints.

    The builder validates the wire contract, and a frame it rejects is dropped
    at debug level — so a shape error here is invisible in the logs and shows
    up only as a blank browser terminal.
    """

    from provide.uterm.server.bridge.routes.websockets_worker import _build_worker_frame

    connector = CaptureConnector("s", "d", {"socket_path": "/tmp/probe.sock"})
    connector._buffer = "CHOOSE A DOOR"

    frame = _build_worker_frame("snapshot", connector._snapshot())

    assert frame["screen"] == "CHOOSE A DOOR"
    assert frame["prompt_detected"] is None
    assert frame["cursor"] == {"x": 0, "y": 0}
