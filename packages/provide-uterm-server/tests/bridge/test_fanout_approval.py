#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.fanout._controller import FanOutController
from provide.uterm.server.bridge.fanout._models import FanOutGroup, FanOutResult
from provide.uterm.server.bridge.hub.core import TermHub
from provide.uterm.server.bridge.hub.ext import FanOutPolicyGate, PolicyContext, PolicyDecision
from provide.uterm.server.bridge.identity import Principal

ADMIN = Principal(subject_id="admin", roles=frozenset({"admin"}))


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
    ctrl = FanOutController(
        hub=hub,
        fanout_policy_gate=gate,
        is_global_admin=AsyncMock(return_value=True),
        resolve_session=AsyncMock(return_value=object()),
        can_read_session=AsyncMock(return_value=True),
    )
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

    async def fake_send_parallel(group, data, q_ms, m_ms, *, principal):
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

    res = await controller.send(group_id, "ls", principal=ADMIN)

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

    async def fake_send_parallel(group, data, q_ms, m_ms, *, principal):
        nonlocal called
        called = True
        return

    controller._send_parallel = fake_send_parallel

    res = await controller.send(group_id, "rm -rf /", principal=ADMIN)

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

    async def fake_send_parallel(group, data, q_ms, m_ms, *, principal):
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
    res = await controller.send(group_id, "apt-get upgrade", principal=ADMIN)

    assert called_with is None
    assert getattr(res, "approval_required", False) is True
    request_id = res.approval_id
    request = hub.approval_store.get(request_id)
    assert request is not None

    # 2. Resolve the approval via the Hub
    await hub.resolve_approval(
        worker_id=request.worker_id,
        request_id=request_id,
        decision=PolicyDecision(action="allow"),
        command="apt-get upgrade",
        approval_request=request,
    )

    # 3. Verify it was released
    assert called_with == "apt-get upgrade"
    assert request_id not in controller._pending_approvals


@pytest.mark.asyncio
async def test_fanout_approval_rejects_request_id_collision(controller, hub, gate, monkeypatch):
    from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus

    await controller.create_group(
        FanOutGroup(group_id="g-collision", name="GC", worker_ids=["w1"], created_by="admin", created_at=0),
        principal="admin",
    )
    gate.next_decision = PolicyDecision(action="hold")
    monkeypatch.setattr("provide.uterm.server.bridge.fanout._controller.uuid.uuid4", lambda: MagicMock(hex="duplicate"))
    assert hub.approval_store.add(
        ApprovalRequest("duplicate", "existing", "user", "first", ApprovalStatus.PENDING, time.time(), time.time() + 60)
    )

    with pytest.raises(RuntimeError, match="request ID collision"):
        await controller.send("g-collision", "second", principal=ADMIN)

    assert controller._pending_approvals == {}


@pytest.mark.asyncio
async def test_approval_release_rechecks_current_session_authorization(hub, gate):
    from provide.uterm.server.bridge.identity import Principal
    from provide.uterm.server.config_schema import SessionDefinition

    definition = SessionDefinition(
        session_id="w1",
        display_name="W1",
        connector_type="shell",
        owner="admin",
        visibility="private",
    )
    readable = True

    async def resolve_session(worker_id: str):
        return definition if worker_id == "w1" else None

    async def can_read_session(principal, session):
        return readable

    ctrl = FanOutController(
        hub=hub,
        fanout_policy_gate=gate,
        is_global_admin=AsyncMock(return_value=True),
        resolve_session=resolve_session,
        can_read_session=can_read_session,
    )
    hub.fan_out_controller = ctrl
    principal = Principal(subject_id="admin", roles=frozenset({"admin"}))
    group_id = await ctrl.create_group(
        FanOutGroup(group_id="g-revoke", name="G", worker_ids=["w1"], created_by="admin", created_at=time.time()),
        principal=principal,
    )
    gate.next_decision = PolicyDecision(action="hold")
    held = await ctrl.send(group_id, "id", principal=principal)
    approval = hub.approval_store.get(held.approval_id)
    assert approval is not None

    readable = False
    result = await ctrl.release_approved_command(held.approval_id, expected_revision=approval.revision)

    assert result is not None
    assert result.failed_sessions == ["w1"]
    assert not hub.sent_messages


