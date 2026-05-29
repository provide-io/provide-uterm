#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PTYConnector: child-branch (fork=0) and start() env-setup tests."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ._connector_helpers import (
    _child_fork_patches,
    _child_fork_patches_recording,
    make_connector,
)

# ── Env setup helpers ─────────────────────────────────────────────────────────


async def test_start_inject_uterm_capture_socket_value() -> None:
    """start() sets UTERM_CAPTURE_SOCKET to the actual path, not None."""
    captured_env: dict[str, str] = {}
    mock_cap = AsyncMock()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
        patches[11],
        patches[12],
        patch("provide.uterm.pty.connector.get_capture_lib_path", return_value=None),
        patch("provide.uterm.pty.connector.CaptureSocket", return_value=mock_cap),
    ):
        conn = make_connector("/bin/echo", inject=True)
        with pytest.raises(SystemExit):
            await conn.start()
    assert captured_env.get("UTERM_CAPTURE_SOCKET") is not None
    assert captured_env.get("UTERM_CAPTURE_SOCKET", "").endswith("cap.sock")


async def test_start_child_dup2_fd_numbers() -> None:
    """Child calls os.dup2(slave_fd, 0/1/2) with exact fd numbers.

    Kills mutations like os.dup2(slave_fd, 1) instead of os.dup2(slave_fd, 0).
    """
    dup2_calls: list[tuple[int, int]] = []
    captured_env: dict[str, str] = {}
    patches = _child_fork_patches_recording(captured_env, dup2_calls=dup2_calls)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
        patches[11],
        patches[12],
    ):
        conn = make_connector("/bin/echo")
        with pytest.raises(SystemExit):
            await conn.start()
    newfd_list = [newfd for _, newfd in dup2_calls[:3]]
    assert 0 in newfd_list, "stdin (0) must be dup2'd"
    assert 1 in newfd_list, "stdout (1) must be dup2'd"
    assert 2 in newfd_list, "stderr (2) must be dup2'd"


async def test_start_child_setgid_initgroups_setuid_args() -> None:
    """Child calls setgid/initgroups/setuid with resolved user's exact values.

    Kills mutations that pass None instead of the resolved values.
    """
    mock_uid_map = MagicMock()
    mock_resolved = MagicMock()
    mock_resolved.home = "/home/test"
    mock_resolved.shell = "/bin/sh"
    mock_resolved.name = "testuser"
    mock_resolved.uid = 5001
    mock_resolved.gid = 5002
    mock_uid_map.resolve.return_value = mock_resolved

    setgid_calls: list[int] = []
    initgroups_calls: list[tuple[Any, Any]] = []
    setuid_calls: list[int] = []
    captured_env: dict[str, str] = {}
    patches = _child_fork_patches_recording(
        captured_env,
        setgid_calls=setgid_calls,
        initgroups_calls=initgroups_calls,
        setuid_calls=setuid_calls,
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
        patches[11],
        patches[12],
        patch("provide.uterm.pty.connector.UidMap", return_value=mock_uid_map),
    ):
        conn = make_connector("/bin/echo", run_as_uid=5001)
        with pytest.raises(SystemExit):
            await conn.start()
    assert setgid_calls == [5002], f"setgid must be called with gid=5002, got {setgid_calls}"
    assert initgroups_calls == [("testuser", 5002)], f"initgroups args wrong: {initgroups_calls}"
    assert setuid_calls == [5001], f"setuid must be called with uid=5001, got {setuid_calls}"


async def test_start_child_termios_echo_bit_cleared() -> None:
    """Child clears the ECHO bit in attrs[3] (lflags index).

    Kills mutations like attrs[4] &= ~ECHO or attrs[3] &= ECHO.
    """
    import termios as _termios

    tcsetattr_calls: list[tuple[Any, ...]] = []
    captured_env: dict[str, str] = {}
    patches = _child_fork_patches_recording(captured_env, tcsetattr_calls=tcsetattr_calls)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
        patches[11],
        patches[12],
    ):
        conn = make_connector("/bin/echo")
        with pytest.raises(SystemExit):
            await conn.start()
    assert tcsetattr_calls, "tcsetattr must be called"
    _, _, attrs = tcsetattr_calls[0]
    assert attrs[3] & _termios.ECHO == 0, "ECHO bit must be cleared in attrs[3]"


async def test_start_child_ioctl_tiocsctty_zero() -> None:
    """Child calls fcntl.ioctl(slave_fd, TIOCSCTTY, 0).

    Kills mutations that pass 1 instead of 0 or omit the arg.
    """
    import termios as _termios

    ioctl_calls: list[tuple[Any, ...]] = []
    captured_env: dict[str, str] = {}
    patches = _child_fork_patches_recording(captured_env, ioctl_calls=ioctl_calls)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
        patches[11],
        patches[12],
    ):
        conn = make_connector("/bin/echo")
        with pytest.raises(SystemExit):
            await conn.start()
    tiocsctty_calls = [args for args in ioctl_calls if len(args) >= 2 and args[1] == _termios.TIOCSCTTY]
    assert tiocsctty_calls, "ioctl(slave_fd, TIOCSCTTY, ...) must be called"
    assert tiocsctty_calls[0][2] == 0, f"TIOCSCTTY arg must be 0, got {tiocsctty_calls[0]}"


