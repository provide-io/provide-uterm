#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.pty.pam_listener import PamEvent, PamNotifyListener, _parse_event


@pytest.fixture
def tmp_path():
    """Short-path override of pytest's built-in ``tmp_path``.

    These tests bind ``AF_UNIX`` sockets, whose ``sun_path`` is capped at ~104
    bytes. macOS's long ``$TMPDIR`` combined with pytest's long per-test
    directory names (e.g. ``pytest-of-<user>/pytest-N/<test_name>0/``) pushes
    ``<tmp_path>/notify.sock`` over that limit, so ``listener.start()`` fails
    with ``OSError: AF_UNIX path too long``. ``tempfile.TemporaryDirectory()``
    keeps the base short (``$TMPDIR/tmpXXXXXXXX``) so the bind succeeds — the
    same pattern the other socket tests in this file already use.
    """
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# ── _parse_event ─────────────────────────────────────────────────────────────


def test_parse_open_event() -> None:
    line = b'{"event":"open","username":"alice","tty":"/dev/pts/3","pid":12345}\n'
    ev = _parse_event(line)
    assert ev is not None
    assert ev.event == "open"
    assert ev.username == "alice"
    assert ev.tty == "/dev/pts/3"
    assert ev.pid == 12345


def test_parse_close_event() -> None:
    line = b'{"event":"close","username":"bob","tty":"/dev/pts/7","pid":99}\n'
    ev = _parse_event(line)
    assert ev is not None
    assert ev.event == "close"
    assert ev.pid == 99


def test_parse_bad_json_returns_none() -> None:
    assert _parse_event(b"not-json\n") is None


@pytest.mark.parametrize("value", [[], "text", 42, True, None])
def test_parse_non_object_json_returns_none(value: object) -> None:
    assert _parse_event(json.dumps(value).encode()) is None


def test_parse_unknown_event_returns_none() -> None:
    assert _parse_event(b'{"event":"reboot","username":"root","tty":"","pid":1}\n') is None


def test_parse_missing_username_returns_none() -> None:
    assert _parse_event(b'{"event":"open","username":"","tty":"/dev/pts/1","pid":5}\n') is None


def test_parse_missing_pid_defaults_zero() -> None:
    line = b'{"event":"open","username":"alice","tty":"/dev/pts/0"}\n'
    ev = _parse_event(line)
    assert ev is not None
    assert ev.pid == 0


def test_parse_event_timestamp_set() -> None:
    import time

    t0 = time.time()
    ev = _parse_event(b'{"event":"open","username":"u","tty":"","pid":1}\n')
    assert ev is not None
    assert ev.timestamp >= t0


# ── PamNotifyListener ────────────────────────────────────────────────────────


def test_invalid_socket_path_null_byte() -> None:
    with pytest.raises(ValueError, match="null byte"):
        PamNotifyListener("/run/\x00bad.sock")


def test_invalid_socket_path_relative() -> None:
    with pytest.raises(ValueError, match="absolute"):
        PamNotifyListener("relative/path.sock")


async def test_start_stop_creates_and_removes_socket() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))
        assert Path(path).exists()
        await listener.stop()
        assert not Path(path).exists()


async def test_receives_open_event() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        await _send_line(
            path,
            {"event": "open", "username": "alice", "tty": "/dev/pts/3", "pid": 111},
        )
        await asyncio.sleep(0.05)

        await listener.stop()
        assert len(events) == 1
        assert events[0].event == "open"
        assert events[0].username == "alice"


async def test_receives_close_event() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        await _send_line(path, {"event": "close", "username": "bob", "tty": "/dev/pts/5", "pid": 222})
        await asyncio.sleep(0.05)

        await listener.stop()
        assert events[0].event == "close"


