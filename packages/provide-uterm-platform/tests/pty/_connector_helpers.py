#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared helpers for PTYConnector tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from provide.terminal.pty.connector import PTYConnector


def make_connector(command: str = "/bin/echo", args: list[str] | None = None, **kwargs: Any) -> PTYConnector:
    return PTYConnector(
        session_id="test-pty-1",
        display_name="Test PTY",
        config={"command": command, "args": args or [], **kwargs},
    )


def _child_fork_patches(captured_env: dict[str, str]) -> list[Any]:
    """Return patches that simulate os.fork() returning 0 (child) and capture execve env.

    Patches all syscalls needed in the child branch so tests run without root
    and without actually forking. The captured_env dict is populated by the
    fake os.execve before raising SystemExit(0).
    """

    def _fake_execve(cmd: str, argv: list[str], env: dict[str, str]) -> None:
        captured_env.update(env)
        raise SystemExit(0)

    return [
        patch("provide.terminal.pty.connector.os.fork", return_value=0),
        patch("provide.terminal.pty.connector.os.close"),
        patch("provide.terminal.pty.connector.os.setsid"),
        patch("provide.terminal.pty.connector.fcntl.ioctl"),
        patch("provide.terminal.pty.connector.os.dup2"),
        patch("provide.terminal.pty.connector.termios.tcgetattr", return_value=[0, 0, 0, 0b11111111, 0, 0, []]),
        patch("provide.terminal.pty.connector.termios.tcsetattr"),
        patch("provide.terminal.pty.connector.fcntl.fcntl"),
        patch("provide.terminal.pty.connector.os.execve", side_effect=_fake_execve),
        patch("provide.terminal.pty.connector.os._exit"),
        patch("provide.terminal.pty.connector.os.setgid"),
        patch("provide.terminal.pty.connector.os.initgroups"),
        patch("provide.terminal.pty.connector.os.setuid"),
    ]


def _child_fork_patches_recording(
    captured_env: dict[str, str],
    dup2_calls: list[tuple[int, int]] | None = None,
    setgid_calls: list[int] | None = None,
    initgroups_calls: list[tuple[Any, Any]] | None = None,
    setuid_calls: list[int] | None = None,
    exit_calls: list[int] | None = None,
    ioctl_calls: list[tuple[Any, ...]] | None = None,
    tcsetattr_calls: list[tuple[Any, ...]] | None = None,
) -> list[Any]:
    """Like _child_fork_patches but records specific syscall arguments."""

    def _fake_execve(cmd: str, argv: list[str], env: dict[str, str]) -> None:
        captured_env.update(env)
        raise SystemExit(0)

    def _fake_dup2(fd: int, newfd: int) -> int:
        if dup2_calls is not None:
            dup2_calls.append((fd, newfd))
        return newfd

    def _fake_setgid(gid: int) -> None:
        if setgid_calls is not None:
            setgid_calls.append(gid)

    def _fake_initgroups(name: str | int, gid: int) -> None:
        if initgroups_calls is not None:
            initgroups_calls.append((name, gid))

    def _fake_setuid(uid: int) -> None:
        if setuid_calls is not None:
            setuid_calls.append(uid)

    def _fake_exit(code: int) -> None:
        if exit_calls is not None:
            exit_calls.append(code)

    def _fake_ioctl(fd: int, request: int, *args: Any) -> bytes:
        if ioctl_calls is not None:
            ioctl_calls.append((fd, request, *args))
        return b""

    def _fake_tcsetattr(fd: int, when: int, attrs: list[Any]) -> None:
        if tcsetattr_calls is not None:
            tcsetattr_calls.append((fd, when, attrs))

    return [
        patch("provide.terminal.pty.connector.os.fork", return_value=0),
        patch("provide.terminal.pty.connector.os.close"),
        patch("provide.terminal.pty.connector.os.setsid"),
        patch("provide.terminal.pty.connector.fcntl.ioctl", side_effect=_fake_ioctl),
        patch("provide.terminal.pty.connector.os.dup2", side_effect=_fake_dup2),
        patch("provide.terminal.pty.connector.termios.tcgetattr", return_value=[0, 0, 0, 0b11111111, 0, 0, []]),
        patch("provide.terminal.pty.connector.termios.tcsetattr", side_effect=_fake_tcsetattr),
        patch("provide.terminal.pty.connector.fcntl.fcntl"),
        patch("provide.terminal.pty.connector.os.execve", side_effect=_fake_execve),
        patch("provide.terminal.pty.connector.os._exit", side_effect=_fake_exit),
        patch("provide.terminal.pty.connector.os.setgid", side_effect=_fake_setgid),
        patch("provide.terminal.pty.connector.os.initgroups", side_effect=_fake_initgroups),
        patch("provide.terminal.pty.connector.os.setuid", side_effect=_fake_setuid),
    ]
