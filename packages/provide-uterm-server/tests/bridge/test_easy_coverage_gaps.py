#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted unit tests covering single-line gaps surfaced by the coverage
report (the "easy wins" set).

Each test docstring names the file:line it covers so future report-vs-test
correlation is obvious.
"""

from __future__ import annotations

import logging

import pytest


# ---------------------------------------------------------------------------
# bridge/hub/core.py:180 — set_worker_hello_mode rejects invalid input mode
# ---------------------------------------------------------------------------


async def test_set_worker_hello_mode_rejects_invalid_mode() -> None:
    from provide.uterm.bridge.hub import TermHub

    hub = TermHub()
    with pytest.raises(ValueError, match="invalid input mode"):
        await hub.set_worker_hello_mode("nobody", "bogus-mode")


# ---------------------------------------------------------------------------
# bridge/hub/connections.py:205 — legacy protocol_version warning log
# ---------------------------------------------------------------------------


async def test_worker_hello_logs_warning_for_legacy_protocol(caplog: pytest.LogCaptureFixture) -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.bridge.models import WorkerTermState

    hub = TermHub()
    hub._workers["w1"] = WorkerTermState(worker_ws=AsyncMock())
    caplog.set_level(logging.WARNING, logger="provide.uterm.bridge.hub.connections")
    await hub.set_worker_hello("w1", "open", protocol_version=0)
    assert any("worker_hello_legacy_protocol" in r.getMessage() for r in caplog.records), (
        "set_worker_hello with protocol_version<1 must log worker_hello_legacy_protocol warning"
    )


# ---------------------------------------------------------------------------
# bridge/hub/semantics.py:16 — CommandSplitter.split("") -> []
# ---------------------------------------------------------------------------


def test_command_splitter_empty_returns_empty_list() -> None:
    from provide.uterm.bridge.hub.semantics import CommandSplitter

    assert CommandSplitter().split("") == []


# ---------------------------------------------------------------------------
# server/connectors/__init__.py:38 — __getattr__ raises AttributeError for unknown
# ---------------------------------------------------------------------------


def test_connectors_module_getattr_unknown_raises() -> None:
    from provide.uterm.server import connectors

    with pytest.raises(AttributeError, match="has no attribute"):
        _ = connectors.NoSuchSymbol  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# server/discovery.py:33 — NoOpDiscoveryProvider.announce is a no-op
# ---------------------------------------------------------------------------


async def test_noop_discovery_provider_announce_is_noop() -> None:
    from provide.uterm.server.discovery import NoOpDiscoveryProvider, NodeStatus

    provider = NoOpDiscoveryProvider()
    status = NodeStatus(node_id="n1", active_sessions=0, worker_count=0, timestamp=0.0)
    # Must accept the argument and return None without side effects or raise.
    assert await provider.announce(status) is None


# ---------------------------------------------------------------------------
# server/app/auth.py:114 — placeholder jwt_public_key_pem raises in jwt mode
# ---------------------------------------------------------------------------


def test_validate_auth_config_rejects_placeholder_jwt_public_key() -> None:
    from provide.uterm.server.app.auth import _validate_auth_config
    from provide.uterm.server.models import AuthConfig, ServerBindConfig, ServerConfig

    # Placeholder check only runs in "production-like" mode (non-loopback
    # host or require_jwt_in_production=True); 127.0.0.1 would short-circuit.
    config = ServerConfig(
        server=ServerBindConfig(host="0.0.0.0"),  # noqa: S104 — non-loopback to trip prod-mode validation
        auth=AuthConfig(
            mode="jwt",
            jwt_public_key_pem="changeme",  # known placeholder marker
            jwt_algorithms=["HS256"],
            worker_bearer_token="real-bearer-token-32-chars-long-x",
        ),
    )
    with pytest.raises(ValueError, match="placeholder value"):
        _validate_auth_config(config)


# ---------------------------------------------------------------------------
# server/runtime.py:206 — get_recording_path delegates to the recording store
# ---------------------------------------------------------------------------


async def test_hosted_runtime_get_recording_path_returns_store_result(tmp_path) -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.server.models import SessionDefinition
    from provide.uterm.server.runtime import HostedSessionRuntime

    session = SessionDefinition(session_id="s1", display_name="s1", connector_type="shell")
    runtime = HostedSessionRuntime.__new__(HostedSessionRuntime)
    runtime.definition = session
    fake_path = tmp_path / "s1.jsonl"
    runtime._recording_store = AsyncMock()  # type: ignore[attr-defined]
    runtime._recording_store.get_path = AsyncMock(return_value=fake_path)
    result = await runtime.get_recording_path()
    assert result == fake_path
    runtime._recording_store.get_path.assert_awaited_once_with("s1")


# ---------------------------------------------------------------------------
# bridge/hub/ownership.py:57-60 — peek_expiry returns (browser, rest) tuple
# ---------------------------------------------------------------------------


def test_ownership_compute_lease_expirations_reports_both_expired() -> None:
    import time as _time
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub.ownership import _OwnershipMixin
    from provide.uterm.bridge.models import HijackSession, WorkerTermState

    now = _time.monotonic()
    state = WorkerTermState(worker_ws=AsyncMock())
    state.hijack_session = HijackSession(
        hijack_id="h1",
        owner="o",
        acquired_at=now - 100,
        lease_expires_at=now - 1,  # rest expired
        last_heartbeat=now,
    )
    state.hijack_owner = AsyncMock()
    state.hijack_owner_expires_at = now - 1  # browser expired
    browser_expired, rest_expired = _OwnershipMixin._compute_lease_expirations(state, now)
    assert browser_expired is True
    assert rest_expired is True


# ---------------------------------------------------------------------------
# bridge/hub/approvals.py:70 — cleanup_expired awaits an async on_expired
# callback when the registered handler returns a coroutine.
# ---------------------------------------------------------------------------


async def test_in_memory_approval_store_awaits_async_on_expired() -> None:
    import time as _time

    from provide.uterm.bridge.hub.approvals import (
        ApprovalRequest,
        ApprovalStatus,
        InMemoryApprovalStore,
    )

    store = InMemoryApprovalStore()
    called_with: list[str] = []

    async def _async_on_expired(req_id: str) -> None:
        called_with.append(req_id)

    store.on_expired = _async_on_expired
    store.add(
        ApprovalRequest(
            id="r1",
            worker_id="w1",
            submitter_id="alice",
            command="ls",
            status=ApprovalStatus.PENDING,
            created_at=_time.time() - 100,
            expires_at=_time.time() - 1,  # already expired
        ),
    )
    await store.cleanup_expired()
    assert called_with == ["r1"], "async on_expired callback must be awaited"
    assert store.get("r1").status == ApprovalStatus.TIMEOUT


# ---------------------------------------------------------------------------
# server/routes/approvals.py:44, 59, 73, 76 — error paths in approve/reject:
# missing principal (401), no-such-request (404), already-resolved (400).
# ---------------------------------------------------------------------------


async def test_approvals_router_error_paths() -> None:
    import time as _time
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from provide.uterm.bridge.hub.approvals import (
        ApprovalRequest,
        ApprovalStatus,
        InMemoryApprovalStore,
    )
    from provide.uterm.server.routes.approvals import create_approvals_router

    router = create_approvals_router()
    approve = next(r for r in router.routes if "/approve" in r.path).endpoint
    reject = next(r for r in router.routes if "/reject" in r.path).endpoint

    # 401 — missing principal (covers line 44)
    req_unauth = MagicMock()
    req_unauth.state.uterm_principal = None
    with pytest.raises(HTTPException) as exc:
        await approve("r1", req_unauth)
    assert exc.value.status_code == 401

    # 404 — request not found (covers line 56)
    store = InMemoryApprovalStore()
    hub = MagicMock()
    hub._approval_store = store

    async def _is_admin(_p: object) -> bool:
        return True

    authz = MagicMock()
    authz.is_admin = _is_admin

    req_admin = MagicMock()
    req_admin.state.uterm_principal = MagicMock()
    req_admin.app.state.uterm_hub = hub
    req_admin.app.state.uterm_authz = authz

    with pytest.raises(HTTPException) as exc:
        await approve("missing", req_admin)
    assert exc.value.status_code == 404

    # 400 — already-resolved request on approve (covers line 59) and reject (76)
    already = ApprovalRequest(
        id="r2",
        worker_id="w1",
        submitter_id="alice",
        command="ls",
        status=ApprovalStatus.APPROVED,  # already terminal
        created_at=_time.time(),
        expires_at=_time.time() + 60,
    )
    store.add(already)
    with pytest.raises(HTTPException) as exc:
        await approve("r2", req_admin)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await reject("r2", req_admin)
    assert exc.value.status_code == 400

    # Reject's 404 path (covers line 73)
    with pytest.raises(HTTPException) as exc:
        await reject("nope", req_admin)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# bridge/hub/ext.py — three webhook gates (FanOut, BehavioralAudit, Output)
# plus their NoOp counterparts; covers lines 102-104, 112-127, 184, 201-215,
# 232, 239-241, 244-259.
# ---------------------------------------------------------------------------


async def test_noop_fanout_audit_output_gates_allow_by_default() -> None:
    import time as _time

    from provide.uterm.bridge.hub.ext import (
        BehavioralThresholds,
        ConnectionHeuristics,
        NoOpBehavioralAuditGate,
        NoOpOutputPolicyGate,
        PolicyContext,
    )

    ctx = PolicyContext(worker_id="w", client_id="c", role="admin", metadata={})
    audit = await NoOpBehavioralAuditGate().audit_connection(
        ConnectionHeuristics(cps=0.0, jitter=0.0, timestamp=_time.time()),
        ctx,
        BehavioralThresholds(),
    )
    assert audit.action == "allow"
    assert await NoOpOutputPolicyGate().get_redaction_rules(ctx) == []


async def test_webhook_fanout_gate_handles_200_non_200_and_exception() -> None:
    import httpx
    import respx

    from provide.uterm.bridge.hub.ext import PolicyContext, WebhookFanOutPolicyGate

    ctx = PolicyContext(worker_id="w", client_id="c", role="admin", metadata={"k": "v"})
    gate = WebhookFanOutPolicyGate(url="http://hook.test/fanout", secret="s", timeout_s=1.0)

    # 200 -> decision propagated
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/fanout").mock(
            return_value=httpx.Response(200, json={"action": "deny", "reason": "policy"})
        )
        d = await gate.intercept_fanout("ls", ctx, group_id="g1")
    assert d.action == "deny"
    assert d.reason == "policy"

    # Non-200 -> deny
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/fanout").mock(return_value=httpx.Response(500))
        assert (await gate.intercept_fanout("ls", ctx)).action == "deny"

    # Network exception -> deny (catch-all branch)
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/fanout").mock(side_effect=httpx.ConnectError("network down"))
        assert (await gate.intercept_fanout("ls", ctx)).action == "deny"


async def test_webhook_behavioral_gate_defaults_to_allow_on_error() -> None:
    import time as _time

    import httpx
    import respx

    from provide.uterm.bridge.hub.ext import (
        BehavioralThresholds,
        ConnectionHeuristics,
        PolicyContext,
        WebhookBehavioralAuditGate,
    )

    ctx = PolicyContext(worker_id="w", client_id="c", role="admin", metadata={})
    heur = ConnectionHeuristics(cps=0.0, jitter=0.0, timestamp=_time.time())
    thr = BehavioralThresholds()
    gate = WebhookBehavioralAuditGate(url="http://hook.test/audit", secret="s", timeout_s=1.0)

    # 200 -> decision propagated
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/audit").mock(return_value=httpx.Response(200, json={"action": "deny"}))
        assert (await gate.audit_connection(heur, ctx, thr)).action == "deny"

    # Non-200 -> allow (safety default)
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/audit").mock(return_value=httpx.Response(503))
        assert (await gate.audit_connection(heur, ctx, thr)).action == "allow"

    # Exception -> allow
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/audit").mock(side_effect=httpx.ConnectError("network down"))
        assert (await gate.audit_connection(heur, ctx, thr)).action == "allow"


async def test_webhook_output_policy_gate_returns_rules_and_handles_failures() -> None:
    import httpx
    import respx

    from provide.uterm.bridge.hub.ext import PolicyContext, WebhookOutputPolicyGate

    ctx = PolicyContext(worker_id="w", client_id="c", role="admin", metadata={})
    gate = WebhookOutputPolicyGate(url="http://hook.test/output", secret="s", timeout_s=1.0)

    # 200 -> rules parsed
    payload = {"rules": [{"pattern": r"\d+", "replacement": "###"}]}
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/output").mock(return_value=httpx.Response(200, json=payload))
        rules = await gate.get_redaction_rules(ctx)
    assert len(rules) == 1
    assert rules[0].pattern == r"\d+"
    assert rules[0].replacement == "###"

    # Non-200 -> empty list
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/output").mock(return_value=httpx.Response(503))
        assert await gate.get_redaction_rules(ctx) == []

    # Exception -> empty list
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/output").mock(side_effect=httpx.ConnectError("network down"))
        assert await gate.get_redaction_rules(ctx) == []
