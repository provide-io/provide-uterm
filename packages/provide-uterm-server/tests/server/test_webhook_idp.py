#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import httpx
import pytest
import respx

from provide.uterm.server.auth import WebhookIdentityProvider


@pytest.mark.asyncio
@respx.mock
async def test_webhook_idp_resolve_success():
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, secret="uterm-test-secret-32-byte-minimum-key")

    respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "subject_id": "user-123",
                "roles": ["admin"],
                "claims": {"email": "user@example.com"},
                "display_name": "Test User",
            },
        )
    )

    class MockConnection:
        headers = {"Authorization": "Bearer some-token"}
        cookies = {"session": "abc"}

    principal = await idp.resolve_principal(MockConnection())

    assert principal.subject_id == "user-123"
    assert "admin" in principal.roles
    assert principal.claims["email"] == "user@example.com"
    assert principal.display_name == "Test User"


@pytest.mark.asyncio
@respx.mock
async def test_webhook_idp_resolve_error():
    """Finding #7: default failure mode is ``deny`` → None (was: viewer)."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url)

    respx.post(url).mock(return_value=httpx.Response(500))

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is None


@pytest.mark.asyncio
@respx.mock
async def test_webhook_idp_resolve_timeout():
    """Finding #7: default failure mode is ``deny`` → None on timeout."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, timeout_s=0.1)

    respx.post(url).mock(side_effect=httpx.TimeoutException("Too slow"))

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is None


@pytest.mark.asyncio
@respx.mock
async def test_webhook_idp_resolve_error_viewer_on_failure():
    """Finding #7: ``on_failure='viewer'`` preserves legacy fail-open."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, on_failure="viewer")

    respx.post(url).mock(return_value=httpx.Response(500))

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is not None
    assert principal.subject_id == "anonymous"
    assert "viewer" in principal.roles
