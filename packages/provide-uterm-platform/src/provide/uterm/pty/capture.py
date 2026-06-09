#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from provide.telemetry import get_logger

from provide.uterm.pty.socket_utils import validate_socket_path

logger = get_logger(__name__)

CHANNEL_STDOUT = 0x01
CHANNEL_STDIN = 0x02
CHANNEL_CONNECT = 0x03

_HEADER = struct.Struct(">BI")  # channel (1B) + length (4B big-endian)

# Upper bound on buffered capture frames. A fast/local client on the capture
# socket would otherwise grow the queue without bound and OOM the host; once
# full we drop the oldest frame (the reader is the only consumer and capture is
# best-effort, so dropping beats blocking the connection handler).
_QUEUE_MAXSIZE = 4096

# Upper bound on a single frame's payload. The 4-byte length field admits up to
# ~4 GiB, and ``readexactly(length)`` accumulates that many bytes before
# enqueueing — so a malicious or buggy producer on the (owner-only) socket could
# announce a huge length and drive the host into OOM. Legitimate frames are at
# most a single intercepted ``write()`` (capture.c emits one frame per write,
# kilobytes in practice), so we cap well above any real frame and treat an
# over-cap length as a framing violation: drop the connection (PLAT-cap).
_MAX_FRAME_BYTES = 16 * 1024 * 1024  # 16 MiB

# Owner-only permissions for the listening Unix socket.
_SOCKET_MODE = 0o600
# umask that yields 0o600 at file creation (0o777 & ~0o177 == 0o600). Set around
# the bind so the socket is owner-only the instant it appears — see start().
_BIND_UMASK = 0o177


@dataclass
class CaptureFrame:
    channel: int
    data: bytes


class CaptureSocket:
    """
    Async Unix domain socket server that receives frames from libuterm_capture.

    Frame wire format: [1B channel][4B length big-endian][N bytes payload]
    """

    def __init__(self, socket_path: str) -> None:
        validate_socket_path(socket_path)
        self._path = socket_path
        self._server: asyncio.Server | None = None
        self._queue: asyncio.Queue[CaptureFrame] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    async def start(self) -> None:
        # Restrict the listening socket to the owner so other local users can't
        # connect and inject/observe captured terminal traffic. Set the umask
        # *before* the bind so the socket is created 0o600 atomically: a
        # post-bind chmod would leave a window where the socket exists with
        # default-umask perms (e.g. srwxr-xr-x) that any local user could
        # connect to. Restore the previous umask in finally so it always
        # happens.
        # NOTE: os.umask is process-global and not async-safe — keep the
        # set→bind→restore window as tight as possible (only the bind call).
        prev_umask = os.umask(_BIND_UMASK)
        try:
            self._server = await asyncio.start_unix_server(self._handle_connection, path=self._path)
        finally:
            os.umask(prev_umask)
        # Belt-and-suspenders: enforce 0o600 even if the platform ignored the
        # umask for AF_UNIX sockets. With the umask in place this is a no-op.
        os.chmod(self._path, _SOCKET_MODE)  # noqa: PTH101 — chmod the just-bound socket fd path

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        try:
            Path(self._path).unlink()
        except FileNotFoundError:
            pass

    async def read_frame(self) -> CaptureFrame:
        return await self._queue.get()

    def read_nowait(self) -> CaptureFrame | None:
        """Return the next buffered frame without blocking, or None when empty."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def _enqueue(self, frame: CaptureFrame) -> None:
        """Buffer *frame*, applying drop-oldest backpressure when the queue is
        full so a fast producer cannot grow it without bound (PLAT-cap)."""
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover — full then empty is racy/unreachable single-threaded
                pass
            self._queue.put_nowait(frame)
            logger.warning("capture_backpressure_drop_oldest", maxsize=self._queue.maxsize)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header_bytes = await reader.readexactly(_HEADER.size)
                channel, length = _HEADER.unpack(header_bytes)
                if length > _MAX_FRAME_BYTES:
                    # Hostile/corrupt producer: refuse to allocate gigabytes for
                    # one frame. Drop the connection rather than read the body —
                    # checking BEFORE readexactly is what prevents the OOM.
                    logger.warning("capture_frame_too_large", length=length, cap=_MAX_FRAME_BYTES)
                    break
                data = await reader.readexactly(length)
                self._enqueue(CaptureFrame(channel=channel, data=data))
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: S110
                pass
