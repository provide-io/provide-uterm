#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import respx
from httpx import Response
from starlette.testclient import TestClient

from provide.uterm.server.app import create_server_app
from provide.uterm.server.auth import LocalIdentityProvider, WebhookIdentityProvider
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.models import AuthConfig, ServerConfig


def test_create_server_app_instantiates_correct_idp():
    # Local IDP
    config = ServerConfig(auth=AuthConfig(identity_provider="local", mode="dev_token"))
    app = create_server_app(config, api_only=True)
    assert isinstance(app.state.uterm_hub.identity_provider, LocalIdentityProvider)

    # Webhook IDP
    config = ServerConfig(
        auth=AuthConfig(identity_provider="webhook", mode="dev_token", webhook_idp_url="http://localhost:8080/auth")
    )
    app = create_server_app(config, api_only=True)
    assert isinstance(app.state.uterm_hub.identity_provider, WebhookIdentityProvider)
    assert app.state.uterm_hub.identity_provider.url == "http://localhost:8080/auth"


async def test_webhook_idp_is_used_by_auth_dependency(monkeypatch) -> None:
    from fastapi.routing import APIRoute
    from starlette.requests import HTTPConnection

    config = ServerConfig(
        auth=AuthConfig(identity_provider="webhook", mode="dev_token", webhook_idp_url="http://localhost:8080/auth")
    )
    app = create_server_app(config, api_only=True)

    dep = None
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == "/api/sessions":
            dep = route.dependencies[0].dependency
            break
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
        auth=AuthConfig(identity_provider="webhook", mode="dev_token", webhook_idp_url="http://localhost:8080/auth")
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


@respx.mock
def test_webhook_idp_e2e_route_auth_success_and_failure() -> None:
    webhook_url = "https://idp.example.test/resolve"
    config = ServerConfig(auth=AuthConfig(identity_provider="webhook", mode="dev_token", webhook_idp_url=webhook_url))

    app = create_server_app(config, api_only=True)
    with TestClient(app) as client:
        route_ok = respx.post(webhook_url).mock(
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

        respx.post(webhook_url).mock(return_value=Response(500))
        denied = client.get("/api/sessions")
        assert denied.status_code == 401


async def test_resolve_browser_role_webhook_idp_none_principal_is_anonymous_viewer(monkeypatch) -> None:
    """Non-Local IDP browser-role path: a webhook IDP returning None yields an anonymous viewer.

    Covers the ``else`` branch of ``_resolve_browser_role`` (idp is not a
    ``LocalIdentityProvider``) and its ``principal is None`` fallback.
    """
    from types import SimpleNamespace

    config = ServerConfig(
        auth=AuthConfig(identity_provider="webhook", mode="dev_token", webhook_idp_url="http://localhost:8080/auth")
    )
    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    async def _none(_ws):
        return None

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _none)
    # Browser WS with no pre-resolved principal in ws.state; the worker_id is
    # unregistered (no SessionDefinition) so resolution falls through to viewer.
    ws = SimpleNamespace(state=SimpleNamespace())
    role = await hub.resolve_role_for_browser(ws, "ad-hoc-unregistered-1")
    assert role == "viewer"


async def test_resolve_browser_role_webhook_idp_principal_role_honored(monkeypatch) -> None:
    """Non-Local IDP browser-role path: a webhook IDP principal's role is honored.

    Covers the ``principal is not None`` edge of the same ``else`` branch.
    """
    from types import SimpleNamespace

    config = ServerConfig(
        auth=AuthConfig(identity_provider="webhook", mode="dev_token", webhook_idp_url="http://localhost:8080/auth")
    )
    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    async def _admin(_ws):
        return Principal(subject_id="alice", roles=frozenset({"admin"}), scopes=frozenset({"*"}))

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _admin)
    ws = SimpleNamespace(state=SimpleNamespace())
    role = await hub.resolve_role_for_browser(ws, "ad-hoc-unregistered-2")
    assert role == "admin"
