#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from provide.uterm.server.app import create_server_app
from provide.uterm.server.auth import LocalIdentityProvider, WebhookIdentityProvider
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
