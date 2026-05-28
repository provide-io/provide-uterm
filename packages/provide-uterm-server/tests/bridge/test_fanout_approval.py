#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import asyncio
import time
from typing import Any

import pytest

from provide.uterm.server.bridge.fanout._controller import FanOutController
from provide.uterm.server.bridge.fanout._models import FanOutGroup, FanOutResult
from provide.uterm.server.bridge.hub.core import TermHub
from provide.uterm.server.bridge.hub.ext import FanOutPolicyGate, PolicyContext, PolicyDecision


class MockFanOutGate(FanOutPolicyGate):
    def __init__(self):
        self.next_decision = PolicyDecision(action="allow")
        self.calls = []

    async def intercept_fanout(self, command: str, context: PolicyContext, group_id: str) -> PolicyDecision:
        self.calls.append((command, context, group_id))
        return self.next_decision


class MockTermHub(TermHub):
    def __init__(self):
        super().__init__()
        self.sent_messages = []
        self.events = []

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        self.sent_messages.append((worker_id, msg))
        return True

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.events.append((worker_id, event_type, data))
        return {"ts": time.time()}


@pytest.fixture
def hub():
    return MockTermHub()


@pytest.fixture
def gate():
    return MockFanOutGate()


@pytest.fixture
def controller(hub, gate):
    ctrl = FanOutController(hub=hub, fanout_policy_gate=gate)
    hub.fan_out_controller = ctrl  # Wire it up like app.py does
    return ctrl


@pytest.mark.asyncio
async def test_fanout_passthrough_allow(controller, hub, gate):
    """Checklist: Gate Passthrough (allow) broadcasts immediately."""
    group_id = await controller.create_group(
        FanOutGroup(group_id="g1", name="G1", worker_ids=["w1", "w2"], created_by="admin", created_at=time.time()),
        principal="admin",
    )

    gate.next_decision = PolicyDecision(action="allow")

    called = False

    async def fake_send_parallel(group, data, q_ms, m_ms):
        nonlocal called
        called = True
        return FanOutResult(
            group_id=group.group_id,
            send_id="s1",
            command=data,
            sent_at=time.time(),
            results=[],
            divergent_sessions=[],
            failed_sessions=[],
        )

    controller._send_parallel = fake_send_parallel

    res = await controller.send(group_id, "ls", principal="admin")

    assert gate.calls[0][0] == "ls"
    assert called is True
    assert getattr(res, "approval_required", False) is False


@pytest.mark.asyncio
async def test_fanout_rejection_deny(controller, hub, gate):
    """Checklist: Gate Rejection (deny) blocks immediately."""
    group_id = await controller.create_group(
        FanOutGroup(group_id="g2", name="G2", worker_ids=["w1", "w2"], created_by="admin", created_at=time.time()),
        principal="admin",
    )

    gate.next_decision = PolicyDecision(action="deny", reason="Forbidden command")

    called = False

    async def fake_send_parallel(group, data, q_ms, m_ms):
        nonlocal called
        called = True
        return

    controller._send_parallel = fake_send_parallel

    res = await controller.send(group_id, "rm -rf /", principal="admin")

    assert called is False
    assert res.error == "Forbidden command"


@pytest.mark.asyncio
async def test_fanout_approval_lifecycle(controller, hub, gate):
    """Checklist: Approval Interception, Hub Resolution Routing, Command Release."""
    group_id = await controller.create_group(
        FanOutGroup(group_id="g3", name="G3", worker_ids=["w1", "w2"], created_by="admin", created_at=time.time()),
        principal="admin",
    )

    gate.next_decision = PolicyDecision(action="hold")

    called_with = None

    async def fake_send_parallel(group, data, q_ms, m_ms):
        nonlocal called_with
        called_with = data
        return FanOutResult(
            group_id=group.group_id,
            send_id="s1",
            command=data,
            sent_at=time.time(),
            results=[],
            divergent_sessions=[],
            failed_sessions=[],
        )

    controller._send_parallel = fake_send_parallel

    # 1. Send the command -> Should be intercepted
    res = await controller.send(group_id, "apt-get upgrade", principal="admin")

    assert called_with is None
    assert getattr(res, "approval_required", False) is True
    request_id = res.approval_id

    # 2. Resolve the approval via the Hub
    await hub.resolve_approval(
        worker_id="doesnt-matter-for-fanout",
        request_id=request_id,
        decision=PolicyDecision(action="allow"),
        command="apt-get upgrade",
    )

    # 3. Verify it was released
    assert called_with == "apt-get upgrade"
    assert request_id not in controller._pending_approvals


@pytest.mark.asyncio
async def test_fanout_approval_expiration_cleanup(controller, hub, gate):
    """Checklist 5.1: Controller Memory Leak (Cleanup on Expiration)."""
    group_id = await controller.create_group(
        FanOutGroup(group_id="g-expire", name="GE", worker_ids=["w1"], created_by="admin", created_at=time.time()),
        principal="admin",
    )
    gate.next_decision = PolicyDecision(action="hold")

    res = await controller.send(group_id, "long-task", principal="admin")
    req_id = res.approval_id

    assert req_id in controller._pending_approvals

    # Force expiration in Hub
    req = hub._approval_store.get(req_id)
    req.expires_at = time.time() - 10

    # Trigger cleanup
    await hub._approval_store.cleanup_expired()

    # Verify controller state is pruned
    assert req_id not in controller._pending_approvals


