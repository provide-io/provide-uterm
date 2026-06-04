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
    hub.registry._workers["w1"] = WorkerTermState(worker_ws=AsyncMock())
    # Phase 6 of refactor #16: log lives on the new ConnectionManager module.
    caplog.set_level(logging.WARNING, logger="provide.uterm.server.bridge.hub.connection")
    await hub.set_worker_hello("w1", "open", protocol_version=0)
    assert any("worker_hello_legacy_protocol" in r.getMessage() for r in caplog.records), (
        "set_worker_hello with protocol_version<1 must log worker_hello_legacy_protocol warning"
    )


# ---------------------------------------------------------------------------
# bridge/hub/semantics.py:16 — CommandSplitter.split("") -> []
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

    from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
    from provide.uterm.server.bridge.hub.ext import BehavioralThresholds, ConnectionHeuristics

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

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
    from provide.uterm.server.bridge.hub.ext import PolicyDecision

    hub = TermHub()
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    browser_ws = AsyncMock()
    await hub.register_browser("w1", browser_ws, "admin")

    # Seed a non-fanout approval request so the deny path falls into the
    # in-place rejection branch (not the fanout-controller branch).
    hub.approval_store.add(
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

    from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
    from provide.uterm.server.bridge.hub.ext import BehavioralThresholds, ConnectionHeuristics

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
    from unittest.mock import AsyncMock, MagicMock

    from provide.uterm.server.bridge.hub.resume import ControlPlaneResumeStore

    tx = AsyncMock()
    cp = MagicMock()
    cp.begin = AsyncMock(return_value=tx)
    cp.token_store = MagicMock(return_value=AsyncMock())
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

    from provide.uterm.server.bridge.hub.resume import ControlPlaneResumeStore

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

    from provide.uterm.server.bridge.hub.resume import ControlPlaneResumeStore

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
    """browser_handlers.py:236 — NoOp-gate early-return path emits the
    Worker-connection-lost error when send_worker returns False."""
    from unittest.mock import AsyncMock, patch

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.routes.browser_handlers import _handle_input

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


async def test_resolve_approval_fanout_allow_without_fan_out_controller() -> None:
    """approvalflow.py:55->66 — fanout approval+allow when no fan_out_controller wired."""
    import time as _time
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
    from provide.uterm.server.bridge.hub.ext import PolicyDecision

    hub = TermHub()  # no fan_out_controller attribute set
    await hub.register_worker("w1", AsyncMock())
    hub.approval_store.add(
        ApprovalRequest(
            id="r-fo",
            worker_id="w1",
            submitter_id="a",
            command="x",
            status=ApprovalStatus.PENDING,
            created_at=_time.time(),
            expires_at=_time.time() + 60,
            is_fanout=True,
        ),
    )
    # Must not raise even though there's no controller.
    await hub.resolve_approval("w1", "r-fo", PolicyDecision(action="allow"), "x")


async def test_resolve_approval_fanout_deny_without_fan_out_controller() -> None:
    """approvalflow.py:64->66 — fanout approval+deny when no fan_out_controller wired."""
    import time as _time
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
    from provide.uterm.server.bridge.hub.ext import PolicyDecision

    hub = TermHub()
    await hub.register_worker("w1", AsyncMock())
    hub.approval_store.add(
        ApprovalRequest(
            id="r-fd",
            worker_id="w1",
            submitter_id="a",
            command="x",
            status=ApprovalStatus.PENDING,
            created_at=_time.time(),
            expires_at=_time.time() + 60,
            is_fanout=True,
        ),
    )
    await hub.resolve_approval("w1", "r-fd", PolicyDecision(action="deny", reason="no"), "x")


async def test_resolve_approval_fanout_hold_decision_is_noop() -> None:
    """approvalflow.py:57->66 — fanout approval with non-allow/non-deny decision."""
    import time as _time
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
    from provide.uterm.server.bridge.hub.ext import PolicyDecision

    hub = TermHub()
    await hub.register_worker("w1", AsyncMock())
    hub.approval_store.add(
        ApprovalRequest(
            id="r-fh",
            worker_id="w1",
            submitter_id="a",
            command="x",
            status=ApprovalStatus.PENDING,
            created_at=_time.time(),
            expires_at=_time.time() + 60,
            is_fanout=True,
        ),
    )
    # "hold" isn't allow or deny — falls through both branches and returns.
    await hub.resolve_approval("w1", "r-fh", PolicyDecision(action="hold"), "x")


async def test_resolve_approval_non_fanout_hold_decision_is_noop_on_worker() -> None:
    """approvalflow.py:72->80 — non-fanout decision other than allow/deny falls through."""
    import time as _time
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
    from provide.uterm.server.bridge.hub.ext import PolicyDecision

    hub = TermHub()
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    browser_ws = AsyncMock()
    await hub.register_browser("w1", browser_ws, "admin")
    hub.approval_store.add(
        ApprovalRequest(
            id="r-h",
            worker_id="w1",
            submitter_id="a",
            command="x",
            status=ApprovalStatus.PENDING,
            created_at=_time.time(),
            expires_at=_time.time() + 60,
            is_fanout=False,
        ),
    )
    await hub.resolve_approval("w1", "r-h", PolicyDecision(action="hold"), "x")
    # Worker must not have received any input.
    worker_ws.send_text.assert_not_called()


async def test_intercept_cancel_all_skips_already_done_futures() -> None:
    """tunnel/intercept.py:150->149 — futures already done don't double-set."""
    import asyncio as _asyncio

    from provide.uterm.tunnel.intercept import InterceptGate

    store = InterceptGate.__new__(InterceptGate)
    store._pending = {}  # type: ignore[attr-defined]

    loop = _asyncio.get_event_loop()
    done_fut = loop.create_future()
    done_fut.set_result(None)  # already resolved
    pending_fut = loop.create_future()
    store._pending = {"a": done_fut, "b": pending_fut}  # type: ignore[attr-defined]

    count = store.cancel_all()
    assert count == 1  # only pending_fut got resolved


def test_redaction_stream_redactor_with_no_rules_is_passthrough() -> None:
    """bridge/hub/redaction.py:29->exit — empty rules list skips the entire setup block."""
    from provide.uterm.server.bridge.hub.redaction import StreamRedactor

    redactor = StreamRedactor([])
    assert redactor.redact("anything goes") == "anything goes"
