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


# ---------------------------------------------------------------------------
# bridge/routes/browser_handlers.py:244-270 — `hold` policy decision branch
# creates an ApprovalRequest, parks the browser, and notifies all browsers
# with an approval_pending frame.
# ---------------------------------------------------------------------------


async def test_handle_input_hold_decision_creates_approval_and_notifies_browsers() -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub import PolicyContext, PolicyDecision, TermHub
    from provide.uterm.bridge.hub.approvals import ApprovalStatus
    from provide.uterm.bridge.routes.browser_handlers import _handle_input

    class HoldGate:
        async def intercept_input(self, _data: str, _context: PolicyContext) -> PolicyDecision:
            return PolicyDecision(action="hold", timeout_s=30)

    hub = TermHub(policy_gate=HoldGate())
    ws_a = AsyncMock()
    ws_b = AsyncMock()
    worker_ws = AsyncMock()

    worker_id = "w1"
    await hub.register_worker(worker_id, worker_ws)
    await hub.register_browser(worker_id, ws_a, "admin")
    await hub.register_browser(worker_id, ws_b, "admin")
    await hub.try_acquire_ws_hijack(worker_id, ws_a)

    await _handle_input(hub, ws_a, worker_id, {"type": "input", "data": "rm -rf /"})

    # An ApprovalRequest must be in the store, PENDING.
    pending = [
        r for r in hub._approval_store._requests.values() if r.status == ApprovalStatus.PENDING
    ]
    assert len(pending) == 1
    assert pending[0].worker_id == worker_id
    assert pending[0].command == "rm -rf /"

    # The submitter browser must be parked.
    assert ws_a in hub._paused_browsers

    # All browsers (including the submitter) must have been sent an
    # approval_pending frame.
    assert ws_a.send_text.called
    assert ws_b.send_text.called

    # The frame body must contain the request_id.
    payload_b = ws_b.send_text.call_args[0][0]
    assert "approval_pending" in payload_b
    assert pending[0].id in payload_b

    # Worker must NOT have received the held input.
    worker_ws.send_text.assert_not_called()


# ---------------------------------------------------------------------------
# bridge/hub/messaging.py:281 — _get_heuristics empty path (no keystrokes yet)
# ---------------------------------------------------------------------------


def test_messaging_get_heuristics_empty_returns_zeros() -> None:
    from provide.uterm.bridge.hub import TermHub

    hub = TermHub()
    # Brand-new browser ref the hub has never seen — no timestamps yet.
    heur = hub._get_heuristics(object())
    assert heur == {"cps": 0.0, "jitter": 0.0}


# ---------------------------------------------------------------------------
# bridge/hub/messaging.py:308-313 — _run_behavioral_audit_loop swallows
# exceptions from _audit_all_browsers so the loop survives.
# ---------------------------------------------------------------------------


async def test_behavioral_audit_loop_swallows_errors(caplog: pytest.LogCaptureFixture) -> None:
    import asyncio as _asyncio
    import logging

    from provide.uterm.bridge.hub import TermHub

    hub = TermHub()
    hub._behavioral_audit_interval_s = 0.01  # type: ignore[attr-defined]

    async def _boom() -> None:
        raise RuntimeError("audit blew up")

    hub._audit_all_browsers = _boom  # type: ignore[assignment]
    caplog.set_level(logging.ERROR, logger="provide.uterm.bridge.hub.messaging")

    task = _asyncio.create_task(hub._run_behavioral_audit_loop())
    try:
        # Wait long enough for at least one tick + the swallowed exception.
        await _asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib_suppress():
            await task
    assert any("behavioral_audit_loop_error" in r.getMessage() for r in caplog.records)


import contextlib as _contextlib  # noqa: E402


def contextlib_suppress():
    """Tiny shim so _asyncio.CancelledError on task cancellation is swallowed."""
    return _contextlib.suppress(BaseException)


# ---------------------------------------------------------------------------
# server/webhooks.py:68-69, 74, 78, 81, 100 — URL & pattern validation edges
# ---------------------------------------------------------------------------


def test_validate_webhook_url_rejects_invalid_scheme_and_metadata_host() -> None:
    from provide.uterm.server.webhooks import validate_webhook_url

    with pytest.raises(ValueError, match="must use http or https"):
        validate_webhook_url("ftp://example.com/hook")
    with pytest.raises(ValueError, match="must include a host"):
        validate_webhook_url("http:///hook")
    with pytest.raises(ValueError, match="host is not allowed"):
        validate_webhook_url("http://metadata.google.internal/hook")
    with pytest.raises(ValueError, match="host is not allowed"):
        validate_webhook_url("http://localhost/hook")
    # ``allow_loopback_destinations`` lets localhost through.
    assert (
        validate_webhook_url("http://localhost/hook", allow_loopback_destinations=True)
        == "http://localhost/hook"
    )