async def test_start_child_exit_code_is_127() -> None:
    """Child calls os._exit(127) when execve returns normally (doesn't replace process).

    Kills mutations that change 127 to None or 128.
    Uses a no-raise execve mock so execution falls through to the _exit call.
    """
    exit_calls: list[int] = []
    captured_env: dict[str, str] = {}

    def _fake_execve_no_raise(cmd: str, argv: list[str], env: dict[str, str]) -> None:
        captured_env.update(env)
        # Don't raise — execution falls through to os._exit(127)

    patches = _child_fork_patches_recording(captured_env, exit_calls=exit_calls)
    # Replace execve with no-raise version
    patches[8] = patch("provide.uterm.pty.connector.os.execve", side_effect=_fake_execve_no_raise)

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
        patches[11],
        patches[12],
    ):
        conn = make_connector("/bin/echo")
        with pytest.raises(SystemExit):
            # execve returns normally → child body falls through to os._exit(127),
            # which the test harness turns into SystemExit to stand in for
            # real process termination.
            await conn.start()

    assert exit_calls == [127], f"os._exit must be called with 127, got {exit_calls}"


def test_child_exec_exits_127_on_privilege_drop_failure() -> None:
    """A raise from setgid/setuid in the child must route to os._exit(127),
    never unwind into inherited parent atexit/buffered-IO handlers (PLAT-fork)."""
    exit_calls: list[int] = []

    def _record_exit(code: int) -> None:
        exit_calls.append(code)
        raise SystemExit(code)

    resolved = MagicMock(gid=999, uid=999, name="op")
    conn = make_connector("/bin/echo")
    with (
        patch("provide.uterm.pty.connector.os.close"),
        patch("provide.uterm.pty.connector.os.setsid"),
        patch("provide.uterm.pty.connector.fcntl.ioctl"),
        patch("provide.uterm.pty.connector.os.dup2"),
        patch("provide.uterm.pty.connector.os.setgid", side_effect=PermissionError("denied")),
        patch("provide.uterm.pty.connector.os.initgroups"),
        patch("provide.uterm.pty.connector.os.setuid"),
        patch("provide.uterm.pty.connector.os.execve"),
        patch("provide.uterm.pty.connector.os._exit", side_effect=_record_exit),
    ):
        with pytest.raises(SystemExit):
            conn._child_exec(3, 4, resolved, {"PATH": "/bin"})
    # PermissionError was swallowed; child terminated with 127, not the raise.
    assert exit_calls == [127]


def test_child_exec_exits_127_on_execve_failure() -> None:
    """execve raising (e.g. missing command) must also terminate the child 127."""
    exit_calls: list[int] = []

    def _record_exit(code: int) -> None:
        exit_calls.append(code)
        raise SystemExit(code)

    conn = make_connector("/no/such/cmd")
    with (
        patch("provide.uterm.pty.connector.os.close"),
        patch("provide.uterm.pty.connector.os.setsid"),
        patch("provide.uterm.pty.connector.fcntl.ioctl"),
        patch("provide.uterm.pty.connector.os.dup2"),
        patch("provide.uterm.pty.connector.os.execve", side_effect=FileNotFoundError("nope")),
        patch("provide.uterm.pty.connector.os._exit", side_effect=_record_exit),
    ):
        # slave_fd <= 2 also exercises the branch that skips the extra close.
        with pytest.raises(SystemExit):
            conn._child_exec(3, 2, None, {"PATH": "/bin"})
    assert exit_calls == [127]


async def test_start_resolve_username_fallback_is_empty_string() -> None:
    """start() passes '' (not 'XXXX') as username fallback when self._username is None.

    Kills the mutation: self._username or 'XXXX'.
    """
    mock_uid_map = MagicMock()
    mock_resolved = MagicMock()
    mock_resolved.home = "/h"
    mock_resolved.shell = "/bin/sh"
    mock_resolved.name = "x"
    mock_resolved.uid = os.geteuid()
    mock_resolved.gid = os.getegid()
    mock_uid_map.resolve.return_value = mock_resolved

    captured_env: dict[str, str] = {}
    patches = _child_fork_patches(captured_env)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
        patches[11],
        patches[12],
        patch("provide.uterm.pty.connector.UidMap", return_value=mock_uid_map),
    ):
        conn = make_connector("/bin/echo", run_as_uid=os.geteuid())
        with pytest.raises(SystemExit):
            await conn.start()

    # First positional arg to resolve() must be "" not "XXXX" when username is None
    first_arg = mock_uid_map.resolve.call_args[0][0]
    assert first_arg == "", f"username fallback must be '' not {first_arg!r}"