@pytest.mark.asyncio
async def test_approval_release_rechecks_global_admin_and_keeps_full_principal(hub, gate):
    admin_allowed = True

    async def is_admin(principal: Principal) -> bool:
        return admin_allowed

    ctrl = FanOutController(
        hub=hub,
        fanout_policy_gate=gate,
        is_global_admin=is_admin,
        resolve_session=AsyncMock(return_value=object()),
        can_read_session=AsyncMock(return_value=True),
    )
    hub.fan_out_controller = ctrl
    principal = Principal(
        subject_id="admin",
        roles=frozenset({"admin"}),
        scopes=frozenset({"session.read"}),
        claims={"issuer": "test"},
    )
    group_id = await ctrl.create_group(
        FanOutGroup(group_id="g-admin-revoke", name="G", worker_ids=["w1"], created_by="admin", created_at=0.0),
        principal=principal,
    )
    gate.next_decision = PolicyDecision(action="hold")
    held = await ctrl.send(group_id, "id", principal=principal)
    approval = hub.approval_store.get(held.approval_id)
    assert approval is not None
    assert ctrl._pending_approvals[held.approval_id]["principal"] is principal

    admin_allowed = False
    result = await ctrl.release_approved_command(held.approval_id, expected_revision=approval.revision)

    assert result is not None
    assert result.error == "global admin role required"
    assert not hub.sent_messages


@pytest.mark.asyncio
async def test_approval_release_rechecks_current_group_acl(hub, gate):
    ctrl = FanOutController(
        hub=hub,
        fanout_policy_gate=gate,
        is_global_admin=AsyncMock(return_value=True),
        resolve_session=AsyncMock(return_value=object()),
        can_read_session=AsyncMock(return_value=True),
    )
    principal = Principal(subject_id="grantee", roles=frozenset({"admin"}))
    await ctrl.create_group(
        FanOutGroup(
            group_id="g-grant-revoke",
            name="G",
            worker_ids=["w1"],
            created_by="creator",
            created_at=0.0,
            grants=["grantee"],
        ),
        principal="creator",
    )
    gate.next_decision = PolicyDecision(action="hold")
    held = await ctrl.send("g-grant-revoke", "id", principal=principal)
    approval = hub.approval_store.get(held.approval_id)
    assert approval is not None
    stored = await ctrl._store.get("g-grant-revoke")
    assert stored is not None
    stored.grants.clear()
    await ctrl._store.save(stored)

    result = await ctrl.release_approved_command(held.approval_id, expected_revision=approval.revision)

    assert result is not None
    assert result.error == "fan-out group not found"
    assert not hub.sent_messages


@pytest.mark.asyncio
async def test_hold_audit_failure_leaves_nothing_releasable_or_duplicable(hub, gate, monkeypatch):
    request_id = "audit-failed"
    ctrl = FanOutController(
        hub=hub,
        fanout_policy_gate=gate,
        is_global_admin=AsyncMock(return_value=True),
        resolve_session=AsyncMock(return_value=object()),
        can_read_session=AsyncMock(return_value=True),
    )
    await ctrl.create_group(
        FanOutGroup(group_id="g-audit-fail", name="G", worker_ids=["w1"], created_by="admin", created_at=0.0),
        principal=ADMIN,
    )
    gate.next_decision = PolicyDecision(action="hold")
    monkeypatch.setattr(
        "provide.uterm.server.bridge.fanout._controller.uuid.uuid4",
        lambda: MagicMock(hex=request_id),
    )
    hub.append_event = AsyncMock(side_effect=RuntimeError("audit unavailable"))

    for _ in range(2):
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await ctrl.send("g-audit-fail", "id", principal=ADMIN)

    assert ctrl._pending_approvals == {}
    assert hub.approval_store.get(request_id) is None
    assert await ctrl.release_approved_command(request_id, expected_revision=1) is None
    assert not hub.sent_messages