async def test_multiple_events_on_one_connection() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"event":"open","username":"u1","tty":"/dev/pts/1","pid":1}\n')
        writer.write(b'{"event":"close","username":"u1","tty":"/dev/pts/1","pid":1}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert len(events) == 2
        assert events[0].event == "open"
        assert events[1].event == "close"


async def test_bad_json_line_skipped_gracefully() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b"not-json\n")
        writer.write(b'{"event":"open","username":"alice","tty":"","pid":9}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert len(events) == 1  # bad line skipped, good one still received


async def test_non_object_json_line_does_not_stop_listener() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b"[]\n")
        writer.write(b'{"event":"open","username":"alice","tty":"","pid":9}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert [event.username for event in events] == ["alice"]


async def test_handler_exception_does_not_kill_listener() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        call_count = [0]

        async def bad_handler(e: PamEvent) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("handler exploded")

        await listener.start(bad_handler)

        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"event":"open","username":"a","tty":"","pid":1}\n')
        writer.write(b'{"event":"open","username":"b","tty":"","pid":2}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert call_count[0] == 2  # second event still delivered


async def test_stop_without_start_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        listener = PamNotifyListener(str(Path(td) / "notify.sock"))
        await listener.stop()  # must not raise


async def test_double_start_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        await listener.start(lambda e: _collect([], e))
        with pytest.raises(RuntimeError, match="already started"):
            await listener.start(lambda e: _collect([], e))
        await listener.stop()


async def test_multiple_concurrent_connections() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        async def send(username: str, pid: int) -> None:
            await _send_line(path, {"event": "open", "username": username, "tty": "", "pid": pid})

        await asyncio.gather(send("u1", 1), send("u2", 2), send("u3", 3))
        await asyncio.sleep(0.1)

        await listener.stop()
        assert len(events) == 3


def test_socket_path_property() -> None:
    """socket_path property returns the configured path."""
    listener = PamNotifyListener("/tmp/test.sock")
    assert listener.socket_path == "/tmp/test.sock"


async def test_handle_connection_readline_timeout_drops_connection() -> None:
    """readline TimeoutError causes the connection to be dropped gracefully."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        reader = AsyncMock()
        reader.readline.side_effect = TimeoutError("mock timeout")
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await listener._handle_connection(reader, writer)
        writer.close.assert_called_once()
        await listener.stop()


async def test_handle_connection_exception_in_readline_drops_connection() -> None:
    """Non-timeout exception during readline breaks the loop."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        reader = AsyncMock()
        reader.readline.side_effect = ConnectionResetError("reset")
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await listener._handle_connection(reader, writer)
        writer.close.assert_called_once()
        await listener.stop()


async def test_handle_connection_oversized_line_skipped() -> None:
    """Lines longer than _MAX_LINE are skipped with a warning."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))

        big_line = b"x" * 5000 + b"\n"
        good_line = b'{"event":"open","username":"alice","tty":"","pid":1}\n'

        _reader, writer = await asyncio.open_unix_connection(path)
        writer.write(big_line + good_line)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert len(events) == 1  # big line skipped, good line received


async def test_handle_connection_null_handler_skips_dispatch() -> None:
    """Events are not dispatched when handler is None."""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "notify.sock")
        listener = PamNotifyListener(path)
        events: list[PamEvent] = []
        await listener.start(lambda e: _collect(events, e))
        listener._handler = None

        _reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"event":"open","username":"alice","tty":"","pid":1}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()
        assert events == []


def test_parse_event_non_numeric_pid_defaults_to_zero() -> None:
    """_parse_event() handles non-numeric pid gracefully."""
    line = b'{"event":"open","username":"alice","tty":"/dev/pts/0","pid":"bad"}\n'
    ev = _parse_event(line)
    assert ev is not None
    assert ev.pid == 0


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture
def short_sock_dir():
    """A SHORT temp dir for binding AF_UNIX sockets.

    macOS limits ``sun_path`` to ~104 bytes and the worktree path can be long
    enough that pytest's ``tmp_path`` (rooted under the worktree) overflows it
    with ``OSError: AF_UNIX path too long``. Bind sockets under a short
    ``/tmp``-rooted dir instead (mirrors the established
    ``tempfile.TemporaryDirectory()`` pattern used elsewhere in this file).
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        yield Path(td)


async def _collect(events: list[PamEvent], ev: PamEvent) -> None:
    events.append(ev)


async def _send_line(path: str, data: dict) -> None:
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps(data) + "\n").encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def test_notify_socket_is_owner_only(short_sock_dir) -> None:
    import stat

    sock = short_sock_dir / "notify.sock"
    listener = PamNotifyListener(str(sock))
    await listener.start(AsyncMock())
    try:
        mode = stat.S_IMODE(sock.stat().st_mode)
        assert mode == 0o600
    finally:
        await listener.stop()


