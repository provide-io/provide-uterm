#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Deterministic coverage for ownership-fence replacement barriers."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus, InMemoryApprovalStore
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState
from provide.uterm.server.bridge.routes.browser_handlers import _handle_input, _try_reclaim_hijack


async def _replace_while_waiting(hub: TermHub, worker_id: str, state: WorkerTermState, task: asyncio.Task):
    await asyncio.sleep(0)
    replacement = WorkerTermState(worker_ws=AsyncMock())
    async with hub._lock:
        hub.registry._workers[worker_id] = replacement
    state.owned_input_fence.release()
    return await asyncio.wait_for(task, timeout=1.0)


async def test_connection_lifecycle_rejects_replaced_state() -> None:
    hub = TermHub()
    worker_id = "connection-replaced"
    ws = AsyncMock()
    state = WorkerTermState(worker_ws=ws)
    hub.registry._workers[worker_id] = state
    await state.owned_input_fence.acquire()
    result = await _replace_while_waiting(
        hub,
        worker_id,
        state,
        asyncio.create_task(hub.connection_mgr.deregister_worker(worker_id, ws)),
    )
    assert result == (False, False)

    state = WorkerTermState(worker_ws=ws)
    hub.registry._workers[worker_id] = state
    await state.owned_input_fence.acquire()
    result = await _replace_while_waiting(
        hub, worker_id, state, asyncio.create_task(hub.connection_mgr.disconnect_worker(worker_id))
    )
    assert result is False

    state = WorkerTermState(worker_ws=ws, hijack_owner=AsyncMock(), hijack_owner_expires_at=time.monotonic() + 60)
    hub.registry._workers[worker_id] = state
    await state.owned_input_fence.acquire()
    result = await _replace_while_waiting(
        hub, worker_id, state, asyncio.create_task(hub.connection_mgr.force_release_hijack(worker_id))
    )
    assert result is False


async def test_registering_same_worker_does_not_advance_ownership_generation() -> None:
    hub = TermHub()
    ws = AsyncMock()
    assert await hub.register_worker("same-worker", ws) is False
    generation = hub.registry._workers["same-worker"].ownership_generation
    assert await hub.register_worker("same-worker", ws) is False
    assert hub.registry._workers["same-worker"].ownership_generation == generation


async def test_force_release_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from provide.uterm.server.bridge.hub import connection_hijack

    hub = TermHub()
    state = WorkerTermState(worker_ws=AsyncMock())
    state.hijack_session = HijackSession("rest", "owner", 0, time.monotonic() + 60, 0)
    hub.registry._workers["timeout"] = state

    async def timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(connection_hijack.asyncio, "wait_for", timeout)
    assert await hub.connection_mgr.force_release_hijack("timeout") is True


async def test_lease_operations_reject_state_replacement() -> None:
    hub = TermHub()
    worker_id = "lease-replaced"
    ws = AsyncMock()

    state = WorkerTermState(worker_ws=AsyncMock())
    hub.registry._workers[worker_id] = state
    await state.owned_input_fence.acquire()
    task = asyncio.create_task(
        hub.try_acquire_rest_hijack(worker_id, owner="owner", lease_s=30, hijack_id="rest-id", now=time.monotonic())
    )
    assert await _replace_while_waiting(hub, worker_id, state, task) == (False, "no_worker")

    state = WorkerTermState(worker_ws=AsyncMock())
    hub.registry._workers[worker_id] = state
    await state.owned_input_fence.acquire()
    task = asyncio.create_task(hub.try_acquire_ws_hijack(worker_id, ws))
    assert await _replace_while_waiting(hub, worker_id, state, task) == (False, "no_worker")

    state = WorkerTermState(worker_ws=AsyncMock())
    state.browsers[ws] = "admin"
    hub.registry._workers[worker_id] = state
    await state.owned_input_fence.acquire()
    task = asyncio.create_task(hub.remove_dead_browsers(worker_id, {ws}))
    assert await _replace_while_waiting(hub, worker_id, state, task) is False

    state = WorkerTermState(worker_ws=AsyncMock())
    state.browsers[ws] = "admin"
    state.input_mode = "open"
    hub.registry._workers[worker_id] = state
    await state.owned_input_fence.acquire()
    task = asyncio.create_task(hub.send_owned_worker(worker_id, {"type": "input"}, browser_ws=ws))
    assert await _replace_while_waiting(hub, worker_id, state, task) == (False, "invalid_owner")


