#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import asyncio
import struct
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.pty.capture import (
    CHANNEL_CONNECT,
    CHANNEL_STDIN,
    CHANNEL_STDOUT,
    CaptureFrame,
    CaptureSocket,
)


def _make_frame(channel: int, data: bytes) -> bytes:
    return struct.pack(">BI", channel, len(data)) + data


async def _send_frames(path: str, frames: list[bytes]) -> None:
    reader, writer = await asyncio.open_unix_connection(path)
    for frame in frames:
        writer.write(frame)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def test_capture_frame_attrs() -> None:
    f = CaptureFrame(channel=CHANNEL_STDOUT, data=b"hello")
    assert f.channel == CHANNEL_STDOUT
    assert f.data == b"hello"


def test_channel_constants() -> None:
    assert CHANNEL_STDOUT == 0x01
    assert CHANNEL_STDIN == 0x02
    assert CHANNEL_CONNECT == 0x03


async def test_start_stop() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "test.sock")
        sock = CaptureSocket(path)
        await sock.start()
        assert Path(path).exists()
        await sock.stop()
        assert not Path(path).exists()


async def test_receive_stdout_frame() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "test.sock")
        sock = CaptureSocket(path)
        await sock.start()

        await _send_frames(path, [_make_frame(CHANNEL_STDOUT, b"hello world")])
        await asyncio.sleep(0.05)

        frame = await asyncio.wait_for(sock.read_frame(), timeout=1.0)
        assert frame.channel == CHANNEL_STDOUT
        assert frame.data == b"hello world"

        await sock.stop()


async def test_receive_multiple_frames() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "test.sock")
        sock = CaptureSocket(path)
        await sock.start()

        payloads = [b"frame1", b"frame2", b"frame3"]
        await _send_frames(path, [_make_frame(CHANNEL_STDOUT, p) for p in payloads])
        await asyncio.sleep(0.05)

        received = []
        for _ in payloads:
            frame = await asyncio.wait_for(sock.read_frame(), timeout=1.0)
            received.append(frame.data)

        assert received == payloads
        await sock.stop()


async def test_receive_connect_frame() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "test.sock")
        sock = CaptureSocket(path)
        await sock.start()

        addr = b"192.168.1.1:8080"
        await _send_frames(path, [_make_frame(CHANNEL_CONNECT, addr)])
        await asyncio.sleep(0.05)

        frame = await asyncio.wait_for(sock.read_frame(), timeout=1.0)
        assert frame.channel == CHANNEL_CONNECT
        assert frame.data == addr

        await sock.stop()


async def test_stop_cleans_up_socket_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "test.sock")
        sock = CaptureSocket(path)
        await sock.start()
        await sock.stop()
        assert not Path(path).exists()


async def test_stop_without_start_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "test.sock")
        sock = CaptureSocket(path)
        await sock.stop()  # must not raise


def test_socket_path_with_null_byte_rejected() -> None:
    with pytest.raises(ValueError, match="null byte"):
        CaptureSocket("/tmp/ok\x00bad.sock")


def test_socket_path_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        CaptureSocket("relative/path.sock")


async def test_stop_socket_already_removed() -> None:
    """stop() handles FileNotFoundError when socket file was externally removed."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        cap = CaptureSocket(path)
        await cap.start()
        Path(path).unlink()  # remove before stop()
        await cap.stop()  # must not raise


async def test_handle_connection_wait_closed_exception_ignored() -> None:
    """_handle_connection() suppresses exceptions from writer.wait_closed()."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        cap = CaptureSocket(path)
        await cap.start()

        reader = AsyncMock()
        reader.readexactly.side_effect = asyncio.IncompleteReadError(b"", 5)
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=RuntimeError("boom"))

        await cap._handle_connection(reader, writer)
        writer.close.assert_called_once()

        await cap.stop()
