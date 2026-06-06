#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PTYConnector: stop() and inject/capture-socket teardown tests."""

from __future__ import annotations

import os
import signal as _signal
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ._connector_helpers import make_connector


async def test_stop_without_start_is_safe() -> None:
    await make_connector().stop()


async def test_inject_start_creates_capture_socket() -> None:
    """start() with inject=True wires up a CaptureSocket and cleans up on stop."""
    from provide.uterm.pty.capture import CaptureSocket

    mock_cap = AsyncMock(spec=CaptureSocket)
    with patch("provide.uterm.pty.connector.CaptureSocket", return_value=mock_cap):
        conn = make_connector(__import__("sys").executable, ["-c", "import time; time.sleep(0.1)"], inject=True)
        await conn.start()
        assert conn._capture_socket is mock_cap
        assert conn._capture_tmpdir is not None
        await conn.stop()
    mock_cap.stop.assert_awaited_once()
    assert conn._capture_socket is None
    assert conn._capture_tmpdir is None


async def test_stop_handles_dead_child_pid() -> None:
    """stop() handles ProcessLookupError + ChildProcessError from dead child."""
    conn = make_connector("/bin/echo", ["done"])
    conn._connected = True
    conn._child_pid = 999999999
    r_fd, w_fd = os.pipe()
    conn._master_fd = r_fd
    os.close(w_fd)
    await conn.stop()
    assert conn._child_pid is None
    assert conn._master_fd is None


async def test_stop_escalates_to_sigkill_when_child_survives_sighup() -> None:
    """stop() grants a grace window, then escalates to SIGKILL and block-reaps.

    A child that never exits (every WNOHANG returns 0) must be polled the full
    grace budget before SIGKILL — proving the escalation is not instant. The
    hardcoded ``call_count``/``assert_any_call`` values pin the grace constants
    so a mutated poll count or interval is killed by the mutation gate.
    """
    conn = make_connector("/bin/echo", ["done"])
    conn._connected = True
    conn._child_pid = 12345
    waitpid_calls: list[tuple[int, int]] = []

    def _fake_waitpid(pid: int, flags: int) -> tuple[int, int]:
        waitpid_calls.append((pid, flags))
        if flags == os.WNOHANG:
            return (0, 0)
        return (pid, 0)

    kill_calls: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    with (
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("os.waitpid", side_effect=_fake_waitpid),
        patch("os.kill", side_effect=_fake_kill),
    ):
        await conn.stop()

    assert conn._child_pid is None
    assert (12345, _signal.SIGHUP) in kill_calls
    assert (12345, _signal.SIGKILL) in kill_calls
    assert any(flags == 0 for _, flags in waitpid_calls), "blocking waitpid must be called"
    # The grace window polls the full budget before escalating (no early kill).
    assert mock_sleep.call_count == 20
    mock_sleep.assert_any_call(0.05)
    # Every WNOHANG poll — fast-path AND grace — must target the real child pid.
    # Pins the pid argument so os.waitpid(pid -> None) and
    # _reap_within_grace(self._child_pid -> None) mutants (which a pid-ignoring
    # mock would otherwise let survive) are killed.
    wnohang_pids = {pid for pid, flags in waitpid_calls if flags == os.WNOHANG}
    assert wnohang_pids == {12345}


async def test_stop_grace_reaps_child_that_exits_during_window() -> None:
    """A child that exits during the grace window is reaped WITHOUT SIGKILL.

    This is the fix's core guarantee: a still-live child gets time to run its
    hangup path (flush shell history, clean editor swap files) and exit on its
    own terms instead of being force-killed the instant the fast-path WNOHANG
    sees it still running.
    """
    conn = make_connector("/bin/echo", ["done"])
    conn._connected = True
    conn._child_pid = 12345
    wnohang_count = [0]

    def _fake_waitpid(pid: int, flags: int) -> tuple[int, int]:
        if flags == os.WNOHANG:
            wnohang_count[0] += 1
            # 1st WNOHANG = the fast-path check (still running); the child exits
            # by the first grace poll (2nd WNOHANG).
            if wnohang_count[0] >= 2:
                return (pid, 0)
            return (0, 0)
        return (pid, 0)

    kill_calls: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch("os.waitpid", side_effect=_fake_waitpid),
        patch("os.kill", side_effect=_fake_kill),
    ):
        await conn.stop()

    assert conn._child_pid is None
    assert (12345, _signal.SIGHUP) in kill_calls
    assert (12345, _signal.SIGKILL) not in kill_calls, "a child that exits gracefully must not be SIGKILLed"


