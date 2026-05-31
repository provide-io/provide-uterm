#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the assert_webhook_target_allowed egress guard and its integration
with governance/IDP/authz webhook senders."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import respx
from httpx import Response

# Capture the real _resolve_cached at import time, before any fixture can replace it.
# Tests that need to exercise the real caching logic restore the module attribute to
# this reference before running.
import provide.uterm.server.egress as _egress_mod_for_capture

_REAL_RESOLVE_CACHED = _egress_mod_for_capture._resolve_cached

# ---------------------------------------------------------------------------
# Unit tests: assert_webhook_target_allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_literal_ip_blocked() -> None:
    """A literal metadata IP in the webhook URL must raise EgressBlockedError."""
    from provide.uterm.server.egress import EgressBlockedError, assert_webhook_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_webhook_target_allowed("https://169.254.169.254/hook")


@pytest.mark.asyncio
async def test_metadata_alicloud_ip_blocked() -> None:
    """Alibaba Cloud metadata literal IP is blocked."""
    from provide.uterm.server.egress import EgressBlockedError, assert_webhook_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_webhook_target_allowed("https://100.100.100.200/hook")


@pytest.mark.asyncio
async def test_metadata_ipv6_literal_blocked() -> None:
    """IPv6 metadata literal IP fd00:ec2::254 is blocked."""
    from provide.uterm.server.egress import EgressBlockedError, assert_webhook_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_webhook_target_allowed("https://[fd00:ec2::254]/hook")


@pytest.mark.asyncio
async def test_dns_resolving_to_metadata_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that DNS-rebinds to a metadata IP must be blocked."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.egress import EgressBlockedError, assert_webhook_target_allowed

    monkeypatch.setattr(egress_mod, "_resolve_cached", AsyncMock(return_value=("169.254.169.254",)))
    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_webhook_target_allowed("https://evil.example.com/hook")


@pytest.mark.asyncio
async def test_benign_host_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname resolving to a public IP must NOT raise."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.egress import assert_webhook_target_allowed

    monkeypatch.setattr(egress_mod, "_resolve_cached", AsyncMock(return_value=("93.184.216.34",)))
    # Must not raise
    await assert_webhook_target_allowed("https://example.com/hook")


@pytest.mark.asyncio
async def test_private_host_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname resolving to a private IP is NOT blocked (policy engines may be internal)."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.egress import assert_webhook_target_allowed

    monkeypatch.setattr(egress_mod, "_resolve_cached", AsyncMock(return_value=("192.168.1.100",)))
    # Must not raise — private is allowed for webhook targets
    await assert_webhook_target_allowed("https://internal-policy.corp/hook")


@pytest.mark.asyncio
async def test_no_host_in_url_no_raise() -> None:
    """A URL with no parseable host must not raise (guard is a no-op)."""
    from provide.uterm.server.egress import assert_webhook_target_allowed

    # urlparse of a bare path returns empty hostname
    await assert_webhook_target_allowed("not-a-url-at-all")


