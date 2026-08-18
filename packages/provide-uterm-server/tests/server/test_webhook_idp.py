#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import httpx2
import pytest

from provide.uterm.server.auth import WebhookIdentityProvider, _filter_known_roles
from provide.uterm.server.webhook_signing import verify_webhook_signature
from tests.helpers import http_mock


@pytest.mark.parametrize(
    "raw",
    [
        ["Admin"],
        ["ADMIN"],
        ["aDmIn"],
        ["  Admin  "],
    ],
)
def test_filter_known_roles_case_folds_admin(raw):
    """L24 regression: role resolution case-folds so mixed-case role strings
    from an external IDP normalize to the canonical lower-cased allow-list
    entry (consistent with the JWT/header paths). Pin this behavior so a
    future refactor can't silently make role matching case-sensitive again."""
    assert _filter_known_roles(raw) == frozenset({"admin"})


@pytest.mark.asyncio
@http_mock.mock
async def test_webhook_idp_resolve_success():
    url = "https://auth.example.com/resolve"
    # This test predates 1f response-signature verification and mocks an
    # unsigned response; opt out so it keeps exercising the happy-path mapping.
    idp = WebhookIdentityProvider(
        url=url,
        secret="uterm-test-secret-32-byte-minimum-key",  # pragma: allowlist secret
        require_signed_response=False,
    )

    http_mock.post(url).mock(
        return_value=httpx2.Response(
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
@http_mock.mock
async def test_webhook_idp_resolve_error():
    """Finding #7: default failure mode is ``deny`` → None (was: viewer)."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url)

    http_mock.post(url).mock(return_value=httpx2.Response(500))

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is None


@pytest.mark.asyncio
@http_mock.mock
async def test_webhook_idp_resolve_timeout():
    """Finding #7: default failure mode is ``deny`` → None on timeout."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, timeout_s=0.1)

    http_mock.post(url).mock(side_effect=httpx2.TimeoutException("Too slow"))

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is None


@pytest.mark.asyncio
@http_mock.mock
async def test_webhook_idp_resolve_error_viewer_on_failure():
    """Finding #7: ``on_failure='viewer'`` preserves legacy fail-open."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, on_failure="viewer")

    http_mock.post(url).mock(return_value=httpx2.Response(500))

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is not None
    assert principal.subject_id == "anonymous"
    assert "viewer" in principal.roles


@pytest.mark.asyncio
@http_mock.mock
async def test_webhook_idp_sends_signed_headers_not_cleartext_secret():
    """IDP requests carry X-Uterm-Timestamp + X-Uterm-Signature, no X-Webhook-Secret."""
    url = "https://auth.example.com/resolve"
    secret = "uterm-test-secret-32-byte-minimum-key"  # pragma: allowlist secret
    idp = WebhookIdentityProvider(url=url, secret=secret)

    route = http_mock.post(url).mock(
        return_value=httpx2.Response(
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
@http_mock.mock
async def test_webhook_idp_filters_unknown_roles():
    """Fix 1e: roles returned by the webhook IDP are filtered to the known
    allow-list — a compromised/MITM'd webhook cannot inject bogus roles
    (e.g. superuser/root) alongside a legitimate one."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, require_signed_response=False)

    http_mock.post(url).mock(
        return_value=httpx2.Response(
            200,
            json={"subject_id": "x", "roles": ["admin", "superuser", "root"]},
        )
    )

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is not None
    assert principal.roles == frozenset({"admin"})


@pytest.mark.asyncio
@http_mock.mock
async def test_webhook_idp_empty_after_filter_falls_back_to_viewer():
    """Fix 1e: if every returned role is filtered out, fall back to viewer."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, require_signed_response=False)

    http_mock.post(url).mock(
        return_value=httpx2.Response(200, json={"subject_id": "x", "roles": ["nonsense"]}),
    )

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is not None
    assert principal.roles == frozenset({"viewer"})


@pytest.mark.asyncio
@http_mock.mock
async def test_webhook_idp_keeps_legitimate_role():
    """Fix 1e: a single legitimate role passes through unchanged."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, require_signed_response=False)

    http_mock.post(url).mock(
        return_value=httpx2.Response(200, json={"subject_id": "x", "roles": ["operator"]}),
    )

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is not None
    assert principal.roles == frozenset({"operator"})


@pytest.mark.asyncio
@http_mock.mock
async def test_webhook_idp_case_folds_mixed_case_admin_role():
    """L24 regression: a webhook IDP returning a mixed-case ``Admin`` role
    resolves to an admin Principal — the role is case-folded to the canonical
    ``admin`` allow-list entry rather than dropped as unknown."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, require_signed_response=False)

    http_mock.post(url).mock(
        return_value=httpx2.Response(200, json={"subject_id": "x", "roles": ["Admin"]}),
    )

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is not None
    assert principal.roles == frozenset({"admin"})


@pytest.mark.asyncio
@http_mock.mock
async def test_webhook_idp_failure_emits_audit_event(monkeypatch):
    """Fix 1g: a webhook-IDP failure emits a structured audit event so the
    fail-open/attack signal lands in the audit trail, not just a log warning."""
    import provide.uterm.server.auth as auth_mod

    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url, on_failure="deny")

    http_mock.post(url).mock(side_effect=httpx2.ConnectError("network down"))

    captured: list[tuple[str, dict]] = []

    def _spy_audit(action: str, **kwargs) -> None:
        captured.append((action, kwargs))

    monkeypatch.setattr(auth_mod, "audit_event", _spy_audit)

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal is None

    actions = [a for a, _ in captured]
    assert "auth.webhook_idp_failure" in actions
    detail = next(kw["detail"] for a, kw in captured if a == "auth.webhook_idp_failure")
    assert detail["url"] == url
    assert detail["on_failure"] == "deny"
    assert "error" in detail
    # The signed secret must never appear in the audit detail.
    assert "secret" not in detail


@pytest.mark.asyncio
@http_mock.mock
async def test_webhook_idp_no_secret_sends_no_signing_headers():
    """When no secret is configured, no signing headers are sent."""
    url = "https://auth.example.com/resolve"
    idp = WebhookIdentityProvider(url=url)

    route = http_mock.post(url).mock(
        return_value=httpx2.Response(
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