async def test_owned_send_argument_and_missing_owner_guards() -> None:
    hub = TermHub()
    with pytest.raises(ValueError, match="exactly one"):
        await hub.send_owned_worker("missing", {"type": "input"})
    assert await hub.send_owned_worker("missing", {"type": "input"}, browser_ws=AsyncMock()) == (
        False,
        "invalid_owner",
    )


async def test_owned_browser_operation_guards_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from provide.uterm.server.bridge.hub import lease

    hub = TermHub()
    owner = AsyncMock()

    async def operation(send):
        return await send({"type": "input"}), None

    assert await hub.run_owned_browser_operation("missing", operation, browser_ws=owner, ownership_generation=0) == (
        None,
        "invalid_owner",
    )

    state = WorkerTermState()
    state.browsers[owner] = "admin"
    state.input_mode = "open"
    hub.registry._workers["no-worker"] = state
    assert await hub.run_owned_browser_operation("no-worker", operation, browser_ws=owner, ownership_generation=0) == (
        None,
        "no_worker",
    )

    worker = AsyncMock()
    await hub.register_worker("timeout", worker)
    await hub.register_browser("timeout", owner, "admin")
    hub.registry._workers["timeout"].input_mode = "open"
    generation = await hub.capture_browser_ownership("timeout", owner)
    assert generation is not None

    async def timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(lease.asyncio, "wait_for", timeout)
    assert await hub.run_owned_browser_operation(
        "timeout", operation, browser_ws=owner, ownership_generation=generation
    ) == ((False, None), None)


async def test_router_expected_worker_and_reclaim_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    from provide.uterm.server.bridge.hub.router_impl import MessageRouter

    hub = TermHub()
    worker = AsyncMock()
    await hub.register_worker("router", worker)
    assert await hub.send_worker("router", {"type": "input"}, expected_worker=AsyncMock()) is False

    reclaim = AsyncMock(return_value=(True, False))
    monkeypatch.setattr(MessageRouter, "try_reclaim_hijack_status", reclaim)
    assert await hub.try_reclaim_hijack("router", AsyncMock()) is True
    reclaim.assert_awaited_once()

    monkeypatch.undo()
    assert await TermHub().router.try_reclaim_hijack_status("missing", AsyncMock()) == (False, False)


class _HoldGate:
    async def intercept_input(self, _data: str, _context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action="hold", request_id="duplicate")


class _AllowGate:
    async def intercept_input(self, _data: str, _context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action="allow")


class _DenyGate:
    async def intercept_input(self, _data: str, _context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action="deny", reason="blocked replay")


async def test_browser_input_collision_and_reserved_paths() -> None:
    owner = AsyncMock()
    hub = TermHub(policy_gate=_HoldGate())
    worker = AsyncMock()
    await hub.register_worker("browser", worker)
    await hub.register_browser("browser", owner, "admin")
    hub.registry._workers["browser"].input_mode = "open"
    assert hub.approval_store.add(
        ApprovalRequest("duplicate", "browser", "u", "old", ApprovalStatus.PENDING, 0, time.time() + 60)
    )
    assert await _handle_input(hub, owner, "browser", {"type": "input", "data": "part"}) == "collision"
    assert await _handle_input(hub, owner, "browser", {"type": "input", "data": "complete\n"}) == "collision"

    hub = TermHub(policy_gate=_AllowGate())
    await hub.register_worker("reserved", worker)
    await hub.register_browser("reserved", owner, "admin")
    hub.registry._workers["reserved"].input_mode = "open"
    generation = await hub.capture_browser_ownership("reserved", owner)
    assert generation is not None
    assert (
        await _handle_input(
            hub,
            owner,
            "reserved",
            {"type": "input", "data": "complete\n"},
            ownership_generation_override=generation,
            reserved_sender=AsyncMock(return_value=True),
        )
        == "sent"
    )


