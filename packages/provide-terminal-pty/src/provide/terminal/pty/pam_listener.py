# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Async Unix-socket listener that receives JSON notifications from pam_uterm.so.

Wire format: newline-delimited JSON, one event per line.
  {"event":"open",  "username":"alice","tty":"/dev/pts/3","pid":12345}
  {"event":"close", "username":"alice","tty":"/dev/pts/3","pid":12345}

The listener accepts multiple concurrent connections (e.g. sshd can spawn many
PAM processes in parallel).  Each connection is read until EOF; the newline
delimiter lets a single connection carry multiple events if needed.

Usage::

    async def on_event(ev: PamEvent) -> None:
        print(ev)

    listener = PamNotifyListener("/run/uterm-notify.sock")
    await listener.start(on_event)
    # ... server runs ...
    await listener.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_MAX_LINE = 4096  # bytes — guard against runaway senders


@dataclass
class PamEvent:
    """A single notification received from pam_uterm.so."""

    event: Literal["open", "close"]
    username: str
    tty: str
    pid: int
    mode: Literal["notify", "capture"] = "notify"
    capture_socket: str | None = None  # set when mode="capture"
    timestamp: float = field(default_factory=time.time)


PamEventHandler = Callable[[PamEvent], Awaitable[None]]


def _validate_socket_path(path: str) -> None:
    if "\x00" in path:
        raise ValueError("socket path contains null byte")
    if not path.startswith("/"):
        raise ValueError("socket path must be an absolute path")


class PamNotifyListener:
    """
    Async Unix-domain socket server for pam_uterm.so notifications.

    Call ``start(handler)`` to begin accepting connections; ``stop()`` to
    shut down and remove the socket file.

    The handler coroutine is awaited for every successfully parsed event.
    Parse errors are logged and skipped; handler exceptions are caught and
    logged so one bad event never kills the listener.
    """

    def __init__(self, socket_path: str = "/run/uterm-notify.sock") -> None:
        _validate_socket_path(socket_path)
        self._path = socket_path
        self._handler: PamEventHandler | None = None
        self._server: asyncio.Server | None = None

    @property
    def socket_path(self) -> str:
        return self._path

    async def start(self, handler: PamEventHandler) -> None:
        """Start listening.  *handler* is called for each PamEvent received."""
        if self._server is not None:
            raise RuntimeError("PamNotifyListener already started")
        self._handler = handler
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=self._path
        )
        logger.info("pam_notify_listener started socket=%s", self._path)

    async def stop(self) -> None:
        """Stop the server and remove the socket file."""
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        with contextlib.suppress(FileNotFoundError):
            Path(self._path).unlink()
        logger.info("pam_notify_listener stopped socket=%s", self._path)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                except TimeoutError:
                    logger.warning(
                        "pam_notify_listener readline_timeout — dropping connection"
                    )
                    break
                except Exception:
                    break
                if not line:
                    break
                if len(line) > _MAX_LINE:
                    logger.warning(
                        "pam_notify_listener oversized_line bytes=%d — dropped",
                        len(line),
                    )
                    continue
                event = _parse_event(line)
                if event is None:
                    continue
                if self._handler is not None:
                    try:
                        await self._handler(event)
                    except Exception:
                        logger.exception(
                            "pam_notify_listener handler error event=%s username=%s",
                            event.event,
                            event.username,
                        )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _parse_event(line: bytes) -> PamEvent | None:
    """Parse one JSON line into a PamEvent, returning None on any error."""
    import json

    try:
        data = json.loads(line.decode("utf-8", errors="replace").strip())
    except Exception:
        logger.warning("pam_notify_listener bad_json line=%r", line[:80])
        return None

    ev = data.get("event")
    if ev not in ("open", "close"):
        logger.warning("pam_notify_listener unknown_event event=%r", ev)
        return None

    username = str(data.get("username") or "")
    tty = str(data.get("tty") or "")
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0

    if not username:
        logger.warning("pam_notify_listener missing username — dropped")
        return None

    raw_mode = str(data.get("mode") or "notify")
    mode: Literal["notify", "capture"] = (
        "capture" if raw_mode == "capture" else "notify"
    )
    capture_socket: str | None = (
        str(data["capture_socket"]) if data.get("capture_socket") else None
    )

    return PamEvent(
        event=ev,
        username=username,
        tty=tty,
        pid=pid,
        mode=mode,
        capture_socket=capture_socket,
    )
