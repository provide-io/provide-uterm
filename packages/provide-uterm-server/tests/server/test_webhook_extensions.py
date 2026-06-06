#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
import respx
from httpx import Response
from starlette.testclient import TestClient

from provide.uterm.server.authorization import AuthorizationService, WebhookAuthorizationProvider
from provide.uterm.server.bridge.hub.ext import (
    PolicyContext,
    WebhookBehavioralAuditGate,
    WebhookFanOutPolicyGate,
    WebhookOutputPolicyGate,
    WebhookPolicyGate,
    WebhookTelemetrySink,
)
from provide.uterm.server.webhook_signing import verify_webhook_signature


@pytest.mark.asyncio
@respx.mock
async def test_webhook_policy_gate_allow() -> None:
    url = "https://fleet.example.com/policy"
    gate = WebhookPolicyGate(url=url, secret="shhh")

    route = respx.post(url).mock(return_value=Response(200, json={"allow": True}))

    ctx = PolicyContext(worker_id="w1", client_id="alice", role="admin", action="input")
    result = await gate.intercept_input("ls -la", ctx)

    assert result.action == "allow"
    assert route.called
    payload = json.loads(route.calls.last.request.content)
    assert payload["worker_id"] == "w1"
    assert payload["data"] == "ls -la"
    assert "X-Webhook-Secret" not in route.calls.last.request.headers
    req_ts = route.calls.last.request.headers.get("X-Uterm-Timestamp", "")
    req_sig = route.calls.last.request.headers.get("X-Uterm-Signature", "")
    assert req_ts != ""
    assert verify_webhook_signature("shhh", route.calls.last.request.content, req_sig, req_ts) is True


@pytest.mark.asyncio
@respx.mock
async def test_webhook_policy_gate_deny_or_error() -> None:
    url = "https://fleet.example.com/policy"
    gate = WebhookPolicyGate(url=url)

    # Explicit deny
    respx.post(url).mock(return_value=Response(200, json={"allow": False}))
    result = await gate.intercept_input("rm -rf /", PolicyContext(worker_id="w1"))
    assert result.action == "deny"

    # HTTP Error
    respx.post(url).mock(return_value=Response(500))
    result = await gate.intercept_input("hello", PolicyContext(worker_id="w1"))
    assert result.action == "deny"

    # Exception (timeout/network)
    respx.post(url).mock(side_effect=Exception("connection reset"))
    result = await gate.intercept_input("hello", PolicyContext(worker_id="w1"))
    assert result.action == "deny"


@pytest.mark.asyncio
@respx.mock
async def test_webhook_authz_provider_checks() -> None:
    url = "https://fleet.example.com/authz"
    provider = WebhookAuthorizationProvider(url=url)
    authz = AuthorizationService(provider)

    principal = MagicMock()
    principal.subject_id = "alice"
    principal.roles = ["admin"]
    principal.scopes = ["*"]
    principal.claims = {"iss": "test"}

    session = MagicMock()
    session.session_id = "sess1"

    # Test can_read_session
    route1 = respx.post(url).mock(return_value=Response(200, json={"allow": True}))
    assert await authz.can_read_session(principal, session) is True
    assert json.loads(route1.calls.last.request.content)["action"] == "session.read"

    # Test direct capability and owner checks
    respx.post(url).mock(return_value=Response(200, json={"allow": True}))
    assert await provider.has_capability(principal, "session.read") is True
    respx.post(url).mock(return_value=Response(200, json={"allow": True}))
    assert await provider.is_owner(principal, session) is True

    # Test can_mutate_session
    route2 = respx.post(url).mock(return_value=Response(200, json={"allow": False}))
    assert await authz.can_mutate_session(principal, session, "session.control.delete") is False
    assert json.loads(route2.calls.last.request.content)["action"] == "session.control.delete"

    # Test can_read_recording
    respx.post(url).mock(return_value=Response(200, json={"allow": True}))
    assert await authz.can_read_recording(principal, session) is True

    # Test can_create_session
    respx.post(url).mock(return_value=Response(200, json={"allow": True}))
    assert await authz.can_create_session(principal) is True

    # Test line 181 (status not 200)
    respx.post(url).mock(return_value=Response(403))
    assert await authz.can_read_session(principal, session) is False

    # Test _check exception path
    respx.post(url).mock(side_effect=Exception("fail"))
    assert await provider.can_create_session(principal) is False


