#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Human VNC WebSocket route authz + upstream-unavailable path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from provide.uterm.server.bridge.frames import make_hello_frame
from provide.uterm.server.bridge.routes.ws_gui_vnc import (
    check_vnc_relay_authz,
    principal_role_name,
    register_gui_vnc_ws_routes,
)

WID = "vnc-worker"
HID = "00000000-0000-0000-0000-0000000000ab"
PATH = f"/worker/{WID}/hijack/{HID}/gui/vnc"


def _principal(*, subject: str = "alice", roles: frozenset[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject, roles=roles if roles is not None else frozenset({"operator"}))


def test_principal_role_name_picks_highest() -> None:
    p = _principal(roles=frozenset({"viewer", "admin"}))
    assert principal_role_name(p) == "admin"


def test_authz_requires_principal() -> None:
    assert check_vnc_relay_authz(principal=None, hijack_session=object(), hijack_id=HID) == "authentication required"


def test_authz_requires_operator() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    err = check_vnc_relay_authz(principal=_principal(roles=frozenset({"viewer"})), hijack_session=hs, hijack_id=HID)
    assert err == "insufficient privileges"


def test_authz_requires_session() -> None:
    err = check_vnc_relay_authz(principal=_principal(), hijack_session=None, hijack_id=HID)
    assert err == "invalid or expired hijack session"


def test_authz_principal_bind() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    err = check_vnc_relay_authz(principal=_principal(subject="bob"), hijack_session=hs, hijack_id=HID)
    assert err == "hijack lease not owned by caller"


def test_authz_ok_owner() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    got = check_vnc_relay_authz(principal=_principal(subject="alice"), hijack_session=hs, hijack_id=HID)
    assert not isinstance(got, str)
    assert got.principal_id == "alice"
    assert got.principal_role == "operator"
    assert got.lease_id == HID


def test_authz_legacy_unbound_lease() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by=None)
    got = check_vnc_relay_authz(principal=_principal(subject="anyone"), hijack_session=hs, hijack_id=HID)
    assert not isinstance(got, str)


def test_hello_vnc_supported_default() -> None:
    assert make_hello_frame()["vnc_supported"] is True


class _PrincipalASGI:
    """Inject ``scope['state']['uterm_principal']`` for HTTP and WebSocket."""

    def __init__(self, app: Any, principal: Any | None) -> None:
        self.app = app
        self.principal = principal

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in {"http", "websocket"}:
            state = scope.setdefault("state", {})
            # Starlette may already have installed a State object.
            if hasattr(state, "__setattr__") and not isinstance(state, dict):
                state.uterm_principal = self.principal
            else:
                state["uterm_principal"] = self.principal
        await self.app(scope, receive, send)


def _client(*, rest_session: Any, principal: Any | None) -> TestClient:
    hub = SimpleNamespace()
    hub.get_rest_session = AsyncMock(return_value=rest_session)
    hub.vnc_upstream_factory = None

    app = FastAPI()
    router = APIRouter()
    register_gui_vnc_ws_routes(hub, router)
    app.include_router(router)
    return TestClient(_PrincipalASGI(app, principal))


def _connect_close_code(client: TestClient, path: str = PATH) -> int | None:
    """Return WebSocket close code when the server rejects/ends immediately."""
    try:
        with client.websocket_connect(path) as ws:
            try:
                msg = ws.receive()
                if isinstance(msg, dict) and msg.get("type") == "websocket.close":
                    code = msg.get("code")
                    return int(code) if code is not None else None
            except WebSocketDisconnect as exc:
                return int(exc.code) if exc.code is not None else None
    except WebSocketDisconnect as exc:
        return int(exc.code) if exc.code is not None else None
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code is not None:
            return int(code)
        # httpx/starlette sometimes wraps: "WebSocket is not connected. Need to call "accept" first"?
        # Fall through — none means test fails with a clear assertion.
    return None


def test_ws_closes_upstream_unavailable_after_authz() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    client = _client(rest_session=hs, principal=_principal(subject="alice"))
    code = _connect_close_code(client)
    assert code == 1013


def test_ws_policy_reject_viewer() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    client = _client(rest_session=hs, principal=_principal(subject="alice", roles=frozenset({"viewer"})))
    code = _connect_close_code(client)
    assert code == 1008


def test_ws_policy_reject_non_owner() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    client = _client(rest_session=hs, principal=_principal(subject="bob"))
    code = _connect_close_code(client)
    assert code == 1008


def test_ws_policy_reject_missing_session() -> None:
    client = _client(rest_session=None, principal=_principal(subject="alice"))
    code = _connect_close_code(client)
    assert code == 1008