@pytest.mark.asyncio
async def test_fanout_approval_rejection_cleanup(controller, hub, gate):
    """Checklist 2.4: Active Rejection Resolution."""
    group_id = await controller.create_group(
        FanOutGroup(group_id="g-reject", name="GR", worker_ids=["w1"], created_by="admin", created_at=time.time()),
        principal="admin",
    )
    gate.next_decision = PolicyDecision(action="hold")

    res = await controller.send(group_id, "dangerous-command", principal="admin")
    req_id = res.approval_id

    assert req_id in controller._pending_approvals

    # Resolve as DENY
    await hub.resolve_approval(
        worker_id="any",
        request_id=req_id,
        decision=PolicyDecision(action="deny", reason="No way"),
        command="dangerous-command",
    )

    # Verify controller state is pruned and no command executed
    assert req_id not in controller._pending_approvals
    # Note: Hub resolve_approval for deny doesn't call back to controller for execution


@pytest.mark.asyncio
async def test_fanout_sequential_release(controller, hub, gate):
    """Checklist 3.2: Sequential Release after approval."""
    group_id = await controller.create_group(
        FanOutGroup(
            group_id="g-seq",
            name="GS",
            worker_ids=["w1", "w2"],
            mode="sequential",  # SEQUENTIAL
            created_by="admin",
            created_at=time.time(),
        ),
        principal="admin",
    )
    gate.next_decision = PolicyDecision(action="hold")

    called_sequential = False

    async def fake_send_sequential(group, data, q_ms, m_ms):
        nonlocal called_sequential
        called_sequential = True
        return FanOutResult(
            group_id=group.group_id,
            send_id="s1",
            command=data,
            sent_at=time.time(),
            results=[],
            divergent_sessions=[],
            failed_sessions=[],
        )

    controller._send_sequential = fake_send_sequential

    res = await controller.send(group_id, "rollout", principal="admin")
    req_id = res.approval_id

    # Approve
    await hub.resolve_approval("any", req_id, PolicyDecision(action="allow"), "rollout")

    assert called_sequential is True


@pytest.mark.asyncio
async def test_fanout_audit_trail(controller, hub, gate):
    """Checklist 4.1: Submission Audit."""
    group_id = await controller.create_group(
        FanOutGroup(group_id="g-audit", name="GA", worker_ids=["w1"], created_by="admin", created_at=time.time()),
        principal="admin",
    )
    gate.next_decision = PolicyDecision(action="hold")

    await controller.send(group_id, "sensitive-task", principal="admin")

    # Verify terminal.fanout.hold event
    hold_events = [e for e in hub.events if e[1] == "terminal.fanout.hold"]
    assert len(hold_events) == 1
    assert hold_events[0][0] == f"group:{group_id}"
    assert hold_events[0][2]["command"] == "sensitive-task"
    assert hold_events[0][2]["principal"] == "admin"


@pytest.mark.asyncio
async def test_fanout_approval_rbac_admin_only(controller, hub, gate):
    """Checklist 2.2: Only authorized admins can resolve."""
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from provide.uterm.server.routes.approvals import create_approvals_router

    group_id = await controller.create_group(
        FanOutGroup(group_id="g-rbac", name="GR", worker_ids=["w1"], created_by="admin", created_at=time.time()),
        principal="admin",
    )
    gate.next_decision = PolicyDecision(action="hold")
    res = await controller.send(group_id, "secure-cmd", principal="admin")
    req_id = res.approval_id

    router = create_approvals_router()

    # 1. Mock request with non-admin principal
    mock_principal = MagicMock()
    mock_authz = MagicMock()
    # authz.is_admin returns False
    mock_authz.is_admin = asyncio.iscoroutinefunction(lambda p: None)  # Make it async

    async def is_admin_false(p):
        return False

    mock_authz.is_admin = is_admin_false

    mock_request = MagicMock()
    mock_request.state.uterm_principal = mock_principal
    mock_request.app.state.uterm_authz = mock_authz
    mock_request.app.state.uterm_hub = hub

    # Extract the endpoint function from router
    # Note: prefix might or might not be present in r.path depending on how FastAPI internalizes it
    approve_route = [r for r in router.routes if "/approve" in r.path][0]

    with pytest.raises(HTTPException) as exc:
        await approve_route.endpoint(req_id, mock_request)
    assert exc.value.status_code == 403
    assert "Admin role required" in exc.value.detail

    # 2. Mock request with admin principal
    async def is_admin_true(p):
        return True

    mock_authz.is_admin = is_admin_true

    resp = await approve_route.endpoint(req_id, mock_request)
    assert resp["status"] == "approved"
    assert hub._approval_store.get(req_id).status.value == "approved"
