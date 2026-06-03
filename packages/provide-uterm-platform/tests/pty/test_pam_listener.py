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


async def _collect(events: list[PamEvent], ev: PamEvent) -> None:
    events.append(ev)


async def _send_line(path: str, data: dict) -> None:
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps(data) + "\n").encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def test_notify_socket_is_owner_only(tmp_path) -> None:
    import stat

    sock = tmp_path / "notify.sock"
    listener = PamNotifyListener(str(sock))
    await listener.start(AsyncMock())
    try:
        mode = stat.S_IMODE(sock.stat().st_mode)
        assert mode == 0o600
    finally:
        await listener.stop()


async def test_notify_socket_umask_constrains_bind_and_is_restored(tmp_path, monkeypatch) -> None:
    """The socket is created 0o600 atomically (umask before bind), not via a
    post-bind chmod that leaves a permission window.

    Regression for the bind-then-chmod race: any local user could connect in
    the gap between bind (default umask perms, e.g. srwxr-xr-x) and the chmod —
    and forge login events that drive root-side session creation. start() must
    set a restrictive umask (0o177) *before* the bind and restore the caller's
    umask afterward, so the socket never exists world-accessible.
    """
    import asyncio as _asyncio
    import os
    import stat

    observed_umask: list[int] = []
    real_bind = _asyncio.start_unix_server

    async def _spy_bind(*args, **kwargs):
        # Capture the umask in effect at the moment of bind by reading and
        # immediately restoring it (os.umask has no read-only variant).
        cur = os.umask(0o022)
        os.umask(cur)
        observed_umask.append(cur)
        return await real_bind(*args, **kwargs)

    monkeypatch.setattr(_asyncio, "start_unix_server", _spy_bind)

    sock = tmp_path / "notify.sock"
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


# ── peer-uid auth (Fix 2) ─────────────────────────────────────────────────────


async def test_peer_uid_allowed_when_in_allowlist(tmp_path) -> None:
    """require_peer_uids=[0] + peer euid 0 → connection allowed, handler called."""
    path = str(tmp_path / "notify.sock")
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


async def test_peer_uid_rejected_when_not_in_allowlist(tmp_path) -> None:
    """require_peer_uids=[0] + peer euid 1000 → connection rejected, handler NOT called."""
    path = str(tmp_path / "notify.sock")
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


async def test_peer_uid_no_enforcement_when_allowlist_none(tmp_path) -> None:
    """require_peer_uids=None (default) → no enforcement, even euid 1000 allowed."""
    path = str(tmp_path / "notify.sock")
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


async def test_peer_uid_none_platform_allowed_with_warning(tmp_path) -> None:
    """_peer_euid returning None (unsupported platform) → allowed (warn, not reject)."""
    path = str(tmp_path / "notify.sock")
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


async def test_handle_connection_peer_euid_logged_when_no_enforcement(tmp_path, caplog) -> None:
    """Without allowlist, peer euid is still logged at debug level."""
    import logging

    path = str(tmp_path / "notify.sock")
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
