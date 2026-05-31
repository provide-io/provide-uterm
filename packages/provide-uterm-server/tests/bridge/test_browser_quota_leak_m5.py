#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for M5: per-principal browser-quota counter leak.

``register_browser`` increments ``hub._principal_browser_counts[subject_id]``
BEFORE the browser-WS handler's main ``try:`` block. The decrement lives in
``cleanup_browser_disconnect`` which only runs from the handler's ``finally``.
If the browser disconnects (or any setup line raises) between the increment
and the ``try:``, the counter leaks: after ``max_connections_per_principal``
leaks the principal is permanently locked out (1008).

The fix pulls the ``try:`` up so every line that can raise between register
and the receive loop runs inside the try, guaranteeing ``finally`` always runs
the decrement. These tests exercise the route handler end-to-end and assert
the count returns to its pre-connect value after a mid-handshake failure.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.identity import Principal

_ALICE = Principal(subject_id="alice", roles=frozenset({"admin"}), scopes=frozenset())


def _counted_router_client(hub: TermHub) -> TestClient:
    """Build a TestClient where every browser WS carries the 'alice' principal.

    Wraps the app in a tiny ASGI middleware that injects ``uterm_principal``
    into the connection scope state, so the route handler increments the
    per-principal quota for every browser connection — letting us observe
    quota leaks deterministically at the route level.
    """
    app = FastAPI()
    app.include_router(hub.create_router())

    inner = app.middleware_stack or app.build_middleware_stack()

    async def _inject_principal(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            scope.setdefault("state", {})["uterm_principal"] = _ALICE
        await inner(scope, receive, send)

    return TestClient(_inject_principal)


def test_mid_handshake_raise_does_not_leak_quota() -> None:
    """A setup call raising AFTER register_browser must NOT leak the quota count."""
    hub = TermHub(max_connections_per_principal=2)

    async def _boom(_worker_id: str, _ws: object) -> None:
        raise RuntimeError("simulated mid-handshake failure")

    # activate_browser_broadcasts runs between register_browser and the loop.
    hub.activate_browser_broadcasts = _boom  # type: ignore[method-assign]

    client = _counted_router_client(hub)

    assert hub._principal_browser_counts.get("alice", 0) == 0
    # The handler raises mid-handshake; the WS closes. We tolerate the close.
    with contextlib.suppress(Exception), client.websocket_connect("/ws/browser/w1/term"):
        pass

    # No leak: count is back to its pre-connect value.
    assert hub._principal_browser_counts.get("alice", 0) == 0, "browser quota leaked on mid-handshake failure"


def test_no_lockout_after_repeated_mid_handshake_failures() -> None:
    """Repeated mid-handshake failures must not eventually lock out the principal."""
    hub = TermHub(max_connections_per_principal=2)

    async def _boom(_worker_id: str, _ws: object) -> None:
        raise RuntimeError("simulated mid-handshake failure")

    hub.activate_browser_broadcasts = _boom  # type: ignore[method-assign]
    client = _counted_router_client(hub)

    # Far more failures than the cap — if the count leaked, alice would lock out.
    for _ in range(5):
        with contextlib.suppress(Exception), client.websocket_connect("/ws/browser/w1/term"):
            pass

    assert hub._principal_browser_counts.get("alice", 0) == 0, "repeated failures must not accumulate quota"


def test_normal_connect_still_counts_and_decrements() -> None:
    """The happy path must still increment on connect and decrement on disconnect."""
    hub = TermHub(max_connections_per_principal=2)
    client = _counted_router_client(hub)

    with client.websocket_connect("/ws/browser/w1/term") as ws:
        # Drain the hello + hijack_state so the handler is in its receive loop.
        ws.receive_text()
        ws.receive_text()
        # Mid-session: the principal is counted.
        assert hub._principal_browser_counts.get("alice", 0) == 1

    # After clean disconnect the finally decremented the count (key removed).
    assert hub._principal_browser_counts.get("alice", 0) == 0
