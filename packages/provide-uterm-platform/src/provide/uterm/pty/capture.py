#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import errno
import os
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

from provide.telemetry import get_logger

from provide.uterm.pty.socket_utils import validate_socket_path

logger = get_logger(__name__)

CHANNEL_STDOUT = 0x01
CHANNEL_STDIN = 0x02
CHANNEL_CONNECT = 0x03
# The shim's own writer counters, as an ASCII key=value line. Every other
# channel says what the terminal did; this one says what the capture path
# failed to deliver, which is otherwise invisible from this side.
CHANNEL_STATS = 0x04
# The terminal's size, from whoever owns the pty (a supervisor), as >HH: cols,
# rows. The LD_PRELOAD shim never sends it: it cannot see a resize. A reader
# that predates it ignores it, as it does any channel it does not know, so a
# producer can send it unconditionally.
CHANNEL_GEOMETRY = 0x05
GEOMETRY = struct.Struct(">HH")
# Inclusive bounds on each side. Nothing real is wider or taller, and a size
# the emulator would allocate a screen for must not be the producer's choice.
GEOMETRY_MIN = 1
GEOMETRY_MAX = 1000
# The producer's keystroke socket, as a UTF-8 absolute path. An empty payload
# withdraws the path (closes the keystroke writer, as reconfigure does when
# stdin_socket_path is cleared). Sent only by a producer that can deliver
# keystrokes (a supervisor) — never the LD_PRELOAD shim, which has no keystroke
# socket to name. A reader that predates it ignores it, as it does any channel
# it does not know, so a producer may send it unconditionally.
CHANNEL_INPUT = 0x06
# Upper bound on a keystroke-socket path. Real paths are at most the Unix
# socket address limit (108 bytes on Linux, 104 on macOS); 4096 is generous and
# matches the spec's over-length vector. Past this the frame is malformed.
INPUT_MAX_LEN = 4096


def parse_geometry(payload: bytes) -> tuple[int, int] | None:
    """(cols, rows) from a geometry payload, or None for anything malformed.

    Malformed is ignored rather than fatal: the frame boundary is intact (the
    header said how long it was), so the stream is still trustworthy and only
    this one frame is not.
    """

    if len(payload) != GEOMETRY.size:
        return None
    cols, rows = GEOMETRY.unpack(payload)
    if not (GEOMETRY_MIN <= cols <= GEOMETRY_MAX and GEOMETRY_MIN <= rows <= GEOMETRY_MAX):
        return None
    return cols, rows


def parse_input_path(payload: bytes) -> str | None:
    """The keystroke-socket path from an input payload, or None for malformed.

    An empty payload withdraws the path (returns ``""``); a non-empty payload is
    a UTF-8 absolute path. Non-UTF-8, a path over ``INPUT_MAX_LEN``, or a path
    that is not absolute returns None — the caller ignores the frame, since the
    frame's own length kept the stream in step and only this one is untrustworthy.

    The path the function returns is **not** validated against the capture-socket
    convention (``.sock`` → ``.stdin.sock``); that is the connector's job, since
    it depends on the capture socket the session was created with.
    """

    if len(payload) > INPUT_MAX_LEN:
        return None
    if len(payload) == 0:
        return ""
    try:
        path = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not path.startswith("/"):
        return None
    return path


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

# The permissions a listening capture socket may have. Connecting to a Unix
# socket needs write permission on it, so these say who may produce into it and
# so read and type into the captured terminal. Owner-only is the default. Owner
# and group is for a server that runs as a user of its own while the producers
# (the shim, a session supervisor) run as another: the group comes from a
# setgid directory, so nothing here chowns. No other mode is accepted, since a
# world bit would hand the terminal to every local user.
OWNER_ONLY_SOCKET_MODE = 0o600
OWNER_AND_GROUP_SOCKET_MODE = 0o660
_SOCKET_MODES = (OWNER_ONLY_SOCKET_MODE, OWNER_AND_GROUP_SOCKET_MODE)


def check_socket_mode(mode: object) -> int:
    """*mode*, if it is one a capture socket may be bound with; else ValueError.

    A bool is refused although it is an int to Python: ``True`` is not a mode
    anyone meant to write.
    """
    if not isinstance(mode, int) or isinstance(mode, bool):
        raise ValueError("capture socket mode must be an integer: 0o600 or 0o660")
    if mode not in _SOCKET_MODES:
        raise ValueError(f"capture socket mode must be 0o600 or 0o660, got: {oct(mode)}")
    return mode