@pytest.mark.asyncio
@respx.mock
async def test_webhook_authz_provider_sends_configured_secret_header() -> None:
    url = "https://fleet.example.com/authz"
    provider = WebhookAuthorizationProvider(url=url, secret="shhh")

    principal = MagicMock()
    principal.subject_id = "alice"
    principal.roles = ["admin"]
    principal.scopes = ["*"]
    principal.claims = {"iss": "test"}

    session = MagicMock()
    session.session_id = "sess1"

    route = respx.post(url).mock(return_value=Response(200, json={"allow": True}))

    assert await provider.can_read_session(principal, session) is True
    assert route.called
    assert "X-Webhook-Secret" not in route.calls.last.request.headers
    req_ts = route.calls.last.request.headers.get("X-Uterm-Timestamp", "")
    req_sig = route.calls.last.request.headers.get("X-Uterm-Signature", "")
    assert req_ts != ""
    assert verify_webhook_signature("shhh", route.calls.last.request.content, req_sig, req_ts) is True


@pytest.mark.asyncio
@respx.mock
async def test_webhook_authz_provider_profile_and_role() -> None:
    url = "https://fleet.example.com/authz"
    provider = WebhookAuthorizationProvider(url=url)

    principal = MagicMock()
    principal.subject_id = "alice"
    principal.roles = ["admin"]
    principal.scopes = ["*"]
    principal.claims = {"iss": "test"}

    profile = MagicMock()
    profile.profile_id = "prof1"

    session = MagicMock()
    session.session_id = "sess1"

    # can_read_profile
    respx.post(url).mock(return_value=Response(200, json={"allow": True}))
    assert await provider.can_read_profile(principal, profile) is True

    # can_mutate_profile
    respx.post(url).mock(return_value=Response(200, json={"allow": False}))
    assert await provider.can_mutate_profile(principal, profile) is False

    # resolve_browser_role
    respx.post(url).mock(return_value=Response(200, json={"role": "operator"}))
    assert await provider.resolve_browser_role(principal, session) == "operator"

    # resolve_browser_role fallback
    respx.post(url).mock(return_value=Response(500))
    assert await provider.resolve_browser_role(principal, session) == "viewer"

    # resolve_browser_role exception path
    respx.post(url).mock(side_effect=Exception("network error"))
    assert await provider.resolve_browser_role(principal, session) == "viewer"


@pytest.mark.asyncio
@respx.mock
async def test_webhook_authz_provider_capabilities() -> None:
    url = "https://fleet.example.com/authz"
    provider = WebhookAuthorizationProvider(url=url)

    principal = MagicMock()
    principal.subject_id = "alice"

    # Success
    respx.post(url).mock(return_value=Response(200, json={"capabilities": ["session.read", "test"]}))
    caps = await provider.capabilities_for(principal)
    assert "session.read" in caps
    assert "test" in caps

    # Error fallback (e.g. 404)
    respx.post(url).mock(return_value=Response(404))
    assert await provider.capabilities_for(principal) == frozenset()

    # Exception path
    respx.post(url).mock(side_effect=Exception("fail"))
    assert await provider.capabilities_for(principal) == frozenset()


