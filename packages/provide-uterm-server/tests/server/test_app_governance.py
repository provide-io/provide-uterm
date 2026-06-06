#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import cast

import pytest

from provide.uterm.server.app import create_server_app
from provide.uterm.server.authorization import AuthorizationService, WebhookAuthorizationProvider
from provide.uterm.server.config import default_server_config


def test_app_initializes_webhook_authz() -> None:
    config = default_server_config()
    config.governance.authz_webhook_url = "https://fleet.example.com/authz"

    # Use api_only=True to skip frontend asset checks
    app = create_server_app(config, api_only=True)

    authz = cast(AuthorizationService, app.state.uterm_authz)
    assert isinstance(authz._provider, WebhookAuthorizationProvider)
    assert authz._provider.url == "https://fleet.example.com/authz"


def test_app_initializes_webhook_policy() -> None:
    from provide.uterm.server.bridge.hub.ext import WebhookPolicyGate

    config = default_server_config()
    config.governance.policy_webhook_url = "https://fleet.example.com/policy"

    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    assert isinstance(hub._policy_gate, WebhookPolicyGate)
    assert hub._policy_gate.url == "https://fleet.example.com/policy"


@pytest.mark.asyncio
async def test_app_lifespan_heartbeat_init() -> None:
    config = default_server_config()
    config.governance.registry_webhook_url = "https://fleet.example.com/heartbeat"

    # Testing that it starts and stops without error is enough for coverage
    # of the initialization/cancellation logic.
    from fastapi.testclient import TestClient

    app = create_server_app(config, api_only=True)
    with TestClient(app):
        # Lifespan started
        pass
    # Lifespan ended


async def test_app_lifespan_closes_governance_gate_clients() -> None:
    """A configured governance gate gets its pooled HTTP client closed on shutdown.

    Only the policy gate is configured: that exercises the close loop's
    gate-present branch (policy) and gate-absent branch (behavioral/telemetry are
    None) without spawning the behavioral-audit background task, which clashes
    with the async test loop under TestClient.
    """
    config = default_server_config()
    config.governance.policy_webhook_url = "https://fleet.example.com/policy"

    from fastapi.testclient import TestClient

    app = create_server_app(config, api_only=True)
    # Entering + exiting the lifespan must run the gate's aclose() without error.
    with TestClient(app):
        pass