async def test_browser_invalid_owner_and_failed_reclaim_pause() -> None:
    hub = TermHub()
    owner = AsyncMock()
    await hub.register_worker("invalid", AsyncMock())
    await hub.register_browser("invalid", owner, "admin")
    hub.registry._workers["invalid"].input_mode = "open"
    hub.send_owned_worker = AsyncMock(return_value=(False, "invalid_owner"))  # type: ignore[method-assign]
    assert await _handle_input(hub, owner, "invalid", {"type": "input", "data": "x"}) == "invalid_owner"

    session = type("Session", (), {"was_hijack_owner": True})()
    hub.try_reclaim_hijack_status = AsyncMock(return_value=(True, False))  # type: ignore[method-assign]
    hub.capture_browser_ownership = AsyncMock(return_value=1)  # type: ignore[method-assign]
    hub.send_owned_worker = AsyncMock(return_value=(False, "no_worker"))  # type: ignore[method-assign]
    hub.try_release_ws_hijack = AsyncMock(return_value=(True, False))  # type: ignore[method-assign]
    assert await _try_reclaim_hijack(hub, owner, "invalid", session, True) == (False, False, False)


async def _approval_hub(policy_gate=None) -> tuple[TermHub, AsyncMock, ApprovalRequest]:
    hub = TermHub(policy_gate=policy_gate)
    owner = AsyncMock()
    worker = AsyncMock()
    await hub.register_worker("approval", worker)
    await hub.register_browser("approval", owner, "admin")
    assert await hub.try_acquire_ws_hijack("approval", owner) == (True, None)
    generation = await hub.capture_browser_ownership("approval", owner)
    assert generation is not None
    assert hub.approval_store.add(
        ApprovalRequest(
            "approved",
            "approval",
            "submitter",
            "approved-command",
            ApprovalStatus.PENDING,
            time.time(),
            time.time() + 60,
            origin_browser=owner,
            ownership_generation=generation,
        )
    )
    request = hub.approval_store.get("approved")
    assert request is not None
    hub._paused_browsers.add(owner)
    return hub, owner, request


async def test_approval_command_delivery_failure_is_refused() -> None:
    hub, _owner, request = await _approval_hub()
    hub.send_worker = AsyncMock(return_value=False)  # type: ignore[method-assign]

    result = await hub.resolve_approval(
        "approval", "approved", PolicyDecision(action="allow"), "untrusted", approval_request=request
    )

    assert result == (False, "no_worker")


async def test_missing_or_mismatched_approval_cannot_reach_worker_state() -> None:
    hub = TermHub()
    assert await hub.resolve_approval(
        "missing-worker", "missing-request", PolicyDecision(action="allow"), "injected"
    ) == (False, "approval_not_found")
    assert "missing-worker" not in hub.registry._workers

    assert hub.approval_store.add(
        ApprovalRequest(
            "real-request",
            "real-worker",
            "submitter",
            "stored-command",
            ApprovalStatus.PENDING,
            time.time(),
            time.time() + 60,
        )
    )
    request = hub.approval_store.get("real-request")
    assert request is not None
    assert await hub.resolve_approval(
        "wrong-worker",
        "real-request",
        PolicyDecision(action="allow"),
        "injected",
        approval_request=request,
    ) == (False, "approval_mismatch")
    assert "wrong-worker" not in hub.registry._workers


async def test_approval_replay_can_create_a_second_pending_approval() -> None:
    hub, owner, request = await _approval_hub(_HoldGate())
    hub._hold_buffers[owner] = "next-command"

    result = await hub.resolve_approval(
        "approval", "approved", PolicyDecision(action="allow"), "untrusted", approval_request=request
    )

    assert result == (True, "replay_pending")
    assert owner in hub._paused_browsers
    duplicate = hub.approval_store.get("duplicate")
    assert duplicate is not None
    assert duplicate.command == "next-command"


async def test_approval_replay_policy_block_is_reported_after_command_success() -> None:
    hub, owner, request = await _approval_hub(_DenyGate())
    hub._hold_buffers[owner] = "blocked-command"

    result = await hub.resolve_approval(
        "approval", "approved", PolicyDecision(action="allow"), "untrusted", approval_request=request
    )

    assert result == (True, "replay_blocked")
    assert owner not in hub._paused_browsers


def test_approval_store_rejects_invalid_final_status() -> None:
    store = InMemoryApprovalStore()
    with pytest.raises(ValueError, match="finalize"):
        store.finalize("missing", ApprovalStatus.REJECTED, expected_revision=1)