async def test_notify_socket_umask_constrains_bind_and_is_restored(short_sock_dir, monkeypatch) -> None:
    """The socket is created 0o600 atomically (umask before bind), not via a
    post-bind chmod that leaves a permission window.

    Regression for the bind-then-chmod race: any local user could connect in
    the gap between bind (default umask perms, e.g. srwxr-xr-x) and the chmod —
    and forge login events that drive root-side session creation. start() must
    set a restrictive umask (0o177) *before* the synchronous bind and restore
    the caller's umask afterward, so the socket never exists world-accessible.
    """
    import os
    import socket as _socket
    import stat

    observed_umask: list[int] = []
    real_bind = _socket.socket.bind

    def _spy_bind(self, *args, **kwargs):
        # Capture the umask in effect at the moment of the real socket bind by
        # reading and immediately restoring it (os.umask has no read-only
        # variant). This is now where the 0o600 atomicity is enforced.
        cur = os.umask(0o022)
        os.umask(cur)
        observed_umask.append(cur)
        return real_bind(self, *args, **kwargs)

    monkeypatch.setattr(_socket.socket, "bind", _spy_bind)

    sock = short_sock_dir / "notify.sock"
    prev = os.umask(0o000)  # most permissive — surfaces any window
    try:
        listener = PamNotifyListener(str(sock))
        await listener.start(AsyncMock())
        try:
            # The bind itself must have run under a 0o177 umask (atomic 0o600).
            assert observed_umask == [0o177]
            mode = stat.S_IMODE(sock.stat().st_mode)
            assert mode == 0o600
            # And the caller's umask must be restored once start() returns.
            current = os.umask(0o022)
            assert current == 0o000
        finally:
            await listener.stop()
    finally:
        os.umask(prev)


async def test_start_does_not_hold_restrictive_umask_across_await(short_sock_dir, monkeypatch) -> None:
    """The umask window must not span any ``await`` in ``start()``.

    Regression for the async-umask leak: the old code held the process-global
    0o177 umask across ``await asyncio.start_unix_server(...)``, so any OTHER
    coroutine creating a file during that await inherited the restrictive
    umask. The fix binds the socket synchronously (no await between umask set
    and restore), so by the time ``start_unix_server`` is awaited the caller's
    umask has already been restored.
    """
    import asyncio as _asyncio
    import os

    observed_umask_at_await: list[int] = []
    real_start = _asyncio.start_unix_server

    async def _spy_start(*args, **kwargs):
        # Capture the umask in effect when start_unix_server is awaited — the
        # exact point a concurrent coroutine could leak through.
        cur = os.umask(0o022)
        os.umask(cur)
        observed_umask_at_await.append(cur)
        return await real_start(*args, **kwargs)

    monkeypatch.setattr(_asyncio, "start_unix_server", _spy_start)

    sock = short_sock_dir / "notify.sock"
    prev = os.umask(0o000)
    try:
        listener = PamNotifyListener(str(sock))
        await listener.start(AsyncMock())
        try:
            # The await must see the caller's umask (0o000), NOT the restrictive
            # 0o177 bind umask. If it saw 0o177 the window spans the await.
            assert observed_umask_at_await == [0o000]
        finally:
            await listener.stop()
    finally:
        os.umask(prev)


