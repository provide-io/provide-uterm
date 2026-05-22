#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocketOriginMiddleware: reject WS upgrades from disallowed Origins."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from provide.uterm.server.app.middleware import WebSocketOriginMiddleware


async def _echo(ws: WebSocket) -> None:
    await ws.accept()
    msg = await ws.receive_text()
    await ws.send_text(f"echo:{msg}")
    await ws.close()


def _build_app(allowed: tuple[str, ...]) -> Starlette:
    app = Starlette(routes=[WebSocketRoute("/ws", _echo)])
    app.add_middleware(WebSocketOriginMiddleware, allowed_origins=allowed)
    return app


def test_allowed_origin_passes() -> None:
    app = _build_app(("https://uterm.example",))
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws", headers={"origin": "https://uterm.example"}) as ws,
    ):
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"


def test_origin_match_is_case_insensitive() -> None:
    app = _build_app(("https://UTERM.Example",))
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws", headers={"origin": "https://uterm.example"}) as ws,
    ):
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"


def test_origin_trailing_slash_is_normalised() -> None:
    app = _build_app(("https://uterm.example/",))
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws", headers={"origin": "https://uterm.example"}) as ws,
    ):
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"


def test_disallowed_origin_is_rejected() -> None:
    app = _build_app(("https://uterm.example",))
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
            pass
    assert excinfo.value.code == 4403


def test_missing_origin_header_is_allowed_by_default() -> None:
    """Non-browser clients (python websockets, wscat, server-to-server)
    don't send Origin. They must NOT be blocked — they're authenticated
    via JWT/identity frames, which is a separate concern."""
    app = _build_app(("https://uterm.example",))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"


def test_empty_allowlist_rejects_browser_origins() -> None:
    """Default-deny posture: empty allowlist + present Origin header → 4403.

    This is a behaviour flip from earlier releases where empty allowlist
    was a no-op. Operators who want any-origin access must explicitly
    set ``allowed_origins = ["*"]``.
    """
    app = _build_app(())
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws", headers={"origin": "https://anywhere.example"}):
            pass
    assert excinfo.value.code == 4403


def test_empty_allowlist_still_allows_non_browser_clients() -> None:
    """No Origin header → non-browser client → always allowed (auth is
    handled by JWT / identity frames downstream)."""
    app = _build_app(())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"


def test_wildcard_entry_disables_gate() -> None:
    """The literal '*' entry is an explicit opt-out for operators who
    deliberately want any-origin access (e.g. for a development host)."""
    app = _build_app(("*",))
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws", headers={"origin": "https://anywhere.example"}) as ws,
    ):
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"


def test_http_requests_pass_through_untouched() -> None:
    """The middleware is WS-only; HTTP is CORSMiddleware's concern."""
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def _hello(_request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", _hello)])
    app.add_middleware(WebSocketOriginMiddleware, allowed_origins=("https://uterm.example",))

    with TestClient(app) as client:
        resp = client.get("/", headers={"origin": "https://evil.example"})
        assert resp.status_code == 200
        assert resp.text == "ok"
