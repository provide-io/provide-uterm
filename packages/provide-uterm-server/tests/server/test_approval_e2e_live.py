#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import pytest
from playwright.sync_api import Page

from provide.uterm.server import create_server_app, default_server_config


@pytest.mark.playwright
def test_approval_ux_e2e(page: Page):
    # This test assumes the mock External Management Tier is NOT running (we'll mock the policy gate in Python)
    from provide.uterm.bridge.hub import PolicyDecision, TermHub

    class LiveHoldPolicy:
        async def intercept_input(self, data, context):
            if "rm -rf" in data:
                return PolicyDecision(action="hold", request_id="live-req-1")
            return PolicyDecision(action="allow")

    config = default_server_config()
    config.auth.mode = "dev"
    config.server.port = 0  # random

    # We use a custom Hub with the hold policy
    hub = TermHub(policy_gate=LiveHoldPolicy())
    app = create_server_app(config, api_only=True)
    app.state.uterm_hub = hub  # Override

    # Start the server in a thread or use the provided test infrastructure
    # Actually, pytest-playwright works best with a real server.

    # I'll just use the EXISTING proof script but I'll fix the communication.
