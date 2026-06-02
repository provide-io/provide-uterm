#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing unit tests for :class:`HijackLeaseManager` dashboard-WS methods.

Targets the dashboard-WebSocket / browser-input lease surface that does
*not* go through the REST session path:

* :meth:`HijackLeaseManager.try_acquire_ws`
* :meth:`HijackLeaseManager.try_release_ws`
* :meth:`HijackLeaseManager.touch_owner`
* :meth:`HijackLeaseManager.touch_if_owner`
* :meth:`HijackLeaseManager.prepare_browser_input`
* :meth:`HijackLeaseManager.still_hijacked`
* :meth:`HijackLeaseManager.is_input_open_mode`

Each test pins exact return values (both tuple elements + exact reason
strings), every state field the method mutates or clears, and each branch
is reached with a *distinct* outcome so an operator/comparison mutation
flips the asserted result. Mirrors the harness in ``test_lease.py``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

from provide.uterm.server.bridge.hub.ext import (
    EVENT_HIJACK_ACQUIRED,
    EVENT_HIJACK_RELEASED,
)
from provide.uterm.server.bridge.hub.lease import HijackLeaseManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState


class _FakeHub:
    """Minimal :class:`_LeaseHubCallbacks` impl for unit tests."""

    def __init__(self) -> None:
        self.send_worker_calls: list[tuple[str, dict[str, Any]]] = []
        self.notify_calls: list[tuple[str, bool, str | None]] = []
        self.broadcast_calls: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.metrics: list[str] = []
        self.prune_calls: list[str] = []
        self.recheck_calls: list[tuple[str, float]] = []
        self._mgr: HijackLeaseManager | None = None

    def is_hijacked(self, st: WorkerTermState) -> bool:
        return self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st)

    def is_dashboard_hijack_active(self, st: WorkerTermState) -> bool:
        return st.hijack_owner is not None and (
            st.hijack_owner_expires_at is None or st.hijack_owner_expires_at > time.monotonic()
        )

    def has_valid_rest_lease(self, st: WorkerTermState) -> bool:
        return st.hijack_session is not None and st.hijack_session.lease_expires_at > time.monotonic()

    def can_send_input(self, st: WorkerTermState, ws: Any) -> bool:
        return st.hijack_owner is ws or st.input_mode == "open"

    def metric(self, name: str, value: int = 1) -> None:
        self.metrics.append(name)

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        self.notify_calls.append((worker_id, enabled, owner))

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        self.send_worker_calls.append((worker_id, msg))
        return True

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcast_calls.append(worker_id)

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.events.append((worker_id, event_type))
        return {}

    async def prune_if_idle(self, worker_id: str) -> None:
        self.prune_calls.append(worker_id)

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        self.recheck_calls.append((worker_id, now))
        if self._mgr is not None:
            await self._mgr._recheck_and_resume(worker_id, now)


def _make_state(worker_id: str = "w1") -> WorkerTermState:
    """Create a registered worker state with a live worker_ws."""
    st = WorkerTermState()
    st.worker_ws = AsyncMock()
    return st


def _make_manager(
    *, dashboard_hijack_lease_s: int = 45
) -> tuple[HijackLeaseManager, WorkerRegistry, _FakeHub, asyncio.Lock]:
    """Construct a fresh manager + registry + fake hub for a single test."""
    registry = WorkerRegistry()
    lock = asyncio.Lock()
    hub = _FakeHub()
    mgr = HijackLeaseManager(
        registry=registry,
        lock=lock,
        dashboard_hijack_lease_s=dashboard_hijack_lease_s,
        hub=hub,
    )
    hub._mgr = mgr
    return mgr, registry, hub, lock


def _rest_session(*, hijack_id: str = "h", expires_in: float = 30.0) -> HijackSession:
    """A REST :class:`HijackSession` whose lease expires *expires_in* from now."""
    return HijackSession(hijack_id=hijack_id, owner="o", lease_expires_at=time.monotonic() + expires_in)


# ---------------------------------------------------------------------------
# try_acquire_ws
# ---------------------------------------------------------------------------


