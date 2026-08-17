#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocketAuthDenied: refuse a handshake with a real status, not a 403 close."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from provide.uterm.server.app.ws_denial import (
    _UVICORN_INCOMPLETE_HANDSHAKE,
    WebSocketAuthDenied,
    _denial_sent,
    _IncompleteHandshakeFilter,
    handle_ws_auth_denied,
    install_ws_denial_support,
)


def _build_app(status_code: int = 401, detail: str = "authentication required") -> Starlette:
    async def refuse(_ws: WebSocket) -> None:
        raise WebSocketAuthDenied(status_code, detail)

    app = Starlette(routes=[WebSocketRoute("/ws", refuse)])
    app.add_exception_handler(WebSocketAuthDenied, handle_ws_auth_denied)  # type: ignore[arg-type]
    return app


def test_unauthenticated_handshake_is_refused_with_401() -> None:
    """The client gets 401 and the detail body, not an accepted socket."""
    with TestClient(_build_app()) as client, pytest.raises(WebSocketDenialResponse) as exc_info:
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover — the connect above always raises

    assert exc_info.value.status_code == 401
    assert exc_info.value.json() == {"detail": "authentication required"}


def test_a_401_carries_the_www_authenticate_challenge() -> None:
    """RFC 7235: a 401 has to say how to authenticate."""
    with TestClient(_build_app()) as client, pytest.raises(WebSocketDenialResponse) as exc_info:
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover — the connect above always raises

    assert exc_info.value.headers["www-authenticate"] == "Bearer"


def test_a_non_401_refusal_carries_no_challenge() -> None:
    """403 is an authorization answer; there is nothing to challenge for."""
    with (
        TestClient(_build_app(status_code=403, detail="insufficient privileges")) as client,
        pytest.raises(WebSocketDenialResponse) as exc_info,
    ):
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover — the connect above always raises

    assert exc_info.value.status_code == 403
    assert "www-authenticate" not in exc_info.value.headers


async def test_a_server_without_the_extension_falls_back_to_the_close() -> None:
    """No denial extension: close before accept, which the server reports as 403."""
    sent: list[dict[str, object]] = []

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    async def _receive() -> dict[str, object]:  # pragma: no cover — close needs no inbound message
        return {"type": "websocket.connect"}

    # scope carries no "extensions", so send_denial_response is unavailable.
    websocket = WebSocket({"type": "websocket", "path": "/ws"}, receive=_receive, send=_send)
    await handle_ws_auth_denied(websocket, WebSocketAuthDenied(401, "authentication required"))

    assert sent == [{"type": "websocket.close", "code": 1008, "reason": "authentication required"}]


def test_the_fallback_close_reaches_a_client_as_a_disconnect() -> None:
    """End-to-end shape of the fallback, with the extension stripped from the scope."""

    async def refuse(ws: WebSocket) -> None:
        ws.scope["extensions"] = {}
        raise WebSocketAuthDenied(401, "authentication required")

    app = Starlette(routes=[WebSocketRoute("/ws", refuse)])
    app.add_exception_handler(WebSocketAuthDenied, handle_ws_auth_denied)  # type: ignore[arg-type]

    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass  # pragma: no cover — the connect above always raises

    assert exc_info.value.code == 1008


class TestIncompleteHandshakeFilter:
    """The filter silences uvicorn's spurious ERROR without silencing real ones."""

    def _record(self, message: str) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 0, message, None, None)

    def test_an_unrelated_record_passes(self) -> None:
        assert _IncompleteHandshakeFilter().filter(self._record("something else entirely")) is True

    def test_a_genuine_incomplete_handshake_passes(self) -> None:
        """No denial on this connection: the app really did fail to finish. Stay loud."""
        token = _denial_sent.set(False)
        try:
            assert _IncompleteHandshakeFilter().filter(self._record(_UVICORN_INCOMPLETE_HANDSHAKE)) is True
        finally:
            _denial_sent.reset(token)

    def test_our_own_denial_is_suppressed(self) -> None:
        """uvicorn checks handshake_complete but the denial sets initial_response."""
        token = _denial_sent.set(True)
        try:
            assert _IncompleteHandshakeFilter().filter(self._record(_UVICORN_INCOMPLETE_HANDSHAKE)) is False
        finally:
            _denial_sent.reset(token)


class TestInstallWsDenialSupport:
    """Installation is per-app, but the uvicorn logger it touches is global."""

    def _filters(self) -> list[logging.Filter]:
        return [f for f in logging.getLogger("uvicorn.error").filters if isinstance(f, _IncompleteHandshakeFilter)]

    def test_installing_registers_the_handler_and_one_filter(self) -> None:
        before = len(self._filters())
        app = FastAPI()
        install_ws_denial_support(app)

        assert WebSocketAuthDenied in app.exception_handlers
        assert len(self._filters()) == max(before, 1)

    def test_installing_twice_does_not_stack_filters(self) -> None:
        """A suite builds hundreds of apps; each must not leave a filter behind."""
        install_ws_denial_support(FastAPI())
        after_first = len(self._filters())
        install_ws_denial_support(FastAPI())

        assert len(self._filters()) == after_first == 1