async def test_start_removes_stale_socket_before_bind(short_sock_dir) -> None:
    """A leftover socket file at the path is removed so the bind succeeds.

    Reproduces a prior crash: with sock-based binding the bind fails with
    EADDRINUSE if a stale socket file is present (the old path-based
    start_unix_server cleared it). start() must replicate that cleanup.
    """
    import socket as _socket
    import stat

    sock_path = short_sock_dir / "notify.sock"
    # Create a genuine, dead socket file at the path (bind then close).
    stale = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    stale.bind(str(sock_path))
    stale.close()
    assert sock_path.exists()

    listener = PamNotifyListener(str(sock_path))
    await listener.start(AsyncMock())  # must not raise EADDRINUSE
    try:
        # Fresh socket bound at the path with the enforced owner-only mode.
        assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
    finally:
        await listener.stop()
    assert not sock_path.exists()


async def test_start_does_not_clobber_non_socket_file(short_sock_dir) -> None:
    """A non-socket file at the path is NOT unlinked (and the bind then fails).

    Guards the ``is_socket()`` check: only stale *sockets* are removed, never
    an unrelated regular file that happens to share the path.
    """
    reg_path = short_sock_dir / "notify.sock"
    reg_path.write_text("not a socket")

    listener = PamNotifyListener(str(reg_path))
    with pytest.raises(OSError):
        await listener.start(AsyncMock())
    # The regular file must survive untouched.
    assert reg_path.read_text() == "not a socket"


async def test_start_unlink_stale_socket_is_socket_oserror_swallowed(short_sock_dir, monkeypatch) -> None:
    """_unlink_stale_socket returns quietly if is_socket() raises OSError.

    On a path we cannot stat, we must neither crash nor attempt to unlink — we
    fall through to the bind (which then governs success/failure).
    """
    from pathlib import Path as _Path

    sock_path = short_sock_dir / "notify.sock"

    real_is_socket = _Path.is_socket

    def _raising_is_socket(self):
        if str(self) == str(sock_path):
            raise OSError("stat denied")
        return real_is_socket(self)

    monkeypatch.setattr(_Path, "is_socket", _raising_is_socket)

    listener = PamNotifyListener(str(sock_path))
    # No file exists yet, so the bind still succeeds despite the stat raising.
    await listener.start(AsyncMock())
    try:
        assert sock_path.exists()
    finally:
        await listener.stop()


async def test_start_closes_socket_on_bind_failure(short_sock_dir, monkeypatch) -> None:
    """If start_unix_server raises, the pre-bound socket is closed (no fd leak).

    Exercises the ``except BaseException: sock.close(); raise`` cleanup branch.
    """
    import asyncio as _asyncio
    import socket as _socket

    from provide.uterm.pty import pam_listener as _pl

    created: list[_socket.socket] = []
    real_factory = _socket.socket

    def _tracking_factory(*args, **kwargs):
        sock = real_factory(*args, **kwargs)
        created.append(sock)
        return sock

    # Patch the name the module under test actually uses (module-level `socket`).
    monkeypatch.setattr(_pl.socket, "socket", _tracking_factory)

    async def _boom(*args, **kwargs):
        raise RuntimeError("start_unix_server failed")

    monkeypatch.setattr(_asyncio, "start_unix_server", _boom)

    sock_path = short_sock_dir / "notify.sock"
    listener = PamNotifyListener(str(sock_path))
    with pytest.raises(RuntimeError, match="start_unix_server failed"):
        await listener.start(AsyncMock())
    # The socket we created must have been closed during cleanup: a closed
    # socket reports fileno() == -1.
    assert len(created) == 1
    assert created[0].fileno() == -1, "pre-bound socket must be closed on start failure"
    assert listener._server is None


# ── peer-uid auth (Fix 2) ─────────────────────────────────────────────────────


