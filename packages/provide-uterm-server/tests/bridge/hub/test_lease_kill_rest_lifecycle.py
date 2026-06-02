#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing unit tests for the REST-lease lifecycle methods of
:class:`HijackLeaseManager`.

Targets ``extend_lease``, ``release_rest``, ``check_valid``,
``get_fresh_expiry``, ``get_rest_session`` and the
``_get_rest_session_no_cleanup`` helper. Each test pins exact return
values, exact mutated session fields, exact metric/callback emissions
and the strict comparison boundaries so that flipping any operator or
constant in the target method changes a test outcome.

Mirrors the ``_FakeHub`` / ``_make_state`` / ``_make_manager`` harness
from ``test_lease.py`` exactly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

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


# ---------------------------------------------------------------------------
# extend_lease
# ---------------------------------------------------------------------------


class TestExtendLease:
    """``extend_lease`` heartbeat-extension semantics."""

    async def test_extends_and_returns_exact_new_expiry(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        hs = HijackSession(
            hijack_id="h",
            owner="o",
            lease_expires_at=1000.0,
            acquired_at=10.0,
            last_heartbeat=20.0,
        )
        st.hijack_session = hs
        registry.put("w1", st)

        now = 500.0
        lease_s = 90
        result = await mgr.extend_lease("w1", "h", "o", lease_s, now)

        # Return value is exactly now + lease_s.
        assert result == now + lease_s
        # The session was mutated in-place.
        assert st.hijack_session is hs
        assert hs.last_heartbeat == now
        assert hs.lease_expires_at == now + lease_s
        # acquired_at and identity fields are untouched.
        assert hs.acquired_at == 10.0
        assert hs.hijack_id == "h"
        assert hs.owner == "o"
        # No metric is emitted on the success path.
        assert hub.metrics == []

    async def test_distinct_now_and_lease_s_pin_arithmetic(self) -> None:
        """Different now/lease_s must give now+lease_s, not now, lease_s, or now*lease_s."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=0.0)
        registry.put("w1", st)

        result = await mgr.extend_lease("w1", "h", "o", 7, 3.0)
        assert result == 10.0
        assert st.hijack_session.last_heartbeat == 3.0
        assert st.hijack_session.lease_expires_at == 10.0

    async def test_missing_worker_returns_none(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        # Worker not registered at all.
        result = await mgr.extend_lease("ghost", "h", "o", 90, 500.0)
        assert result is None
        assert hub.metrics == []

    async def test_no_session_returns_none(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = None
        registry.put("w1", st)
        result = await mgr.extend_lease("w1", "h", "o", 90, 500.0)
        assert result is None
        assert hub.metrics == []

    async def test_hijack_id_mismatch_returns_none_without_mutation(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        hs = HijackSession(hijack_id="real", owner="o", lease_expires_at=1000.0, last_heartbeat=20.0)
        st.hijack_session = hs
        registry.put("w1", st)

        result = await mgr.extend_lease("w1", "wrong", "o", 90, 500.0)
        assert result is None
        # Session untouched: id-mismatch returns before owner check / mutation.
        assert hs.lease_expires_at == 1000.0
        assert hs.last_heartbeat == 20.0
        # No owner-mismatch metric — we never reached the owner check.
        assert hub.metrics == []

    async def test_owner_mismatch_emits_metric_and_returns_none(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        hs = HijackSession(hijack_id="h", owner="real-owner", lease_expires_at=1000.0, last_heartbeat=20.0)
        st.hijack_session = hs
        registry.put("w1", st)

        result = await mgr.extend_lease("w1", "h", "attacker", 90, 500.0)
        assert result is None
        # Exactly one metric with the exact name.
        assert hub.metrics == ["hijack_heartbeat_denied_owner_mismatch"]
        # Session not extended on denial.
        assert hs.lease_expires_at == 1000.0
        assert hs.last_heartbeat == 20.0

    async def test_matching_owner_does_not_emit_denial_metric(self) -> None:
        """Owner equality branch flips: same owner must NOT take the denial path."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="same", lease_expires_at=0.0)
        registry.put("w1", st)
        result = await mgr.extend_lease("w1", "h", "same", 90, 500.0)
        assert result == 590.0
        assert "hijack_heartbeat_denied_owner_mismatch" not in hub.metrics

    async def test_success_opens_exact_heartbeat_span(self) -> None:
        """The success path must open exactly one span with the exact name + attrs.

        Pins ``tracer.start_as_current_span("uterm.hijack.heartbeat",
        attributes={"worker_id": <wid>, "owner": <owner>})`` so a mutant
        that nulls the span name, drops/renames ``attributes``, flips a key
        case, or nulls a value is caught.
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=0.0)
        registry.put("w1", st)

        with (
            patch("provide.uterm.server.bridge.hub.lease.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr,
        ):
            result = await mgr.extend_lease("w1", "h", "o", 90, 500.0)

        assert result == 590.0
        mtr.start_as_current_span.assert_called_once_with(
            "uterm.hijack.heartbeat", attributes={"worker_id": "w1", "owner": "o"}
        )
        # Success path takes no warning branch.
        mlog.warning.assert_not_called()

    async def test_owner_mismatch_logs_exact_warning(self) -> None:
        """Owner mismatch must emit the exact positional ``logger.warning`` call.

        Pins the format string and every positional arg (worker_id,
        hijack_id, current owner, attempted owner) so a mutant that nulls
        the message, drops an arg, reorders, or case-flips the format is
        caught. Also pins the span (still opened on the denial path).
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        hs = HijackSession(hijack_id="h", owner="real-owner", lease_expires_at=1000.0, last_heartbeat=20.0)
        st.hijack_session = hs
        registry.put("w1", st)

        with (
            patch("provide.uterm.server.bridge.hub.lease.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr,
        ):
            result = await mgr.extend_lease("w1", "h", "attacker", 90, 500.0)

        assert result is None
        mlog.warning.assert_called_once_with(
            "hijack_heartbeat_denied_owner_mismatch worker_id=%s hijack_id=%s current=%s attempted=%s",
            "w1",
            "h",
            "real-owner",
            "attacker",
        )
        # The span is opened even on the denial path.
        mtr.start_as_current_span.assert_called_once_with(
            "uterm.hijack.heartbeat", attributes={"worker_id": "w1", "owner": "attacker"}
        )


# ---------------------------------------------------------------------------
# release_rest
# ---------------------------------------------------------------------------


class TestReleaseRest:
    """``release_rest`` clears the REST session and reports resume eligibility."""

    async def test_release_clears_session_and_should_resume_true_when_no_dashboard(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        # No dashboard owner -> is_dashboard_hijack_active is False -> should_resume True.
        st.hijack_owner = None
        registry.put("w1", st)

        released, should_resume = await mgr.release_rest("w1", "h")
        assert released is True
        assert should_resume is True
        assert st.hijack_session is None

    async def test_release_should_resume_false_when_dashboard_active(self) -> None:
        """A live dashboard lease flips should_resume to False (not-of-active)."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)

        released, should_resume = await mgr.release_rest("w1", "h")
        assert released is True
        assert should_resume is False
        # Session still cleared regardless of dashboard state.
        assert st.hijack_session is None

    async def test_missing_worker_returns_false_false(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        released, should_resume = await mgr.release_rest("ghost", "h")
        assert released is False
        assert should_resume is False

    async def test_no_session_returns_false_false(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = None
        registry.put("w1", st)
        released, should_resume = await mgr.release_rest("w1", "h")
        assert released is False
        assert should_resume is False

    async def test_id_mismatch_returns_false_false_and_keeps_session(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        hs = HijackSession(hijack_id="real", owner="o", lease_expires_at=time.monotonic() + 30)
        st.hijack_session = hs
        registry.put("w1", st)

        released, should_resume = await mgr.release_rest("w1", "fake")
        assert released is False
        assert should_resume is False
        # Session not cleared on mismatch.
        assert st.hijack_session is hs

    async def test_release_opens_exact_release_span(self) -> None:
        """Release must open exactly one span with the exact name + attrs.

        Pins ``tracer.start_as_current_span("uterm.hijack.release",
        attributes={"worker_id": <wid>})`` so a mutant that nulls the span
        name, drops/renames ``attributes``, flips the key case, or nulls
        the value is caught.
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        st.hijack_owner = None
        registry.put("w1", st)

        with patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr:
            released, should_resume = await mgr.release_rest("w1", "h")

        assert released is True
        assert should_resume is True
        mtr.start_as_current_span.assert_called_once_with("uterm.hijack.release", attributes={"worker_id": "w1"})


# ---------------------------------------------------------------------------
# check_valid
# ---------------------------------------------------------------------------


class TestCheckValid:
    """``check_valid`` strict-greater-than expiry boundary + id match."""

    async def test_future_lease_is_valid(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 100)
        registry.put("w1", st)
        assert await mgr.check_valid("w1", "h") is True

    async def test_expired_lease_is_invalid(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() - 1)
        registry.put("w1", st)
        assert await mgr.check_valid("w1", "h") is False

    async def test_boundary_is_strict_greater_than(self, monkeypatch: Any) -> None:
        """lease_expires_at == now must be invalid (> not >=).

        Pin the in-method ``time.monotonic()`` to a fixed value and set
        ``lease_expires_at`` to that exact value so ``> now`` is False while
        a ``>=`` mutant would (wrongly) report valid.
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        frozen = 5000.0
        monkeypatch.setattr("provide.uterm.server.bridge.hub.lease.time.monotonic", lambda: frozen)
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=frozen)
        registry.put("w1", st)
        assert await mgr.check_valid("w1", "h") is False

    async def test_boundary_one_tick_above_now_is_valid(self, monkeypatch: Any) -> None:
        """lease_expires_at strictly above now is valid (anchors the `>` direction)."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        frozen = 5000.0
        monkeypatch.setattr("provide.uterm.server.bridge.hub.lease.time.monotonic", lambda: frozen)
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=frozen + 0.001)
        registry.put("w1", st)
        assert await mgr.check_valid("w1", "h") is True

    async def test_missing_worker_is_invalid(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        assert await mgr.check_valid("ghost", "h") is False

    async def test_no_session_is_invalid(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = None
        registry.put("w1", st)
        assert await mgr.check_valid("w1", "h") is False

    async def test_id_mismatch_is_invalid(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="real", owner="o", lease_expires_at=time.monotonic() + 100)
        registry.put("w1", st)
        assert await mgr.check_valid("w1", "wrong") is False


# ---------------------------------------------------------------------------
# get_fresh_expiry
# ---------------------------------------------------------------------------


class TestGetFreshExpiry:
    """``get_fresh_expiry`` returns the live session expiry or the fallback."""

    async def test_returns_live_session_expiry_when_id_matches(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=12345.0)
        registry.put("w1", st)
        # Distinct from fallback so we know which branch fired.
        assert await mgr.get_fresh_expiry("w1", "h", fallback=42.0) == 12345.0

    async def test_returns_fallback_when_missing_worker(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        assert await mgr.get_fresh_expiry("ghost", "h", fallback=42.0) == 42.0

    async def test_returns_fallback_when_no_session(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = None
        registry.put("w1", st)
        assert await mgr.get_fresh_expiry("w1", "h", fallback=99.0) == 99.0

    async def test_returns_fallback_when_id_mismatch(self) -> None:
        """Id-mismatch must fall through to fallback, not return the live expiry."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="real", owner="o", lease_expires_at=12345.0)
        registry.put("w1", st)
        assert await mgr.get_fresh_expiry("w1", "wrong", fallback=7.0) == 7.0

    async def test_returns_live_expiry_even_if_in_past(self) -> None:
        """get_fresh_expiry does not gate on expiry — only on id match."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=-5.0)
        registry.put("w1", st)
        assert await mgr.get_fresh_expiry("w1", "h", fallback=500.0) == -5.0


# ---------------------------------------------------------------------------
# _get_rest_session_no_cleanup
# ---------------------------------------------------------------------------


class TestGetRestSessionNoCleanup:
    """``_get_rest_session_no_cleanup`` lookup + strict expiry + id match."""

    async def test_returns_session_when_valid_and_id_matches(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 100)
        st.hijack_session = hs
        registry.put("w1", st)
        assert await mgr._get_rest_session_no_cleanup("w1", "h") is hs

    async def test_missing_worker_returns_none(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        assert await mgr._get_rest_session_no_cleanup("ghost", "h") is None

    async def test_no_session_returns_none(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = None
        registry.put("w1", st)
        assert await mgr._get_rest_session_no_cleanup("w1", "h") is None

    async def test_expired_session_returns_none(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() - 1)
        registry.put("w1", st)
        assert await mgr._get_rest_session_no_cleanup("w1", "h") is None

    async def test_boundary_is_strict_less_equal_expiry(self, monkeypatch: Any) -> None:
        """lease_expires_at == now must be treated as expired (<= now -> None).

        Pin the in-method ``time.monotonic()`` and set ``lease_expires_at``
        to exactly that value: ``<= now`` returns None, while a ``< now``
        mutant would (wrongly) return the session.
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        frozen = 5000.0
        monkeypatch.setattr("provide.uterm.server.bridge.hub.lease.time.monotonic", lambda: frozen)
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=frozen)
        registry.put("w1", st)
        assert await mgr._get_rest_session_no_cleanup("w1", "h") is None

    async def test_boundary_one_tick_above_now_returns_session(self, monkeypatch: Any) -> None:
        """lease_expires_at strictly above now returns the session (anchors `<=`)."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        frozen = 5000.0
        monkeypatch.setattr("provide.uterm.server.bridge.hub.lease.time.monotonic", lambda: frozen)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=frozen + 0.001)
        st.hijack_session = hs
        registry.put("w1", st)
        assert await mgr._get_rest_session_no_cleanup("w1", "h") is hs

    async def test_id_mismatch_returns_none(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="real", owner="o", lease_expires_at=time.monotonic() + 100)
        registry.put("w1", st)
        assert await mgr._get_rest_session_no_cleanup("w1", "wrong") is None

    async def test_no_cleanup_does_not_run_cleanup_pipeline(self) -> None:
        """The no-cleanup helper must NOT expire a stale session or emit events."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() - 1)
        st.hijack_session = hs
        registry.put("w1", st)
        result = await mgr._get_rest_session_no_cleanup("w1", "h")
        assert result is None
        # Session left in place (not cleared) and no cleanup side effects.
        assert st.hijack_session is hs
        assert hub.events == []
        assert hub.broadcast_calls == []
        assert hub.metrics == []
        assert hub.prune_calls == []


# ---------------------------------------------------------------------------
# get_rest_session (cleanup -> no_cleanup)
# ---------------------------------------------------------------------------


class TestGetRestSession:
    """``get_rest_session`` runs cleanup_expired first, then the no-cleanup lookup."""

    async def test_runs_cleanup_then_returns_live_session(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 100)
        st.hijack_session = hs
        registry.put("w1", st)
        assert await mgr.get_rest_session("w1", "h") is hs

    async def test_cleanup_expires_stale_session_then_lookup_returns_none(self) -> None:
        """A stale session is swept by the cleanup pass, so lookup returns None."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() - 1)
        registry.put("w1", st)

        result = await mgr.get_rest_session("w1", "h")
        assert result is None
        # cleanup_expired actually ran the expiry pipeline.
        assert st.hijack_session is None
        assert ("w1", "hijack_lease_expired") in hub.events
        assert "hijack_lease_expiries_total" in hub.metrics
        assert "w1" in hub.broadcast_calls
        assert "w1" in hub.prune_calls

    async def test_id_mismatch_returns_none_after_cleanup(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="real", owner="o", lease_expires_at=time.monotonic() + 100)
        registry.put("w1", st)
        assert await mgr.get_rest_session("w1", "wrong") is None

    async def test_missing_worker_returns_none(self) -> None:
        mgr, registry, hub, _ = _make_manager()
        assert await mgr.get_rest_session("ghost", "h") is None
