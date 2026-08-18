#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from httpx2 import Response
from starlette.testclient import TestClient

from provide.uterm.server.app import create_server_app
from provide.uterm.server.auth import LocalIdentityProvider, WebhookIdentityProvider
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.models import AuthConfig, ServerConfig
from tests.helpers import http_mock
from tests.helpers.fastapi_routes import find_effective_api_route


def test_create_server_app_instantiates_correct_idp():
    # Local IDP
    config = ServerConfig(auth=AuthConfig(identity_provider="local", mode="dev_token"))
    app = create_server_app(config, api_only=True)
    assert isinstance(app.state.uterm_hub.identity_provider, LocalIdentityProvider)

    # Webhook IDP
    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
        )
    )
    app = create_server_app(config, api_only=True)
    assert isinstance(app.state.uterm_hub.identity_provider, WebhookIdentityProvider)
    assert app.state.uterm_hub.identity_provider.url == "http://localhost:8080/auth"


def test_create_server_app_wires_require_response_nonce_flag():
    """L9: the factory-built provider carries webhook_idp_require_response_nonce."""
    # Default False is wired through.
    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
        )
    )
    app = create_server_app(config, api_only=True)
    assert app.state.uterm_hub.identity_provider.require_response_nonce is False

    # True is wired through.
    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
            webhook_idp_require_response_nonce=True,
        )
    )
    app = create_server_app(config, api_only=True)
    assert app.state.uterm_hub.identity_provider.require_response_nonce is True


async def test_webhook_idp_is_used_by_auth_dependency(monkeypatch) -> None:
    from starlette.requests import HTTPConnection

    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
        )
    )
    app = create_server_app(config, api_only=True)

    # The auth dependency is attached by include_router(dependencies=[...]), so
    # it lives on the *effective* route, not on the raw app.routes entry.
    sessions_route = find_effective_api_route(app, "/api/sessions")
    assert sessions_route is not None, "GET /api/sessions route not found"
    dep = sessions_route.dependencies[0].dependency
    assert dep is not None, "auth dependency not found"

    conn = HTTPConnection(
        {
            "type": "http",
            "path": "/api/sessions",
            "headers": [],
            "query_string": b"",
            "app": app,
            "client": ("testclient", 1234),
            "state": {},
        }
    )

    called = {"count": 0}

    async def _resolve_principal(_connection):
        called["count"] += 1
        return Principal(subject_id="webhook-user", roles=frozenset({"admin"}), scopes=frozenset({"*"}))

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _resolve_principal)
    await dep(conn)
    assert called["count"] == 1
    assert conn.state.uterm_principal.subject_id == "webhook-user"


def test_webhook_idp_route_level_failure_modes_require_auth(monkeypatch) -> None:
    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
        )
    )

    # deny-mode semantics: resolver failure / None principal should 401.
    app_deny = create_server_app(config, api_only=True)

    async def _deny(_connection):
        return None

    monkeypatch.setattr(app_deny.state.uterm_idp, "resolve_principal", _deny)
    with TestClient(app_deny) as client:
        assert client.get("/api/sessions").status_code == 401

    # viewer-mode semantics: anonymous viewer principal is still unauthenticated for API.
    app_viewer = create_server_app(config, api_only=True)

    async def _viewer(_connection):
        return Principal(subject_id="anonymous", roles=frozenset({"viewer"}), scopes=frozenset())

    monkeypatch.setattr(app_viewer.state.uterm_idp, "resolve_principal", _viewer)
    with TestClient(app_viewer) as client:
        assert client.get("/api/sessions").status_code == 401


@http_mock.mock
def test_webhook_idp_e2e_route_auth_success_and_failure() -> None:
    webhook_url = "https://idp.example.test/resolve"
    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url=webhook_url,
            webhook_idp_require_signed_response=False,
        )
    )

    app = create_server_app(config, api_only=True)
    with TestClient(app) as client:
        route_ok = http_mock.post(webhook_url).mock(
            return_value=Response(
                200,
                json={
                    "subject_id": "alice",
                    "roles": ["admin"],
                    "scopes": ["*"],
                },
            )
        )
        ok = client.get("/api/sessions")
        assert ok.status_code == 200
        assert route_ok.called

        http_mock.post(webhook_url).mock(return_value=Response(500))
        denied = client.get("/api/sessions")
        assert denied.status_code == 401


import pytest


async def test_resolve_browser_role_webhook_idp_none_principal_denied(monkeypatch) -> None:
    """Non-Local IDP browser-role path: a webhook IDP returning None (anonymous) is denied on ad-hoc workers.

    Covers the ``else`` branch of ``_resolve_browser_role`` (idp is not a
    ``LocalIdentityProvider``) and its ``principal is None`` deny path (anonymous
    is non-admin, so the default deny-ad-hoc policy raises WebSocketException).
    """
    from types import SimpleNamespace

    from fastapi import WebSocketException

    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
        )
    )
    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    async def _none(_ws):
        return None

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _none)
    ws = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(WebSocketException):
        await hub.resolve_role_for_browser(ws, "ad-hoc-unregistered-1")


async def test_resolve_browser_role_webhook_idp_principal_role_honored(monkeypatch) -> None:
    """Non-Local IDP browser-role path: an admin principal's role is honored on ad-hoc workers.

    Covers the ``principal is not None`` + admin branch of the same ``else`` block.
    """
    from types import SimpleNamespace

    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
        )
    )
    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    async def _admin(_ws):
        return Principal(subject_id="alice", roles=frozenset({"admin"}), scopes=frozenset({"*"}))

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _admin)
    ws = SimpleNamespace(state=SimpleNamespace())
    role = await hub.resolve_role_for_browser(ws, "ad-hoc-unregistered-2")
    assert role == "admin"


async def test_resolve_browser_role_adhoc_non_admin_denied_by_default(monkeypatch) -> None:
    """Viewer principal on an ad-hoc (unregistered) worker is denied when the opt-in flag is False (default)."""
    from types import SimpleNamespace

    from fastapi import WebSocketException

    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
        )
    )
    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    async def _viewer(_ws):
        return Principal(subject_id="bob", roles=frozenset({"viewer"}), scopes=frozenset())

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _viewer)
    ws = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(WebSocketException):
        await hub.resolve_role_for_browser(ws, "ad-hoc-unregistered-deny")


async def test_resolve_browser_role_adhoc_viewer_allowed_when_opted_in(monkeypatch) -> None:
    """Viewer principal on an ad-hoc worker is allowed when allow_adhoc_browser_observers=True."""
    from types import SimpleNamespace

    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
            allow_adhoc_browser_observers=True,
        )
    )
    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    async def _viewer(_ws):
        return Principal(subject_id="bob", roles=frozenset({"viewer"}), scopes=frozenset())

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _viewer)
    ws = SimpleNamespace(state=SimpleNamespace())
    assert await hub.resolve_role_for_browser(ws, "ad-hoc-unregistered-optin") == "viewer"


async def test_resolve_browser_role_adhoc_operator_allowed_when_opted_in(monkeypatch) -> None:
    """Operator principal on an ad-hoc worker is allowed when allow_adhoc_browser_observers=True."""
    from types import SimpleNamespace

    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_require_signed_response=False,
            allow_adhoc_browser_observers=True,
        )
    )
    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    async def _operator(_ws):
        return Principal(subject_id="carol", roles=frozenset({"operator"}), scopes=frozenset())

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _operator)
    ws = SimpleNamespace(state=SimpleNamespace())
    assert await hub.resolve_role_for_browser(ws, "ad-hoc-unregistered-optin-op") == "operator"