def test_validate_webhook_pattern_rejects_non_string_input() -> None:
    from provide.uterm.server.webhooks import validate_webhook_pattern

    with pytest.raises(ValueError, match="pattern must be a string"):
        validate_webhook_pattern(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# server/webhooks.py:299-300, 311-326, 331-332 — delivery URL allowlist edges
# ---------------------------------------------------------------------------


async def test_delivery_url_allowed_rejects_unparseable_and_disallowed() -> None:
    from provide.uterm.server.webhooks import _delivery_url_allowed

    async def _no_resolve(_host: str) -> tuple[str, ...]:
        return ()

    # urlparse only raises on rare malformed brackets; force the rejection
    # path with a value the validator considers invalid (no host).
    assert await _delivery_url_allowed("http:///nohost", _no_resolve) is False
    # Non-http(s) scheme
    assert await _delivery_url_allowed("ftp://example/", _no_resolve) is False
    # Resolver raises -> False
    async def _boom(_host: str) -> tuple[str, ...]:
        raise OSError("dns down")

    assert await _delivery_url_allowed("http://example/", _boom) is False
    # Resolver returns no addresses -> False
    assert await _delivery_url_allowed("http://example/", _no_resolve) is False


async def test_resolve_host_returns_addresses() -> None:
    from provide.uterm.server.webhooks import _resolve_host

    addrs = await _resolve_host("localhost")
    # At least one of the loopback addresses must be in the result.
    assert any(a.startswith("127.") or a == "::1" for a in addrs)


# ---------------------------------------------------------------------------
# bridge/hub/event_bus.py — small parser / filter gaps
# ---------------------------------------------------------------------------


async def test_event_bus_filters_non_string_screen_via_coercion() -> None:
    """event_bus.py:115-116 — pattern filter coerces non-string screen to str."""
    import asyncio as _asyncio
    import re as _re

    from provide.uterm.bridge.hub.event_bus import EventBus, _Subscription

    bus = EventBus()
    queue: _asyncio.Queue = _asyncio.Queue()
    sub = _Subscription(sub_id="s1", worker_id="w1", queue=queue, event_types=None, pattern=_re.compile(r"123"))
    # screen is a list (non-str); the filter must coerce via str(...) and match.
    bus._deliver(sub, "w1", {"type": "snapshot", "data": {"screen": ["123 found"]}, "seq": 1})
    assert queue.qsize() == 1


def test_event_bus_pattern_safety_char_class_and_counted_quantifier() -> None:
    """event_bus.py:274-275, 300, 310-317 — pattern safety walks [...] and {N,M}."""
    from provide.uterm.bridge.hub.event_bus import _compile_pattern

    # Character class — exercises in_class branch (274-275)
    assert _compile_pattern("[abc]+def") is not None
    # Counted quantifier `{2}` — exercises 300 + _looks_like_counted_quantifier (310-317)
    assert _compile_pattern("a{2}") is not None
    # Counted quantifier `{2,5}` — comma branch
    assert _compile_pattern("a{2,5}") is not None
    # Counted quantifier with empty upper `{2,}` — comma branch with empty right
    assert _compile_pattern("a{2,}") is not None
    # `{not_a_number}` — body is not all digits; treated as literal, returns False
    assert _compile_pattern("a{abc}") is not None
    # `{` with no matching `}` — returns False, treated as literal
    assert _compile_pattern("a{") is not None
    # `{}` empty body — returns False
    assert _compile_pattern("a{}") is not None


# ---------------------------------------------------------------------------
# bridge/hub/state.py — small property + role-mapping gaps
# ---------------------------------------------------------------------------


def test_state_event_bus_setter_assigns_value() -> None:
    """state.py:54 — event_bus.setter writes through to _event_bus."""
    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.bridge.hub.event_bus import EventBus

    hub = TermHub()
    bus = EventBus()
    hub.event_bus = bus
    assert hub._event_bus is bus
    hub.event_bus = None
    assert hub._event_bus is None


async def test_state_prepare_policy_context_role_resolution() -> None:
    """state.py:188-194, 218-225 — claims-driven role mapping (delegate_roles=False)."""
    from unittest.mock import AsyncMock, MagicMock

    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.bridge.identity import Principal

    # delegate_roles=False: claims-driven role mapping.
    hub = TermHub(delegate_roles=False)
    ws = AsyncMock()

    # admin claim -> admin role
    ws.state = MagicMock(uterm_principal=Principal(subject_id="a", roles=frozenset(), claims={"admin": True}))
    ctx = await hub.prepare_policy_context(ws, "w1", action="test")
    assert ctx.role == "admin"

    # operator claim -> operator role (covers 220-221)
    ws.state = MagicMock(uterm_principal=Principal(subject_id="o", roles=frozenset(), claims={"operator": True}))
    ctx = await hub.prepare_policy_context(ws, "w1", action="test")
    assert ctx.role == "operator"

    # No claim -> viewer fallback (covers 224 + 194)
    ws.state = MagicMock(uterm_principal=Principal(subject_id="v", roles=frozenset(), claims={}))
    ctx = await hub.prepare_policy_context(ws, "w1", action="test")
    assert ctx.role == "viewer"


async def test_state_prepare_policy_context_delegated_roles_empty_falls_back_to_viewer() -> None:
    """state.py:214 — delegate_roles=True with empty principal.roles -> viewer."""
    from unittest.mock import AsyncMock, MagicMock

    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.bridge.identity import Principal

    hub = TermHub(delegate_roles=True)
    ws = AsyncMock()
    ws.state = MagicMock(uterm_principal=Principal(subject_id="x", roles=frozenset(), claims={}))
    ctx = await hub.prepare_policy_context(ws, "w1", action="test")
    assert ctx.role == "viewer"


# ---------------------------------------------------------------------------
# server/app/factory.py:414 — _resolve_browser_role returns "operator" when
# no session is registered AND the principal has an operator (but not admin)
# role claim.
# ---------------------------------------------------------------------------


async def test_resolve_browser_role_no_session_operator_principal_returns_operator() -> None:
    from types import SimpleNamespace

    from provide.uterm.server import create_server_app, default_server_config
    from provide.uterm.bridge.identity import Principal

    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    app = create_server_app(cfg)
    hub = app.state.uterm_hub

    mock_ws = SimpleNamespace(
        state=SimpleNamespace(uterm_principal=Principal(subject_id="op", roles=frozenset({"operator"}))),
        headers={},
        cookies={},
        scope={"type": "websocket", "headers": []},
    )
    role = await hub._resolve_browser_role(mock_ws, "no-such-session")
    assert role == "operator"


# ---------------------------------------------------------------------------
# bridge/hub/redaction.py:39-41, 51 — StreamRedactor ignores invalid regex
# rules and short-circuits when no pattern compiled.
# ---------------------------------------------------------------------------


def test_redaction_engine_ignores_invalid_pattern_and_passes_data_when_empty() -> None:
    from provide.uterm.bridge.hub.ext import RedactionRule
    from provide.uterm.bridge.hub.redaction import StreamRedactor

    # Only an invalid regex -> no compiled pattern -> redact() returns input as-is.
    engine = StreamRedactor([RedactionRule(pattern="[unterminated", replacement="X")])
    assert engine.redact("hello world") == "hello world"

    # Valid + invalid mixed -> only the valid one compiles.
    engine = StreamRedactor(
        [
            RedactionRule(pattern="[unterminated", replacement="X"),
            RedactionRule(pattern=r"\d+", replacement="###"),
        ]
    )
    assert engine.redact("a1 b22 c") == "a### b### c"


# ---------------------------------------------------------------------------
# bridge/fanout/_controller.py:226, 238 — release_approved_command returns
# None for unknown request_id and for revoked groups.
# ---------------------------------------------------------------------------


async def test_fanout_release_approved_command_returns_none_for_unknown_request() -> None:
    from unittest.mock import MagicMock

    from provide.uterm.bridge.fanout._controller import FanOutController

    ctrl = FanOutController(hub=MagicMock(), fanout_policy_gate=MagicMock())
    # No pending approval registered -> None (line 226).
    assert await ctrl.release_approved_command("missing-id") is None


# ---------------------------------------------------------------------------
# server/runtime.py:429 — `outcome == "cancelled"` break exits the run loop.
# ---------------------------------------------------------------------------


async def test_classify_run_error_cancelled() -> None:
    import asyncio as _asyncio

    from provide.uterm.server.runtime import _classify_run_error

    assert _classify_run_error(_asyncio.CancelledError()) == "cancelled"


# ---------------------------------------------------------------------------
# gateway/_ssh_handler.py:169-182 — _openssh_blob_for_key handles None,
# missing methods, raising methods, and str/bytes return types.
# ---------------------------------------------------------------------------


def test_openssh_blob_for_key_handles_all_branches() -> None:
    from provide.uterm.gateway._ssh_handler import _openssh_blob_for_key

    # None -> empty bytes (line 170)
    assert _openssh_blob_for_key(None) == b""

    # No matching attribute -> empty bytes
    assert _openssh_blob_for_key(object()) == b""

    # Attribute exists but not callable -> skipped
    class _NonCallable:
        export_public_key = "not-callable"

    assert _openssh_blob_for_key(_NonCallable()) == b""

    # Method raises -> continue to next attr (line 176-177)
    class _Raises:
        def export_public_key(self) -> bytes:
            raise RuntimeError("nope")

        def public_data(self) -> bytes:
            return b"valid-blob"

    assert _openssh_blob_for_key(_Raises()) == b"valid-blob"

    # str return type -> ASCII-encoded (line 180-181)
    class _StrReturn:
        def export_public_key(self) -> str:
            return "ascii-blob"

    assert _openssh_blob_for_key(_StrReturn()) == b"ascii-blob"

    # Method exists but returns neither str nor bytes -> falls through to next attr
    class _BadReturnThenValid:
        def export_public_key(self) -> object:
            return 12345  # not str/bytes — skip

        def public_data(self) -> bytes:
            return b"fallback-blob"

    assert _openssh_blob_for_key(_BadReturnThenValid()) == b"fallback-blob"


# ---------------------------------------------------------------------------
# gateway/_ssh_handler.py:187-196 — _fingerprint_for_key handles None,
# missing get_fingerprint, and exceptions.
# ---------------------------------------------------------------------------


def test_fingerprint_for_key_handles_none_missing_and_exception() -> None:
    from provide.uterm.gateway._ssh_handler import _fingerprint_for_key

    assert _fingerprint_for_key(None) is None  # line 188
    assert _fingerprint_for_key(object()) is None  # no get_fingerprint
    # get_fingerprint exists but isn't callable
    class _NonCallable:
        get_fingerprint = "not-callable"

    assert _fingerprint_for_key(_NonCallable()) is None  # line 191

    class _Raises:
        def get_fingerprint(self, _algo: str) -> str:
            raise RuntimeError("nope")

    assert _fingerprint_for_key(_Raises()) is None  # lines 195-196

    class _Returns:
        def get_fingerprint(self, _algo: str) -> str:
            return "SHA256:abc"

    assert _fingerprint_for_key(_Returns()) == "SHA256:abc"


# ---------------------------------------------------------------------------
# bridge/hub/messaging.py:317-346 — _audit_all_browsers calls the behavioral
# gate per browser and closes the ws when the decision is 'deny'.
# ---------------------------------------------------------------------------


async def test_audit_all_browsers_closes_browser_on_deny() -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub import PolicyContext, PolicyDecision, TermHub
    from provide.uterm.bridge.hub.ext import BehavioralThresholds, ConnectionHeuristics

    class DenyGate:
        async def audit_connection(
            self,
            _heuristics: ConnectionHeuristics,
            _context: PolicyContext,
            _thresholds: BehavioralThresholds,
        ) -> PolicyDecision:
            return PolicyDecision(action="deny", reason="too noisy")

    hub = TermHub(behavioral_audit_gate=DenyGate())
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    browser_ws = AsyncMock()
    await hub.register_browser("w1", browser_ws, "admin")

    await hub._audit_all_browsers()
    # The browser must have been closed with the policy-violation code.
    browser_ws.close.assert_awaited_once()
    kw = browser_ws.close.await_args.kwargs
    assert kw["reason"] == "too noisy"


async def test_resolve_approval_deny_sends_rejection_with_reason() -> None:
    """bridge/hub/approvalflow.py:72-78 — deny path sends REJECTED + reason to browsers."""
    import time as _time
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
    from provide.uterm.bridge.hub.ext import PolicyDecision

    hub = TermHub()
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    browser_ws = AsyncMock()
    await hub.register_browser("w1", browser_ws, "admin")

    # Seed a non-fanout approval request so the deny path falls into the
    # in-place rejection branch (not the fanout-controller branch).
    hub._approval_store.add(
        ApprovalRequest(
            id="req-x",
            worker_id="w1",
            submitter_id="alice",
            command="rm -rf /",
            status=ApprovalStatus.PENDING,
            created_at=_time.time(),
            expires_at=_time.time() + 60,
            is_fanout=False,
        ),
    )

    await hub.resolve_approval("w1", "req-x", PolicyDecision(action="deny", reason="bad call"), "rm -rf /")
    # The browser must have received a text frame containing the reason.
    sent_payloads = [c.args[0] for c in browser_ws.send_text.await_args_list]
    joined = " ".join(sent_payloads)
    assert "REJECTED" in joined
    assert "bad call" in joined


async def test_audit_all_browsers_leaves_allowed_browsers_open() -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub import PolicyContext, PolicyDecision, TermHub
    from provide.uterm.bridge.hub.ext import BehavioralThresholds, ConnectionHeuristics

    class AllowGate:
        async def audit_connection(
            self,
            _heuristics: ConnectionHeuristics,
            _context: PolicyContext,
            _thresholds: BehavioralThresholds,
        ) -> PolicyDecision:
            return PolicyDecision(action="allow")

    hub = TermHub(behavioral_audit_gate=AllowGate())
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    browser_ws = AsyncMock()
    await hub.register_browser("w1", browser_ws, "admin")

    await hub._audit_all_browsers()
    browser_ws.close.assert_not_called()


# ---------------------------------------------------------------------------
# bridge/hub/resume.py — control-plane transaction error paths.
# ---------------------------------------------------------------------------


async def test_resume_run_tx_rolls_back_on_op_exception() -> None:
    """resume.py:153-156 — _run_tx rolls back when the op raises and re-raises."""
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub.resume import ControlPlaneResumeStore

    tx = AsyncMock()
    cp = AsyncMock()
    cp.begin = AsyncMock(return_value=tx)
    cp.token_store = AsyncMock()
    manager = ControlPlaneResumeStore.__new__(ControlPlaneResumeStore)
    manager._control_plane = cp  # type: ignore[attr-defined]

    async def _boom(_store: object) -> None:
        raise RuntimeError("op failed")

    with pytest.raises(RuntimeError, match="op failed"):
        await manager._run_tx(_boom)
    tx.rollback.assert_awaited_once()
    tx.commit.assert_not_called()


async def test_resume_mark_hijack_owner_returns_silently_for_missing_token() -> None:
    """resume.py:213 — mark_hijack_owner returns without raising when token unknown."""
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub.resume import ControlPlaneResumeStore

    tx = AsyncMock()
    store = AsyncMock()
    store.get_resume_token = AsyncMock(return_value=None)
    cp = AsyncMock()
    cp.begin = AsyncMock(return_value=tx)
    cp.token_store = lambda _tx: store
    manager = ControlPlaneResumeStore.__new__(ControlPlaneResumeStore)
    manager._control_plane = cp  # type: ignore[attr-defined]

    # Must not raise.
    await manager.mark_hijack_owner("unknown-token", is_owner=True)
    # No create call because record was None.
    store.create_resume_token.assert_not_called()


async def test_resume_revoke_unknown_token_pops_creation_time() -> None:
    """resume.py:222-223 — revoke clears local creation-time bookkeeping even when
    the token isn't in the durable store."""
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub.resume import ControlPlaneResumeStore

    tx = AsyncMock()
    store = AsyncMock()
    store.get_resume_token = AsyncMock(return_value=None)
    cp = AsyncMock()
    cp.begin = AsyncMock(return_value=tx)
    cp.token_store = lambda _tx: store
    manager = ControlPlaneResumeStore.__new__(ControlPlaneResumeStore)
    manager._control_plane = cp  # type: ignore[attr-defined]
    manager._created_at_mono = {"stale-token": 0.0}  # type: ignore[attr-defined]

    await manager.revoke("stale-token")
    assert "stale-token" not in manager._created_at_mono  # type: ignore[attr-defined]
    store.revoke_resume_token.assert_not_called()


# ---------------------------------------------------------------------------
# bridge/routes/browser_handlers.py:325 — _handle_input notifies the browser
# when send_worker reports a lost worker connection.
# ---------------------------------------------------------------------------


async def test_handle_input_notifies_browser_when_send_worker_fails() -> None:
    from unittest.mock import AsyncMock, patch

    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.bridge.routes.browser_handlers import _handle_input

    hub = TermHub()
    ws = AsyncMock()
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    await hub.register_browser("w1", ws, "admin")
    await hub.try_acquire_ws_hijack("w1", ws)

    # Force send_worker to report failure (worker connection lost).
    with patch.object(hub, "send_worker", AsyncMock(return_value=False)):
        await _handle_input(hub, ws, "w1", {"type": "input", "data": "hi"})

    # Browser must have received an error frame.
    payload = ws.send_text.await_args.args[0]
    assert "Worker connection lost" in payload


# ---------------------------------------------------------------------------
# server/runtime.py:429 — `outcome == "cancelled"` break path.
# ---------------------------------------------------------------------------


async def test_classify_run_error_classifies_known_categories() -> None:
    """Round-trip _classify_run_error to be sure the cancelled-mapping holds."""
    import asyncio as _asyncio

    from provide.uterm.server.runtime import _classify_run_error

    # Cancellation is the only branch that breaks the runtime's reconnect loop.
    assert _classify_run_error(_asyncio.CancelledError()) == "cancelled"


# ---------------------------------------------------------------------------
# server/dev_idp.py:94->97 — setup_dev_idp preserves an existing worker
# bearer token (False branch of `if not auth.worker_bearer_token`).
# ---------------------------------------------------------------------------


def test_setup_dev_idp_preserves_caller_supplied_bearer(monkeypatch, tmp_path) -> None:
    from provide.uterm.server.dev_idp import setup_dev_idp
    from provide.uterm.server.models import AuthConfig

    monkeypatch.setenv("UTERM_DEV_TOKEN_PATH", str(tmp_path / "tok"))
    auth = AuthConfig(mode="dev_token", worker_bearer_token="caller-supplied-bearer-token-x")
    setup_dev_idp(auth)
    assert auth.worker_bearer_token == "caller-supplied-bearer-token-x"


# ---------------------------------------------------------------------------
# bridge/hub/semantics.py:85->87, 94->97 — CommandSplitter skips empty segments
# both mid-command (`;;`) and at the end (`cmd;`).
# ---------------------------------------------------------------------------


def test_command_splitter_skips_empty_segments() -> None:
    from provide.uterm.bridge.hub.semantics import CommandSplitter

    splitter = CommandSplitter()
    # Empty mid-segment from `;;`
    assert splitter.split("a;;b") == ["a", "b"]
    # Empty trailing segment from `a;`
    assert splitter.split("a;") == ["a"]
    # Whitespace-only mid-segment is also empty after strip
    assert splitter.split("a;   ;b") == ["a", "b"]


# ---------------------------------------------------------------------------
# bridge/fanout/_controller.py:238 — release_approved_command returns None
# when the group is no longer authorized (e.g. deleted between request and
# approval).
# ---------------------------------------------------------------------------


async def test_fanout_release_approved_command_returns_none_for_unauthorized_group() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from provide.uterm.bridge.fanout._controller import FanOutController

    ctrl = FanOutController(hub=MagicMock(), fanout_policy_gate=MagicMock())
    ctrl._pending_approvals["req-y"] = {  # type: ignore[attr-defined]
        "group_id": "g-gone",
        "command": "ls",
        "principal": "user",
        "quiesce_ms": None,
        "max_response_ms": None,
    }
    # _authorized_group resolves to None when the group is missing/unauthorized.
    ctrl._authorized_group = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await ctrl.release_approved_command("req-y") is None


# ---------------------------------------------------------------------------
# bridge/routes/browser_handlers.py:289->291 — buffered prefix branch in
# _handle_input when the chunk completes a buffered command.
# ---------------------------------------------------------------------------


async def test_handle_input_completing_buffered_command_passes_full_string() -> None:
    """When a prior partial chunk landed in _input_buffers, a completing chunk
    should be joined with the prefix before the worker sees it."""
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.bridge.routes.browser_handlers import _handle_input

    hub = TermHub()
    ws = AsyncMock()
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    await hub.register_browser("w1", ws, "admin")
    await hub.try_acquire_ws_hijack("w1", ws)

    # Send a partial first chunk; no newline so it's buffered.
    await _handle_input(hub, ws, "w1", {"type": "input", "data": "ec"})
    # Now the completing chunk; the policy gate sees the joined "echo hi\n".
    await _handle_input(hub, ws, "w1", {"type": "input", "data": "ho hi\n"})
    # Verify the worker received "echo hi" data via the second call.
    sent_payloads = [c.args[0] for c in worker_ws.send_text.await_args_list]
    joined = "".join(sent_payloads)
    assert "echo hi" in joined
