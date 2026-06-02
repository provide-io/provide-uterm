#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing unit tests for the expiry-sweep surface of
:class:`HijackLeaseManager`.

Targets the four lease-expiry methods:

* ``compute_lease_expirations`` — pure static boundary helper.
* ``_expire_leases_under_lock`` — clears stale sub-leases under lock.
* ``_recheck_and_resume`` — re-validates then emits the resume frame.
* ``cleanup_expired`` — the orchestration that ties them together.

Every assertion pins an EXACT value (tuple elements, frame fields,
callback args, recorded call order) so an operator/constant mutation
in the target flips a concrete expectation rather than merely changing
a value a looser test would still accept.

Construction mirrors ``test_lease.py`` exactly: the manager is built
with ``HijackLeaseManager(registry, lock, dashboard_hijack_lease_s,
hub=fake)`` and the fake hub forwards ``_recheck_and_resume`` back to
the real manager so the resume side-effects are exercised end to end.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

from provide.uterm.server.bridge.hub.ext import EVENT_HIJACK_EXPIRED
from provide.uterm.server.bridge.hub.lease import HijackLeaseManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState


class _RecordingHub:
    """``_LeaseHubCallbacks`` impl that records a unified, ordered call log.

    In addition to the per-callback buckets used by ``test_lease.py``,
    every callback appends a tagged tuple to :attr:`seq` so tests can
    assert the exact ORDER cleanup_expired dispatches its side-effects
    (metric → recheck → append_event(s) → broadcast → prune).
    """

    def __init__(self) -> None:
        self.send_worker_calls: list[tuple[str, dict[str, Any]]] = []
        self.notify_calls: list[tuple[str, bool, str | None]] = []
        self.broadcast_calls: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.metrics: list[str] = []
        self.prune_calls: list[str] = []
        self.recheck_calls: list[tuple[str, float]] = []
        self.seq: list[tuple[str, Any]] = []
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
        self.seq.append(("metric", name))

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        self.notify_calls.append((worker_id, enabled, owner))
        self.seq.append(("notify", (worker_id, enabled, owner)))

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        self.send_worker_calls.append((worker_id, msg))
        self.seq.append(("send_worker", (worker_id, msg)))
        return True

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcast_calls.append(worker_id)
        self.seq.append(("broadcast", worker_id))

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.events.append((worker_id, event_type))
        self.seq.append(("append_event", (worker_id, event_type)))
        return {}

    async def prune_if_idle(self, worker_id: str) -> None:
        self.prune_calls.append(worker_id)
        self.seq.append(("prune", worker_id))

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        self.recheck_calls.append((worker_id, now))
        self.seq.append(("recheck", (worker_id, now)))
        if self._mgr is not None:
            await self._mgr._recheck_and_resume(worker_id, now)


def _make_state(worker_id: str = "w1") -> WorkerTermState:
    """Create a registered worker state with a live worker_ws."""
    st = WorkerTermState()
    st.worker_ws = AsyncMock()
    return st


def _make_manager(
    *, dashboard_hijack_lease_s: int = 45
) -> tuple[HijackLeaseManager, WorkerRegistry, _RecordingHub, asyncio.Lock]:
    """Construct a fresh manager + registry + recording hub for one test."""
    registry = WorkerRegistry()
    lock = asyncio.Lock()
    hub = _RecordingHub()
    mgr = HijackLeaseManager(
        registry=registry,
        lock=lock,
        dashboard_hijack_lease_s=dashboard_hijack_lease_s,
        hub=hub,
    )
    hub._mgr = mgr
    return mgr, registry, hub, lock


def _rest_session(*, hijack_id: str = "h", owner: str = "o", expires_in: float) -> HijackSession:
    return HijackSession(hijack_id=hijack_id, owner=owner, lease_expires_at=time.monotonic() + expires_in)


def _seq_tags(hub: _RecordingHub) -> list[str]:
    return [tag for tag, _ in hub.seq]


# =====================================================================
# compute_lease_expirations  (static boundary helper, 123-129)
# =====================================================================


