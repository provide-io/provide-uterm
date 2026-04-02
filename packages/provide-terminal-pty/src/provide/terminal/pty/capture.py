# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from pathlib import Path

CHANNEL_STDOUT = 0x01
CHANNEL_STDIN = 0x02
CHANNEL_CONNECT = 0x03

_HEADER = struct.Struct(">BI")  # channel (1B) + length (4B big-endian)


def _validate_socket_path(path: str) -> None:
    if "\x00" in path:
        raise ValueError("socket path contains null byte")
    if not path.startswith("/"):
        raise ValueError("socket path must be an absolute path")


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
        _validate_socket_path(socket_path)
        self._path = socket_path
        self._server: asyncio.Server | None = None
        self._queue: asyncio.Queue[CaptureFrame] = asyncio.Queue()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=self._path
        )

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

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                header_bytes = await reader.readexactly(_HEADER.size)
                channel, length = _HEADER.unpack(header_bytes)
                data = await reader.readexactly(length)
                await self._queue.put(CaptureFrame(channel=channel, data=data))
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001, S110
                pass
