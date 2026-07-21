#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Human VNC stream relay — upstream pump + RFB client input filter."""

from __future__ import annotations

import io
import socket
import threading
from contextlib import suppress

from provide.uterm.vnc import run_human_relay_streams

from provide.uterm.bridge.policy import can_inject


def _handshake() -> bytes:
    return b"RFB 003.008\n" + bytes([1]) + bytes([1])


def _key_event() -> bytes:
    return bytes([4]) + bytes(7)


def _policy_allow(sid: str, lid: str, _pid: str, role: str) -> bool:
    return can_inject(sid, lid, role) is None


def test_bytesio_relay_forwards_upstream_and_filters_key() -> None:
    """Pre-buffered BytesIO: upstream video passes; key inject needs lease+role."""
    browser_r = io.BytesIO(_handshake() + _key_event())
    browser_w = io.BytesIO()
    upstream_r = io.BytesIO(b"SERVER-FRAME-DATA")
    upstream_w = io.BytesIO()

    run_human_relay_streams(
        browser_r,
        browser_w,
        upstream_r,
        upstream_w,
        can_inject=_policy_allow,
        session_id="s1",
        lease_id="lease-1",
        principal_id="alice",
        principal_role="operator",
    )

    assert browser_w.getvalue() == b"SERVER-FRAME-DATA"
    # handshake (14) + key (8)
    assert len(upstream_w.getvalue()) == 14 + 8


def test_bytesio_nil_can_inject_fails_closed() -> None:
    browser_r = io.BytesIO(_handshake() + _key_event())
    browser_w = io.BytesIO()
    upstream_r = io.BytesIO(b"vid")
    upstream_w = io.BytesIO()

    run_human_relay_streams(
        browser_r,
        browser_w,
        upstream_r,
        upstream_w,
        can_inject=None,
        session_id="s1",
        lease_id="lease-1",
        principal_id="alice",
        principal_role="operator",
    )

    assert browser_w.getvalue() == b"vid"
    assert len(upstream_w.getvalue()) == 14  # handshake only


def test_bytesio_viewer_drops_key() -> None:
    browser_r = io.BytesIO(_handshake() + _key_event())
    browser_w = io.BytesIO()
    upstream_r = io.BytesIO(b"")
    upstream_w = io.BytesIO()

    run_human_relay_streams(
        browser_r,
        browser_w,
        upstream_r,
        upstream_w,
        can_inject=_policy_allow,
        session_id="s1",
        lease_id="lease-1",
        principal_id="v",
        principal_role="viewer",
    )

    assert len(upstream_w.getvalue()) == 14


def test_bytesio_no_lease_drops_key() -> None:
    browser_r = io.BytesIO(_handshake() + _key_event())
    browser_w = io.BytesIO()
    upstream_r = io.BytesIO(b"")
    upstream_w = io.BytesIO()

    run_human_relay_streams(
        browser_r,
        browser_w,
        upstream_r,
        upstream_w,
        can_inject=_policy_allow,
        session_id="s1",
        lease_id="",
        principal_id="op",
        principal_role="operator",
    )

    assert len(upstream_w.getvalue()) == 14