class TestComputeLeaseExpirations:
    """Pin BOTH tuple elements + the ``<= now`` boundary for each slot."""

    def test_both_idle_returns_false_false(self) -> None:
        st = _make_state()
        now = time.monotonic()
        assert HijackLeaseManager.compute_lease_expirations(st, now) == (False, False)

    def test_rest_in_past_only_returns_browser_false_rest_true(self) -> None:
        st = _make_state()
        now = 1000.0
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=999.0)
        # Return order is (browser_expired, rest_expired).
        assert HijackLeaseManager.compute_lease_expirations(st, now) == (False, True)

    def test_rest_in_future_returns_false_false(self) -> None:
        st = _make_state()
        now = 1000.0
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=1001.0)
        assert HijackLeaseManager.compute_lease_expirations(st, now) == (False, False)

    def test_rest_at_exact_now_counts_as_expired(self) -> None:
        """Boundary: ``lease_expires_at <= now`` so equality expires."""
        st = _make_state()
        now = 1000.0
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=1000.0)
        assert HijackLeaseManager.compute_lease_expirations(st, now) == (False, True)

    def test_browser_in_past_only_returns_browser_true_rest_false(self) -> None:
        st = _make_state()
        now = 1000.0
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = 999.0
        assert HijackLeaseManager.compute_lease_expirations(st, now) == (True, False)

    def test_browser_in_future_returns_false_false(self) -> None:
        st = _make_state()
        now = 1000.0
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = 1001.0
        assert HijackLeaseManager.compute_lease_expirations(st, now) == (False, False)

    def test_browser_at_exact_now_counts_as_expired(self) -> None:
        st = _make_state()
        now = 1000.0
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = 1000.0
        assert HijackLeaseManager.compute_lease_expirations(st, now) == (True, False)

    def test_browser_owner_set_but_no_expiry_is_not_expired(self) -> None:
        """``ws_expires_at is None`` must short-circuit to not-expired."""
        st = _make_state()
        now = 1000.0
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = None
        assert HijackLeaseManager.compute_lease_expirations(st, now) == (False, False)

    def test_both_expired_returns_true_true(self) -> None:
        st = _make_state()
        now = 1000.0
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = 998.0
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=999.0)
        assert HijackLeaseManager.compute_lease_expirations(st, now) == (True, True)


# =====================================================================
# _expire_leases_under_lock  (133-146)
# =====================================================================


