#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Deterministic owner-fencing regressions for worker-bound input."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import httpx2
import pytest
from fastapi import FastAPI

from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState
from provide.uterm.server.bridge.routes.browser_handlers import _handle_input


class _GatedAllowPolicy:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def intercept_input(self, _data: str, _context: PolicyContext) -> PolicyDecision:
        self.entered.set()
        await self.release.wait()
        return PolicyDecision(action="allow")


class _HoldPolicy:
    async def intercept_input(self, _data: str, _context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action="hold", request_id="held-command", timeout_s=60)


class _GatedWorker:
    def __init__(self, block_on: str) -> None:
        self.block_on = block_on
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.payloads: list[str] = []

    async def send_text(self, payload: str) -> None:
        if self.block_on not in payload:
            self.payloads.append(payload)
            return
        self.entered.set()
        await self.release.wait()
        self.payloads.append(payload)


class _FailSecondWorker:
    def __init__(self) -> None:
        self.payloads: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.payloads.append(payload)
        if len(self.payloads) == 2:
            raise RuntimeError("replay failed")


async def test_browser_input_is_refused_when_competitor_acquires_during_policy_wait() -> None:
    policy = _GatedAllowPolicy()
    hub = TermHub(policy_gate=policy)
    worker = AsyncMock()
    stale_owner = AsyncMock()
    competitor = AsyncMock()
    worker_id = "browser-policy-race"
    await hub.register_worker(worker_id, worker)
    await hub.register_browser(worker_id, stale_owner, "admin")
    await hub.register_browser(worker_id, competitor, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, stale_owner) == (True, None)

    stale_send = asyncio.create_task(
        _handle_input(hub, stale_owner, worker_id, {"type": "input", "data": "stale-command\n"})
    )
    await asyncio.wait_for(policy.entered.wait(), timeout=1.0)

    assert await hub.try_release_ws_hijack(worker_id, stale_owner) == (True, False)
    assert await hub.try_acquire_ws_hijack(worker_id, competitor) == (True, None)
    policy.release.set()
    await asyncio.wait_for(stale_send, timeout=1.0)

    worker.send_text.assert_not_awaited()


async def test_browser_input_is_refused_after_same_socket_release_reacquire() -> None:
    policy = _GatedAllowPolicy()
    hub = TermHub(policy_gate=policy)
    worker = AsyncMock()
    owner = AsyncMock()
    worker_id = "browser-policy-aba"
    await hub.register_worker(worker_id, worker)
    await hub.register_browser(worker_id, owner, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)

    stale_send = asyncio.create_task(_handle_input(hub, owner, worker_id, {"type": "input", "data": "stale\n"}))
    await asyncio.wait_for(policy.entered.wait(), timeout=1.0)
    assert await hub.try_release_ws_hijack(worker_id, owner) == (True, False)
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)
    policy.release.set()
    await asyncio.wait_for(stale_send, timeout=1.0)

    worker.send_text.assert_not_awaited()


async def test_rest_release_waits_for_reserved_input_delivery() -> None:
    worker = _GatedWorker("reserved-input")
    hub = TermHub()
    worker_id = "rest-release-race"
    hijack_id = "rest-owner"
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    now = time.monotonic()
    async with hub._lock:
        hub.registry._workers[worker_id].hijack_session = HijackSession(
            hijack_id=hijack_id,
            owner="rest-client",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )

    send_task = asyncio.create_task(
        hub.send_owned_worker(
            worker_id,
            {"type": "input", "data": "reserved-input"},
            rest_hijack_id=hijack_id,
        )
    )
    await asyncio.wait_for(worker.entered.wait(), timeout=1.0)
    release_task = asyncio.create_task(hub.release_rest_hijack(worker_id, hijack_id))
    await asyncio.sleep(0)
    assert release_task.done() is False

    worker.release.set()
    assert await asyncio.wait_for(send_task, timeout=1.0) == (True, None)
    assert await asyncio.wait_for(release_task, timeout=1.0) == (True, True)
    assert len(worker.payloads) == 1