def _bind_umask(mode: int) -> int:
    """The umask that leaves exactly *mode*'s bits on a file it creates (0o177 for 0o600)."""
    return 0o777 & ~mode


# How long a probe of a socket already at the path may take to be answered.
_PROBE_TIMEOUT_S = 0.1


def _clear_stale_socket(path: str) -> None:
    """Remove a stale socket at *path*, and refuse one something still listens on.

    A socket a crashed server left behind must go before the bind, and one
    another session is serving must not: that session would lose its producer
    to this one. Only a socket whose connect is refused is stale, so it alone
    is removed; anything else there is left alone and the bind fails as
    ``EADDRINUSE``, as the Go port's does. Not a socket at all, the bind fails
    on it by itself.
    """
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(_PROBE_TIMEOUT_S)
    try:
        probe.connect(path)
    except ConnectionRefusedError:
        Path(path).unlink(missing_ok=True)
        return
    except OSError:
        # A listener too busy to answer in time is still a listener.
        pass
    finally:
        probe.close()
    raise OSError(errno.EADDRINUSE, f"capture socket is in use: {path}")


@dataclass
class CaptureFrame:
    channel: int
    data: bytes


class CaptureSocket:
    """
    Async Unix domain socket server that receives frames from libuterm_capture.

    Frame wire format: [1B channel][4B length big-endian][N bytes payload]
    """

    def __init__(self, socket_path: str, mode: int = OWNER_ONLY_SOCKET_MODE) -> None:
        validate_socket_path(socket_path)
        self._path = socket_path
        self._mode = check_socket_mode(mode)
        self._server: asyncio.Server | None = None
        self._queue: asyncio.Queue[CaptureFrame] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        # Live producer connections. ``Server.close()`` stops accepting new ones
        # and leaves these alone, so stop() has to close them itself — see there.
        self._connections: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        # Restrict the listening socket to its mode (the owner, or the owner
        # and the directory's group) so no other local user can connect and
        # inject/observe captured terminal traffic. Set the umask that leaves
        # exactly the mode's bits *before* the bind so the socket is created
        # with them atomically: a post-bind chmod would leave a window where
        # the socket exists with default-umask perms (e.g. srwxr-xr-x) that any
        # local user could connect to.
        #
        # os.umask is process-global, so it must never be held across an
        # await: ``start_unix_server(path=...)`` yields after its bind, and a
        # second start begun in that tick would read this bind's umask as the
        # one to restore and leave it behind for every file the server creates
        # afterwards (at 0o660, 0o117: wider than a service's 0o077). So the
        # socket is bound here, synchronously, as PamNotifyListener binds its
        # own, and asyncio is handed the bound socket. The event loop runs one
        # coroutine at a time, so no other start can come between the set and
        # the restore, and nothing else in the server sets the umask from a
        # thread.
        # Off the loop: a busy listener holds the probe up to its timeout.
        await asyncio.to_thread(_clear_stale_socket, self._path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            prev_umask = os.umask(_bind_umask(self._mode))
            try:
                sock.bind(self._path)
            finally:
                os.umask(prev_umask)
            self._server = await asyncio.start_unix_server(self._handle_connection, sock=sock)
        except BaseException:
            sock.close()
            raise
        # Belt-and-suspenders: enforce the mode even if the platform ignored the
        # umask for AF_UNIX sockets. With the umask in place this is a no-op.
        os.chmod(self._path, self._mode)  # noqa: PTH101 — chmod the just-bound socket fd path

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        # ``Server.close()`` only stops accepting; it leaves established
        # connections running, and since 3.12 ``wait_closed()`` waits for every
        # handler task to finish. A capture producer is a shim inside a live
        # session that holds its connection open for as long as that session
        # lasts, and the handler sits in ``readexactly`` waiting on it, so
        # waiting here without closing them first never returns: stopping one
        # session hung on a terminal that had done nothing wrong. Closing the
        # transport ends the handler's read, which is the graceful path it
        # already handles. ``close_clients()`` would do this, but it is 3.13+
        # and this package supports 3.11.
        for writer in tuple(self._connections):
            if not writer.is_closing():
                writer.close()
        await self._server.wait_closed()
        self._connections.clear()
        self._server = None
        try:
            Path(self._path).unlink()
        except FileNotFoundError:
            # Already removed (or never created); nothing to clean up.
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
        self._connections.add(writer)
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
            # The capture client disconnected (EOF mid-frame or reset); the finally block drops it.
            pass
        finally:
            self._connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: S110
                pass