@pytest.mark.asyncio
async def test_hold_approval_failure_leaves_nothing_releasable_or_duplicable(hub, gate, monkeypatch):
    request_id = "approval-failed"
    ctrl = FanOutController(
        hub=hub,
        fanout_policy_gate=gate,
        is_global_admin=AsyncMock(return_value=True),
        resolve_session=AsyncMock(return_value=object()),
        can_read_session=AsyncMock(return_value=True),
    )
    await ctrl.create_group(
        FanOutGroup(group_id="g-approval-fail", name="G", worker_ids=["w1"], created_by="admin", created_at=0.0),
        principal=ADMIN,
    )
    gate.next_decision = PolicyDecision(action="hold")
    monkeypatch.setattr(
        "provide.uterm.server.bridge.fanout._controller.uuid.uuid4",
        lambda: MagicMock(hex=request_id),
    )
    monkeypatch.setattr(
        hub.approval_store,
        "add",
        MagicMock(side_effect=RuntimeError("approval unavailable")),
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="approval unavailable"):
            await ctrl.send("g-approval-fail", "id", principal=ADMIN)

    assert ctrl._pending_approvals == {}
    assert await ctrl.release_approved_command(request_id, expected_revision=1) is None
    assert not hub.sent_messages


@pytest.mark.asyncio
async def test_hold_fails_atomically_when_hub_has_no_approval_store(gate):
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.send_worker = AsyncMock()
    hub.append_event = AsyncMock(return_value={"ts": time.time()})
    hub.approval_store = None
    ctrl = FanOutController(
        hub=hub,
        fanout_policy_gate=gate,
        is_global_admin=AsyncMock(return_value=True),
        resolve_session=AsyncMock(return_value=object()),
        can_read_session=AsyncMock(return_value=True),
    )
    await ctrl.create_group(
        FanOutGroup(group_id="missing-store", name="G", worker_ids=["w1"], created_by="admin", created_at=0),
        principal=ADMIN,
    )
    gate.next_decision = PolicyDecision(action="hold")

    with pytest.raises(RuntimeError, match="approval store is unavailable"):
        await ctrl.send("missing-store", "reboot", principal=ADMIN)

    assert ctrl._pending_approvals == {}
    hub.send_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_fanout_approval_expiration_cleanup(controller, hub, gate):
    """Checklist 5.1: Controller Memory Leak (Cleanup on Expiration)."""
    group_id = await controller.create_group(
        FanOutGroup(group_id="g-expire", name="GE", worker_ids=["w1"], created_by="admin", created_at=time.time()),
        principal="admin",
    )
    gate.next_decision = PolicyDecision(action="hold")

    res = await controller.send(group_id, "long-task", principal=ADMIN)
    req_id = res.approval_id

    assert req_id in controller._pending_approvals

    # Force expiration in Hub
    with hub.approval_store._lock:
        hub.approval_store._requests[req_id].expires_at = time.time() - 10

    # Trigger cleanup
    await hub.approval_store.cleanup_expired()

    # Verify controller state is pruned
    assert req_id not in controller._pending_approvals