async def test_start_mkdtemp_prefix_is_uterm_cap() -> None:
    """start() calls tempfile.mkdtemp with prefix='uterm-cap-'.

    Kills mutations that change prefix to None, 'XXuterm-cap-XX', or 'UTERM-CAP-'.
    """
    import tempfile as _tempfile_mod

    # Capture the real function BEFORE patching (patch replaces tempfile.mkdtemp on the module)
    _orig_mkdtemp = _tempfile_mod.mkdtemp

    mkdtemp_calls: list[dict[str, Any]] = []

    def _fake_mkdtemp(**kwargs: Any) -> str:
        mkdtemp_calls.append(kwargs)
        return _orig_mkdtemp(**kwargs)  # call original via direct reference, not via module

    captured_env: dict[str, str] = {}
    mock_cap = AsyncMock()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
        patches[11],
        patches[12],
        patch("provide.uterm.pty.connector.tempfile.mkdtemp", side_effect=_fake_mkdtemp),
        patch("provide.uterm.pty.connector.get_capture_lib_path", return_value=None),
        patch("provide.uterm.pty.connector.CaptureSocket", return_value=mock_cap),
    ):
        conn = make_connector("/bin/echo", inject=True)
        with pytest.raises(SystemExit):
            await conn.start()

    assert mkdtemp_calls, "tempfile.mkdtemp must be called"
    assert mkdtemp_calls[0].get("prefix") == "uterm-cap-", (
        f"mkdtemp prefix must be 'uterm-cap-', got {mkdtemp_calls[0].get('prefix')!r}"
    )


# ── Exact child syscall arguments (mutmut survivor kills) ─────────────────────


async def test_child_exec_passes_exact_fds_and_command() -> None:
    """Child syscalls use the exact fds/command, not None or a wrong fd.

    Kills the argument mutants: os.close(master_fd)->close(None),
    fcntl.ioctl(slave_fd,...)->ioctl(None,...), os.dup2(slave_fd, N)->dup2(None, N),
    the slave_fd>2 os.close(slave_fd)->close(None), argv=[cmd,*args]->None,
    and os.execve(cmd, argv, env) first/second-arg drops.
    """
    from contextlib import ExitStack

    master_fd, slave_fd = 10, 11
    captured_env: dict[str, str] = {}
    dup2_calls: list[tuple[int, int]] = []
    ioctl_calls: list[tuple[Any, ...]] = []
    close_calls: list[int] = []
    execve_calls: list[tuple[Any, Any]] = []
    patches = _child_fork_patches_recording(
        captured_env,
        dup2_calls=dup2_calls,
        ioctl_calls=ioctl_calls,
        close_calls=close_calls,
        execve_calls=execve_calls,
    )
    with ExitStack() as stack:
        stack.enter_context(patch("provide.uterm.pty.connector.pty.openpty", return_value=(master_fd, slave_fd)))
        for p in patches:
            stack.enter_context(p)
        conn = make_connector("/bin/echo", args=["hello"])
        with pytest.raises(SystemExit):
            await conn.start()

    # master fd closed in the child; slave fd (>2) closed after dup2.
    assert master_fd in close_calls, f"master_fd must be closed in child, got {close_calls}"
    assert slave_fd in close_calls, f"slave_fd (>2) must be closed after dup2, got {close_calls}"
    # controlling tty set on the slave fd, not None/other.
    assert ioctl_calls and ioctl_calls[0][0] == slave_fd, f"ioctl must target slave_fd, got {ioctl_calls}"
    # stdio dup2'd FROM the slave fd to 0/1/2 in order.
    assert dup2_calls[:3] == [(slave_fd, 0), (slave_fd, 1), (slave_fd, 2)], f"dup2 args wrong: {dup2_calls}"
    # execve with the exact command and argv.
    assert execve_calls, "execve must be called"
    cmd, argv = execve_calls[0]
    assert cmd == "/bin/echo", f"execve command wrong: {cmd!r}"
    assert argv == ["/bin/echo", "hello"], f"execve argv wrong: {argv!r}"


async def test_start_parent_records_child_pid() -> None:
    """The parent path stores the forked child's pid on the connector.

    Kills ``self._child_pid = pid`` -> ``self._child_pid = None``.
    """
    from contextlib import ExitStack

    master_fd, slave_fd, child_pid = 10, 11, 4242
    captured_env: dict[str, str] = {}
    patches = _child_fork_patches_recording(captured_env)
    with ExitStack() as stack:
        stack.enter_context(patch("provide.uterm.pty.connector.pty.openpty", return_value=(master_fd, slave_fd)))
        for p in patches:
            stack.enter_context(p)
        # Override fork to take the PARENT path (non-zero pid) after the
        # recording helper's child-path fork patch.
        stack.enter_context(patch("provide.uterm.pty.connector.os.fork", return_value=child_pid))
        conn = make_connector("/bin/echo")
        await conn.start()

    assert conn._child_pid == child_pid
    assert conn._master_fd == master_fd
    assert conn._connected is True