@pytest.mark.asyncio
async def test_cache_prevents_second_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call within TTL must use the cache and not call the underlying resolver again.

    The autouse _stub_egress_resolver fixture patches _resolve_cached itself; this test
    needs to exercise the real _resolve_cached implementation, so we restore it (using
    _REAL_RESOLVE_CACHED captured at import time) and patch _resolve_host (the inner
    resolver) to count DNS calls.
    """

    import provide.uterm.server.egress as egress_mod

    # Restore the real _resolve_cached (autouse replaced it with a mock).
    # Use the reference captured at module import time, before any fixture ran.
    monkeypatch.setattr(egress_mod, "_resolve_cached", _REAL_RESOLVE_CACHED)
    # Patch _resolve_host directly to count real resolver calls
    resolver_mock = AsyncMock(return_value=("93.184.216.34",))
    monkeypatch.setattr(egress_mod, "_resolve_host", resolver_mock)
    # Clear the cache so we start fresh
    egress_mod._resolve_cache.clear()

    # First call populates cache
    result1 = await egress_mod._resolve_cached("example.com")
    # Second call within TTL must hit the cache
    result2 = await egress_mod._resolve_cached("example.com")

    assert result1 == ("93.184.216.34",)
    assert result2 == ("93.184.216.34",)
    assert resolver_mock.call_count == 1, "resolver should only be called once within TTL"


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """After TTL elapses the cache is bypassed and the resolver is called again.

    Same reasoning as test_cache_prevents_second_resolve: restore the real
    _resolve_cached so we can test the TTL expiry branch.
    """
    import time

    import provide.uterm.server.egress as egress_mod

    monkeypatch.setattr(egress_mod, "_resolve_cached", _REAL_RESOLVE_CACHED)
    resolver_mock = AsyncMock(return_value=("93.184.216.34",))
    monkeypatch.setattr(egress_mod, "_resolve_host", resolver_mock)
    egress_mod._resolve_cache.clear()

    # Populate cache with a timestamp in the past (beyond TTL)
    egress_mod._resolve_cache["stale.example.com"] = (
        time.time() - egress_mod._EGRESS_DNS_TTL_S - 1,
        ("1.2.3.4",),
    )

    result = await egress_mod._resolve_cached("stale.example.com")
    assert result == ("93.184.216.34",)
    assert resolver_mock.call_count == 1


# ---------------------------------------------------------------------------
# Gate integration: EgressBlockedError → fail-closed behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_policy_gate_metadata_url_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """WebhookPolicyGate with a metadata-IP URL must return deny (EgressBlockedError caught)."""
    from provide.uterm.server.bridge.hub.ext import PolicyContext, WebhookPolicyGate

    gate = WebhookPolicyGate(url="https://169.254.169.254/policy")
    ctx = PolicyContext(worker_id="w1", client_id="alice")
    result = await gate.intercept_input("ls", ctx)
    assert result.action == "deny"


@pytest.mark.asyncio
async def test_webhook_fanout_gate_metadata_url_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """WebhookFanOutPolicyGate with a metadata-IP URL must return deny."""
    from provide.uterm.server.bridge.hub.ext import PolicyContext, WebhookFanOutPolicyGate

    gate = WebhookFanOutPolicyGate(url="https://169.254.169.254/fanout")
    ctx = PolicyContext(worker_id="w1")
    result = await gate.intercept_fanout("reboot", ctx)
    assert result.action == "deny"


@pytest.mark.asyncio
async def test_webhook_behavioral_audit_gate_metadata_url_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """WebhookBehavioralAuditGate with a metadata-IP URL must return deny (fail_open=False)."""
    from provide.uterm.server.bridge.hub.ext import (
        BehavioralThresholds,
        ConnectionHeuristics,
        PolicyContext,
        WebhookBehavioralAuditGate,
    )

    gate = WebhookBehavioralAuditGate(url="https://169.254.169.254/audit", fail_open=False)
    ctx = PolicyContext(worker_id="w1")
    heur = ConnectionHeuristics(cps=10.0, jitter=0.1, timestamp=1.0)
    thresh = BehavioralThresholds()
    result = await gate.audit_connection(heur, ctx, thresh)
    assert result.action == "deny"


@pytest.mark.asyncio
async def test_webhook_behavioral_audit_gate_metadata_url_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """WebhookBehavioralAuditGate fail_open=True must still return allow when egress blocked."""
    from provide.uterm.server.bridge.hub.ext import (
        BehavioralThresholds,
        ConnectionHeuristics,
        PolicyContext,
        WebhookBehavioralAuditGate,
    )

    gate = WebhookBehavioralAuditGate(url="https://169.254.169.254/audit", fail_open=True)
    ctx = PolicyContext(worker_id="w1")
    heur = ConnectionHeuristics(cps=10.0, jitter=0.1, timestamp=1.0)
    thresh = BehavioralThresholds()
    result = await gate.audit_connection(heur, ctx, thresh)
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_webhook_output_policy_gate_metadata_url_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """WebhookOutputPolicyGate with a metadata-IP URL must return [] (EgressBlockedError caught)."""
    from provide.uterm.server.bridge.hub.ext import PolicyContext, WebhookOutputPolicyGate

    gate = WebhookOutputPolicyGate(url="https://169.254.169.254/output")
    ctx = PolicyContext(worker_id="w1")
    rules = await gate.get_redaction_rules(ctx)
    assert rules == []


@pytest.mark.asyncio
async def test_webhook_idp_metadata_url_returns_none_on_deny() -> None:
    """WebhookIdentityProvider with metadata-IP URL must return None (deny path)."""
    from provide.uterm.server.auth import WebhookIdentityProvider

    idp = WebhookIdentityProvider(url="https://169.254.169.254/idp", on_failure="deny")

    class MockConn:
        headers: dict = {}
        cookies: dict = {}

    result = await idp.resolve_principal(MockConn())
    assert result is None


@pytest.mark.asyncio
async def test_webhook_idp_metadata_url_fail_open_returns_viewer() -> None:
    """WebhookIdentityProvider with metadata-IP URL and on_failure='viewer' must return anonymous viewer."""
    from provide.uterm.server.auth import WebhookIdentityProvider

    idp = WebhookIdentityProvider(url="https://169.254.169.254/idp", on_failure="viewer")

    class MockConn:
        headers: dict = {}
        cookies: dict = {}

    result = await idp.resolve_principal(MockConn())
    assert result is not None
    assert result.subject_id == "anonymous"
    assert "viewer" in result.roles


@pytest.mark.asyncio
async def test_webhook_authz_check_metadata_url_returns_false() -> None:
    """WebhookAuthorizationProvider._check with metadata-IP URL must return False."""
    from unittest.mock import MagicMock

    from provide.uterm.server.authorization import WebhookAuthorizationProvider

    provider = WebhookAuthorizationProvider(url="https://169.254.169.254/authz")
    principal = MagicMock()
    principal.subject_id = "alice"
    principal.roles = ["admin"]
    principal.scopes = ["*"]
    principal.claims = {}

    result = await provider._check(principal, "session.read")
    assert result is False


@pytest.mark.asyncio
async def test_webhook_authz_capabilities_metadata_url_returns_empty() -> None:
    """WebhookAuthorizationProvider.capabilities_for with metadata-IP URL must return frozenset()."""
    from unittest.mock import MagicMock

    from provide.uterm.server.authorization import WebhookAuthorizationProvider

    provider = WebhookAuthorizationProvider(url="https://169.254.169.254/authz")
    principal = MagicMock()
    principal.subject_id = "alice"

    result = await provider.capabilities_for(principal)
    assert result == frozenset()


@pytest.mark.asyncio
async def test_webhook_authz_resolve_browser_role_metadata_url_returns_viewer() -> None:
    """WebhookAuthorizationProvider.resolve_browser_role with metadata-IP URL must return 'viewer'."""
    from unittest.mock import MagicMock

    from provide.uterm.server.authorization import WebhookAuthorizationProvider

    provider = WebhookAuthorizationProvider(url="https://169.254.169.254/authz")
    principal = MagicMock()
    principal.subject_id = "alice"
    principal.roles = ["admin"]
    session = MagicMock()
    session.session_id = "sess1"

    result = await provider.resolve_browser_role(principal, session)
    assert result == "viewer"


# ---------------------------------------------------------------------------
# Benign-host gate tests: confirm normal POST still happens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_webhook_policy_gate_benign_host_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """WebhookPolicyGate with a benign host still posts normally."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.bridge.hub.ext import PolicyContext, WebhookPolicyGate

    url = "https://gov.example.com/policy"
    monkeypatch.setattr(egress_mod, "_resolve_cached", AsyncMock(return_value=("93.184.216.34",)))

    route = respx.post(url).mock(return_value=Response(200, json={"action": "allow"}))
    gate = WebhookPolicyGate(url=url)
    ctx = PolicyContext(worker_id="w1", client_id="alice")
    result = await gate.intercept_input("ls", ctx)
    assert result.action == "allow"
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_webhook_idp_benign_host_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """WebhookIdentityProvider with a benign host still posts normally."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.auth import WebhookIdentityProvider

    url = "https://auth.example.com/resolve"
    monkeypatch.setattr(egress_mod, "_resolve_cached", AsyncMock(return_value=("93.184.216.34",)))

    route = respx.post(url).mock(return_value=Response(200, json={"subject_id": "user-1", "roles": ["viewer"]}))

    class MockConn:
        headers: dict = {}
        cookies: dict = {}

    idp = WebhookIdentityProvider(url=url)
    result = await idp.resolve_principal(MockConn())
    assert result is not None
    assert result.subject_id == "user-1"
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_webhook_authz_benign_host_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """WebhookAuthorizationProvider with a benign host still posts normally."""
    from unittest.mock import MagicMock

    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.authorization import WebhookAuthorizationProvider

    url = "https://authz.example.com/check"
    monkeypatch.setattr(egress_mod, "_resolve_cached", AsyncMock(return_value=("93.184.216.34",)))

    route = respx.post(url).mock(return_value=Response(200, json={"allow": True}))

    provider = WebhookAuthorizationProvider(url=url)
    principal = MagicMock()
    principal.subject_id = "alice"
    principal.roles = ["admin"]
    principal.scopes = ["*"]
    principal.claims = {}

    result = await provider._check(principal, "session.read")
    assert result is True
    assert route.called