@pytest.mark.asyncio
async def test_delayed_expiry_of_pruned_revision_cannot_delete_reused_pending_payload(hub, gate, monkeypatch):
    """A queued rev1 timeout must not remove rev2 after its request ID is reused."""
    from provide.uterm.server.bridge.hub.approvals import ApprovalStatus

    notification_started = asyncio.Event()
    release_notification = asyncio.Event()

    async def delay_rev1_notification(_request):
        notification_started.set()
        await release_notification.wait()

    hub.approval_store.subscribe_expired(delay_rev1_notification)
    ctrl = FanOutController(
        hub=hub,
        fanout_policy_gate=gate,
        is_global_admin=AsyncMock(return_value=True),
        resolve_session=AsyncMock(return_value=object()),
        can_read_session=AsyncMock(return_value=True),
    )
    await ctrl.create_group(
        FanOutGroup(group_id="g-aba", name="G", worker_ids=["w1"], created_by="admin", created_at=0.0),
        principal=ADMIN,
    )
    gate.next_decision = PolicyDecision(action="hold")
    monkeypatch.setattr(
        "provide.uterm.server.bridge.fanout._controller.uuid.uuid4",
        lambda: MagicMock(hex="reused-request"),
    )

    first = await ctrl.send("g-aba", "first", principal=ADMIN)
    revision_one = hub.approval_store.get(first.approval_id)
    assert revision_one is not None
    with hub.approval_store._lock:
        hub.approval_store._requests[first.approval_id].expires_at = time.time() - 3601
    assert (
        hub.approval_store.claim_request(
            first.approval_id,
            ApprovalStatus.RESOLVING,
            expected_revision=revision_one.revision,
        )
        is None
    )

    cleanup = asyncio.create_task(hub.approval_store.cleanup_expired())
    await asyncio.wait_for(notification_started.wait(), timeout=1)
    assert hub.approval_store.get(first.approval_id) is None

    second = await ctrl.send("g-aba", "second", principal=ADMIN)
    revision_two = hub.approval_store.get(second.approval_id)
    assert revision_two is not None
    assert revision_two.revision > revision_one.revision
    assert ctrl._pending_approvals[second.approval_id]["command"] == "second"
    assert (
        await ctrl.release_approved_command(
            second.approval_id,
            expected_revision=revision_one.revision,
        )
        is None
    )
    assert ctrl._pending_approvals[second.approval_id]["command"] == "second"

    release_notification.set()
    await cleanup

    current = hub.approval_store.get(second.approval_id)
    assert current is not None
    assert current.status == ApprovalStatus.PENDING
    assert ctrl._pending_approvals[second.approval_id]["command"] == "second"


@pytest.mark.asyncio
async def test_fanout_approval_rejection_cleanup(controller, hub, gate):
    """Checklist 2.4: Active Rejection Resolution."""
    group_id = await controller.create_group(
        FanOutGroup(group_id="g-reject", name="GR", worker_ids=["w1"], created_by="admin", created_at=time.time()),
        principal="admin",
    )
    gate.next_decision = PolicyDecision(action="hold")

    res = await controller.send(group_id, "dangerous-command", principal=ADMIN)
    req_id = res.approval_id

    assert req_id in controller._pending_approvals
    request = hub.approval_store.get(req_id)
    assert request is not None

    # Resolve as DENY
    await hub.resolve_approval(
        worker_id=request.worker_id,
        request_id=req_id,
        decision=PolicyDecision(action="deny", reason="No way"),
        command="dangerous-command",
        approval_request=request,
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

    async def fake_send_sequential(group, data, q_ms, m_ms, *, principal):
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

    res = await controller.send(group_id, "rollout", principal=ADMIN)
    req_id = res.approval_id
    request = hub.approval_store.get(req_id)
    assert request is not None

    # Approve
    await hub.resolve_approval(
        request.worker_id,
        req_id,
        PolicyDecision(action="allow"),
        "rollout",
        approval_request=request,
    )

    assert called_sequential is True


@pytest.mark.asyncio
async def test_fanout_audit_trail(controller, hub, gate):
    """Checklist 4.1: Submission Audit."""
    group_id = await controller.create_group(
        FanOutGroup(group_id="g-audit", name="GA", worker_ids=["w1"], created_by="admin", created_at=time.time()),
        principal="admin",
    )
    gate.next_decision = PolicyDecision(action="hold")

    await controller.send(group_id, "sensitive-task", principal=ADMIN)

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
    res = await controller.send(group_id, "secure-cmd", principal=ADMIN)
    req_id = res.approval_id

    router = create_approvals_router()

    # 1. Mock request with non-admin principal
    mock_principal = MagicMock()
    mock_authz = MagicMock()
    # authz.is_admin returns False
    mock_authz.is_admin = asyncio.iscoroutinefunction(lambda _p: None)  # Make it async

    async def is_admin_false(p):
        return False

    mock_authz.is_admin = is_admin_false

    mock_request = MagicMock()
    mock_request.state.uterm_principal = mock_principal
    mock_request.app.state.uterm_authz = mock_authz
    mock_request.app.state.uterm_hub = hub

    # Extract the endpoint function from router
    # Note: prefix might or might not be present in r.path depending on how FastAPI internalizes it
    approve_route = next(r for r in router.routes if "/approve" in r.path)

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
    assert hub.approval_store.get(req_id).status.value == "approved"
