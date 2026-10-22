#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The permissions a capture socket is bound with.

Owner-only by default. A deployment that runs the server as a user of its own
and the capture producers (the shim, a session supervisor) as another can ask
for owner-and-group instead, taking the group from a setgid directory, so no
other mode is ever accepted: world bits would let any local user read and type
into a captured terminal.
"""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest

from provide.uterm.pty.capture import (
    OWNER_AND_GROUP_SOCKET_MODE,
    OWNER_ONLY_SOCKET_MODE,
    CaptureSocket,
    check_socket_mode,
)
from provide.uterm.pty.capture_connector import CaptureConnector


def _mode(path: str) -> int:
    return stat.S_IMODE(Path(path).stat().st_mode)


def test_the_two_modes_are_owner_only_and_owner_and_group() -> None:
    assert (OWNER_ONLY_SOCKET_MODE, OWNER_AND_GROUP_SOCKET_MODE) == (0o600, 0o660)


@pytest.mark.parametrize("mode", [0o600, 0o660])
def test_an_allowed_mode_is_answered_as_it_is(mode: int) -> None:
    assert check_socket_mode(mode) == mode


@pytest.mark.parametrize(
    ("mode", "shown"),
    [
        (0o666, "0o666"),
        (0o640, "0o640"),
        (0o700, "0o700"),
        (0o770, "0o770"),
        (0o2660, "0o2660"),
        (0, "0o0"),
        (-0o600, "-0o600"),
    ],
)
def test_any_other_mode_is_refused_with_the_mode_in_octal(mode: int, shown: str) -> None:
    with pytest.raises(ValueError) as caught:
        check_socket_mode(mode)
    assert str(caught.value) == f"capture socket mode must be 0o600 or 0o660, got: {shown}"


@pytest.mark.parametrize("mode", ["0o660", "432", 432.0, True, None])
def test_a_mode_that_is_not_an_integer_is_refused(mode: object) -> None:
    with pytest.raises(ValueError) as caught:
        check_socket_mode(mode)
    assert str(caught.value) == "capture socket mode must be an integer: 0o600 or 0o660"


def test_a_capture_socket_refuses_a_mode_before_anything_is_bound() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        with pytest.raises(ValueError, match=r"got: 0o666$"):
            CaptureSocket(path, mode=0o666)
        assert not Path(path).exists()


async def _bound_under(mode: int | None, monkeypatch: pytest.MonkeyPatch) -> tuple[int, list[int], int]:
    """The socket's mode, the umask its bind ran under, and the umask afterwards."""
    observed: list[int] = []
    real_bind = socket.socket.bind

    def spy_bind(self: socket.socket, *args: Any, **kwargs: Any) -> None:
        current = os.umask(0o022)
        os.umask(current)
        observed.append(current)
        real_bind(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "bind", spy_bind)
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        # The most permissive umask, so any mode the bind did not set shows.
        prev = os.umask(0o000)
        try:
            cap = CaptureSocket(path) if mode is None else CaptureSocket(path, mode=mode)
            await cap.start()
            after = os.umask(0o022)
            os.umask(after)
            bound = _mode(path)
            await cap.stop()
        finally:
            os.umask(prev)
    return bound, observed, after


async def test_a_capture_socket_is_owner_only_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _bound_under(None, monkeypatch) == (0o600, [0o177], 0o000)


async def test_a_capture_socket_asked_for_owner_and_group_is_bound_so(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _bound_under(0o660, monkeypatch) == (0o660, [0o117], 0o000)


async def test_the_mode_is_set_even_where_the_umask_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # A platform that ignores the umask for AF_UNIX sockets leaves the bind's
    # default; the chmod after it is what makes the mode hold there too.
    monkeypatch.setattr(os, "umask", lambda _mask: 0o000)
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        cap = CaptureSocket(path, mode=0o660)
        await cap.start()
        assert _mode(path) == 0o660
        await cap.stop()


def test_a_connector_refuses_a_mode_it_would_not_bind() -> None:
    with pytest.raises(ValueError) as caught:
        CaptureConnector("s1", "name", {"socket_path": "/tmp/x.sock", "socket_mode": 0o644})
    assert str(caught.value) == "capture socket mode must be 0o600 or 0o660, got: 0o644"


def test_a_connector_refuses_a_mode_that_is_not_an_integer() -> None:
    with pytest.raises(ValueError) as caught:
        CaptureConnector("s1", "name", {"socket_path": "/tmp/x.sock", "socket_mode": "0o660"})
    assert str(caught.value) == "capture socket mode must be an integer: 0o600 or 0o660"


@pytest.mark.parametrize(
    ("config", "expected"), [({}, 0o600), ({"socket_mode": 0o600}, 0o600), ({"socket_mode": 0o660}, 0o660)]
)
async def test_a_connector_binds_its_socket_with_the_mode_it_was_given(config: dict[str, Any], expected: int) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "cap.sock")
        connector = CaptureConnector("s1", "name", {"socket_path": path, **config})
        await connector.start()
        try:
            assert _mode(path) == expected
        finally:
            await connector.stop()


def test_a_running_connector_takes_a_config_that_carries_its_mode() -> None:
    # The server hands the mode to every config it gives a capture connector,
    # the one a PATCH offers to the running connector included.
    connector = CaptureConnector("s1", "name", {"socket_path": "/tmp/x.sock", "socket_mode": 0o660})
    assert connector.reconfigure({"socket_path": "/tmp/x.sock", "socket_mode": 0o660, "cols": 100}) is True


def test_a_different_mode_is_left_for_a_restart() -> None:
    connector = CaptureConnector("s1", "name", {"socket_path": "/tmp/x.sock"})
    assert connector.reconfigure({"socket_path": "/tmp/x.sock", "socket_mode": 0o660}) is False


def test_a_running_connector_refuses_a_mode_it_would_not_bind() -> None:
    connector = CaptureConnector("s1", "name", {"socket_path": "/tmp/x.sock"})
    with pytest.raises(ValueError, match=r"got: 0o666$"):
        connector.reconfigure({"socket_path": "/tmp/x.sock", "socket_mode": 0o666})


@pytest.mark.parametrize("mode", [0o600, 0o660])
async def test_concurrent_starts_leave_the_process_umask_as_it_was(mode: int) -> None:
    # The umask is process-wide. Held across an await, a second start begun in
    # that tick reads the first one's bind umask as the one to restore, and
    # leaves it behind for every file the server creates afterwards: at 0o660
    # that is 0o117, wider than a service's 0o077 in the group bits.
    with tempfile.TemporaryDirectory() as td:
        sockets = [CaptureSocket(str(Path(td) / f"c{i}.sock"), mode=mode) for i in range(8)]
        prev = os.umask(0o077)
        try:
            await asyncio.gather(*(cap.start() for cap in sockets))
            after = os.umask(0o077)
            modes = {_mode(cap._path) for cap in sockets}
            await asyncio.gather(*(cap.stop() for cap in sockets))
        finally:
            os.umask(prev)
    assert (after, modes) == (0o077, {mode})