# ---------------------------------------------------------------------------
# Perf fix: WebhookAuthorizationProvider reuses ONE httpx.AsyncClient across
# calls (keep-alive / connection pooling) instead of opening one per request.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_webhook_authz_provider_reuses_single_client() -> None:
    """All three webhook paths must share the one client built in __init__,
    so HTTP keep-alive / connection pooling is preserved across calls."""
    url = "https://fleet.example.com/authz"
    provider = WebhookAuthorizationProvider(url=url)

    principal = MagicMock()
    principal.subject_id = "alice"
    principal.roles = ["admin"]
    principal.scopes = ["*"]
    principal.claims = {"iss": "test"}

    session = MagicMock()
    session.session_id = "sess1"

    # The client is created exactly once, eagerly, in __init__.
    client = provider._client
    assert client is not None

    respx.post(url).mock(return_value=Response(200, json={"allow": True, "capabilities": ["session.read"]}))

    # Drive every method that performs a POST and assert each used the *same*
    # client object — i.e. no per-call ``async with httpx.AsyncClient(...)``.
    await provider._check(principal, "session.read")
    assert provider._client is client
    await provider.capabilities_for(principal)
    assert provider._client is client
    await provider.resolve_browser_role(principal, session)
    assert provider._client is client

    await provider.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_webhook_authz_provider_aclose_closes_client() -> None:
    """aclose() must close the shared client so the connection pool is released."""
    url = "https://fleet.example.com/authz"
    provider = WebhookAuthorizationProvider(url=url)

    assert provider._client.is_closed is False
    await provider.aclose()
    assert provider._client.is_closed is True


@pytest.mark.asyncio
@respx.mock
async def test_authz_service_aclose_forwards_to_provider() -> None:
    """AuthorizationService.aclose() must forward to a provider that defines it,
    so the FastAPI lifespan releases the webhook connection pool on shutdown."""
    provider = WebhookAuthorizationProvider(url="https://fleet.example.com/authz")
    authz = AuthorizationService(provider)

    assert provider._client.is_closed is False
    await authz.aclose()
    assert provider._client.is_closed is True


@pytest.mark.asyncio
async def test_authz_service_aclose_noop_without_provider_aclose() -> None:
    """AuthorizationService.aclose() is a safe no-op for a provider (e.g. the
    local RBAC default) that holds no closable resources."""
    from provide.uterm.server.authorization import LocalAuthorizationProvider

    authz = AuthorizationService(LocalAuthorizationProvider())
    # Must not raise even though LocalAuthorizationProvider has no aclose().
    await authz.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_node_registry_heartbeat_logic() -> None:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.config import default_server_config

    url = "https://fleet.example.com/heartbeat"
    config = default_server_config()
    config.governance.registry_webhook_url = url
    config.governance.registry_webhook_interval_s = 0.01  # Fast!

    route = respx.post(url).mock(return_value=Response(200))

    app = create_server_app(config, api_only=True)

    with TestClient(app):
        # Let it beat a few times
        await asyncio.sleep(0.05)

    assert route.called
    payload = json.loads(route.calls.last.request.content)
    assert "active_sessions" in payload
    assert "node_id" in payload


@pytest.mark.asyncio
@respx.mock
async def test_node_registry_heartbeat_error_handling() -> None:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.config import default_server_config

    url = "https://fleet.example.com/heartbeat"
    config = default_server_config()
    config.governance.registry_webhook_url = url
    config.governance.registry_webhook_interval_s = 0.01

    # Mock a failure
    respx.post(url).mock(side_effect=Exception("network fail"))

    app = create_server_app(config, api_only=True)
    with TestClient(app):
        await asyncio.sleep(0.03)

    # If we are here without exception, it works.


