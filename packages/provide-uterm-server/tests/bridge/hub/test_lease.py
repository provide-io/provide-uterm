#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the :class:`HijackLeaseManager` service.

These cover the new service surface directly (``try_acquire_rest``,
``try_acquire_ws``, ``touch_owner``, ``release_rest`` etc.) plus the
``_get_rest_session_no_cleanup`` helper that the ownership-mixin shim
relies on. The existing ``test_hub_ownership_mutations*`` files
continue to exercise the legacy mixin entry points; this file is the
canonical unit-test for the extracted service.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
        # Forward to the real manager so existing test expectations (worker
        # ``send_worker`` resume frame, ``notify_hijack_changed`` callback)
        # are still satisfied; the manager itself records all side effects
        # on this fake hub via its other callbacks.
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


def test_init_clamps_dashboard_lease_to_minimum() -> None:
    """A zero TTL must be clamped up to the floor of 1 second."""
    mgr, *_ = _make_manager(dashboard_hijack_lease_s=0)
    assert mgr.dashboard_hijack_lease_s == 1


def test_init_clamps_dashboard_lease_to_maximum() -> None:
    """A huge TTL must be clamped down to the 600 second ceiling."""
    mgr, *_ = _make_manager(dashboard_hijack_lease_s=10_000)
    assert mgr.dashboard_hijack_lease_s == 600


def test_dashboard_lease_setter_reclamps() -> None:
    mgr, *_ = _make_manager()
    mgr.dashboard_hijack_lease_s = -50
    assert mgr.dashboard_hijack_lease_s == 1
    mgr.dashboard_hijack_lease_s = 5000
    assert mgr.dashboard_hijack_lease_s == 600


@pytest.mark.asyncio
async def test_try_acquire_ws_no_worker_returns_error() -> None:
    mgr, registry, *_ = _make_manager()
    ok, err = await mgr.try_acquire_ws("missing", AsyncMock())
    assert (ok, err) == (False, "no_worker")


@pytest.mark.asyncio
async def test_try_acquire_ws_sets_owner_and_expiry() -> None:
    mgr, registry, hub, _ = _make_manager()
    st = _make_state()
    registry.put("w1", st)
    ws = AsyncMock()
    ok, err = await mgr.try_acquire_ws("w1", ws)
    assert (ok, err) == (True, None)
    assert st.hijack_owner is ws
    assert st.hijack_owner_expires_at is not None


@pytest.mark.asyncio
async def test_try_acquire_ws_already_hijacked_via_rest() -> None:
    mgr, registry, hub, _ = _make_manager()
    st = _make_state()
    st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
    registry.put("w1", st)
    ok, err = await mgr.try_acquire_ws("w1", AsyncMock())
    assert (ok, err) == (False, "already_hijacked")


@pytest.mark.asyncio
async def test_touch_owner_returns_none_when_no_owner() -> None:
    mgr, registry, *_ = _make_manager()
    registry.put("w1", _make_state())
    assert await mgr.touch_owner("w1") is None


@pytest.mark.asyncio
async def test_touch_owner_extends_lease() -> None:
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = time.monotonic()
    registry.put("w1", st)
    new_exp = await mgr.touch_owner("w1", lease_s=120)
    assert new_exp is not None
    assert st.hijack_owner_expires_at == new_exp


@pytest.mark.asyncio
async def test_touch_owner_clamps_lease() -> None:
    """``lease_s`` outside [1, 600] must be clamped before extending."""
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = time.monotonic()
    registry.put("w1", st)
    before = time.monotonic()
    await mgr.touch_owner("w1", lease_s=10_000)
    # Upper clamp of 600 means the new expiry sits well under +10000.
    assert st.hijack_owner_expires_at is not None
    assert st.hijack_owner_expires_at - before <= 601


@pytest.mark.asyncio
async def test_try_release_ws_clears_owner() -> None:
    mgr, registry, hub, _ = _make_manager()
    st = _make_state()
    ws = AsyncMock()
    st.hijack_owner = ws
    st.hijack_owner_expires_at = time.monotonic() + 30
    registry.put("w1", st)
    released, rest_active = await mgr.try_release_ws("w1", ws)
    assert released is True
    assert rest_active is False
    assert st.hijack_owner is None
    assert st.hijack_owner_expires_at is None


