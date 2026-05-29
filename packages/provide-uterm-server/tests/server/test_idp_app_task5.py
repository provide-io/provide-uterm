#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
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
