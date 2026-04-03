# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PamTunnelBridge — connects a local PTY/capture session to a CF DO tunnel.

PTY: asyncio add_reader on master_fd → tunnel.send_data (outbound);
     tunnel.recv() CHANNEL_DATA → os.write(master_fd) (inbound).

Capture (read-only): capture_socket.read_frame() CHANNEL_STDOUT → tunnel.send_data;
     tunnel input is discarded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class PamTunnelBridge:
    """Bridges a local PTY or capture connector to a CF DO WebSocket tunnel."""

    def __init__(self, ws_url: str, worker_token: str, connector: Any) -> None:
        self._ws_url = ws_url
        self._worker_token = worker_token
        self._connector = connector
        self._tunnel: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._master_fd: int | None = None

    async def start(self) -> None:
        from provide.terminal.tunnel.client import TunnelClient

        self._tunnel = TunnelClient(self._ws_url, token=self._worker_token)
        await self._tunnel.connect()
        await self._tunnel.open_terminal(cols=80, rows=24)

        if type(self._connector).__name__ == "PTYConnector":
            self._start_pty_bridge()
        else:
            self._tasks.append(asyncio.create_task(self._capture_to_tunnel_loop()))

    def _start_pty_bridge(self) -> None:
        master_fd: int = self._connector._master_fd
        self._master_fd = master_fd
        loop = asyncio.get_event_loop()
        tunnel = self._tunnel

        def _on_pty_output() -> None:
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                return
            loop.create_task(tunnel.send_data(data))  # noqa: RUF006

        loop.add_reader(master_fd, _on_pty_output)
        self._tasks.append(asyncio.create_task(self._tunnel_to_pty_loop(master_fd)))

    async def _tunnel_to_pty_loop(self, master_fd: int) -> None:
        from provide.terminal.tunnel.protocol import CHANNEL_DATA

        try:
            while True:
                frame = await self._tunnel.recv()
                if frame.is_eof:
                    break
                if frame.channel == CHANNEL_DATA and frame.payload:
                    os.write(master_fd, frame.payload)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("tunnel_to_pty_error: %s", exc)

    async def _capture_to_tunnel_loop(self) -> None:
        from provide.terminal.pty.capture import CHANNEL_STDOUT  # type: ignore[import-untyped]

        capture_socket = self._connector._capture
        try:
            while True:
                frame = await capture_socket.read_frame()
                if frame.channel == CHANNEL_STDOUT:
                    await self._tunnel.send_data(frame.data)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("capture_to_tunnel_error: %s", exc)

    async def stop(self) -> None:
        if self._master_fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_event_loop().remove_reader(self._master_fd)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._tunnel is not None:
            with contextlib.suppress(Exception):
                await self._tunnel.close()
            self._tunnel = None