@pytest.mark.asyncio
async def test_try_release_ws_rejects_non_owner() -> None:
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    owner = AsyncMock()
    st.hijack_owner = owner
    st.hijack_owner_expires_at = time.monotonic() + 30
    registry.put("w1", st)
    other = AsyncMock()
    released, rest_active = await mgr.try_release_ws("w1", other)
    assert released is False
    assert st.hijack_owner is owner  # untouched


@pytest.mark.asyncio
async def test_release_rest_clears_session() -> None:
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
    registry.put("w1", st)
    released, should_resume = await mgr.release_rest("w1", "h")
    assert released is True
    assert should_resume is True
    assert st.hijack_session is None


@pytest.mark.asyncio
async def test_release_rest_rejects_id_mismatch() -> None:
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    st.hijack_session = HijackSession(hijack_id="real", owner="o", lease_expires_at=time.monotonic() + 30)
    registry.put("w1", st)
    released, should_resume = await mgr.release_rest("w1", "fake")
    assert released is False
    assert should_resume is False
    assert st.hijack_session is not None


@pytest.mark.asyncio
async def test_check_valid_returns_false_when_expired() -> None:
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() - 1)
    registry.put("w1", st)
    assert await mgr.check_valid("w1", "h") is False


@pytest.mark.asyncio
async def test_still_hijacked_false_for_idle() -> None:
    mgr, registry, *_ = _make_manager()
    registry.put("w1", _make_state())
    assert await mgr.still_hijacked("w1") is False


@pytest.mark.asyncio
async def test_still_hijacked_false_when_unknown_worker() -> None:
    mgr, *_ = _make_manager()
    assert await mgr.still_hijacked("ghost") is False


@pytest.mark.asyncio
async def test_is_input_open_mode_reflects_state() -> None:
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    st.input_mode = "open"
    registry.put("w1", st)
    assert await mgr.is_input_open_mode("w1") is True


@pytest.mark.asyncio
async def test_is_input_open_mode_missing_worker() -> None:
    mgr, *_ = _make_manager()
    assert await mgr.is_input_open_mode("ghost") is False


@pytest.mark.asyncio
async def test_prepare_browser_input_returns_false_for_unknown() -> None:
    mgr, *_ = _make_manager()
    assert await mgr.prepare_browser_input("ghost", AsyncMock()) is False


@pytest.mark.asyncio
async def test_prepare_browser_input_extends_lease_for_owner() -> None:
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    ws = AsyncMock()
    st.hijack_owner = ws
    st.hijack_owner_expires_at = time.monotonic() + 1
    registry.put("w1", st)
    assert await mgr.prepare_browser_input("w1", ws) is True
    # Lease was extended.
    assert st.hijack_owner_expires_at is not None
    assert st.hijack_owner_expires_at > time.monotonic() + 30


@pytest.mark.asyncio
async def test_get_fresh_expiry_returns_fallback_for_missing_session() -> None:
    mgr, registry, *_ = _make_manager()
    registry.put("w1", _make_state())
    assert await mgr.get_fresh_expiry("w1", "h", fallback=42.0) == 42.0


@pytest.mark.asyncio
async def test_get_events_data_filters_by_after_seq_and_limit() -> None:
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    st.event_seq = 5
    st.min_event_seq = 0
    for i in range(1, 6):
        st.events.append({"seq": i, "type": f"e{i}"})
    registry.put("w1", st)
    hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
    data = await mgr.get_events_data("w1", "h", hs, after_seq=2, limit=2)
    assert [r["seq"] for r in data["rows"]] == [3, 4]
    assert data["latest_seq"] == 5
    assert data["fresh_expires"] == hs.lease_expires_at


@pytest.mark.asyncio
async def test_cleanup_expired_returns_false_for_idle() -> None:
    mgr, registry, *_ = _make_manager()
    registry.put("w1", _make_state())
    assert await mgr.cleanup_expired("w1") is False