class TestExpireLeasesUnderLock:
    """Pin the None sentinels, the 3-tuple, and the should_resume logic."""

    async def test_missing_worker_returns_none(self) -> None:
        mgr, _registry, _hub, _ = _make_manager()
        assert await mgr._expire_leases_under_lock("ghost", time.monotonic()) is None

    async def test_idle_worker_returns_none(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        registry.put("w1", _make_state())
        assert await mgr._expire_leases_under_lock("w1", time.monotonic()) is None

    async def test_rest_expired_alone_returns_tuple_and_clears_session(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)
        now = time.monotonic()
        result = await mgr._expire_leases_under_lock("w1", now)
        # rest expired, dashboard not, idle afterwards → resume.
        assert result == (True, False, True)
        assert st.hijack_session is None

    async def test_dashboard_expired_alone_returns_tuple_and_clears_owner(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        result = await mgr._expire_leases_under_lock("w1", time.monotonic())
        assert result == (False, True, True)
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None

    async def test_partial_expiry_leaves_live_slot_and_blocks_resume(self) -> None:
        """REST stale, dashboard live → resume is False (still hijacked)."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        owner = AsyncMock()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        result = await mgr._expire_leases_under_lock("w1", time.monotonic())
        assert result == (True, False, False)
        # Stale REST slot cleared; live dashboard slot preserved.
        assert st.hijack_session is None
        assert st.hijack_owner is owner

    async def test_live_only_returns_no_expiry_and_no_resume(self) -> None:
        """Occupied but fresh: not idle, nothing expires → (False, False, False)."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=30)
        registry.put("w1", st)
        result = await mgr._expire_leases_under_lock("w1", time.monotonic())
        assert result == (False, False, False)
        # Nothing expired so the session must be untouched.
        assert st.hijack_session is not None

    async def test_both_expired_returns_resume_true(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        result = await mgr._expire_leases_under_lock("w1", time.monotonic())
        assert result == (True, True, True)
        assert st.hijack_session is None
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None


# =====================================================================
# _recheck_and_resume  (148-158)
# =====================================================================


class TestRecheckAndResume:
    """Pin the resume frame fields + notify args, gated on is_hijacked."""

    async def test_resume_frame_and_notify_when_idle(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        registry.put("w1", _make_state())  # fully idle → not hijacked
        now = 1234.5
        await mgr._recheck_and_resume("w1", now)
        # Exactly one resume frame with every field pinned.
        assert len(hub.send_worker_calls) == 1
        worker_id, frame = hub.send_worker_calls[0]
        assert worker_id == "w1"
        assert frame == {
            "type": "control",
            "action": "resume",
            "owner": "lease-expired",
            "lease_s": 0,
            "ts": now,
        }
        # Exactly one notify with enabled False / owner None.
        assert hub.notify_calls == [("w1", False, None)]

    async def test_no_send_when_dashboard_hijack_active(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        await mgr._recheck_and_resume("w1", time.monotonic())
        assert hub.send_worker_calls == []
        assert hub.notify_calls == []

    async def test_no_send_when_rest_lease_active(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=30)
        registry.put("w1", st)
        await mgr._recheck_and_resume("w1", time.monotonic())
        assert hub.send_worker_calls == []
        assert hub.notify_calls == []

    async def test_missing_worker_still_sends_resume(self) -> None:
        """st2 is None → guard is False → resume fires (clears stale UI)."""
        mgr, _registry, hub, _ = _make_manager()
        now = 77.0
        await mgr._recheck_and_resume("ghost", now)
        assert hub.send_worker_calls == [
            (
                "ghost",
                {
                    "type": "control",
                    "action": "resume",
                    "owner": "lease-expired",
                    "lease_s": 0,
                    "ts": now,
                },
            )
        ]
        assert hub.notify_calls == [("ghost", False, None)]

    async def test_send_precedes_notify(self) -> None:
        """Frame must go out before the local notify callback fires."""
        mgr, registry, hub, _ = _make_manager()
        registry.put("w1", _make_state())
        await mgr._recheck_and_resume("w1", time.monotonic())
        assert _seq_tags(hub) == ["send_worker", "notify"]


# =====================================================================
# cleanup_expired  (160-190) — the orchestration
# =====================================================================


class TestCleanupExpired:
    """Pin the boolean return + the exact ordered side-effect pipeline."""

    async def test_returns_false_for_missing_worker(self) -> None:
        mgr, _registry, hub, _ = _make_manager()
        assert await mgr.cleanup_expired("ghost") is False
        # No pipeline side-effects when result is None.
        assert hub.seq == []

    async def test_returns_false_for_idle_worker(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        registry.put("w1", _make_state())
        assert await mgr.cleanup_expired("w1") is False
        assert hub.seq == []

    async def test_returns_false_when_nothing_expired(self) -> None:
        """Occupied-but-fresh lease: not idle, neither slot expired."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=30)
        registry.put("w1", st)
        assert await mgr.cleanup_expired("w1") is False
        # The early ``not rest and not dashboard`` return happens BEFORE
        # any metric/event, so the pipeline stays empty.
        assert hub.metrics == []
        assert hub.seq == []
        assert st.hijack_session is not None

    async def test_rest_only_full_pipeline_order(self) -> None:
        """Rest expires, no dashboard → full pipeline, resume fires."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)
        assert await mgr.cleanup_expired("w1") is True
        # Exact ordered pipeline: metric, recheck (which sends+notifies),
        # then the rest event, broadcast, prune.
        assert _seq_tags(hub) == [
            "metric",
            "recheck",
            "send_worker",
            "notify",
            "append_event",
            "broadcast",
            "prune",
        ]
        assert hub.metrics == ["hijack_lease_expiries_total"]
        assert hub.recheck_calls and hub.recheck_calls[0][0] == "w1"
        assert hub.events == [("w1", "hijack_lease_expired")]
        assert hub.broadcast_calls == ["w1"]
        assert hub.prune_calls == ["w1"]
        # Resume frame dispatched once both slots idle.
        assert hub.send_worker_calls == [
            (
                "w1",
                {
                    "type": "control",
                    "action": "resume",
                    "owner": "lease-expired",
                    "lease_s": 0,
                    "ts": hub.recheck_calls[0][1],
                },
            )
        ]
        assert hub.notify_calls == [("w1", False, None)]

    async def test_dashboard_only_emits_owner_expired_not_lease_expired(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        assert await mgr.cleanup_expired("w1") is True
        # Only the dashboard audit row — never the rest one.
        assert hub.events == [("w1", "hijack_owner_expired")]
        assert ("w1", "hijack_lease_expired") not in hub.events
        assert _seq_tags(hub) == [
            "metric",
            "recheck",
            "send_worker",
            "notify",
            "append_event",
            "broadcast",
            "prune",
        ]

    async def test_both_expired_emits_both_events_in_order(self) -> None:
        """rest event precedes dashboard event; both fire."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        assert await mgr.cleanup_expired("w1") is True
        assert hub.events == [
            ("w1", "hijack_lease_expired"),
            ("w1", "hijack_owner_expired"),
        ]
        assert _seq_tags(hub) == [
            "metric",
            "recheck",
            "send_worker",
            "notify",
            "append_event",
            "append_event",
            "broadcast",
            "prune",
        ]

    async def test_partial_expiry_skips_recheck_and_resume(self) -> None:
        """Rest stale, dashboard live → should_resume False, recheck skipped."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        assert await mgr.cleanup_expired("w1") is True
        # No recheck → no resume frame, no notify.
        assert hub.recheck_calls == []
        assert hub.send_worker_calls == []
        assert hub.notify_calls == []
        # Metric still fires; only the rest audit row is written.
        assert hub.metrics == ["hijack_lease_expiries_total"]
        assert hub.events == [("w1", "hijack_lease_expired")]
        assert _seq_tags(hub) == [
            "metric",
            "append_event",
            "broadcast",
            "prune",
        ]

    async def test_metric_fires_before_recheck(self) -> None:
        """Metric must precede the resume recheck in the pipeline."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)
        await mgr.cleanup_expired("w1")
        tags = _seq_tags(hub)
        assert tags.index("metric") < tags.index("recheck")

    async def test_broadcast_precedes_prune(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)
        await mgr.cleanup_expired("w1")
        tags = _seq_tags(hub)
        assert tags.index("broadcast") < tags.index("prune")

    async def test_recheck_receives_same_now_as_metric_pass(self) -> None:
        """The ``now`` handed to recheck is the sweep's single monotonic read."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)
        before = time.monotonic()
        await mgr.cleanup_expired("w1")
        after = time.monotonic()
        assert hub.recheck_calls
        _worker, recheck_now = hub.recheck_calls[0]
        assert before <= recheck_now <= after
        # That same ``now`` is the resume frame's ts.
        assert hub.send_worker_calls[0][1]["ts"] == recheck_now


# =====================================================================
# cleanup_expired observability — logger.info(EVENT_HIJACK_EXPIRED, ...)
# =====================================================================


class TestCleanupExpiredExpiryTelemetry:
    """Pin the EXACT ``logger.info`` calls emitted by ``cleanup_expired``.

    mutmut mutates the arguments of ``logger.info(EVENT_HIJACK_EXPIRED,
    worker_id=..., hijack_type="rest"|"dashboard")`` (event const → None,
    kwargs dropped/None, string case-flips). These survive unless a test
    pins the exact call, so each test below patches the module-level
    ``logger`` and asserts the constant + every kwarg verbatim.
    """

    async def test_rest_only_expiry_logs_hijack_type_rest(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            assert await mgr.cleanup_expired("w1") is True
        mlog.info.assert_called_once_with(EVENT_HIJACK_EXPIRED, worker_id="w1", hijack_type="rest")

    async def test_dashboard_only_expiry_logs_hijack_type_dashboard(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            assert await mgr.cleanup_expired("w1") is True
        mlog.info.assert_called_once_with(EVENT_HIJACK_EXPIRED, worker_id="w1", hijack_type="dashboard")

    async def test_both_expired_logs_both_rest_and_dashboard(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            assert await mgr.cleanup_expired("w1") is True
        # Both info calls fire — rest first, then dashboard.
        assert mlog.info.call_count == 2
        mlog.info.assert_any_call(EVENT_HIJACK_EXPIRED, worker_id="w1", hijack_type="rest")
        mlog.info.assert_any_call(EVENT_HIJACK_EXPIRED, worker_id="w1", hijack_type="dashboard")
        assert mlog.info.call_args_list == [
            (((EVENT_HIJACK_EXPIRED,), {"worker_id": "w1", "hijack_type": "rest"})),
            (((EVENT_HIJACK_EXPIRED,), {"worker_id": "w1", "hijack_type": "dashboard"})),
        ]

    async def test_partial_expiry_logs_only_rest(self) -> None:
        """Rest stale, dashboard live → only the rest info line fires."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = _rest_session(expires_in=-1)
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            assert await mgr.cleanup_expired("w1") is True
        mlog.info.assert_called_once_with(EVENT_HIJACK_EXPIRED, worker_id="w1", hijack_type="rest")
