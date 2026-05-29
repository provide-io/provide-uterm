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
    from provide.uterm.server.bridge.hub import TermHub

    hub = TermHub()
    with pytest.raises(ValueError, match="invalid input mode"):
        await hub.set_worker_hello_mode("nobody", "bogus-mode")


# ---------------------------------------------------------------------------
# bridge/hub/connections.py:205 — legacy protocol_version warning log
# ---------------------------------------------------------------------------


async def test_worker_hello_logs_warning_for_legacy_protocol(caplog: pytest.LogCaptureFixture) -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.models import WorkerTermState

    hub = TermHub()
    hub._workers["w1"] = WorkerTermState(worker_ws=AsyncMock())
    # Phase 6 of refactor #16: log lives on the new ConnectionManager module.
    caplog.set_level(logging.WARNING, logger="provide.uterm.server.bridge.hub.connection")
    await hub.set_worker_hello("w1", "open", protocol_version=0)
    assert any("worker_hello_legacy_protocol" in r.getMessage() for r in caplog.records), (
        "set_worker_hello with protocol_version<1 must log worker_hello_legacy_protocol warning"
    )


# ---------------------------------------------------------------------------
# bridge/hub/semantics.py:16 — CommandSplitter.split("") -> []
# ---------------------------------------------------------------------------


def test_ownership_compute_lease_expirations_reports_both_expired() -> None:
    import time as _time
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub.core import TermHub
    from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

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
    browser_expired, rest_expired = TermHub._compute_lease_expirations(state, now)
    assert browser_expired is True
    assert rest_expired is True


# ---------------------------------------------------------------------------
# bridge/hub/approvals.py:70 — cleanup_expired awaits an async on_expired
# callback when the registered handler returns a coroutine.
# ---------------------------------------------------------------------------


async def test_in_memory_approval_store_awaits_async_on_expired() -> None:
    import time as _time

    from provide.uterm.server.bridge.hub.approvals import (
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

    from provide.uterm.server.bridge.hub.approvals import (
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

    from provide.uterm.server.bridge.hub.ext import (
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
    import hashlib
    import hmac

    import httpx
    import respx

    from provide.uterm.server.bridge.hub.ext import PolicyContext, WebhookFanOutPolicyGate

    ctx = PolicyContext(worker_id="w", client_id="c", role="admin", metadata={"k": "v"})
    gate = WebhookFanOutPolicyGate(url="http://hook.test/fanout", secret="s", timeout_s=1.0)

    # 200 -> decision propagated
    with respx.mock(assert_all_called=False) as r:
        route = r.post("http://hook.test/fanout").mock(
            return_value=httpx.Response(200, json={"action": "deny", "reason": "policy"})
        )
        d = await gate.intercept_fanout("ls", ctx, group_id="g1")
    assert d.action == "deny"
    assert d.reason == "policy"
    assert route.calls.last.request.headers["X-Webhook-Secret"] == "s"
    sig = route.calls.last.request.headers.get("X-Uterm-Signature", "")
    expected = hmac.new(b"s", route.calls.last.request.content, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"

    # Non-200 -> deny
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/fanout").mock(return_value=httpx.Response(500))
        assert (await gate.intercept_fanout("ls", ctx)).action == "deny"

    # Network exception -> deny (catch-all branch)
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/fanout").mock(side_effect=httpx.ConnectError("network down"))
        assert (await gate.intercept_fanout("ls", ctx)).action == "deny"


async def test_webhook_behavioral_gate_defaults_to_allow_on_error() -> None:
    import hashlib
    import hmac
    import time as _time

    import httpx
    import respx

    from provide.uterm.server.bridge.hub.ext import (
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
        route = r.post("http://hook.test/audit").mock(return_value=httpx.Response(200, json={"action": "deny"}))
        assert (await gate.audit_connection(heur, ctx, thr)).action == "deny"
    assert route.calls.last.request.headers["X-Webhook-Secret"] == "s"
    sig = route.calls.last.request.headers.get("X-Uterm-Signature", "")
    expected = hmac.new(b"s", route.calls.last.request.content, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"

    # Non-200 -> allow (safety default)
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/audit").mock(return_value=httpx.Response(503))
        assert (await gate.audit_connection(heur, ctx, thr)).action == "allow"

    # Exception -> allow
    with respx.mock(assert_all_called=False) as r:
        r.post("http://hook.test/audit").mock(side_effect=httpx.ConnectError("network down"))
        assert (await gate.audit_connection(heur, ctx, thr)).action == "allow"


async def test_webhook_output_policy_gate_returns_rules_and_handles_failures() -> None:
    import hashlib
    import hmac

    import httpx
    import respx

    from provide.uterm.server.bridge.hub.ext import PolicyContext, WebhookOutputPolicyGate

    ctx = PolicyContext(worker_id="w", client_id="c", role="admin", metadata={})
    gate = WebhookOutputPolicyGate(url="http://hook.test/output", secret="s", timeout_s=1.0)

    # 200 -> rules parsed
    payload = {"rules": [{"pattern": r"\d+", "replacement": "###"}]}
    with respx.mock(assert_all_called=False) as r:
        route = r.post("http://hook.test/output").mock(return_value=httpx.Response(200, json=payload))
        rules = await gate.get_redaction_rules(ctx)
    assert len(rules) == 1
    assert rules[0].pattern == r"\d+"
    assert rules[0].replacement == "###"
    assert route.calls.last.request.headers["X-Webhook-Secret"] == "s"
    sig = route.calls.last.request.headers.get("X-Uterm-Signature", "")
    expected = hmac.new(b"s", route.calls.last.request.content, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"

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

    from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
    from provide.uterm.server.bridge.hub.approvals import ApprovalStatus
    from provide.uterm.server.bridge.routes.browser_handlers import _handle_input

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
    pending = [r for r in hub._approval_store._requests.values() if r.status == ApprovalStatus.PENDING]
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
    from provide.uterm.server.bridge.hub import TermHub

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

    from provide.uterm.server.bridge.hub import TermHub

    hub = TermHub()
    hub._behavioral_audit_interval_s = 0.01  # type: ignore[attr-defined]

    async def _boom() -> None:
        raise RuntimeError("audit blew up")

    hub._audit_all_browsers = _boom  # type: ignore[assignment]
    caplog.set_level(logging.ERROR, logger="provide.uterm.server.bridge.hub.core")

    task = _asyncio.create_task(hub._run_behavioral_audit_loop())
    try:
        # Wait long enough for at least one tick + the swallowed exception.
        await _asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib_suppress():
            await task
    assert any("behavioral_audit_loop_error" in r.getMessage() for r in caplog.records)


import contextlib as _contextlib


def contextlib_suppress():
    """Tiny shim so _asyncio.CancelledError on task cancellation is swallowed."""
    return _contextlib.suppress(BaseException)


# ---------------------------------------------------------------------------
# server/webhooks.py:68-69, 74, 78, 81, 100 — URL & pattern validation edges
# ---------------------------------------------------------------------------