@pytest.mark.asyncio
async def test_cleanup_expired_returns_false_for_unknown() -> None:
    mgr, *_ = _make_manager()
    assert await mgr.cleanup_expired("ghost") is False


@pytest.mark.asyncio
async def test_remove_dead_browsers_keeps_owner_when_other_dies() -> None:
    mgr, registry, hub, _ = _make_manager()
    st = _make_state()
    owner = AsyncMock()
    other = AsyncMock()
    st.hijack_owner = owner
    st.hijack_owner_expires_at = time.monotonic() + 30
    st.browsers[owner] = "admin"
    st.browsers[other] = "viewer"
    registry.put("w1", st)
    changed = await mgr.remove_dead_browsers("w1", {other})
    assert changed is False
    assert st.hijack_owner is owner
    assert other not in st.browsers


def test_compute_lease_expirations_both_idle() -> None:
    """Static helper returns (False, False) when no leases are set."""
    st = _make_state()
    assert HijackLeaseManager.compute_lease_expirations(st, time.monotonic()) == (False, False)


@pytest.mark.asyncio
async def test_cleanup_expired_rest_lease_emits_events() -> None:
    """``cleanup_expired`` (service path) walks the full event/broadcast pipeline."""
    mgr, registry, hub, _ = _make_manager()
    st = _make_state()
    st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() - 1)
    registry.put("w1", st)
    cleaned = await mgr.cleanup_expired("w1")
    assert cleaned is True
    assert ("w1", "hijack_lease_expired") in hub.events
    assert "w1" in hub.broadcast_calls
    assert "w1" in hub.prune_calls
    assert "hijack_lease_expiries_total" in hub.metrics
    # The resume control frame was dispatched once both slots went idle.
    assert any(m.get("action") == "resume" for _, m in hub.send_worker_calls)


@pytest.mark.asyncio
async def test_cleanup_expired_dashboard_lease_emits_owner_expired_event() -> None:
    """Dashboard lease expiry hits the ``hijack_owner_expired`` audit row."""
    mgr, registry, hub, _ = _make_manager()
    st = _make_state()
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = time.monotonic() - 1
    registry.put("w1", st)
    cleaned = await mgr.cleanup_expired("w1")
    assert cleaned is True
    assert ("w1", "hijack_owner_expired") in hub.events


@pytest.mark.asyncio
async def test_get_rest_session_runs_cleanup_then_returns_session() -> None:
    """``get_rest_session`` is the service-level entrypoint: cleanup → lookup."""
    mgr, registry, *_ = _make_manager()
    st = _make_state()
    hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
    st.hijack_session = hs
    registry.put("w1", st)
    result = await mgr.get_rest_session("w1", "h")
    assert result is hs


@pytest.mark.asyncio
async def test_cleanup_expired_partial_expiry_skips_resume() -> None:
    """One slot expires while the other holds — no resume frame is emitted."""
    mgr, registry, hub, _ = _make_manager()
    st = _make_state()
    # REST session is stale, dashboard owner is still fresh.
    st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() - 1)
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = time.monotonic() + 30
    registry.put("w1", st)
    cleaned = await mgr.cleanup_expired("w1")
    assert cleaned is True
    # Only the rest path expired; should_resume is False because the
    # dashboard lease is still live, so no resume control frame fires.
    assert hub.send_worker_calls == []
    assert hub.notify_calls == []
    assert ("w1", "hijack_lease_expired") in hub.events


@pytest.mark.asyncio
async def test_recheck_and_resume_skips_send_when_already_hijacked() -> None:
    """Concurrent acquire between expiry and resume must suppress the resume."""
    mgr, registry, hub, _ = _make_manager()
    st = _make_state()
    # Make is_hijacked() report True via an active dashboard owner.
    st.hijack_owner = AsyncMock()
    st.hijack_owner_expires_at = time.monotonic() + 30
    registry.put("w1", st)
    await mgr._recheck_and_resume("w1", time.monotonic())
    assert hub.send_worker_calls == []
    assert hub.notify_calls == []