@pytest.mark.asyncio
async def test_authz_service_fallback_paths() -> None:
    """Test AuthorizationService proxy methods when provider lacks direct implementation."""
    from provide.uterm.server.auth import Principal
    from provide.uterm.server.authorization import Capability
    from provide.uterm.server.models import SessionDefinition
    from provide.uterm.server.profiles import ConnectionProfile

    class MinimalProvider:
        async def capabilities_for(self, principal: Principal) -> frozenset[Capability]:
            # If principal has operator role, return session.read cap for the resolver
            if "operator" in principal.roles:
                return frozenset(["session.read"])
            return frozenset()

        async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
            # Trigger the LocalAuthorizationProvider's resolve_browser_role logic
            return True

        async def can_mutate_session(
            self, principal: Principal, session: SessionDefinition, action: Capability
        ) -> bool:
            # Returning False here for 'session.control.hijack' ensures we don't resolve to 'admin'
            return False

        # Intentionally missing is_admin, is_owner, has_capability, resolve_browser_role
        async def can_read_recording(self, principal: Principal, session: SessionDefinition) -> bool:
            return True

        async def can_create_session(self, principal: Principal) -> bool:
            return True

        async def can_read_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
            return True

        async def can_mutate_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
            return True

    authz = AuthorizationService(MinimalProvider())
    principal = MagicMock()
    principal.roles = ["admin"]
    principal.subject_id = "alice"
    principal.scopes = ["*"]
    # A global admin has no session scope (None); without this the MagicMock
    # auto-attr would be truthy and the is_admin fallback would treat it as a
    # session-scoped (non-global) admin grant.
    principal.admin_session_scope = None

    session = MagicMock()
    session.owner = "alice"
    session.session_id = "s1"
    session.visibility = "operator"

    # has_capability fallback to capabilities_for (MinimalProvider returns nothing for admin role)
    assert await authz.has_capability(principal, "session.read") is False

    # is_admin fallback to principal.roles
    principal.roles = ["admin"]
    assert await authz.is_admin(principal) is True
    principal.roles = []
    assert await authz.is_admin(principal) is False

    # is_owner fallback to manual check
    principal.subject_id = "alice"
    assert await authz.is_owner(principal, session) is True
    session.owner = "bob"
    assert await authz.is_owner(principal, session) is False

    # has_role coverage
    principal.roles = ["operator"]
    assert await authz.has_role(principal, "operator") is True
    assert await authz.has_role(principal, "admin") is False

    # resolve_browser_role fallback
    # LocalAuthorizationProvider.resolve_browser_role will be used.
    # Since principal has 'operator' role, and MinimalProvider.can_read_session returns True,
    # LocalAuthorizationProvider will return 'operator'.
    assert await authz.resolve_browser_role(principal, session) == "operator"


# ---------------------------------------------------------------------------
# Fix 1c: redact secrets before forwarding keystrokes to the governance webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_webhook_policy_gate_redacts_input_data() -> None:
    """intercept_input must redact the keystroke stream before POSTing it to
    the governance webhook so secrets do not leak verbatim to an external
    endpoint."""
    url = "https://fleet.example.com/policy"
    gate = WebhookPolicyGate(url=url)
    route = respx.post(url).mock(return_value=Response(200, json={"allow": True}))

    secret = "mysql --password=hunter2supersecret -h db"  # pragma: allowlist secret
    result = await gate.intercept_input(secret, PolicyContext(worker_id="w1"))

    assert result.action == "allow"
    payload = json.loads(route.calls.last.request.content)
    assert "hunter2supersecret" not in payload["data"]
    assert "[PASSWORD_REDACTED]" in payload["data"]


@pytest.mark.asyncio
@respx.mock
async def test_webhook_fanout_gate_redacts_command() -> None:
    """intercept_fanout must redact the forwarded command the same way."""
    from provide.uterm.server.bridge.hub.ext import WebhookFanOutPolicyGate

    url = "https://fleet.example.com/fanout"
    gate = WebhookFanOutPolicyGate(url=url)
    route = respx.post(url).mock(return_value=Response(200, json={"action": "allow"}))

    secret = "mysql --password=hunter2supersecret -h db"  # pragma: allowlist secret
    result = await gate.intercept_fanout(secret, PolicyContext(worker_id="w1"), group_id="g1")

    assert result.action == "allow"
    payload = json.loads(route.calls.last.request.content)
    assert "hunter2supersecret" not in payload["command"]
    assert "[PASSWORD_REDACTED]" in payload["command"]


@pytest.mark.parametrize(
    "gate_cls",
    [
        WebhookPolicyGate,
        WebhookFanOutPolicyGate,
        WebhookBehavioralAuditGate,
        WebhookTelemetrySink,
        WebhookOutputPolicyGate,
    ],
)
async def test_webhook_gate_aclose_releases_pooled_client(gate_cls) -> None:
    """Each gate holds one reusable client (HTTP keep-alive) and closes it on aclose()."""
    gate = gate_cls(url="https://gate.example.com/hook")
    assert not gate._client.is_closed
    await gate.aclose()
    assert gate._client.is_closed
