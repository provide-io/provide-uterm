#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PTY capture for tunnel sharing."""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import tty
import typing
from contextlib import suppress
from dataclasses import dataclass, field
from typing import cast


def _get_term_size(fd: int) -> tuple[int, int]:
    """Return (cols, rows) for *fd*, falling back to (80, 24)."""
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols = struct.unpack("HHHH", packed)[:2]
        if cols > 0 and rows > 0:
            return cols, rows
    except (OSError, ValueError):
        pass
    return 80, 24


def _set_term_size(fd: int, cols: int, rows: int) -> None:
    """Set the terminal size on *fd*."""
    packed = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


@dataclass
class SpawnedPty:
    """A spawned PTY child process."""

    master_fd: int
    child_pid: int
    _closed: bool = field(default=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    async def read(self, size: int = 4096) -> bytes:
        """Read up to *size* bytes from the PTY master."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: os.read(self.master_fd, size))

    async def write(self, data: bytes) -> None:
        """Write *data* to the PTY master."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, os.write, self.master_fd, data)

    def resize(self, cols: int, rows: int) -> None:
        """Set the PTY window size. No-op if already closed."""
        if self._closed:
            return
        _set_term_size(self.master_fd, cols, rows)

    def term_size(self) -> tuple[int, int]:
        """Return current (cols, rows)."""
        return _get_term_size(self.master_fd)

    def close(self) -> None:
        """Close the master fd and reap the child. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with suppress(OSError):
            os.close(self.master_fd)
        with suppress(ChildProcessError):
            os.waitpid(self.child_pid, os.WNOHANG)


def spawn_pty(cmd: list[str] | None = None) -> SpawnedPty:
    """Spawn a child process in a new PTY.

    *cmd* defaults to the user's ``$SHELL``.
    """
    if cmd is None:
        cmd = [os.environ.get("SHELL", "/bin/sh")]

    child_pid, master_fd = pty.fork()
    if child_pid == 0:  # pragma: no cover — runs in forked child
        os.execvp(cmd[0], cmd)  # noqa: S606
        sys.exit(1)

    return SpawnedPty(master_fd=master_fd, child_pid=child_pid)


@dataclass
class TtyProxy:
    """Raw-mode proxy on the local TTY (stdin/stdout)."""

    _fd: int | None = field(default=None, repr=False)
    _old_attrs: list[object] | None = field(default=None, repr=False)
    _active: bool = field(default=False, repr=False)

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> tuple[int, int]:
        """Enter raw mode on stdin. Returns (cols, rows). Raises OSError if not a TTY."""
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            raise OSError("stdin is not a TTY")
        self._fd = fd
        self._old_attrs = termios.tcgetattr(fd)
        tty.setraw(fd)
        self._active = True
        return _get_term_size(fd)

    async def read(self, size: int = 4096) -> bytes:
        """Read from stdin."""
        fd = self._fd
        if fd is None:
            msg = "stdin is not active"
            raise OSError(msg)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: os.read(fd, size))

    async def write_local(self, data: bytes) -> None:
        """Write to stdout."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, sys.stdout.buffer.write, data)

    def term_size(self) -> tuple[int, int]:
        """Return current (cols, rows) of the local TTY."""
        if self._fd is not None:
            return _get_term_size(self._fd)
        return 80, 24

    def close(self) -> None:
        """Restore original TTY attributes. Safe to call when not active."""
        if not self._active:
            return
        self._active = False
        if self._fd is not None and self._old_attrs is not None:
            with suppress(OSError, termios.error):
                termios.tcsetattr(self._fd, termios.TCSAFLUSH, cast("list[int | list[bytes]]", self._old_attrs))


def install_sigwinch_handler(callback: typing.Callable[[], object]) -> signal.Handlers:
    """Install a SIGWINCH handler that calls *callback* with no arguments.

    Returns the previous handler.
    """
    return cast(
        "signal.Handlers",
        signal.signal(
            signal.SIGWINCH,
            lambda _signum, _frame: callback(),
        ),
    )