async def test_peer_uid_allowed_when_in_allowlist(short_sock_dir) -> None:
    """require_peer_uids=[0] + peer euid 0 → connection allowed, handler called."""
    path = str(short_sock_dir / "notify.sock")
    events: list[PamEvent] = []
    listener = PamNotifyListener(path, require_peer_uids=[0])
    # Monkeypatch _peer_euid to return 0 (root)
    listener._peer_euid = lambda w: 0  # type: ignore[method-assign]

    await listener.start(lambda e: _collect(events, e))

    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(b'{"event":"open","username":"alice","tty":"","pid":1}\n')
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)

    await listener.stop()
    assert len(events) == 1


async def test_peer_uid_rejected_when_not_in_allowlist(short_sock_dir) -> None:
    """require_peer_uids=[0] + peer euid 1000 → connection rejected, handler NOT called."""
    path = str(short_sock_dir / "notify.sock")
    events: list[PamEvent] = []
    listener = PamNotifyListener(path, require_peer_uids=[0])
    # Monkeypatch _peer_euid to return 1000 (unprivileged user)
    listener._peer_euid = lambda w: 1000  # type: ignore[method-assign]

    await listener.start(lambda e: _collect(events, e))

    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(b'{"event":"open","username":"alice","tty":"","pid":1}\n')
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)

    await listener.stop()
    # Rejected: handler must not have been called
    assert len(events) == 0


async def test_peer_uid_no_enforcement_when_allowlist_none(short_sock_dir) -> None:
    """require_peer_uids=None (default) → no enforcement, even euid 1000 allowed."""
    path = str(short_sock_dir / "notify.sock")
    events: list[PamEvent] = []
    listener = PamNotifyListener(path, require_peer_uids=None)
    listener._peer_euid = lambda w: 1000  # type: ignore[method-assign]

    await listener.start(lambda e: _collect(events, e))

    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(b'{"event":"open","username":"alice","tty":"","pid":1}\n')
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)

    await listener.stop()
    assert len(events) == 1


async def test_peer_uid_none_platform_allowed_with_warning(short_sock_dir) -> None:
    """_peer_euid returning None (unsupported platform) → allowed (warn, not reject)."""
    path = str(short_sock_dir / "notify.sock")
    events: list[PamEvent] = []
    listener = PamNotifyListener(path, require_peer_uids=[0])
    # Simulate macOS: SO_PEERCRED unavailable
    listener._peer_euid = lambda w: None  # type: ignore[method-assign]

    await listener.start(lambda e: _collect(events, e))

    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(b'{"event":"open","username":"alice","tty":"","pid":1}\n')
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)

    await listener.stop()
    # Platform without SO_PEERCRED → allow (chmod 0o600 is the baseline)
    assert len(events) == 1


def test_peer_euid_returns_none_without_so_peercred(tmp_path) -> None:
    """_peer_euid returns None when SO_PEERCRED is unavailable on the platform."""
    import socket as _socket

    listener = PamNotifyListener(str(tmp_path / "s.sock"))
    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=None)

    # Simulate platform without SO_PEERCRED
    orig = getattr(_socket, "SO_PEERCRED", None)
    try:
        if hasattr(_socket, "SO_PEERCRED"):
            del _socket.SO_PEERCRED  # type: ignore[attr-defined]
        result = listener._peer_euid(writer)
        assert result is None
    finally:
        if orig is not None:
            _socket.SO_PEERCRED = orig  # type: ignore[attr-defined]


def test_peer_euid_returns_none_when_socket_is_none(tmp_path) -> None:
    """_peer_euid returns None when get_extra_info('socket') returns None."""
    import socket as _socket

    listener = PamNotifyListener(str(tmp_path / "s.sock"))
    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=None)

    # Ensure SO_PEERCRED is present so we exercise line 128-130 (sock is None branch)
    fake_so_peercred = getattr(_socket, "SO_PEERCRED", 17)
    orig_peercred = getattr(_socket, "SO_PEERCRED", None)
    try:
        _socket.SO_PEERCRED = fake_so_peercred  # type: ignore[attr-defined]
        result = listener._peer_euid(writer)
    finally:
        if orig_peercred is None and hasattr(_socket, "SO_PEERCRED"):
            del _socket.SO_PEERCRED  # type: ignore[attr-defined]
        elif orig_peercred is not None:
            _socket.SO_PEERCRED = orig_peercred  # type: ignore[attr-defined]

    assert result is None