async def test_stop_grace_childprocesserror_during_window_no_sigkill() -> None:
    """A child reaped (ChildProcessError) during the grace window skips SIGKILL."""
    conn = make_connector("/bin/echo", ["done"])
    conn._connected = True
    conn._child_pid = 12345
    wnohang_count = [0]

    def _fake_waitpid(pid: int, flags: int) -> tuple[int, int]:
        if flags == os.WNOHANG:
            wnohang_count[0] += 1
            if wnohang_count[0] >= 2:
                raise ChildProcessError("reaped during grace")
            return (0, 0)
        return (pid, 0)

    kill_calls: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch("os.waitpid", side_effect=_fake_waitpid),
        patch("os.kill", side_effect=_fake_kill),
    ):
        await conn.stop()

    assert conn._child_pid is None
    assert (12345, _signal.SIGKILL) not in kill_calls


async def test_stop_sigkill_processlookuperror_and_waitpid_childprocesserror() -> None:
    """stop() survives when SIGKILL raises ProcessLookupError and waitpid raises ChildProcessError."""
    conn = make_connector("/bin/echo", ["done"])
    conn._connected = True
    conn._child_pid = 12345

    def _fake_waitpid(pid: int, flags: int) -> tuple[int, int]:
        if flags == os.WNOHANG:
            return (0, 0)
        raise ChildProcessError("already reaped")

    def _fake_kill(pid: int, sig: int) -> None:
        if sig == _signal.SIGKILL:
            raise ProcessLookupError("child already gone")

    with patch("os.waitpid", side_effect=_fake_waitpid), patch("os.kill", side_effect=_fake_kill):
        await conn.stop()
    assert conn._child_pid is None


async def test_stop_cleans_up_orphaned_master_fd() -> None:
    """stop() closes a dangling master_fd even when child_pid is already None."""
    conn = make_connector("/bin/echo", ["done"])
    r_fd, w_fd = os.pipe()
    os.close(w_fd)
    conn._master_fd = r_fd
    await conn.stop()
    assert conn._master_fd is None
    import pytest

    with pytest.raises(OSError):
        os.close(r_fd)


async def test_stop_orphaned_master_fd_oserror_suppressed() -> None:
    """stop() suppresses OSError when closing an already-closed orphaned master_fd."""
    conn = make_connector("/bin/echo", ["done"])
    r_fd, w_fd = os.pipe()
    os.close(w_fd)
    os.close(r_fd)
    conn._master_fd = r_fd
    await conn.stop()
    assert conn._master_fd is None


async def test_stop_handles_oserror_on_master_close() -> None:
    """stop() gracefully handles OSError when master_fd already closed."""
    conn = make_connector("/bin/echo", ["done"])
    await conn.start()
    if conn._master_fd is not None:
        os.close(conn._master_fd)
    await conn.stop()


async def test_stop_calls_pam_close_session() -> None:
    """stop() calls pam.close_session() and sets _pam to exactly None."""
    conn = make_connector("/bin/echo")
    await conn.start()
    mock_pam = MagicMock()
    conn._pam = mock_pam
    await conn.stop()
    mock_pam.close_session.assert_called_once()
    assert conn._pam is None


async def test_stop_cleans_capture_tmpdir() -> None:
    """stop() removes the capture tmpdir via shutil.rmtree."""
    conn = make_connector("/bin/echo")
    await conn.start()
    tmpdir = tempfile.mkdtemp(prefix="test-uterm-cap-")
    Path(tmpdir).joinpath("cap.sock").touch()
    conn._capture_tmpdir = tmpdir
    await conn.stop()
    assert not Path(tmpdir).exists()
    assert conn._capture_tmpdir is None


async def test_stop_capture_tmpdir_none_after_cleanup() -> None:
    """stop() sets _capture_tmpdir to exactly None after removal."""
    conn = make_connector("/bin/echo")
    await conn.start()
    tmpdir = tempfile.mkdtemp(prefix="test-uterm-cap2-")
    conn._capture_tmpdir = tmpdir
    await conn.stop()
    assert conn._capture_tmpdir is None
