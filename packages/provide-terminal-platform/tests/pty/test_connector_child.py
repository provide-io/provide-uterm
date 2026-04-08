# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PTYConnector: child-branch (fork=0) and start() env-setup tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ._connector_helpers import (
    _child_fork_patches,
    _child_fork_patches_recording,
    make_connector,
)


# ── Env setup helpers ─────────────────────────────────────────────────────────

def _strip_user_vars() -> dict[str, str]:
    """Return os.environ without HOME/SHELL/USER/LOGNAME so setdefault fires."""
    return {k: v for k, v in os.environ.items() if k not in ("HOME", "SHELL", "USER", "LOGNAME")}


# ── Basic child-branch coverage ───────────────────────────────────────────────

async def test_start_env_sets_user_home_shell_logname() -> None:
    """start() with run_as_uid populates HOME/SHELL/USER/LOGNAME in the child env."""
    import pwd
    pw = pwd.getpwuid(os.geteuid())
    captured_env: dict[str, str] = {}
    patches = _child_fork_patches(captured_env)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
        conn = make_connector("/bin/echo", ["x"], run_as_uid=os.geteuid())
        with pytest.raises(SystemExit):
            await conn.start()
    assert captured_env.get("HOME") == pw.pw_dir
    assert captured_env.get("USER") == pw.pw_name
    assert captured_env.get("LOGNAME") == pw.pw_name
    assert captured_env.get("SHELL") == pw.pw_shell


async def test_start_env_without_resolved_user_keeps_process_env() -> None:
    """start() without user resolution uses process env unchanged."""
    captured_env: dict[str, str] = {}
    patches = _child_fork_patches(captured_env)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
        conn = make_connector("/bin/echo", ["x"])
        with pytest.raises(SystemExit):
            await conn.start()
    assert captured_env.get("HOME") == os.environ.get("HOME")


async def test_start_inject_no_lib_path_skips_env_var() -> None:
    """start() with inject=True but no lib path skips LD_PRELOAD/DYLD env vars."""
    captured_env: dict[str, str] = {}
    mock_cap = AsyncMock()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.get_capture_lib_path", return_value=None),
        patch("provide.terminal.pty.connector.CaptureSocket", return_value=mock_cap),
    ):
        conn = make_connector("/bin/echo", ["x"], inject=True)
        with pytest.raises(SystemExit):
            await conn.start()
    assert "LD_PRELOAD" not in captured_env
    assert "DYLD_INSERT_LIBRARIES" not in captured_env


async def test_start_inject_sets_lib_env_when_lib_present() -> None:
    """start() with inject=True on macOS sets DYLD_INSERT_LIBRARIES."""
    fake_lib = Path("/fake/libuterm_capture.dylib")
    captured_env: dict[str, str] = {}
    mock_cap = AsyncMock()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.get_capture_lib_path", return_value=fake_lib),
        patch("provide.terminal.pty.connector.CaptureSocket", return_value=mock_cap),
        patch("provide.terminal.pty.connector.sys") as mock_sys,
    ):
        mock_sys.platform = "darwin"
        conn = make_connector("/bin/echo", ["x"], inject=True)
        with pytest.raises(SystemExit):
            await conn.start()
    assert captured_env.get("DYLD_INSERT_LIBRARIES") == str(fake_lib)
    assert captured_env.get("DYLD_FORCE_FLAT_NAMESPACE") == "1"


async def test_start_inject_sets_ld_preload_on_linux() -> None:
    """start() with inject=True on non-darwin platform sets LD_PRELOAD."""
    fake_lib = Path("/fake/libuterm_capture.so")
    captured_env: dict[str, str] = {}
    mock_cap = AsyncMock()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.get_capture_lib_path", return_value=fake_lib),
        patch("provide.terminal.pty.connector.CaptureSocket", return_value=mock_cap),
        patch("provide.terminal.pty.connector.sys") as mock_sys,
    ):
        mock_sys.platform = "linux"
        conn = make_connector("/bin/echo", ["x"], inject=True)
        with pytest.raises(SystemExit):
            await conn.start()
    assert captured_env.get("LD_PRELOAD") == str(fake_lib)
    assert "DYLD_INSERT_LIBRARIES" not in captured_env


