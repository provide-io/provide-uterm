#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
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
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from provide.uterm.pty.socket_utils import validate_socket_path

logger = logging.getLogger(__name__)

_MAX_LINE = 4096  # bytes — guard against runaway senders
_NOTIFY_SOCKET_MODE = 0o600
# umask that yields 0o600 at file creation (0o777 & ~0o177 == 0o600). Set around
# the bind so the socket is owner-only the instant it appears — see start().
_NOTIFY_BIND_UMASK = 0o177


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


class PamNotifyListener:
    """
    Async Unix-domain socket server for pam_uterm.so notifications.

    Call ``start(handler)`` to begin accepting connections; ``stop()`` to
    shut down and remove the socket file.

    The handler coroutine is awaited for every successfully parsed event.
    Parse errors are logged and skipped; handler exceptions are caught and
    logged so one bad event never kills the listener.

    Args:
        socket_path: Path to the Unix domain socket.
        require_peer_uids: Opt-in allowlist of peer euids that may connect.
            ``None`` (default) means no enforcement — the euid is still
            logged at DEBUG for observability.  On platforms without
            ``SO_PEERCRED`` the check is skipped (warn + allow).
    """

    def __init__(
        self,
        socket_path: str = "/run/uterm-notify.sock",
        require_peer_uids: list[int] | None = None,
    ) -> None:
        validate_socket_path(socket_path)
        self._path = socket_path
        self._require_peer_uids = require_peer_uids
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
        # Restrict the notify socket to the owner so other local users cannot
        # forge login events that drive root-side session creation. Set the
        # umask *before* the bind so the socket is created 0o600 atomically:
        # a post-bind chmod would leave a window where the socket exists with
        # default-umask perms (e.g. srwxr-xr-x) that any local user could
        # connect to. Restore the previous umask in finally so it always
        # happens. Mirrors CaptureSocket.start() in pty/capture.py.
        # NOTE: os.umask is process-global and not async-safe — keep the
        # set→bind→restore window as tight as possible (only the bind call).
        prev_umask = os.umask(_NOTIFY_BIND_UMASK)
        try:
            self._server = await asyncio.start_unix_server(self._handle_connection, path=self._path)
        finally:
            os.umask(prev_umask)
        # Belt-and-suspenders: enforce 0o600 even if the platform ignored the
        # umask for AF_UNIX sockets. With the umask in place this is a no-op.
        os.chmod(self._path, _NOTIFY_SOCKET_MODE)  # noqa: PTH101 — chmod the just-bound socket fd path
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

    def _peer_euid(self, writer: asyncio.StreamWriter) -> int | None:
        """Return the connecting peer's uid via SO_PEERCRED, or None if unavailable."""
        import socket as _socket
        import struct

        so_peercred = getattr(_socket, "SO_PEERCRED", None)
        if so_peercred is None:
            return None  # platform without SO_PEERCRED (e.g. macOS)
        sock = writer.get_extra_info("socket")
        if sock is None:
            return None
        try:
            raw = sock.getsockopt(_socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return int(uid)
        except (OSError, struct.error, Exception):
            return None

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Peer-uid authentication: check SO_PEERCRED before processing any data.
        euid = self._peer_euid(writer)
        logger.debug("pam_notify peer euid=%s", euid)
        if euid is None:
            # Platform without SO_PEERCRED — warn but allow (chmod 0o600 is the baseline).
            logger.warning("pam_notify peer auth unavailable on this platform; relying on socket permissions")
        elif self._require_peer_uids is not None and euid not in self._require_peer_uids:
            logger.warning(
                "pam_notify rejected connection from peer euid=%d (not in allowlist=%s)",
                euid,
                self._require_peer_uids,
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                except TimeoutError:
                    logger.warning("pam_notify_listener readline_timeout — dropping connection")
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
    mode: Literal["notify", "capture"] = "capture" if raw_mode == "capture" else "notify"
    capture_socket: str | None = str(data["capture_socket"]) if data.get("capture_socket") else None

    return PamEvent(
        event=ev,
        username=username,
        tty=tty,
        pid=pid,
        mode=mode,
        capture_socket=capture_socket,
    )