def test_peer_euid_handles_getsockopt_oserror(tmp_path) -> None:
    """_peer_euid returns None when getsockopt raises OSError."""
    import socket as _socket

    so_peercred = getattr(_socket, "SO_PEERCRED", None)
    if so_peercred is None:
        pytest.skip("SO_PEERCRED not available on this platform")

    listener = PamNotifyListener(str(tmp_path / "s.sock"))
    mock_sock = MagicMock()
    mock_sock.getsockopt = MagicMock(side_effect=OSError("permission denied"))
    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=mock_sock)

    result = listener._peer_euid(writer)
    assert result is None


def test_peer_euid_returns_uid_when_so_peercred_available(tmp_path) -> None:
    """_peer_euid returns the uid from getsockopt when SO_PEERCRED is available."""
    import socket as _socket
    import struct

    listener = PamNotifyListener(str(tmp_path / "s.sock"))
    # Simulate getsockopt returning pid=100, uid=42, gid=1000 packed as 3i
    fake_raw = struct.pack("3i", 100, 42, 1000)
    mock_sock = MagicMock()
    mock_sock.getsockopt = MagicMock(return_value=fake_raw)
    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=mock_sock)

    # Install a fake SO_PEERCRED if not available so the code path executes
    fake_so_peercred = getattr(_socket, "SO_PEERCRED", 17)
    orig_peercred = getattr(_socket, "SO_PEERCRED", None)
    try:
        _socket.SO_PEERCRED = fake_so_peercred  # type: ignore[attr-defined]
        result = listener._peer_euid(writer)
    finally:
        if orig_peercred is None and hasattr(_socket, "SO_PEERCRED"):
            del _socket.SO_PEERCRED  # type: ignore[attr-defined]
        elif orig_peercred is not None:
            _socket.SO_PEERCRED = orig_peercred  # type: ignore[attr-defined]

    assert result == 42


def test_peer_euid_returns_none_on_struct_error(tmp_path) -> None:
    """_peer_euid returns None when struct.unpack raises (malformed getsockopt data)."""
    import socket as _socket

    listener = PamNotifyListener(str(tmp_path / "s.sock"))
    # Return too-short bytes to force a struct.error on unpack
    mock_sock = MagicMock()
    mock_sock.getsockopt = MagicMock(return_value=b"\x00\x01")  # too short for 3i
    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=mock_sock)

    fake_so_peercred = getattr(_socket, "SO_PEERCRED", 17)
    orig_peercred = getattr(_socket, "SO_PEERCRED", None)
    try:
        _socket.SO_PEERCRED = fake_so_peercred  # type: ignore[attr-defined]
        result = listener._peer_euid(writer)
    finally:
        if orig_peercred is None and hasattr(_socket, "SO_PEERCRED"):
            del _socket.SO_PEERCRED  # type: ignore[attr-defined]
        elif orig_peercred is not None:
            _socket.SO_PEERCRED = orig_peercred  # type: ignore[attr-defined]

    assert result is None


async def test_handle_connection_peer_euid_logged_when_no_enforcement(short_sock_dir, caplog) -> None:
    """Without allowlist, peer euid is still logged at debug level."""
    import logging

    path = str(short_sock_dir / "notify.sock")
    events: list[PamEvent] = []
    listener = PamNotifyListener(path, require_peer_uids=None)
    listener._peer_euid = lambda w: 500  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger="provide.uterm.pty.pam_listener"):
        await listener.start(lambda e: _collect(events, e))

        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"event":"open","username":"alice","tty":"","pid":1}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

        await listener.stop()

    assert any("euid=500" in r.message for r in caplog.records)
