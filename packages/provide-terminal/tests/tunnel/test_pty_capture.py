#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for PTY capture module."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from unittest.mock import patch

import pytest

from provide.terminal.tunnel.pty_capture import (
    TtyProxy,
    _get_term_size,
    _set_term_size,
    install_sigwinch_handler,
    spawn_pty,
)


class TestGetTermSize:
    def test_returns_tuple(self) -> None:
        if os.isatty(0):
            cols, rows = _get_term_size(0)
            assert cols > 0
            assert rows > 0
        else:
            # in CI / no tty — should fall back to 80x24
            cols, rows = _get_term_size(0)
            assert (cols, rows) == (80, 24)

    def test_invalid_fd_returns_fallback(self) -> None:
        cols, rows = _get_term_size(9999)
        assert (cols, rows) == (80, 24)


class TestSpawnPty:
    def test_spawn_echo(self) -> None:
        sp = spawn_pty(["echo", "hello"])
        assert sp.master_fd >= 0
        assert sp.child_pid > 0
        assert not sp.closed
        sp.close()
        assert sp.closed

    def test_spawn_default_shell(self) -> None:
        sp = spawn_pty()
        assert sp.master_fd >= 0
        assert sp.child_pid > 0
        sp.close()

    @pytest.mark.asyncio
    async def test_read_output(self) -> None:
        sp = spawn_pty(["echo", "tunnel_test_marker"])
        try:
            data = b""
            for _ in range(20):
                try:
                    chunk = await asyncio.wait_for(sp.read(4096), timeout=0.5)
                    data += chunk
                    if b"tunnel_test_marker" in data:
                        break
                except (TimeoutError, OSError):
                    break
            assert b"tunnel_test_marker" in data
        finally:
            sp.close()

    @pytest.mark.asyncio
    async def test_write_input(self) -> None:
        sp = spawn_pty(["/bin/cat"])
        try:
            await sp.write(b"hello\n")
            data = b""
            for _ in range(20):
                try:
                    chunk = await asyncio.wait_for(sp.read(4096), timeout=0.5)
                    data += chunk
                    if b"hello" in data:
                        break
                except (TimeoutError, OSError):
                    break
            assert b"hello" in data
        finally:
            sp.close()

    def test_close_idempotent(self) -> None:
        sp = spawn_pty(["true"])
        sp.close()
        sp.close()  # second close is a no-op
        assert sp.closed

    def test_resize(self) -> None:
        sp = spawn_pty(["sleep", "1"])
        try:
            sp.resize(120, 40)
            cols, rows = sp.term_size()
            assert cols == 120
            assert rows == 40
        finally:
            sp.close()

    def test_resize_after_close(self) -> None:
        sp = spawn_pty(["true"])
        sp.close()
        # resize after close should not raise
        sp.resize(80, 24)

    def test_close_already_closed_fd(self) -> None:
        """close() handles OSError when fd is already closed."""
        sp = spawn_pty(["true"])
        os.close(sp.master_fd)  # pre-close the fd
        sp.close()  # should not raise
        assert sp.closed

    def test_close_child_already_reaped(self) -> None:
        """close() handles ChildProcessError when child was already waited."""
        import time

        sp = spawn_pty(["true"])
        time.sleep(0.1)
        with suppress(ChildProcessError):
            os.waitpid(sp.child_pid, os.WNOHANG)
        sp.close()  # should not raise
        assert sp.closed


class TestSetTermSize:
    def test_set_on_pty(self) -> None:
        sp = spawn_pty(["sleep", "1"])
        try:
            _set_term_size(sp.master_fd, 132, 50)
            cols, rows = _get_term_size(sp.master_fd)
            assert cols == 132
            assert rows == 50
        finally:
            sp.close()


class TestTtyProxy:
    def test_not_a_tty(self) -> None:
        proxy = TtyProxy()
        # pytest captures stdin, so fileno() may raise or fd may not be a tty
        with pytest.raises(OSError):
            proxy.start()

    def test_close_when_not_active(self) -> None:
        proxy = TtyProxy()
        assert not proxy.active
        proxy.close()  # should not raise

    def test_term_size_no_fd(self) -> None:
        proxy = TtyProxy()
        assert proxy.term_size() == (80, 24)

    def test_close_with_bad_fd(self) -> None:
        """close() handles OSError from tcsetattr gracefully."""
        proxy = TtyProxy()
        proxy._active = True
        proxy._fd = 9999
        proxy._old_attrs = [0, 0, 0, 0, 0, 0, [b"\x00"] * 32]
        proxy.close()  # should not raise
        assert not proxy.active

    def test_term_size_with_pty_fd(self) -> None:
        """term_size() works when _fd is set to a real PTY fd."""
        sp = spawn_pty(["sleep", "1"])
        try:
            proxy = TtyProxy()
            proxy._fd = sp.master_fd
            cols, rows = proxy.term_size()
            assert cols > 0 and rows > 0
        finally:
            sp.close()

    @pytest.mark.asyncio
    async def test_read_with_pty_fd(self) -> None:
        """read() returns data from a PTY fd."""
        sp = spawn_pty(["echo", "proxy_read_test"])
        try:
            proxy = TtyProxy()
            proxy._fd = sp.master_fd
            data = b""
            for _ in range(20):
                try:
                    chunk = await asyncio.wait_for(proxy.read(4096), timeout=0.5)
                    data += chunk
                    if b"proxy_read_test" in data:
                        break
                except (TimeoutError, OSError):
                    break
            assert b"proxy_read_test" in data
        finally:
            sp.close()

    @pytest.mark.asyncio
    async def test_read_raises_when_not_started(self) -> None:
        """Line 128-129: read() raises OSError when _fd is None."""
        proxy = TtyProxy()
        with pytest.raises(OSError, match="not active"):
            await proxy.read()

    @pytest.mark.asyncio
    async def test_write_local(self) -> None:
        """write_local() doesn't raise."""
        proxy = TtyProxy()
        proxy._fd = 1  # stdout
        await proxy.write_local(b"")

    def test_not_a_tty_with_pipe_fd(self) -> None:
        """start() raises OSError when fd is valid but not a TTY."""
        r, w = os.pipe()
        try:
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.fileno.return_value = r
                proxy = TtyProxy()
                with pytest.raises(OSError, match="not a TTY"):
                    proxy.start()
        finally:
            os.close(r)
            os.close(w)

    def test_start_with_pty_fd(self) -> None:
        """start() succeeds when stdin is redirected to a PTY slave."""
        import pty

        master, slave = pty.openpty()
        try:
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.fileno.return_value = slave
                proxy = TtyProxy()
                cols, rows = proxy.start()
                assert cols > 0 and rows > 0
                assert proxy.active
                proxy.close()
                assert not proxy.active
        finally:
            os.close(master)
            with suppress(OSError):
                os.close(slave)


class TestSigwinchHandler:
    def test_install_handler(self) -> None:
        called = []
        prev = install_sigwinch_handler(lambda: called.append(True))
        try:
            os.kill(os.getpid(), signal.SIGWINCH)
            assert len(called) == 1
        finally:
            signal.signal(signal.SIGWINCH, prev or signal.SIG_DFL)

    def test_returns_previous_handler(self) -> None:
        sentinel = lambda _s, _f: None  # noqa: E731
        signal.signal(signal.SIGWINCH, sentinel)
        prev = install_sigwinch_handler(lambda: None)
        assert prev is sentinel
        signal.signal(signal.SIGWINCH, signal.SIG_DFL)