async def test_rest_send_route_uses_owner_reservation_before_delivery() -> None:
    worker = _GatedWorker("route-reserved-input")
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())
    worker_id = "rest-send-route-race"
    hijack_id = "abcdef12-0000-0000-0000-000000000000"
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    now = time.monotonic()
    async with hub._lock:
        hub.registry._workers[worker_id].hijack_session = HijackSession(
            hijack_id=hijack_id,
            owner="rest-client",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        send_task = asyncio.create_task(
            client.post(
                f"/worker/{worker_id}/hijack/{hijack_id}/send",
                json={"keys": "route-reserved-input"},
            )
        )
        await asyncio.wait_for(worker.entered.wait(), timeout=1.0)
        release_task = asyncio.create_task(hub.release_rest_hijack(worker_id, hijack_id))
        await asyncio.sleep(0)
        assert release_task.done() is False

        worker.release.set()
        response = await asyncio.wait_for(send_task, timeout=1.0)
        assert response.status_code == 200
        assert await asyncio.wait_for(release_task, timeout=1.0) == (True, True)


async def test_rest_step_route_blocks_expiry_transition_until_delivery() -> None:
    worker = _GatedWorker('"action":"step"')
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())
    worker_id = "rest-step-expiry-race"
    hijack_id = "abcdef12-1111-1111-1111-111111111111"
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    now = time.monotonic()
    async with hub._lock:
        hub.registry._workers[worker_id].hijack_session = HijackSession(
            hijack_id=hijack_id,
            owner="rest-client",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        step_task = asyncio.create_task(client.post(f"/worker/{worker_id}/hijack/{hijack_id}/step"))
        await asyncio.wait_for(worker.entered.wait(), timeout=1.0)
        async with hub._lock:
            session = hub.registry._workers[worker_id].hijack_session
            assert session is not None
            session.lease_expires_at = time.monotonic() - 1
        expiry_task = asyncio.create_task(hub.cleanup_expired_hijack(worker_id))
        await asyncio.sleep(0)
        assert expiry_task.done() is False

        worker.release.set()
        response = await asyncio.wait_for(step_task, timeout=1.0)
        assert response.status_code == 200
        assert await asyncio.wait_for(expiry_task, timeout=1.0) is True


async def test_expired_rest_owner_delivery_blocks_competing_acquisition() -> None:
    worker = _GatedWorker("reserved-before-competitor")
    hub = TermHub()
    worker_id = "rest-competitor-race"
    hijack_id = "rest-owner"
    competitor = AsyncMock()
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    now = time.monotonic()
    async with hub._lock:
        hub.registry._workers[worker_id].hijack_session = HijackSession(
            hijack_id=hijack_id,
            owner="rest-client",
            acquired_at=now,
            lease_expires_at=now + 60,
            last_heartbeat=now,
        )

    send_task = asyncio.create_task(
        hub.send_owned_worker(
            worker_id,
            {"type": "input", "data": "reserved-before-competitor"},
            rest_hijack_id=hijack_id,
        )
    )
    await asyncio.wait_for(worker.entered.wait(), timeout=1.0)
    async with hub._lock:
        session = hub.registry._workers[worker_id].hijack_session
        assert session is not None
        session.lease_expires_at = time.monotonic() - 1
    acquire_task = asyncio.create_task(hub.try_acquire_ws_hijack(worker_id, competitor))
    await asyncio.sleep(0)
    assert acquire_task.done() is False

    worker.release.set()
    assert await asyncio.wait_for(send_task, timeout=1.0) == (True, None)
    assert await asyncio.wait_for(acquire_task, timeout=1.0) == (True, None)


async def test_worker_replacement_waits_for_reserved_delivery() -> None:
    worker = _GatedWorker("reserved-before-replacement")
    replacement = AsyncMock()
    hub = TermHub()
    worker_id = "worker-replacement-race"
    owner = AsyncMock()
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    await hub.register_browser(worker_id, owner, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)

    send_task = asyncio.create_task(
        hub.send_owned_worker(
            worker_id,
            {"type": "input", "data": "reserved-before-replacement"},
            browser_ws=owner,
        )
    )
    await asyncio.wait_for(worker.entered.wait(), timeout=1.0)
    replace_task = asyncio.create_task(hub.register_worker(worker_id, replacement))
    await asyncio.sleep(0)
    assert replace_task.done() is False

    worker.release.set()
    assert await asyncio.wait_for(send_task, timeout=1.0) == (True, None)
    await asyncio.wait_for(replace_task, timeout=1.0)
    replacement.send_text.assert_not_awaited()
    assert len(worker.payloads) == 1


async def test_worker_registration_retries_if_idle_state_is_pruned() -> None:
    hub = TermHub()
    worker_id = "register-prune-race"
    stale_state = WorkerTermState()
    hub.registry._workers[worker_id] = stale_state
    await stale_state.owned_input_fence.acquire()
    replacement = AsyncMock()

    register_task = asyncio.create_task(hub.register_worker(worker_id, replacement))
    await asyncio.sleep(0)
    await hub.prune_if_idle(worker_id)
    stale_state.owned_input_fence.release()

    assert await asyncio.wait_for(register_task, timeout=1.0) is False
    async with hub._lock:
        current = hub.registry._workers[worker_id]
        assert current is not stale_state
        assert current.worker_ws is replacement


async def test_dead_owner_removal_waits_for_reserved_delivery() -> None:
    worker = _GatedWorker("reserved-before-disconnect")
    hub = TermHub()
    worker_id = "dead-owner-race"
    owner = AsyncMock()
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    await hub.register_browser(worker_id, owner, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)

    send_task = asyncio.create_task(
        hub.send_owned_worker(
            worker_id,
            {"type": "input", "data": "reserved-before-disconnect"},
            browser_ws=owner,
        )
    )
    await asyncio.wait_for(worker.entered.wait(), timeout=1.0)
    remove_task = asyncio.create_task(hub.remove_dead_browsers(worker_id, {owner}))
    await asyncio.sleep(0)
    assert remove_task.done() is False

    worker.release.set()
    assert await asyncio.wait_for(send_task, timeout=1.0) == (True, None)
    assert await asyncio.wait_for(remove_task, timeout=1.0) is True


async def test_owned_delivery_timeout_releases_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    from provide.uterm.server.bridge.hub import lease

    worker = _GatedWorker("never-finishes")
    hub = TermHub()
    worker_id = "owned-send-timeout"
    owner = AsyncMock()
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    await hub.register_browser(worker_id, owner, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)
    monkeypatch.setattr(lease, "_OWNED_INPUT_SEND_TIMEOUT_S", 0.01)

    result = await hub.send_owned_worker(
        worker_id,
        {"type": "input", "data": "never-finishes"},
        browser_ws=owner,
    )
    assert result == (False, "no_worker")
    assert await asyncio.wait_for(hub.try_release_ws_hijack(worker_id, owner), timeout=1.0) == (True, False)


async def test_approved_held_command_is_refused_after_owner_changes() -> None:
    hub = TermHub(policy_gate=_HoldPolicy())
    worker = AsyncMock()
    stale_owner = AsyncMock()
    competitor = AsyncMock()
    worker_id = "approval-owner-race"
    await hub.register_worker(worker_id, worker)
    await hub.register_browser(worker_id, stale_owner, "admin")
    await hub.register_browser(worker_id, competitor, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, stale_owner) == (True, None)

    await _handle_input(hub, stale_owner, worker_id, {"type": "input", "data": "held-command"})
    request = hub.approval_store.get("held-command")
    assert request is not None
    assert await hub.try_release_ws_hijack(worker_id, stale_owner) == (True, False)
    assert await hub.try_acquire_ws_hijack(worker_id, competitor) == (True, None)

    delivered, reason = await hub.resolve_approval(
        worker_id,
        request.id,
        PolicyDecision(action="allow"),
        request.command,
    )
    assert (delivered, reason) == (False, "invalid_owner")
    worker.send_text.assert_not_awaited()


async def test_approved_held_command_is_refused_after_same_socket_reacquires() -> None:
    hub = TermHub(policy_gate=_HoldPolicy())
    worker = AsyncMock()
    owner = AsyncMock()
    worker_id = "approval-owner-aba"
    await hub.register_worker(worker_id, worker)
    await hub.register_browser(worker_id, owner, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)

    await _handle_input(hub, owner, worker_id, {"type": "input", "data": "held-command"})
    request = hub.approval_store.get("held-command")
    assert request is not None
    assert await hub.try_release_ws_hijack(worker_id, owner) == (True, False)
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)

    result = await hub.resolve_approval(
        worker_id, request.id, PolicyDecision(action="allow"), request.command, approval_request=request
    )
    assert result == (False, "invalid_owner")
    worker.send_text.assert_not_awaited()


