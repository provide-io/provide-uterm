#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import httpx
import pytest
import respx

from provide.uterm.server.auth import WebhookIdentityProvider
from provide.uterm.server.webhook_signing import verify_webhook_signature


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


@pytest.mark.asyncio
@respx.mock
async def test_webhook_idp_sends_signed_headers_not_cleartext_secret():
    """IDP requests carry X-Uterm-Timestamp + X-Uterm-Signature, no X-Webhook-Secret."""
    url = "https://auth.example.com/resolve"
    secret = "uterm-test-secret-32-byte-minimum-key"  # pragma: allowlist secret
    idp = WebhookIdentityProvider(url=url, secret=secret)

    route = respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={"subject_id": "user-1", "roles": ["viewer"]},
        )
    )

    class MockConnection:
        headers = {}
        cookies = {}

    await idp.resolve_principal(MockConnection())

    assert route.called
    req = route.calls.last.request
    assert "X-Webhook-Secret" not in req.headers
    ts = req.headers.get("X-Uterm-Timestamp", "")
    sig = req.headers.get("X-Uterm-Signature", "")
    assert ts != ""
    assert verify_webhook_signature(secret, req.content, sig, ts) is True


@pytest.mark.asyncio
@respx.mock
async def test_webhook_idp_no_secret_sends_no_signing_headers():
    """When no secret is configured, no signing headers are sent."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url)

    route = respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={"subject_id": "user-1", "roles": ["viewer"]},
        )
    )

    class MockConnection:
        headers = {}
        cookies = {}

    await idp.resolve_principal(MockConnection())

    assert route.called
    req = route.calls.last.request
    assert "X-Webhook-Secret" not in req.headers
    assert "X-Uterm-Signature" not in req.headers
    assert "X-Uterm-Timestamp" not in req.headers
