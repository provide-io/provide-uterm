#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fork-free mutation coverage for PTYConnector's parent-side surface.

PTYConnector is exercised mostly by real-fork integration tests (test_connector_io
etc.) which spawn a real child. Those leak into mutmut's fork-loop os.wait() reaper
on low-core runners, so the pty conftest skips them during a mutation run. This
suite reproduces their *mutant-killing* coverage without forking: it drives the
parent-side methods (__init__ config, _snapshot, handle_control/set_mode/clear,
poll_messages/handle_input/_read_master over a real os.pipe(), stop's reap
escalation, and start's setup via a mocked fork-parent) so the connector stays at
killed==100 with no real subprocess.
"""

from __future__ import annotations

import fcntl
import os
import termios
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.pty.connector import PTYConnector


def _conn(**config: Any) -> PTYConnector:
    cfg = {"command": "/bin/echo", "args": []}
    cfg.update(config)
    return PTYConnector(session_id="s1", display_name="d", config=cfg)


def _connected(conn: PTYConnector, master_fd: int) -> None:
    """Put *conn* into the connected state with *master_fd* but no real child."""
    conn._master_fd = master_fd
    conn._connected = True


# == __init__ config reads ===================================================


def test_init_reads_every_config_key() -> None:
    """Each attribute is read from its exact config key with the right coercion."""
    conn = _conn(
        username="alice",
        password="pw",  # pragma: allowlist secret
        run_as="bob",
        run_as_uid=5,
        run_as_gid=6,
        env={"K": "V"},
        inject=True,
        cols=120,
        rows=40,
        input_mode="hijack",
    )
    assert conn._username == "alice"
    assert conn._password == "pw"  # pragma: allowlist secret
    assert conn._run_as == "bob"
    assert conn._run_as_uid == 5
    assert conn._run_as_gid == 6
    assert conn._extra_env == {"K": "V"}
    assert conn._inject is True
    assert conn._cols == 120
    assert conn._rows == 40
    assert conn._input_mode == "hijack"


def test_init_defaults() -> None:
    conn = _conn()
    assert conn._inject is False
    assert conn._cols == 80
    assert conn._rows == 24
    assert conn._input_mode == "open"
    assert conn._extra_env == {}
    assert conn._connected is False
    assert conn._master_fd is None


# == _snapshot ===============================================================


async def test_snapshot_full_structure() -> None:
    conn = _conn(cols=100, rows=30)
    conn._buffer = "hello"
    snap = await conn.get_snapshot()
    import hashlib

    assert snap == {
        "type": "snapshot",
        "screen": "hello",
        "cursor": {"row": 0, "col": 0},
        "cols": 100,
        "rows": 30,
        "screen_hash": hashlib.md5(b"hello").hexdigest(),  # noqa: S324
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": False,
        "ts": snap["ts"],
    }
    assert isinstance(snap["ts"], float)
    assert snap["ts"] > 0


# == handle_control / set_mode / _hello / clear ==============================


async def test_handle_control_pause_resume_step() -> None:
    conn = _conn()
    assert (await conn.handle_control("pause"))[0]["type"] == "snapshot"
    assert conn._paused is True
    await conn.handle_control("resume")
    assert conn._paused is False
    conn._paused = True
    await conn.handle_control("step")
    assert conn._paused is False
    # An unknown action leaves _paused unchanged.
    conn._paused = True
    await conn.handle_control("bogus")
    assert conn._paused is True


async def test_set_mode_valid_returns_hello_and_snapshot() -> None:
    conn = _conn()
    out = await conn.set_mode("hijack")
    assert conn._input_mode == "hijack"
    assert out[0] == {"type": "worker_hello", "input_mode": "hijack"}
    assert out[1]["type"] == "snapshot"


async def test_set_mode_invalid_raises() -> None:
    conn = _conn()
    with pytest.raises(ValueError, match="invalid mode"):
        await conn.set_mode("nope")


async def test_clear_resets_buffer() -> None:
    conn = _conn()
    conn._buffer = "stuff"
    out = await conn.clear()
    assert conn._buffer == ""
    assert out[0]["type"] == "snapshot"


# == poll_messages / handle_input / _read_master (real pipe fd) ==============


async def test_poll_messages_reads_and_snapshots() -> None:
    r, w = os.pipe()
    try:
        conn = _conn()
        _connected(conn, r)
        os.write(w, b"abc")
        out = await conn.poll_messages()
        assert out and out[0]["screen"] == "abc"
        assert conn._buffer == "abc"
    finally:
        os.close(w)
        if conn._master_fd is not None:
            os.close(conn._master_fd)


async def test_poll_messages_empty_when_not_connected_or_paused() -> None:
    conn = _conn()
    assert await conn.poll_messages() == []  # not connected
    r, w = os.pipe()
    try:
        _connected(conn, r)
        conn._paused = True
        assert await conn.poll_messages() == []  # paused
    finally:
        os.close(w)
        os.close(r)


async def test_poll_messages_truncates_buffer() -> None:
    r, w = os.pipe()
    try:
        conn = _conn()
        _connected(conn, r)
        conn._buffer = "x" * 32768
        os.write(w, b"yy")
        await conn.poll_messages()
        assert len(conn._buffer) == 32768
        assert conn._buffer.endswith("yy")
    finally:
        os.close(w)
        os.close(r)


async def test_handle_input_writes_to_master() -> None:
    r, w = os.pipe()
    try:
        os.set_blocking(r, False)  # so a no-write mutant returns fast, not a blocking hang
        conn = _conn()
        _connected(conn, w)
        await conn.handle_input("hi")
        try:
            got = os.read(r, 16)
        except BlockingIOError:
            got = b""  # a guard mutant skipped the write → no data
        assert got == b"hi"
    finally:
        os.close(r)
        os.close(w)


async def test_poll_messages_disconnected_with_data_returns_empty() -> None:
    """Disconnected must short-circuit even when the fd has data (pins ``or`` vs ``and``)."""
    r, w = os.pipe()
    try:
        conn = _conn()
        conn._connected = False  # is_connected() False ...
        conn._master_fd = r  # ... but fd is set, with data waiting
        conn._paused = False
        os.write(w, b"data")
        assert await conn.poll_messages() == []  # `not connected or paused` → []
    finally:
        os.close(w)
        os.close(r)


async def test_handle_input_noop_when_not_connected() -> None:
    conn = _conn()  # not connected, _master_fd None
    out = await conn.handle_input("hi")
    assert out[0]["type"] == "snapshot"  # still returns a snapshot, no write


async def test_handle_input_no_write_when_connected_but_paused() -> None:
    """Paused (connected, fd set) → the guard is False → no write. Pins
    ``not self._paused`` so a ``self._paused`` mutant (which would write while
    paused) is killed."""
    r, w = os.pipe()
    try:
        os.set_blocking(r, False)
        conn = _conn()
        _connected(conn, w)
        conn._paused = True
        await conn.handle_input("nope")
        try:
            got = os.read(r, 16)
        except BlockingIOError:
            got = b""
        assert got == b""
    finally:
        os.close(r)
        os.close(w)


async def test_handle_input_no_write_when_disconnected_with_fd() -> None:
    """Disconnected but with an fd set → the guard is False → no write. Pins the
    ``is_connected() and ...`` conjunction so an ``and``->``or`` mutant (which
    would write while disconnected) is killed."""
    r, w = os.pipe()
    try:
        os.set_blocking(r, False)
        conn = _conn()
        conn._master_fd = w  # fd present ...
        conn._connected = False  # ... but not connected
        await conn.handle_input("nope")
        try:
            got = os.read(r, 16)
        except BlockingIOError:
            got = b""
        assert got == b""
    finally:
        os.close(r)
        os.close(w)


async def test_handle_input_oserror_marks_disconnected() -> None:
    """A failed os.write (closed fd → OSError) flips _connected to False. Pins
    the ``self._connected = False`` assignment against a ``= True`` mutant."""
    r, w = os.pipe()
    os.close(w)  # writing to a closed fd raises OSError (EBADF)
    conn = _conn()
    _connected(conn, w)
    assert conn._connected is True
    await conn.handle_input("boom")
    assert conn._connected is False
    os.close(r)


async def test_poll_messages_empty_when_connected_but_read_yields_nothing() -> None:
    """Connected + not paused but the read returns no data → []. Exercises the
    ``if data:`` False branch and the guard's connected / not-paused path."""
    r, w = os.pipe()
    try:
        os.set_blocking(r, False)
        conn = _conn()
        _connected(conn, r)  # connected, reading the empty pipe
        assert await conn.poll_messages() == []
    finally:
        os.close(r)
        os.close(w)


def test_read_master_none_fd_returns_empty() -> None:
    conn = _conn()
    assert conn._read_master() == b""


def test_read_master_oserror_marks_disconnected() -> None:
    conn = _conn()
    conn._master_fd = 9999  # invalid fd → os.read raises OSError (not BlockingIOError)
    conn._connected = True
    assert conn._read_master() == b""
    assert conn._connected is False


def test_read_master_blocking_returns_empty_without_disconnect() -> None:
    r, _w = os.pipe()
    try:
        os.set_blocking(r, False)
        conn = _conn()
        conn._master_fd = r
        conn._connected = True
        assert conn._read_master() == b""  # no data → BlockingIOError → b""
        assert conn._connected is True  # NOT disconnected
    finally:
        os.close(r)
        os.close(_w)


# == stop: reap escalation ===================================================


async def test_stop_escalates_to_sigkill_when_child_alive() -> None:
    import signal

    conn = _conn()
    conn._child_pid = 4242
    conn._connected = True
    waitpids: list[tuple[int, int]] = []

    def _fake_waitpid(pid: int, flags: int) -> tuple[int, int]:
        waitpids.append((pid, flags))
        return (0, 0) if flags == os.WNOHANG else (pid, 0)  # WNOHANG → still alive

    kills: list[tuple[int, int]] = []
    with (
        patch("provide.uterm.pty.connector.os.waitpid", side_effect=_fake_waitpid),
        patch("provide.uterm.pty.connector.os.kill", side_effect=lambda p, s: kills.append((p, s))),
    ):
        await conn.stop()

    assert (4242, signal.SIGKILL) in kills  # escalated
    assert (4242, 0) in waitpids  # final blocking wait
    assert conn._child_pid is None
    assert conn._connected is False


async def test_stop_no_escalation_when_child_already_reaped() -> None:
    import signal

    conn = _conn()
    conn._child_pid = 4242
    conn._connected = True
    kills: list[Any] = []
    with (
        patch("provide.uterm.pty.connector.os.waitpid", return_value=(4242, 0)),  # reaped on WNOHANG
        patch("provide.uterm.pty.connector.os.kill", side_effect=lambda p, s: kills.append((p, s))),
    ):
        await conn.stop()
    # The initial SIGHUP is always sent; reaped-on-WNOHANG means NO SIGKILL escalation.
    assert kills == [(4242, signal.SIGHUP)]
    assert conn._child_pid is None


async def test_stop_cleans_capture_tmpdir_and_pam_to_none(tmp_path: Path) -> None:
    """stop() tears down the capture socket, removes the tmpdir, closes PAM, and
    resets each attribute to None (not '') so a second stop() doesn't re-enter.

    Pins the ``= None`` resets and the ``if self._capture_tmpdir is not None`` guard.
    """
    conn = _conn()
    conn._connected = True
    conn._child_pid = None
    conn._master_fd = None
    sock = AsyncMock()
    conn._capture_socket = sock
    capdir = tmp_path / "cap"
    capdir.mkdir()
    conn._capture_tmpdir = str(capdir)
    pam = MagicMock()
    conn._pam = pam

    await conn.stop()

    sock.stop.assert_awaited_once()
    assert conn._capture_socket is None
    pam.close_session.assert_called_once()
    assert conn._pam is None
    assert conn._capture_tmpdir is None  # guard taken + reset
    assert not capdir.exists()  # rmtree actually ran


async def test_handle_input_requires_connected_not_just_master_fd() -> None:
    """A set master_fd alone must NOT enable writes — is_connected() must be true too.

    Pins the ``and`` (vs ``or``): not connected but master_fd set + not paused.
    """
    conn = _conn()
    conn._connected = False  # is_connected() is False ...
    conn._master_fd = 7  # ... even though master_fd is set
    conn._paused = False
    with patch("provide.uterm.pty.connector.os.write") as write_mock:
        await conn.handle_input("x")
    write_mock.assert_not_called()  # `and` short-circuits; `or` would write


# == start: setup via a mocked fork-parent ===================================


def _parent_patches() -> list[Any]:
    """Patch the spawn primitives so start() takes the parent branch, no real fork."""
    return [
        patch("provide.uterm.pty.connector.pty.openpty", return_value=(31, 32)),
        patch("provide.uterm.pty.connector.fcntl.fcntl", return_value=0),
        patch("provide.uterm.pty.connector.termios.tcgetattr", return_value=[0, 0, 0, 0xFF, 0, 0, []]),
        patch("provide.uterm.pty.connector.termios.tcsetattr"),
        patch("provide.uterm.pty.connector.os.fork", return_value=4242),
        patch("provide.uterm.pty.connector.os.close"),
    ]


async def test_start_parent_branch_sets_state_and_closes_slave() -> None:
    conn = _conn()
    patches = _parent_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as close_mock:
        await conn.start()
    assert conn._master_fd == 31
    assert conn._child_pid == 4242
    assert conn._connected is True
    close_mock.assert_any_call(32)  # parent closes the slave fd (not None)


async def test_start_sets_master_nonblocking_and_clears_echo() -> None:
    conn = _conn()
    with (
        patch("provide.uterm.pty.connector.pty.openpty", return_value=(31, 32)),
        patch("provide.uterm.pty.connector.fcntl.fcntl", return_value=0) as fcntl_mock,
        patch("provide.uterm.pty.connector.termios.tcgetattr", return_value=[0, 0, 0, 0xFF, 0, 0, []]),
        patch("provide.uterm.pty.connector.termios.tcsetattr") as tcset_mock,
        patch("provide.uterm.pty.connector.os.fork", return_value=4242),
        patch("provide.uterm.pty.connector.os.close"),
    ):
        await conn.start()
    # F_SETFL is called with the existing flags (0) OR O_NONBLOCK (kills the dropped-arg mutant).
    fcntl_mock.assert_any_call(31, fcntl.F_SETFL, 0 | os.O_NONBLOCK)
    # ECHO is cleared via &= (0xFF & ~ECHO), not overwritten with ~ECHO.
    attrs = tcset_mock.call_args[0][2]
    assert attrs[3] == (0xFF & ~termios.ECHO)


async def test_start_pam_authenticates_with_username_and_password() -> None:
    conn = _conn(username="alice", password="pw")  # pragma: allowlist secret
    fake_pam = MagicMock()
    with (
        patch("provide.uterm.pty.connector.os.geteuid", return_value=0),
        patch("provide.uterm.pty.connector.PamSession", return_value=fake_pam),
        patch("provide.uterm.pty.connector.pty.openpty", return_value=(31, 32)),
        patch("provide.uterm.pty.connector.fcntl.fcntl", return_value=0),
        patch("provide.uterm.pty.connector.termios.tcgetattr", return_value=[0, 0, 0, 0xFF, 0, 0, []]),
        patch("provide.uterm.pty.connector.termios.tcsetattr"),
        patch("provide.uterm.pty.connector.os.fork", return_value=4242),
        patch("provide.uterm.pty.connector.os.close"),
        patch.object(conn._uid_map, "resolve", return_value=None),
    ):
        await conn.start()
    fake_pam.authenticate.assert_called_once_with("alice", "pw")  # pragma: allowlist secret
