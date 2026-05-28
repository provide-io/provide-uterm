#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for WebSocketException propagation in ws_browser_term.

ws_browser_term catches BrowserRoleResolutionError and closes cleanly.
WebSocketException (raised by the resolve_browser_role callback) must propagate
rather than being silently swallowed — FastAPI closes with the socket's code.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocketException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from provide.uterm.client import connect_test_ws
from provide.uterm.server.bridge.hub import BrowserRoleResolutionError, TermHub


def _make_client(hub: TermHub) -> TestClient:
    app = FastAPI()
    app.include_router(hub.create_router())
    return TestClient(app, raise_server_exceptions=False)


class TestWebSocketExceptionPropagation:
    def test_websocket_exception_from_role_resolver_propagates(self) -> None:
        """WebSocketException raised by the role resolver is NOT caught as
        BrowserRoleResolutionError — it must propagate so FastAPI can close
        the socket with the correct code (1008 policy violation, etc.).

        Kills mutations that broaden the except clause to catch all exceptions
        or that convert WebSocketException into BrowserRoleResolutionError.
        """

        def _reject(ws, worker_id):  # type: ignore[no-untyped-def]
            raise WebSocketException(code=1008, reason="insufficient privileges")

        hub = TermHub(resolve_browser_role=_reject)
        client = _make_client(hub)

        with connect_test_ws(client, "/ws/browser/test-w/term") as ws, pytest.raises(WebSocketDisconnect):
            # WebSocketException causes close; receiving should raise.
            ws.receive_text()

    def test_browser_role_resolution_error_still_handled(self) -> None:
        """BrowserRoleResolutionError is still caught and results in a close.

        Verifies that adding the WebSocketException catch didn't break the existing
        BrowserRoleResolutionError handling.
        """

        def _reject(ws, worker_id):  # type: ignore[no-untyped-def]
            raise BrowserRoleResolutionError("not allowed")

        hub = TermHub(resolve_browser_role=_reject)
        client = _make_client(hub)

        with connect_test_ws(client, "/ws/browser/test-w/term") as ws, pytest.raises(WebSocketDisconnect):
            ws.receive_text()