# ── Mutation-killing tests ────────────────────────────────────────────────────

async def test_start_env_setdefault_keys_fire_when_vars_absent() -> None:
    """start() env.setdefault uses exact key names HOME/SHELL/USER/LOGNAME.

    Kills mutations that change these keys (e.g. 'home', 'XXHOMEXX', None).
    Use patch.dict with clear=True to strip those vars so setdefault fires.
    """
    import pwd
    pw = pwd.getpwuid(os.geteuid())
    captured_env: dict[str, str] = {}
    safe_env = _strip_user_vars()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch.dict(os.environ, safe_env, clear=True),
    ):
        conn = make_connector("/bin/echo", run_as_uid=os.geteuid())
        with pytest.raises(SystemExit):
            await conn.start()
    assert captured_env.get("HOME") == pw.pw_dir
    assert captured_env.get("USER") == pw.pw_name
    assert captured_env.get("LOGNAME") == pw.pw_name
    assert captured_env.get("SHELL") == pw.pw_shell
    assert captured_env["HOME"] is not None
    assert captured_env["USER"] is not None
    assert captured_env["SHELL"] is not None
    assert captured_env["LOGNAME"] is not None


async def test_start_env_setdefault_values_from_resolved_user() -> None:
    """start() env.setdefault passes resolved.home/shell/name as values.

    Kills mutations that replace the value with None or empty.
    """
    mock_uid_map = MagicMock()
    mock_resolved = MagicMock()
    mock_resolved.home = "/custom/home"
    mock_resolved.shell = "/custom/shell"
    mock_resolved.name = "customuser"
    mock_resolved.uid = os.geteuid()
    mock_resolved.gid = os.getegid()
    mock_uid_map.resolve.return_value = mock_resolved

    captured_env: dict[str, str] = {}
    safe_env = _strip_user_vars()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.UidMap", return_value=mock_uid_map),
        patch.dict(os.environ, safe_env, clear=True),
    ):
        conn = make_connector("/bin/echo", run_as_uid=os.geteuid())
        with pytest.raises(SystemExit):
            await conn.start()
    assert captured_env.get("HOME") == "/custom/home"
    assert captured_env.get("SHELL") == "/custom/shell"
    assert captured_env.get("USER") == "customuser"
    assert captured_env.get("LOGNAME") == "customuser"


async def test_start_resolve_called_with_run_as_only() -> None:
    """start() calls resolve() when only run_as is provided.

    Kills the 'or' → 'and' mutations in the resolve condition.
    """
    mock_uid_map = MagicMock()
    mock_resolved = MagicMock()
    mock_resolved.home = "/home/op"
    mock_resolved.shell = "/bin/bash"
    mock_resolved.name = "op"
    mock_resolved.uid = os.geteuid()
    mock_resolved.gid = os.getegid()
    mock_uid_map.resolve.return_value = mock_resolved

    captured_env: dict[str, str] = {}
    safe_env = _strip_user_vars()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.UidMap", return_value=mock_uid_map),
        patch.dict(os.environ, safe_env, clear=True),
    ):
        conn = make_connector("/bin/echo", run_as="operator")
        with pytest.raises(SystemExit):
            await conn.start()
    mock_uid_map.resolve.assert_called_once()
    assert captured_env.get("HOME") == "/home/op"


async def test_start_resolve_args_include_run_as_and_gid() -> None:
    """start() passes run_as and run_as_gid to resolve().

    Kills mutations that replace these with None or omit them.
    """
    mock_uid_map = MagicMock()
    mock_resolved = MagicMock()
    mock_resolved.home = "/home/x"
    mock_resolved.shell = "/bin/sh"
    mock_resolved.name = "x"
    mock_resolved.uid = os.geteuid()
    mock_resolved.gid = os.getegid()
    mock_uid_map.resolve.return_value = mock_resolved

    captured_env: dict[str, str] = {}
    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.UidMap", return_value=mock_uid_map),
    ):
        conn = make_connector("/bin/echo", run_as_uid=os.geteuid(), run_as="myuser", run_as_gid=999)
        with pytest.raises(SystemExit):
            await conn.start()
    call_kwargs = mock_uid_map.resolve.call_args[1]
    assert call_kwargs.get("run_as") == "myuser"
    assert call_kwargs.get("run_as_gid") == 999