def test_upstream_partial_flush_before_eof() -> None:
    """RFB banner must reach browser before upstream EOF (live VNC shape).

    x11vnc sends ProtocolVersion then waits; a pump that only flushes on EOF
    would deadlock the browser handshake.
    """
    browser_c, browser_s = socket.socketpair()
    upstream_c, upstream_s = socket.socketpair()
    try:
        banner = b"RFB 003.008\n"
        hold = threading.Event()

        def _server_peer() -> None:
            upstream_s.sendall(banner)
            # Hold the upstream open (no EOF) until the browser saw the banner.
            hold.wait(timeout=5.0)
            with suppress(OSError):
                upstream_s.shutdown(socket.SHUT_WR)

        t_server = threading.Thread(target=_server_peer, daemon=True)
        t_server.start()

        # buffering=0 matches production dial + ws_gui_vnc socketpair streams.
        browser_r = browser_s.makefile("rb", buffering=0)
        browser_w = browser_s.makefile("wb", buffering=0)
        upstream_r = upstream_c.makefile("rb", buffering=0)
        upstream_w = upstream_c.makefile("wb", buffering=0)

        def _run() -> None:
            try:
                run_human_relay_streams(
                    browser_r,
                    browser_w,
                    upstream_r,
                    upstream_w,
                    can_inject=_policy_allow,
                    session_id="sess",
                    lease_id="L1",
                    principal_id="bob",
                    principal_role="admin",
                )
            except Exception:
                # Browser SHUT_WR ends the filter read; treat as normal teardown.
                pass
            finally:
                for f in (browser_r, browser_w, upstream_r, upstream_w):
                    with suppress(OSError):
                        f.close()

        t_relay = threading.Thread(target=_run, daemon=True)
        t_relay.start()

        browser_c.settimeout(2.0)
        got = browser_c.recv(64)
        assert got == banner, f"expected RFB banner before upstream EOF, got {got!r}"
        # Unblock server peer + relay (browser closes read side).
        hold.set()
        with suppress(OSError):
            browser_c.shutdown(socket.SHUT_WR)
        t_server.join(timeout=2.0)
        t_relay.join(timeout=2.0)
    finally:
        for s in (browser_c, browser_s, upstream_c, upstream_s):
            with suppress(OSError):
                s.close()


def test_socketpair_bidirectional_relay() -> None:
    """Concurrent duplex via socketpair (mirrors live WS↔TCP shape)."""
    browser_c, browser_s = socket.socketpair()
    upstream_c, upstream_s = socket.socketpair()
    try:
        # Client (browser) side writes handshake+key into browser_c.
        # Server (upstream) side writes video into upstream_s.
        client_payload = _handshake() + _key_event()
        server_payload = b"RFB-SERVER-BYTES-xyz"

        def _client_peer() -> None:
            browser_c.sendall(client_payload)
            browser_c.shutdown(socket.SHUT_WR)

        def _server_peer() -> None:
            upstream_s.sendall(server_payload)
            upstream_s.shutdown(socket.SHUT_WR)

        t_client = threading.Thread(target=_client_peer, daemon=True)
        t_server = threading.Thread(target=_server_peer, daemon=True)
        t_client.start()
        t_server.start()

        # socket.makefile has no closefd kw; use separate r/w views then close sockets only.
        browser_r = browser_s.makefile("rb")
        browser_w = browser_s.makefile("wb")
        upstream_r = upstream_c.makefile("rb")
        upstream_w = upstream_c.makefile("wb")
        try:
            run_human_relay_streams(
                browser_r,
                browser_w,
                upstream_r,
                upstream_w,
                can_inject=_policy_allow,
                session_id="sess",
                lease_id="L1",
                principal_id="bob",
                principal_role="admin",
            )
        finally:
            for f in (browser_r, browser_w, upstream_r, upstream_w):
                with suppress(OSError):
                    f.close()

        t_client.join(timeout=2.0)
        t_server.join(timeout=2.0)

        # What the "browser" peer received from the relay (upstream video).
        got_video = b""
        browser_c.settimeout(1.0)
        try:
            while True:
                chunk = browser_c.recv(4096)
                if not chunk:
                    break
                got_video += chunk
        except (TimeoutError, OSError):
            pass
        assert got_video == server_payload

        # What the "upstream" peer received (handshake + key, inject allowed).
        got_input = b""
        upstream_s.settimeout(1.0)
        try:
            while True:
                chunk = upstream_s.recv(4096)
                if not chunk:
                    break
                got_input += chunk
        except (TimeoutError, OSError):
            pass
        assert got_input == client_payload
    finally:
        for s in (browser_c, browser_s, upstream_c, upstream_s):
            with suppress(OSError):
                s.close()