async def test_approval_and_fresh_buffered_input_are_one_ordered_operation() -> None:
    worker = _GatedWorker("approved-command")
    hub = TermHub()
    owner = AsyncMock()
    worker_id = "approval-ordered-replay"
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    await hub.register_browser(worker_id, owner, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)
    generation = await hub.capture_browser_ownership(worker_id, owner)
    assert generation is not None
    request = ApprovalRequest(
        "ordered",
        worker_id,
        "submitter",
        "approved-command",
        ApprovalStatus.PENDING,
        time.time(),
        time.time() + 60,
        origin_browser=owner,
        ownership_generation=generation,
    )
    assert hub.approval_store.add(request)
    stored = hub.approval_store.get("ordered")
    assert stored is not None
    hub._paused_browsers.add(owner)

    approval_task = asyncio.create_task(
        hub.resolve_approval(
            worker_id, "ordered", PolicyDecision(action="allow"), "caller-data", approval_request=stored
        )
    )
    await asyncio.wait_for(worker.entered.wait(), timeout=1.0)
    await _handle_input(hub, owner, worker_id, {"type": "input", "data": "fresh-input"})
    release_task = asyncio.create_task(hub.try_release_ws_hijack(worker_id, owner))
    await asyncio.sleep(0)
    assert release_task.done() is False

    worker.release.set()
    assert await asyncio.wait_for(approval_task, timeout=1.0) == (True, None)
    assert await asyncio.wait_for(release_task, timeout=1.0) == (True, False)
    assert "approved-command" in worker.payloads[0]
    assert "fresh-input" in worker.payloads[1]


async def test_replay_failure_does_not_retroactively_refuse_executed_command() -> None:
    worker = _FailSecondWorker()
    hub = TermHub()
    owner = AsyncMock()
    worker_id = "approval-partial-replay"
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    await hub.register_browser(worker_id, owner, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)
    generation = await hub.capture_browser_ownership(worker_id, owner)
    assert generation is not None
    request = ApprovalRequest(
        "partial",
        worker_id,
        "submitter",
        "approved-command",
        ApprovalStatus.PENDING,
        time.time(),
        time.time() + 60,
        origin_browser=owner,
        ownership_generation=generation,
    )
    assert hub.approval_store.add(request)
    stored = hub.approval_store.get("partial")
    assert stored is not None
    hub._paused_browsers.add(owner)
    hub._hold_buffers[owner] = "buffered-input"

    result = await hub.resolve_approval(
        worker_id, "partial", PolicyDecision(action="allow"), "caller-data", approval_request=stored
    )

    assert result == (True, "replay_failed")
    assert "approved-command" in worker.payloads[0]