async def test_start_capture_socket_path_uses_cap_sock() -> None:
    """start() creates capture socket at '<tmpdir>/cap.sock'.

    Kills mutations that change the filename ('XXcap.sockXX', 'CAP.SOCK', None).
    """
    known_tmpdir = "/tmp/test-uterm-cap-known"  # noqa: S108
    captured_env: dict[str, str] = {}
    cap_socket_args: list[str] = []

    def _fake_capture_socket(path: str) -> Any:
        cap_socket_args.append(path)
        return AsyncMock()

    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.tempfile.mkdtemp", return_value=known_tmpdir),
        patch("provide.terminal.pty.connector.get_capture_lib_path", return_value=None),
        patch("provide.terminal.pty.connector.CaptureSocket", side_effect=_fake_capture_socket),
    ):
        conn = make_connector("/bin/echo", inject=True)
        with pytest.raises(SystemExit):
            await conn.start()
    assert len(cap_socket_args) == 1
    assert cap_socket_args[0] == f"{known_tmpdir}/cap.sock"
    assert captured_env.get("UTERM_CAPTURE_SOCKET") == f"{known_tmpdir}/cap.sock"


async def test_start_capture_socket_env_key_exact() -> None:
    """start() sets env['UTERM_CAPTURE_SOCKET'] with exact key name.

    Kills mutations that change key to 'XXUTERM_CAPTURE_SOCKETXX' or 'uterm_capture_socket'.
    """
    known_tmpdir = "/tmp/test-uterm-cap2-known"  # noqa: S108
    captured_env: dict[str, str] = {}
    mock_cap = AsyncMock()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.tempfile.mkdtemp", return_value=known_tmpdir),
        patch("provide.terminal.pty.connector.get_capture_lib_path", return_value=None),
        patch("provide.terminal.pty.connector.CaptureSocket", return_value=mock_cap),
    ):
        conn = make_connector("/bin/echo", inject=True)
        with pytest.raises(SystemExit):
            await conn.start()
    assert "UTERM_CAPTURE_SOCKET" in captured_env
    assert captured_env["UTERM_CAPTURE_SOCKET"] == f"{known_tmpdir}/cap.sock"


async def test_start_inject_uterm_capture_socket_value() -> None:
    """start() sets UTERM_CAPTURE_SOCKET to the actual path, not None."""
    captured_env: dict[str, str] = {}
    mock_cap = AsyncMock()
    patches = _child_fork_patches(captured_env)
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.get_capture_lib_path", return_value=None),
        patch("provide.terminal.pty.connector.CaptureSocket", return_value=mock_cap),
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
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
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
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.UidMap", return_value=mock_uid_map),
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
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
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
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
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
    patches[8] = patch("provide.terminal.pty.connector.os.execve", side_effect=_fake_execve_no_raise)

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
        conn = make_connector("/bin/echo")
        await conn.start()  # must not raise since execve mock returns normally

    assert exit_calls == [127], f"os._exit must be called with 127, got {exit_calls}"


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
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.UidMap", return_value=mock_uid_map),
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
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12],
        patch("provide.terminal.pty.connector.tempfile.mkdtemp", side_effect=_fake_mkdtemp),
        patch("provide.terminal.pty.connector.get_capture_lib_path", return_value=None),
        patch("provide.terminal.pty.connector.CaptureSocket", return_value=mock_cap),
    ):
        conn = make_connector("/bin/echo", inject=True)
        with pytest.raises(SystemExit):
            await conn.start()

    assert mkdtemp_calls, "tempfile.mkdtemp must be called"
    assert mkdtemp_calls[0].get("prefix") == "uterm-cap-", (
        f"mkdtemp prefix must be 'uterm-cap-', got {mkdtemp_calls[0].get('prefix')!r}"
    )
