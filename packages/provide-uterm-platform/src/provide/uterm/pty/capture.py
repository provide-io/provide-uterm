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

# Owner-only permissions for the listening Unix socket.
_SOCKET_MODE = 0o600


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
        self._server = await asyncio.start_unix_server(self._handle_connection, path=self._path)
        # Restrict the listening socket to the owner so other local users can't
        # connect and inject/observe captured terminal traffic.
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