class TestTryAcquireWs:
    async def test_no_worker_when_state_missing(self) -> None:
        """``st is None`` branch -> (False, "no_worker"); nothing mutated."""
        mgr, registry, hub, _ = _make_manager()
        ws = AsyncMock()
        result = await mgr.try_acquire_ws("ghost", ws)
        assert result == (False, "no_worker")
        # Distinct from the no-worker-ws branch: no state exists to touch.
        assert hub.notify_calls == []

    async def test_no_worker_when_worker_ws_none(self) -> None:
        """``st.worker_ws is None`` reaches the SAME (False,"no_worker") via a different leg."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.worker_ws = None
        registry.put("w1", st)
        ws = AsyncMock()
        result = await mgr.try_acquire_ws("w1", ws)
        assert result == (False, "no_worker")
        # Owner must NOT be set on the no_worker path.
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

    async def test_already_hijacked_via_active_dashboard(self) -> None:
        """An existing fresh dashboard owner blocks acquisition."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        existing = AsyncMock()
        st.hijack_owner = existing
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        ws = AsyncMock()
        result = await mgr.try_acquire_ws("w1", ws)
        assert result == (False, "already_hijacked")
        # The incoming ws must NOT have stolen ownership.
        assert st.hijack_owner is existing

    async def test_already_hijacked_via_valid_rest_lease(self) -> None:
        """A valid REST lease (second predicate) blocks acquisition distinctly."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=30)
        registry.put("w1", st)
        ws = AsyncMock()
        result = await mgr.try_acquire_ws("w1", ws)
        assert result == (False, "already_hijacked")
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

    async def test_success_sets_owner_and_exact_expiry(self) -> None:
        """Happy path: owner == ws, expiry == monotonic()+ttl EXACTLY, returns (True, None)."""
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        registry.put("w1", st)
        ws = AsyncMock()
        before = time.monotonic()
        result = await mgr.try_acquire_ws("w1", ws)
        after = time.monotonic()
        assert result == (True, None)
        assert st.hijack_owner is ws
        assert st.hijack_owner_expires_at is not None
        # Expiry sits in [before+45, after+45]; ttl is exactly the clamped lease.
        assert before + 45 <= st.hijack_owner_expires_at <= after + 45

    async def test_success_uses_configured_ttl_not_a_constant(self) -> None:
        """A different configured TTL changes the expiry — pins the ttl source."""
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=120)
        st = _make_state()
        registry.put("w1", st)
        ws = AsyncMock()
        before = time.monotonic()
        result = await mgr.try_acquire_ws("w1", ws)
        after = time.monotonic()
        assert result == (True, None)
        assert st.hijack_owner_expires_at is not None
        assert before + 120 <= st.hijack_owner_expires_at <= after + 120

    async def test_expired_dashboard_owner_does_not_block(self) -> None:
        """A stale dashboard owner (expires_at <= now) is NOT active -> acquire succeeds.

        Pins the ``> now`` strict boundary inside is_dashboard_hijack_active:
        a past expiry must let the new owner through.
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        ws = AsyncMock()
        result = await mgr.try_acquire_ws("w1", ws)
        assert result == (True, None)
        assert st.hijack_owner is ws

    async def test_expired_rest_lease_does_not_block(self) -> None:
        """A stale REST lease (lease_expires_at <= now) does not block acquisition."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)
        ws = AsyncMock()
        result = await mgr.try_acquire_ws("w1", ws)
        assert result == (True, None)
        assert st.hijack_owner is ws

    async def test_success_emits_exact_span_and_acquired_log(self) -> None:
        """Success path traces the exact span and logs the EXACT acquired event.

        Pins the span name, the ``{"worker_id": wid}`` attributes dict, the
        ``EVENT_HIJACK_ACQUIRED`` constant, and every structured kwarg
        (``hijack_type="dashboard"``, ``lease_s`` == configured TTL). Kills
        span-name/attribute-key, event-const, kwarg-drop, and string
        case-flip mutations on these calls.
        """
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        registry.put("w1", st)
        ws = AsyncMock()
        with (
            patch("provide.uterm.server.bridge.hub.lease.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr,
        ):
            result = await mgr.try_acquire_ws("w1", ws)
        assert result == (True, None)
        mtr.start_as_current_span.assert_called_once_with("uterm.hijack.acquire.ws", attributes={"worker_id": "w1"})
        mlog.info.assert_called_once_with(EVENT_HIJACK_ACQUIRED, worker_id="w1", hijack_type="dashboard", lease_s=45)


# ---------------------------------------------------------------------------
# try_release_ws
# ---------------------------------------------------------------------------


class TestTryReleaseWs:
    async def test_release_owner_clears_fields_no_rest(self) -> None:
        """Owner releases: (True, False), owner + expiry cleared to None."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        ws = AsyncMock()
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        result = await mgr.try_release_ws("w1", ws)
        assert result == (True, False)
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

    async def test_release_owner_reports_rest_active_true(self) -> None:
        """Owner releases while a valid REST lease remains -> (True, True)."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        ws = AsyncMock()
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 30
        st.hijack_session = _rest_session(expires_in=30)
        registry.put("w1", st)
        result = await mgr.try_release_ws("w1", ws)
        assert result == (True, True)
        # Dashboard slot cleared; REST slot is untouched by this method.
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None
        assert st.hijack_session is not None

    async def test_release_owner_rest_expired_reports_false(self) -> None:
        """A stale REST lease on the owner-release path yields rest_active False."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        ws = AsyncMock()
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 30
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)
        result = await mgr.try_release_ws("w1", ws)
        assert result == (True, False)

    async def test_non_owner_rejected_rest_active_false(self) -> None:
        """Wrong ws: (False, False); owner untouched."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        owner = AsyncMock()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        other = AsyncMock()
        result = await mgr.try_release_ws("w1", other)
        assert result == (False, False)
        assert st.hijack_owner is owner
        assert st.hijack_owner_expires_at is not None

    async def test_non_owner_rejected_reports_rest_active_true(self) -> None:
        """Wrong ws but a valid REST lease present -> (False, True)."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        owner = AsyncMock()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = time.monotonic() + 30
        st.hijack_session = _rest_session(expires_in=30)
        registry.put("w1", st)
        other = AsyncMock()
        result = await mgr.try_release_ws("w1", other)
        assert result == (False, True)
        assert st.hijack_owner is owner

    async def test_missing_worker_returns_false_false(self) -> None:
        """``st is None`` short-circuits rest_active to False (the ``st is not None`` guard)."""
        mgr, registry, hub, _ = _make_manager()
        other = AsyncMock()
        result = await mgr.try_release_ws("ghost", other)
        assert result == (False, False)

    async def test_inactive_dashboard_with_valid_rest_returns_false_true(self) -> None:
        """No active dashboard lease, but a live REST lease -> rejected with rest_active True.

        Reaches the rejection branch via ``not is_dashboard_hijack_active`` (owner None)
        while still computing rest_active from the surviving REST session.
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=30)
        registry.put("w1", st)
        ws = AsyncMock()
        result = await mgr.try_release_ws("w1", ws)
        assert result == (False, True)
        assert st.hijack_session is not None

    async def test_owner_release_emits_exact_released_log_no_trace(self) -> None:
        """Owner-release success logs the EXACT released event and traces nothing.

        Pins ``EVENT_HIJACK_RELEASED`` plus ``worker_id`` /
        ``hijack_type="dashboard"`` kwargs (kills event-const, kwarg-drop,
        and string case-flip mutations) and asserts ``try_release_ws`` opens
        no span (the released path has no tracer call).
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        ws = AsyncMock()
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        with (
            patch("provide.uterm.server.bridge.hub.lease.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr,
        ):
            result = await mgr.try_release_ws("w1", ws)
        assert result == (True, False)
        mlog.info.assert_called_once_with(EVENT_HIJACK_RELEASED, worker_id="w1", hijack_type="dashboard")
        mtr.start_as_current_span.assert_not_called()

    async def test_non_owner_rejection_does_not_log_released(self) -> None:
        """The rejection branch must NOT emit the released log.

        Guards against a mutation that hoists the ``logger.info`` above the
        ownership check: a wrong-ws release logs nothing.
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        owner = AsyncMock()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        other = AsyncMock()
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            result = await mgr.try_release_ws("w1", other)
        assert result == (False, False)
        mlog.info.assert_not_called()


# ---------------------------------------------------------------------------
# touch_owner
# ---------------------------------------------------------------------------


class TestTouchOwner:
    async def test_returns_none_when_worker_missing(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        assert await mgr.touch_owner("ghost") is None

    async def test_returns_none_when_no_owner(self) -> None:
        """``st.hijack_owner is None`` -> None; expiry stays untouched."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        assert await mgr.touch_owner("w1") is None
        assert st.hijack_owner_expires_at is None

    async def test_extends_with_default_ttl_exactly(self) -> None:
        """lease_s=None uses dashboard ttl; new expiry == monotonic()+ttl and is returned."""
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic()
        registry.put("w1", st)
        before = time.monotonic()
        new_exp = await mgr.touch_owner("w1")
        after = time.monotonic()
        assert new_exp is not None
        assert st.hijack_owner_expires_at == new_exp
        assert before + 45 <= new_exp <= after + 45

    async def test_extends_with_explicit_lease_exactly(self) -> None:
        """An explicit in-range lease_s overrides the default ttl."""
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic()
        registry.put("w1", st)
        before = time.monotonic()
        new_exp = await mgr.touch_owner("w1", lease_s=120)
        after = time.monotonic()
        assert new_exp is not None
        assert before + 120 <= new_exp <= after + 120
        assert st.hijack_owner_expires_at == new_exp

    async def test_clamps_lease_above_ceiling_to_600(self) -> None:
        """lease_s far above the ceiling is clamped to 600."""
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic()
        registry.put("w1", st)
        before = time.monotonic()
        new_exp = await mgr.touch_owner("w1", lease_s=10_000)
        after = time.monotonic()
        assert new_exp is not None
        assert before + 600 <= new_exp <= after + 600

    async def test_clamps_lease_below_floor_to_1(self) -> None:
        """lease_s below the floor is clamped up to 1."""
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic()
        registry.put("w1", st)
        before = time.monotonic()
        new_exp = await mgr.touch_owner("w1", lease_s=0)
        after = time.monotonic()
        assert new_exp is not None
        assert before + 1 <= new_exp <= after + 1


# ---------------------------------------------------------------------------
# touch_if_owner
# ---------------------------------------------------------------------------


class TestTouchIfOwner:
    async def test_returns_none_when_worker_missing(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        assert await mgr.touch_if_owner("ghost", AsyncMock()) is None

    async def test_returns_none_when_no_active_dashboard(self) -> None:
        """Owner unset -> not dashboard-active -> None even if ws passed in."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        assert await mgr.touch_if_owner("w1", AsyncMock()) is None
        assert st.hijack_owner_expires_at is None

    async def test_returns_none_when_ws_is_not_owner(self) -> None:
        """Active dashboard but a different ws -> None; owner's expiry untouched."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        owner = AsyncMock()
        original_exp = time.monotonic() + 30
        st.hijack_owner = owner
        st.hijack_owner_expires_at = original_exp
        registry.put("w1", st)
        assert await mgr.touch_if_owner("w1", AsyncMock()) is None
        assert st.hijack_owner_expires_at == original_exp

    async def test_returns_none_when_owner_lease_expired(self) -> None:
        """Owner matches ws but lease already expired -> not active -> None."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        ws = AsyncMock()
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        assert await mgr.touch_if_owner("w1", ws) is None

    async def test_extends_for_active_owner_exactly(self) -> None:
        """ws is the active owner -> expiry == monotonic()+ttl and is returned."""
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        ws = AsyncMock()
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 5
        registry.put("w1", st)
        before = time.monotonic()
        new_exp = await mgr.touch_if_owner("w1", ws)
        after = time.monotonic()
        assert new_exp is not None
        assert st.hijack_owner_expires_at == new_exp
        assert before + 45 <= new_exp <= after + 45


# ---------------------------------------------------------------------------
# prepare_browser_input
# ---------------------------------------------------------------------------


class TestPrepareBrowserInput:
    async def test_returns_false_when_worker_missing(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        assert await mgr.prepare_browser_input("ghost", AsyncMock()) is False

    async def test_owner_allowed_and_lease_extended_exactly(self) -> None:
        """Active owner: allowed True AND dashboard lease extended to monotonic()+ttl."""
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        ws = AsyncMock()
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 1
        registry.put("w1", st)
        before = time.monotonic()
        allowed = await mgr.prepare_browser_input("w1", ws)
        after = time.monotonic()
        assert allowed is True
        assert st.hijack_owner_expires_at is not None
        assert before + 45 <= st.hijack_owner_expires_at <= after + 45

    async def test_open_mode_non_owner_allowed_but_lease_not_extended(self) -> None:
        """open input mode permits a non-owner ws WITHOUT extending the owner's lease.

        Pins the second guard: extension fires only when ws *is* the active
        owner, not merely because input is permitted.
        """
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        owner = AsyncMock()
        original_exp = time.monotonic() + 5
        st.hijack_owner = owner
        st.hijack_owner_expires_at = original_exp
        st.input_mode = "open"
        registry.put("w1", st)
        other = AsyncMock()
        allowed = await mgr.prepare_browser_input("w1", other)
        assert allowed is True
        # Not the owner -> no extension; owner's expiry is unchanged.
        assert st.hijack_owner_expires_at == original_exp

    async def test_hijack_mode_non_owner_denied(self) -> None:
        """hijack mode + non-owner ws -> denied (False); no lease extension."""
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        owner = AsyncMock()
        original_exp = time.monotonic() + 5
        st.hijack_owner = owner
        st.hijack_owner_expires_at = original_exp
        st.input_mode = "hijack"
        registry.put("w1", st)
        other = AsyncMock()
        allowed = await mgr.prepare_browser_input("w1", other)
        assert allowed is False
        assert st.hijack_owner_expires_at == original_exp

    async def test_idle_open_mode_allows_input(self) -> None:
        """No owner at all, open mode -> allowed True; nothing to extend."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.input_mode = "open"
        registry.put("w1", st)
        ws = AsyncMock()
        allowed = await mgr.prepare_browser_input("w1", ws)
        assert allowed is True
        assert st.hijack_owner_expires_at is None

    async def test_owner_with_expired_lease_allowed_but_not_extended(self) -> None:
        """ws is owner but its lease already expired (not dashboard-active).

        ``can_send_input`` still returns True (identity match), but the
        extension guard requires ``is_dashboard_hijack_active`` so the
        expiry stays in the past.
        """
        mgr, registry, hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        ws = AsyncMock()
        past = time.monotonic() - 1
        st.hijack_owner = ws
        st.hijack_owner_expires_at = past
        st.input_mode = "hijack"
        registry.put("w1", st)
        allowed = await mgr.prepare_browser_input("w1", ws)
        assert allowed is True
        # Lease was NOT refreshed because the dashboard lease was inactive.
        assert st.hijack_owner_expires_at == past


# ---------------------------------------------------------------------------
# still_hijacked
# ---------------------------------------------------------------------------


class TestStillHijacked:
    async def test_false_when_worker_missing(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        assert await mgr.still_hijacked("ghost") is False

    async def test_false_when_idle(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        registry.put("w1", _make_state())
        assert await mgr.still_hijacked("w1") is False

    async def test_true_for_active_dashboard_owner(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        assert await mgr.still_hijacked("w1") is True

    async def test_true_for_valid_rest_lease(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=30)
        registry.put("w1", st)
        assert await mgr.still_hijacked("w1") is True

    async def test_false_when_both_leases_expired(self) -> None:
        """Stale dashboard + stale REST -> not hijacked (boundary on both predicates)."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)
        assert await mgr.still_hijacked("w1") is False


# ---------------------------------------------------------------------------
# is_input_open_mode
# ---------------------------------------------------------------------------


class TestIsInputOpenMode:
    async def test_false_when_worker_missing(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        assert await mgr.is_input_open_mode("ghost") is False

    async def test_true_when_open(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.input_mode = "open"
        registry.put("w1", st)
        assert await mgr.is_input_open_mode("w1") is True

    async def test_false_when_hijack_mode(self) -> None:
        """The default 'hijack' mode is NOT open -> False (pins the == 'open' compare)."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.input_mode = "hijack"
        registry.put("w1", st)
        assert await mgr.is_input_open_mode("w1") is False
