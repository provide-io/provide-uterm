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

from provide.uterm.pty.capture import (
    _MAX_FRAME_BYTES,
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


async def test_queue_is_bounded() -> None:
    """The capture queue must have a finite maxsize (PLAT-cap, no OOM)."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        cap = CaptureSocket(path)
        assert cap._queue.maxsize > 0


async def test_queue_drops_oldest_on_overflow(monkeypatch) -> None:
    """When the queue is full, the oldest frame is dropped (not blocking)."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        cap = CaptureSocket(path)
        # Shrink the queue so we can force overflow deterministically.
        cap._queue = asyncio.Queue(maxsize=2)
        await cap.start()

        await _send_frames(
            path,
            [
                _make_frame(CHANNEL_STDOUT, b"a"),
                _make_frame(CHANNEL_STDOUT, b"b"),
                _make_frame(CHANNEL_STDOUT, b"c"),
            ],
        )
        await asyncio.sleep(0.05)

        # Oldest ("a") dropped; queue still holds exactly maxsize frames.
        assert cap._queue.qsize() == 2
        first = await asyncio.wait_for(cap.read_frame(), timeout=1.0)
        second = await asyncio.wait_for(cap.read_frame(), timeout=1.0)
        assert (first.data, second.data) == (b"b", b"c")
        await cap.stop()


async def test_socket_file_has_restrictive_perms() -> None:
    """The listening socket must be owner-only (0600) (PLAT-cap)."""
    import stat

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        cap = CaptureSocket(path)
        await cap.start()
        mode = stat.S_IMODE(Path(path).stat().st_mode)
        assert mode == 0o600
        await cap.stop()


async def test_socket_umask_constrains_bind_and_is_restored(monkeypatch) -> None:
    """The socket is created 0o600 atomically (umask before bind), not via a
    post-bind chmod that leaves a permission window.

    Regression for the bind-then-chmod race: any local user could connect in
    the gap between bind (default umask perms, e.g. srwxr-xr-x) and the chmod.
    start() must set a restrictive umask (0o177) *before* the bind and restore
    the caller's umask afterward, so the socket never exists world-accessible.
    """
    import asyncio as _asyncio
    import os
    import stat

    observed_umask: list[int] = []
    real_bind = _asyncio.start_unix_server

    async def _spy_bind(*args, **kwargs):
        # Capture the umask in effect at the moment of bind by reading and
        # immediately restoring it (os.umask has no read-only variant).
        cur = os.umask(0o022)
        os.umask(cur)
        observed_umask.append(cur)
        return await real_bind(*args, **kwargs)

    monkeypatch.setattr(_asyncio, "start_unix_server", _spy_bind)

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        prev = os.umask(0o000)  # most permissive — surfaces any window
        try:
            cap = CaptureSocket(path)
            await cap.start()
            # The bind itself must have run under a 0o177 umask (atomic 0o600).
            assert observed_umask == [0o177]
            mode = stat.S_IMODE(Path(path).stat().st_mode)
            assert mode == 0o600
            # And the caller's umask must be restored once start() returns.
            current = os.umask(0o022)
            assert current == 0o000
            await cap.stop()
        finally:
            os.umask(prev)


async def test_oversized_frame_length_is_rejected_without_reading_body() -> None:
    """A frame announcing a length above the cap is dropped BEFORE its body is read.

    Regression (PLAT-cap, capture OOM): the 4-byte length field admits ~4 GiB and
    ``readexactly(length)`` would accumulate that many bytes before enqueueing. The
    cap must short-circuit on the header alone and never attempt the body read.
    """
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        cap = CaptureSocket(path)

        header = struct.pack(">BI", CHANNEL_STDOUT, _MAX_FRAME_BYTES + 1)
        reader = AsyncMock()
        # 1st readexactly → the header. A 2nd call would be the body read; make it
        # blow up so the test fails loudly if the cap fails to short-circuit.
        reader.readexactly.side_effect = [header, AssertionError("body read attempted past the cap")]
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await cap._handle_connection(reader, writer)

        assert reader.readexactly.await_count == 1  # header only — body never read
        assert cap._queue.qsize() == 0  # nothing enqueued
        writer.close.assert_called_once()  # connection dropped


async def test_frame_length_exactly_at_cap_is_accepted(monkeypatch) -> None:
    """A frame whose length equals the cap is accepted (boundary: ``>`` not ``>=``)."""
    from provide.uterm.pty import capture as capture_mod

    monkeypatch.setattr(capture_mod, "_MAX_FRAME_BYTES", 4)
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        cap = CaptureSocket(path)

        header = struct.pack(">BI", CHANNEL_STDOUT, 4)
        reader = AsyncMock()
        reader.readexactly.side_effect = [header, b"abcd", asyncio.IncompleteReadError(b"", 0)]
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await cap._handle_connection(reader, writer)

        assert cap._queue.qsize() == 1
        assert cap._queue.get_nowait().data == b"abcd"


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
