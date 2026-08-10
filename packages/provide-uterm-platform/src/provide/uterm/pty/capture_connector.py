#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""
CaptureConnector — session connector fed by libuterm_capture.so.

No process is forked.  Instead the connector listens on a Unix socket that
``libuterm_capture.so`` (injected via LD_PRELOAD) connects to at shell startup.
Incoming frames are accumulated into a text buffer and exposed as snapshots.

Only CHANNEL_STDOUT (0x01) contributes to the visible screen; CHANNEL_STDIN
(0x02) and CHANNEL_CONNECT (0x03) frames are recorded in the analysis log.

Config keys accepted in connector_config:
  socket_path       str    required — path of the Unix socket to listen on
                           (pam_uterm.so writes this as /run/uterm-cap-{pid}.sock)
  cols              int    terminal width hint (default 80)
  rows              int    terminal height hint (default 24)
  connect_timeout_s float  seconds to wait for capture lib to connect (default 5.0)
  stdin_socket_path str    optional — Unix socket path to forward browser keystrokes
                           to.  When set, handle_input() writes typed bytes there so
                           a listener can pipe them into the captured process's stdin.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from provide.uterm.pty.capture import (
    CHANNEL_CONNECT,
    CHANNEL_STDIN,
    CHANNEL_STDOUT,
    CaptureSocket,
)

_VALID_CONFIG_KEYS = frozenset(
    {
        "socket_path",
        "cols",
        "rows",
        "connect_timeout_s",
        "input_mode",
        "stdin_socket_path",
    }
)


def _register() -> None:
    try:
        from provide.uterm.server.connectors.registry import register_connector

        register_connector("pty_capture", CaptureConnector)  # type: ignore[arg-type]  # ty:ignore[invalid-argument-type]
    except ImportError:
        pass


class CaptureConnector:
    """
    SessionConnector impl that observes an LD_PRELOAD-captured shell.

    connector_type = "pty_capture"
    """

    def __init__(self, session_id: str, display_name: str, config: dict[str, Any]) -> None:
        unknown = set(config) - _VALID_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown config keys for CaptureConnector: {sorted(unknown)}")
        if "socket_path" not in config:
            raise ValueError("CaptureConnector requires 'socket_path' in connector_config")

        self._session_id = session_id
        self._display_name = display_name
        self._socket_path: str = str(config["socket_path"])
        self._cols: int = int(config.get("cols", 80))
        self._rows: int = int(config.get("rows", 24))
        self._connect_timeout: float = float(config.get("connect_timeout_s", 5.0))
        self._stdin_socket_path: str | None = (
            str(config["stdin_socket_path"]) if config.get("stdin_socket_path") else None
        )

        self._capture: CaptureSocket | None = None
        self._connected = False
        self._buffer = ""
        self._pending = ""  # new bytes not yet streamed to the browser
        self._connect_log: list[str] = []
        self._stdin_count = 0
        self._stdin_writer: asyncio.StreamWriter | None = None

    async def start(self) -> None:
        self._capture = CaptureSocket(self._socket_path)
        await self._capture.start()
        self._connected = True

    async def stop(self) -> None:
        await self._close_stdin_writer()
        if self._capture is not None:
            await self._capture.stop()
            self._capture = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def poll_messages(self) -> list[dict[str, Any]]:
        if not self._connected or self._capture is None:
            return []
        changed = False
        # Drain all immediately available frames without blocking
        while True:
            frame = self._capture.read_nowait()
            if frame is None:
                break
            if frame.channel == CHANNEL_STDOUT:
                raw = frame.data.decode("utf-8", errors="replace")
                # Normalize bare \n → \r\n: DYLD capture bypasses the PTY ONLCR
                # driver, so xterm.js would advance cursor down without a CR.
                text = raw.replace("\r\n", "\n").replace("\n", "\r\n")
                self._buffer += text
                if len(self._buffer) > 65536:
                    self._buffer = self._buffer[-65536:]
                self._pending += text
                changed = True
            elif frame.channel == CHANNEL_STDIN:
                self._stdin_count += 1
            elif frame.channel == CHANNEL_CONNECT:
                addr = frame.data.decode("utf-8", errors="replace")
                self._connect_log.append(addr)
                if len(self._connect_log) > 100:
                    self._connect_log = self._connect_log[-100:]
        if changed and self._pending:
            data, self._pending = self._pending, ""
            return [{"type": "term", "data": data}]
        return []

    async def handle_input(self, data: str) -> list[dict[str, Any]]:
        if self._stdin_socket_path:
            await self._forward_stdin(data.encode("utf-8", errors="replace"))
        return []

    async def _forward_stdin(self, data: bytes) -> None:
        """Forward keystrokes to the stdin socket over an asyncio Unix stream.

        Lazy-connects, then writes + drains; on error reconnects and retries once.
        An asyncio stream (rather than a blocking ``socket.sendall``) keeps a slow
        or dead peer from stalling the event loop on a keystroke.
        """
        for _attempt in range(2):
            if self._stdin_writer is None:
                try:
                    _reader, self._stdin_writer = await asyncio.open_unix_connection(self._stdin_socket_path)
                except OSError:
                    return
            try:
                self._stdin_writer.write(data)
                await self._stdin_writer.drain()
                return
            except OSError:
                await self._close_stdin_writer()

    async def _close_stdin_writer(self) -> None:
        """Close the stdin stream writer, swallowing teardown errors."""
        if self._stdin_writer is None:
            return
        self._stdin_writer.close()
        try:
            await self._stdin_writer.wait_closed()
        except OSError:
            pass
        self._stdin_writer = None

    async def handle_control(self, action: str) -> list[dict[str, Any]]:
        return []

    async def get_snapshot(self) -> dict[str, Any]:
        return self._snapshot()

    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        return [{"type": "worker_hello", "input_mode": "open"}]

    async def clear(self) -> list[dict[str, Any]]:
        self._buffer = ""
        self._pending = ""
        return [{"type": "term", "data": ""}]

    async def get_analysis(self) -> str:
        return (
            f"CaptureConnector socket={self._socket_path!r} "
            f"connected={self._connected} buffer_len={len(self._buffer)} "
            f"stdin_keystrokes={self._stdin_count} "
            f"outbound_connections={len(self._connect_log)}"
            + (f" recent_connect={self._connect_log[-1]!r}" if self._connect_log else "")
        )

    def _snapshot(self) -> dict[str, Any]:
        screen = self._buffer
        return {
            "type": "snapshot",
            "screen": screen,
            # x/y, not row/col: that is what the snapshot frame contract and
            # every other connector use, and what the terminal element reads.
            "cursor": {"x": 0, "y": 0},
            "cols": self._cols,
            "rows": self._rows,
            # Non-cryptographic change-detection hash; `usedforsecurity=False`
            # is the canonical way to tell bandit/ruff and Python itself
            # that this is not a security boundary. Drops the orphan `nosec`
            # annotation that previously didn't match any active rule id.
            "screen_hash": hashlib.md5(screen.encode(), usedforsecurity=False).hexdigest(),
            "cursor_at_end": True,
            "has_trailing_space": False,
            # None, not False. The wire contract is a dict of detector output or
            # nothing at all, so a bool fails validation in the hub's frame
            # builder and the snapshot is dropped before any browser sees it —
            # which left a hijacked terminal blank until something repainted it.
            "prompt_detected": None,
            "ts": time.time(),
        }


_register()
