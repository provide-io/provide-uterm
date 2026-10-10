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
import re
import time
from typing import Any

from provide.uterm.pty.capture import (
    CHANNEL_CONNECT,
    CHANNEL_STATS,
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


# A full clear: erase display, switch to the alternate screen, or full reset.
# A full-screen program begins every whole paint with one, so the output after
# the last of them is what draws the current screen from nothing.
_FULL_CLEAR = re.compile(r"\x1b\[2J|\x1b\[\?1049h|\x1bc")
# Bound on that output. A program that never clears again would otherwise grow it
# forever; past this, a replay starts mid-paint and settles at the next clear.
_REPLAY_MAX = 512 * 1024


def _validate_config(config: dict[str, Any]) -> None:
    unknown = set(config) - _VALID_CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown config keys for CaptureConnector: {sorted(unknown)}")
    if "socket_path" not in config:
        raise ValueError("CaptureConnector requires 'socket_path' in connector_config")


def _make_emulator(cols: int, rows: int) -> Any:
    """A terminal emulator to render the captured stream into, if pyte is present.

    The emulator is the ``provide-uterm[emulator]`` extra. Without it a capture
    session still works and its snapshot falls back to the raw output tail.
    """

    try:
        from provide.uterm.emulator import TerminalEmulator
    except ImportError:
        return None
    return TerminalEmulator(cols, rows, receive_encoding="utf-8")


def _register() -> None:
    try:
        from provide.uterm.server.connectors.registry import register_connector

        register_connector("pty_capture", CaptureConnector)  # type: ignore[arg-type]  # ty:ignore[invalid-argument-type]
    except ImportError:
        # The server package is optional; without it there is no connector registry to join.
        pass


class CaptureConnector:
    """
    SessionConnector impl that observes an LD_PRELOAD-captured shell.

    connector_type = "pty_capture"
    """

    def __init__(self, session_id: str, display_name: str, config: dict[str, Any]) -> None:
        _validate_config(config)

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
        # The screen the captured stream draws. The raw tail below is kept for
        # deployments without the emulator, and is what the snapshot falls back
        # to: as "the screen" it fails, because a program that redraws one line
        # -- a blinking cursor -- fills 64 KiB with that line alone.
        self._emulator = _make_emulator(self._cols, self._rows)
        # Output since the last full clear, replayed into a new emulator when the
        # size changes: see reconfigure.
        self._since_clear = ""
        self._buffer = ""
        self._pending = ""  # new bytes not yet streamed to the browser
        self._connect_log: list[str] = []
        self._stdin_count = 0
        self._shim_stats = ""
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
                self._pending += self._ingest_stdout(frame.data)
                changed = True
            elif frame.channel == CHANNEL_STDIN:
                self._stdin_count += 1
            elif frame.channel == CHANNEL_CONNECT:
                addr = frame.data.decode("utf-8", errors="replace")
                self._connect_log.append(addr)
                if len(self._connect_log) > 100:
                    self._connect_log = self._connect_log[-100:]
            elif frame.channel == CHANNEL_STATS:
                # What the shim could not deliver. Kept out of the screen buffer
                # and out of `changed`: it is the capture path describing itself,
                # not something the terminal drew.
                self._shim_stats = frame.data.decode("utf-8", errors="replace")[:200]
        if changed and self._pending:
            data, self._pending = self._pending, ""
            return [{"type": "term", "data": data}]
        return []

    def reconfigure(self, config: dict[str, Any]) -> bool:
        """Apply a new configuration to the running connector, if it can be.

        Whoever creates a capture session (PAM) knows only the socket the shim
        writes to; the keystroke socket and the terminal size arrive later, from
        whoever owns the session. Those apply in place. A different capture
        socket cannot: the program is writing to the one already bound, so that
        answers False and is left for a restart.
        """

        # Everything that can reject the config runs before anything changes:
        # a rejected config must leave the running connector as it was (the
        # registry turns the ValueError/TypeError into a 422 and keeps the stored
        # definition). connect_timeout_s is parsed only to validate it -- a value
        # the constructor cannot parse would fail the next start.
        _validate_config(config)
        cols, rows = int(config.get("cols", self._cols)), int(config.get("rows", self._rows))
        float(config.get("connect_timeout_s", self._connect_timeout))
        if str(config["socket_path"]) != self._socket_path:
            return False
        stdin_socket_path = str(config["stdin_socket_path"]) if config.get("stdin_socket_path") else None
        if stdin_socket_path != self._stdin_socket_path:
            self._stdin_socket_path = stdin_socket_path
            # The next keystroke connects to the new socket.
            if self._stdin_writer is not None:
                self._stdin_writer.close()
                self._stdin_writer = None
        if (cols, rows) != (self._cols, self._rows):
            self._cols, self._rows = cols, rows
            if self._emulator is not None:
                # Not resize(): what was drawn for the real size and clipped by
                # the old one is gone from the old screen. Redraw it from what the
                # program sent since it last cleared -- PAM creates the session
                # before anyone knows its size, so the screen starts at a default
                # the program never drew for.
                self._emulator = _make_emulator(cols, rows)
                self._emulator.process(self._since_clear.encode("utf-8"))
        return True

    def _ingest_stdout(self, data: bytes) -> str:
        """Take one chunk of captured output into the screen; return it for streaming."""

        raw = data.decode("utf-8", errors="replace")
        # Normalize bare \n → \r\n: DYLD capture bypasses the PTY ONLCR
        # driver, so xterm.js would advance cursor down without a CR.
        text = raw.replace("\r\n", "\n").replace("\n", "\r\n")
        self._buffer += text
        if len(self._buffer) > 65536:
            self._buffer = self._buffer[-65536:]
        if self._emulator is not None:
            self._emulator.process(text.encode("utf-8"))
        clears = list(_FULL_CLEAR.finditer(text))
        if clears:
            self._since_clear = text[clears[-1].start() :]
        else:
            self._since_clear = (self._since_clear + text)[-_REPLAY_MAX:]
        return text

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
                    self._stdin_writer = (await asyncio.open_unix_connection(self._stdin_socket_path))[1]
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
            # The peer already went away; the writer is discarded below either way.
            pass
        self._stdin_writer = None

    async def handle_control(self, action: str) -> list[dict[str, Any]]:
        return []

    async def get_snapshot(self) -> dict[str, Any]:
        return self._snapshot()

    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        # Announce the mode asked for. The hub applies the hello's mode, so
        # answering "open" regardless overrode a session defined as hijack --
        # left over from when a capture had no input path and the mode could
        # not matter. With stdin_socket_path it has one, and hijack is what
        # gives a single viewer the lease.
        return [{"type": "worker_hello", "input_mode": mode}]

    async def clear(self) -> list[dict[str, Any]]:
        self._buffer = ""
        self._pending = ""
        self._since_clear = ""
        if self._emulator is not None:
            self._emulator.reset()
        return [{"type": "term", "data": ""}]

    async def get_analysis(self) -> str:
        return (
            f"CaptureConnector socket={self._socket_path!r} "
            f"connected={self._connected} buffer_len={len(self._buffer)} "
            f"stdin_keystrokes={self._stdin_count} "
            f"outbound_connections={len(self._connect_log)} "
            f"shim[{self._shim_stats or 'no report yet'}]"
            + (f" recent_connect={self._connect_log[-1]!r}" if self._connect_log else "")
        )

    def _snapshot(self) -> dict[str, Any]:
        cursor = {"x": 0, "y": 0}
        if self._emulator is not None:
            # The rendered screen, colours included: the browser element clears
            # its terminal and writes this, so it must be a whole screen, not a
            # stretch of the history that drew one.
            screen = self._emulator.ansi_screen()
            cursor = dict(self._emulator.get_snapshot()["cursor"])
        else:
            screen = self._buffer
        return {
            "type": "snapshot",
            "screen": screen,
            # x/y, not row/col: that is what the snapshot frame contract and
            # every other connector use, and what the terminal element reads.
            "cursor": cursor,
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
