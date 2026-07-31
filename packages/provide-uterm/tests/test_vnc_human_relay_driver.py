#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Human VNC relay update-driver — periodic FramebufferUpdateRequest injection.

The driver keeps an animating upstream streaming to clients (e.g. noVNC) that
send exactly one full update request and then go silent. It starts only after
the client's first request (so the client's pixel format is already upstream).
These tests use live socketpairs so the relay stays alive long enough for the
driver thread to run.
"""

from __future__ import annotations

import io
import socket
import struct
import threading
from contextlib import suppress
from typing import Any

import provide.uterm.vnc.human_relay as hr
from provide.uterm.vnc import run_human_relay_streams
from provide.uterm.vnc.human_relay import _DRIVE_FBUR


def _handshake() -> bytes:
    return b"RFB 003.008\n" + bytes([1]) + bytes([1])


def _client_fbur() -> bytes:
    # A distinct FramebufferUpdateRequest (w=h=1) so it never equals _DRIVE_FBUR.
    return struct.pack(">BBHHHH", 3, 1, 0, 0, 1, 1)


def _run_relay_thread(**kwargs: Any) -> threading.Thread:
    def _run() -> None:
        with suppress(Exception):
            run_human_relay_streams(**kwargs)
        for key in ("browser_r", "browser_w", "upstream_r", "upstream_w"):
            with suppress(Exception):
                close = getattr(kwargs[key], "close", None)
                if callable(close):
                    close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def test_update_driver_injects_after_client_first_request() -> None:
    """Once the client's first FBUR lands, the driver injects incremental FBURs."""
    browser_c, browser_s = socket.socketpair()
    upstream_c, upstream_s = socket.socketpair()
    try:
        # Handshake + the client's first update request → driver starts.
        browser_c.sendall(_handshake() + _client_fbur())
        t = _run_relay_thread(
            browser_r=browser_s.makefile("rb", buffering=0),
            browser_w=browser_s.makefile("wb", buffering=0),
            upstream_r=upstream_c.makefile("rb", buffering=0),
            upstream_w=upstream_c.makefile("wb", buffering=0),
            can_inject=None,
            session_id="s",
            lease_id="L",
            principal_id="p",
            principal_role="admin",
            drive_update_interval_s=0.01,
        )
        # upstream sees: handshake(14) + client FBUR(10) + >=1 injected _DRIVE_FBUR.
        upstream_s.settimeout(3.0)
        buf = b""
        want = 14 + len(_client_fbur()) + len(_DRIVE_FBUR)
        while len(buf) < want:
            chunk = upstream_s.recv(64)
            if not chunk:
                break
            buf += chunk
        assert buf[:14] == _handshake()
        assert _DRIVE_FBUR in buf[14 + len(_client_fbur()) :], f"no injected FBUR: {buf!r}"

        with suppress(OSError):
            browser_c.shutdown(socket.SHUT_WR)
        t.join(timeout=2.0)
    finally:
        for s in (browser_c, browser_s, upstream_c, upstream_s):
            with suppress(OSError):
                s.close()


class _FailAfter:
    """Binary sink that raises once cumulative writes exceed *after* bytes."""

    def __init__(self, after: int) -> None:
        self.inner = io.BytesIO()
        self._after = after
        self._n = 0

    def write(self, data: bytes) -> int:
        self._n += len(data)
        if self._n > self._after:
            raise OSError("upstream closed")
        return self.inner.write(data)

    def flush(self) -> None:
        self.inner.flush()


def test_update_driver_stops_on_write_error() -> None:
    """A write error (upstream gone) stops the driver without killing the relay."""
    browser_c, browser_s = socket.socketpair()
    # Allow handshake (14) + client FBUR (10); the driver's first injection raises.
    upstream_w = _FailAfter(14 + len(_client_fbur()))
    try:
        browser_c.sendall(_handshake() + _client_fbur())
        t = _run_relay_thread(
            browser_r=browser_s.makefile("rb", buffering=0),
            browser_w=io.BytesIO(),
            upstream_r=io.BytesIO(b""),
            upstream_w=upstream_w,
            can_inject=None,
            session_id="s",
            lease_id="L",
            principal_id="p",
            principal_role="admin",
            drive_update_interval_s=0.01,
        )
        threading.Event().wait(0.2)  # let the driver attempt (and fail) an injection
        # Only handshake + the client request landed; the driver's FBUR was rejected.
        assert upstream_w.inner.getvalue() == _handshake() + _client_fbur()

        with suppress(OSError):
            browser_c.shutdown(socket.SHUT_WR)
        t.join(timeout=2.0)
    finally:
        for s in (browser_c, browser_s):
            with suppress(OSError):
                s.close()


def test_update_driver_waits_when_client_never_requests(monkeypatch: Any) -> None:
    """Client handshakes but never sends a request → driver gives up, never injects."""
    monkeypatch.setattr(hr, "_DRIVE_HANDSHAKE_WAIT_S", 0.05)
    browser_c, browser_s = socket.socketpair()
    upstream_c, upstream_s = socket.socketpair()
    try:
        # Handshake only — no FramebufferUpdateRequest, so on_client_ready never fires.
        browser_c.sendall(_handshake())
        t = _run_relay_thread(
            browser_r=browser_s.makefile("rb", buffering=0),
            browser_w=browser_s.makefile("wb", buffering=0),
            upstream_r=upstream_c.makefile("rb", buffering=0),
            upstream_w=upstream_c.makefile("wb", buffering=0),
            can_inject=None,
            session_id="s",
            lease_id="L",
            principal_id="p",
            principal_role="admin",
            drive_update_interval_s=0.01,
        )
        threading.Event().wait(0.2)  # let the driver's client-ready wait time out
        # Only the forwarded handshake reached upstream — no injected FBURs.
        upstream_s.setblocking(False)
        got = b""
        with suppress(BlockingIOError, OSError):
            got = upstream_s.recv(256)
        assert got == _handshake()

        with suppress(OSError):
            browser_c.shutdown(socket.SHUT_WR)
        t.join(timeout=2.0)
    finally:
        for s in (browser_c, browser_s, upstream_c, upstream_s):
            with suppress(OSError):
                s.close()
