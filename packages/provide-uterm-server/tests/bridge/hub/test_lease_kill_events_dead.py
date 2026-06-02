#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing unit tests for :class:`HijackLeaseManager`.

Targets the ``get_events_data`` and ``remove_dead_browsers`` service
methods plus the lone ``metric`` call site on ``extend_lease``'s
owner-mismatch denial path. Every assertion pins an exact value (full
tuples, exact field values, exact control-frame contents, exact
callback args/counts/order) so that a mutated operator or constant flips
an outcome a test observes.

Mirrors the ``_FakeHub`` / ``_make_state`` / ``_make_manager`` harness
from ``test_lease.py`` exactly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from provide.uterm.server.bridge.hub.ext import (
    EVENT_HIJACK_ACQUIRED,
    EVENT_HIJACK_EXPIRED,
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


# ---------------------------------------------------------------------------
# get_events_data
# ---------------------------------------------------------------------------


class TestGetEventsData:
    """Pin every field of the events payload + the filter/slice predicates."""

    @pytest.mark.asyncio
    async def test_filters_strictly_greater_than_after_seq(self) -> None:
        """``seq > after_seq`` must be strict: the boundary seq is excluded."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 4
        st.min_event_seq = 1
        for i in range(1, 5):
            st.events.append({"seq": i, "type": f"e{i}"})
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=2, limit=100)
        # seq==2 (the boundary) is excluded; seq 3 and 4 remain.
        assert [r["seq"] for r in data["rows"]] == [3, 4]

    @pytest.mark.asyncio
    async def test_limit_truncates_to_first_n(self) -> None:
        """``[:limit]`` keeps only the first *limit* matches in order."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 6
        st.min_event_seq = 0
        for i in range(1, 7):
            st.events.append({"seq": i, "type": f"e{i}"})
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=0, limit=3)
        assert [r["seq"] for r in data["rows"]] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_missing_seq_defaults_to_zero_and_is_filtered(self) -> None:
        """An event without a ``seq`` key defaults to 0 and is filtered out."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 2
        st.min_event_seq = 0
        st.events.append({"type": "no-seq"})  # int(evt.get("seq", 0)) == 0
        st.events.append({"seq": 2, "type": "e2"})
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=0, limit=100)
        # The seq-less event (default 0) is NOT > after_seq==0, so excluded.
        assert [r.get("seq") for r in data["rows"]] == [2]

    @pytest.mark.asyncio
    async def test_missing_seq_default_zero_passes_when_after_seq_negative(self) -> None:
        """Distinguish the default 0 from any other constant: -1 lets 0 through."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 1
        st.min_event_seq = 0
        st.events.append({"type": "no-seq"})  # default 0 > -1 -> kept
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=-1, limit=100)
        assert data["rows"] == [{"type": "no-seq"}]

    @pytest.mark.asyncio
    async def test_latest_seq_is_event_seq(self) -> None:
        """``latest_seq`` echoes ``st.event_seq`` exactly (distinct from min)."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 17
        st.min_event_seq = 4
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=0, limit=100)
        assert data["latest_seq"] == 17

    @pytest.mark.asyncio
    async def test_min_event_seq_is_min_event_seq(self) -> None:
        """``min_event_seq`` echoes ``st.min_event_seq`` exactly (distinct from latest)."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 17
        st.min_event_seq = 4
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=0, limit=100)
        assert data["min_event_seq"] == 4

    @pytest.mark.asyncio
    async def test_all_payload_keys_present(self) -> None:
        """The payload exposes exactly the four documented keys."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 0
        st.min_event_seq = 0
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=0, limit=100)
        assert set(data) == {"rows", "latest_seq", "min_event_seq", "fresh_expires"}

    @pytest.mark.asyncio
    async def test_fresh_expires_uses_session_when_hijack_id_matches(self) -> None:
        """Session present + id match -> use the live session's lease_expires_at."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 0
        st.min_event_seq = 0
        # Live session expiry differs from the passed-in hs expiry.
        session_expiry = time.monotonic() + 99
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=session_expiry)
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 5)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=0, limit=100)
        assert data["fresh_expires"] == session_expiry
        assert data["fresh_expires"] != hs.lease_expires_at

    @pytest.mark.asyncio
    async def test_fresh_expires_falls_back_when_session_is_none(self) -> None:
        """No session -> fall back to the passed-in ``hs.lease_expires_at``."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 0
        st.min_event_seq = 0
        st.hijack_session = None
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 5)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=0, limit=100)
        assert data["fresh_expires"] == hs.lease_expires_at

    @pytest.mark.asyncio
    async def test_fresh_expires_falls_back_when_hijack_id_mismatches(self) -> None:
        """Session present but id mismatch -> fall back to ``hs.lease_expires_at``."""
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 0
        st.min_event_seq = 0
        st.hijack_session = HijackSession(hijack_id="other", owner="o", lease_expires_at=time.monotonic() + 99)
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 5)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=0, limit=100)
        assert data["fresh_expires"] == hs.lease_expires_at
        assert data["fresh_expires"] != st.hijack_session.lease_expires_at

    @pytest.mark.asyncio
    async def test_normal_path_returns_exact_dict(self) -> None:
        """The non-fallback payload is the exact dict, keys and values pinned.

        Kills RETURN-DICT key case-flips ("rows"->"ROWS" etc.) and value
        substitutions on the normal (``st`` present) path: each of the four
        keys is asserted with its exact value in a single equality.
        """
        mgr, registry, *_ = _make_manager()
        st = _make_state()
        st.event_seq = 9
        st.min_event_seq = 3
        st.events.append({"seq": 5, "type": "e5"})
        registry.put("w1", st)
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        data = await mgr.get_events_data("w1", "h", hs, after_seq=0, limit=100)
        assert data == {
            "rows": [{"seq": 5, "type": "e5"}],
            "latest_seq": 9,
            "min_event_seq": 3,
            "fresh_expires": hs.lease_expires_at,
        }

    @pytest.mark.asyncio
    async def test_unknown_worker_returns_exact_fallback_dict(self) -> None:
        """``st is None`` -> the exact fallback dict (empty rows, zeroed seqs).

        Reaches the ``if st is None`` branch (lease.py:390-396) by querying
        an empty registry. Asserts the full dict so key case-flips and the
        ``0 -> 1`` value mutants on ``latest_seq`` / ``min_event_seq`` die,
        and ``rows == []`` (not a non-empty default) is pinned.
        """
        mgr, registry, *_ = _make_manager()
        # Registry deliberately left empty: no worker "ghost" registered.
        assert registry.get("ghost") is None
        fresh = time.monotonic() + 42
        hs = HijackSession(hijack_id="h", owner="o", lease_expires_at=fresh)
        data = await mgr.get_events_data("ghost", "h", hs, after_seq=0, limit=100)
        assert data == {
            "rows": [],
            "latest_seq": 0,
            "min_event_seq": 0,
            "fresh_expires": hs.lease_expires_at,
        }
        assert data["fresh_expires"] == fresh


# ---------------------------------------------------------------------------
# remove_dead_browsers
# ---------------------------------------------------------------------------


class TestRemoveDeadBrowsers:
    """Pin dead-socket pruning, owner clearing, recheck, resume frame + callbacks."""

    @pytest.mark.asyncio
    async def test_unknown_worker_returns_false_no_side_effects(self) -> None:
        """``st is None`` -> no pop, no resume, returns False."""
        mgr, registry, hub, _ = _make_manager()
        changed = await mgr.remove_dead_browsers("ghost", {AsyncMock()})
        assert changed is False
        assert hub.send_worker_calls == []
        assert hub.notify_calls == []

    @pytest.mark.asyncio
    async def test_pops_every_dead_socket(self) -> None:
        """Each ws in *dead* is removed from ``st.browsers``."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        d1, d2, keep = AsyncMock(), AsyncMock(), AsyncMock()
        st.browsers[d1] = "viewer"
        st.browsers[d2] = "viewer"
        st.browsers[keep] = "admin"
        registry.put("w1", st)
        changed = await mgr.remove_dead_browsers("w1", {d1, d2})
        assert changed is False
        assert d1 not in st.browsers
        assert d2 not in st.browsers
        assert keep in st.browsers

    @pytest.mark.asyncio
    async def test_pop_missing_socket_is_tolerated(self) -> None:
        """A dead ws not in ``browsers`` still pops cleanly (default None)."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        absent = AsyncMock()
        changed = await mgr.remove_dead_browsers("w1", {absent})
        assert changed is False
        assert absent not in st.browsers

    @pytest.mark.asyncio
    async def test_non_owner_death_keeps_owner_and_no_resume(self) -> None:
        """A dead non-owner socket must not clear the owner nor resume."""
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
        assert st.hijack_owner_expires_at is not None
        assert hub.send_worker_calls == []
        assert hub.notify_calls == []

    @pytest.mark.asyncio
    async def test_owner_death_clears_owner_and_resumes(self) -> None:
        """Dashboard owner dies, no rest lease -> clear owner, resume, notify, True."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        owner = AsyncMock()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = time.monotonic() + 30
        st.browsers[owner] = "admin"
        registry.put("w1", st)
        changed = await mgr.remove_dead_browsers("w1", {owner})
        assert changed is True
        # Owner fully cleared.
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None
        assert owner not in st.browsers
        # Exactly one resume frame, with every field pinned.
        assert len(hub.send_worker_calls) == 1
        wid, msg = hub.send_worker_calls[0]
        assert wid == "w1"
        assert msg["type"] == "control"
        assert msg["action"] == "resume"
        assert msg["owner"] == "dead-socket"
        assert msg["lease_s"] == 0
        assert "ts" in msg
        # Exactly one notify with enabled=False, owner=None.
        assert hub.notify_calls == [("w1", False, None)]

    @pytest.mark.asyncio
    async def test_owner_death_with_valid_rest_lease_suppresses_resume(self) -> None:
        """A live REST lease means notify_hijack_off stays False -> no resume."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        owner = AsyncMock()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = time.monotonic() + 30
        st.browsers[owner] = "admin"
        # Valid REST lease present -> has_valid_rest_lease True -> notify off.
        st.hijack_session = HijackSession(hijack_id="h", owner="o", lease_expires_at=time.monotonic() + 30)
        registry.put("w1", st)
        changed = await mgr.remove_dead_browsers("w1", {owner})
        assert changed is False
        # Owner WAS cleared (the dead socket was the owner)...
        assert st.hijack_owner is None
        assert st.hijack_owner_expires_at is None
        # ...but no resume/notify because the rest lease still holds.
        assert hub.send_worker_calls == []
        assert hub.notify_calls == []

    @pytest.mark.asyncio
    async def test_inactive_dashboard_owner_is_not_treated_as_owner(self) -> None:
        """If the dashboard lease is already expired, the dead ws path skips owner-clear."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        owner = AsyncMock()
        # owner set but lease already expired -> is_dashboard_hijack_active False.
        st.hijack_owner = owner
        st.hijack_owner_expires_at = time.monotonic() - 1
        st.browsers[owner] = "admin"
        registry.put("w1", st)
        changed = await mgr.remove_dead_browsers("w1", {owner})
        assert changed is False
        # The owner field is untouched because the active-guard was False.
        assert st.hijack_owner is owner
        assert hub.send_worker_calls == []
        assert hub.notify_calls == []

    @pytest.mark.asyncio
    async def test_concurrent_recheck_suppresses_resume(self) -> None:
        """A concurrently-written hijack seen by the re-check block suppresses the resume.

        The first lock block sets ``notify_hijack_off=True`` (owner dead,
        no rest lease). We then force ``is_hijacked`` to report True in the
        re-check block — modelling a concurrent acquire landing between the
        two lock acquisitions — and assert the resume frame is suppressed.
        """
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        owner = AsyncMock()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = time.monotonic() + 30
        st.browsers[owner] = "admin"
        registry.put("w1", st)

        # First block: no rest lease -> notify_hijack_off becomes True.
        # Re-check block: force is_hijacked True so notify flips back to False.
        original_is_hijacked = hub.is_hijacked
        calls: list[int] = []

        def _fake_is_hijacked(state: WorkerTermState) -> bool:
            calls.append(1)
            return True

        hub.is_hijacked = _fake_is_hijacked  # type: ignore[method-assign]
        try:
            changed = await mgr.remove_dead_browsers("w1", {owner})
        finally:
            hub.is_hijacked = original_is_hijacked  # type: ignore[method-assign]

        assert changed is False
        # The recheck block must have consulted is_hijacked.
        assert calls == [1]
        # Suppressed: no resume frame, no notify.
        assert hub.send_worker_calls == []
        assert hub.notify_calls == []

    @pytest.mark.asyncio
    async def test_owner_death_send_then_notify_order_and_counts(self) -> None:
        """Resume frame is sent before the notify callback fires, exactly once each."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        owner = AsyncMock()
        st.hijack_owner = owner
        st.hijack_owner_expires_at = time.monotonic() + 30
        st.browsers[owner] = "admin"
        registry.put("w1", st)

        order: list[str] = []
        orig_send = hub.send_worker
        orig_notify = hub.notify_hijack_changed

        async def _send(worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
            order.append("send")
            return await orig_send(worker_id, msg, source=source)

        def _notify(worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
            order.append("notify")
            orig_notify(worker_id, enabled=enabled, owner=owner)

        hub.send_worker = _send  # type: ignore[method-assign]
        hub.notify_hijack_changed = _notify  # type: ignore[method-assign]
        await mgr.remove_dead_browsers("w1", {owner})
        assert order == ["send", "notify"]


# ---------------------------------------------------------------------------
# metric path coverage (the lone untested metric call site)
# ---------------------------------------------------------------------------


class TestMetricCallSite:
    """Pin the ``metric`` call on ``extend_lease``'s owner-mismatch denial."""

    @pytest.mark.asyncio
    async def test_owner_mismatch_records_denied_metric_and_returns_none(self) -> None:
        """A heartbeat from the wrong owner records the denial metric, returns None."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        now = time.monotonic()
        st.hijack_session = HijackSession(
            hijack_id="h",
            owner="real-owner",
            acquired_at=now,
            lease_expires_at=now + 30,
            last_heartbeat=now,
        )
        registry.put("w1", st)
        result = await mgr.extend_lease("w1", "h", owner="impostor", lease_s=60, now=now)
        assert result is None
        assert hub.metrics == ["hijack_heartbeat_denied_owner_mismatch"]
        # Lease was NOT extended (still the original expiry).
        assert st.hijack_session.lease_expires_at == now + 30

    @pytest.mark.asyncio
    async def test_matching_owner_extends_without_metric(self) -> None:
        """The happy path must NOT record the denial metric (kills metric-name flip)."""
        mgr, registry, hub, _ = _make_manager()
        st = _make_state()
        now = time.monotonic()
        st.hijack_session = HijackSession(
            hijack_id="h",
            owner="real-owner",
            acquired_at=now,
            lease_expires_at=now + 10,
            last_heartbeat=now,
        )
        registry.put("w1", st)
        new_exp = await mgr.extend_lease("w1", "h", owner="real-owner", lease_s=60, now=now)
        assert new_exp == now + 60
        assert st.hijack_session.lease_expires_at == now + 60
        assert st.hijack_session.last_heartbeat == now
        assert hub.metrics == []


# ---------------------------------------------------------------------------
# structured-event + tracing-span argument kills
# ---------------------------------------------------------------------------
#
# mutmut mutates the ARGUMENTS of the module-level ``logger.info`` /
# ``logger.warning`` / ``tracer.start_as_current_span`` calls (event const ->
# None, kwargs -> None/removed, string case-flips, dict-key case-flips). No
# existing test asserts the exact call, so these mutants survive. Each test
# below patches the ``lease.logger`` / ``lease.tracer`` module objects, drives
# the method down the path that logs/traces, then pins the EXACT span name +
# EXACT attributes dict and the EXACT event constant + every kwarg.


class TestAcquireRestTelemetry:
    """``try_acquire_rest``: acquire span + acquired event, every arg pinned."""

    @pytest.mark.asyncio
    async def test_span_and_event_exact_args(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        now = time.monotonic()
        with (
            patch("provide.uterm.server.bridge.hub.lease.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr,
        ):
            ok, reason = await mgr.try_acquire_rest("w1", owner="op", lease_s=120, hijack_id="hid", now=now)
        assert ok is True
        assert reason is None
        mtr.start_as_current_span.assert_called_once_with(
            "uterm.hijack.acquire.rest", attributes={"worker_id": "w1", "owner": "op"}
        )
        mlog.info.assert_called_once_with(
            EVENT_HIJACK_ACQUIRED, worker_id="w1", hijack_type="rest", owner="op", lease_s=120
        )


class TestAcquireWsTelemetry:
    """``try_acquire_ws``: acquire span + acquired event, every arg pinned."""

    @pytest.mark.asyncio
    async def test_span_and_event_exact_args(self) -> None:
        mgr, registry, _hub, _ = _make_manager(dashboard_hijack_lease_s=45)
        st = _make_state()
        registry.put("w1", st)
        ws = AsyncMock()
        with (
            patch("provide.uterm.server.bridge.hub.lease.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr,
        ):
            ok, reason = await mgr.try_acquire_ws("w1", ws)
        assert ok is True
        assert reason is None
        mtr.start_as_current_span.assert_called_once_with("uterm.hijack.acquire.ws", attributes={"worker_id": "w1"})
        mlog.info.assert_called_once_with(EVENT_HIJACK_ACQUIRED, worker_id="w1", hijack_type="dashboard", lease_s=45)


class TestReleaseWsTelemetry:
    """``try_release_ws``: released event on the owner-clear path, args pinned."""

    @pytest.mark.asyncio
    async def test_event_exact_args(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        ws = AsyncMock()
        st.hijack_owner = ws
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            released, rest_active = await mgr.try_release_ws("w1", ws)
        assert released is True
        assert rest_active is False
        mlog.info.assert_called_once_with(EVENT_HIJACK_RELEASED, worker_id="w1", hijack_type="dashboard")


class TestReleaseRestTelemetry:
    """``release_rest``: release span with exactly the worker_id attribute."""

    @pytest.mark.asyncio
    async def test_span_exact_args(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="hid", owner="o", lease_expires_at=time.monotonic() + 30)
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr:
            ok, should_resume = await mgr.release_rest("w1", "hid")
        assert ok is True
        assert should_resume is True
        mtr.start_as_current_span.assert_called_once_with("uterm.hijack.release", attributes={"worker_id": "w1"})


class TestHeartbeatTelemetry:
    """``extend_lease``: heartbeat span + owner-mismatch warning, args pinned."""

    @pytest.mark.asyncio
    async def test_span_exact_args_on_success(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        now = time.monotonic()
        st.hijack_session = HijackSession(
            hijack_id="hid", owner="real", acquired_at=now, lease_expires_at=now + 10, last_heartbeat=now
        )
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr:
            new_exp = await mgr.extend_lease("w1", "hid", owner="real", lease_s=60, now=now)
        assert new_exp == now + 60
        mtr.start_as_current_span.assert_called_once_with(
            "uterm.hijack.heartbeat", attributes={"worker_id": "w1", "owner": "real"}
        )

    @pytest.mark.asyncio
    async def test_owner_mismatch_logs_warning_with_exact_args(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        now = time.monotonic()
        st.hijack_session = HijackSession(
            hijack_id="hid", owner="real", acquired_at=now, lease_expires_at=now + 30, last_heartbeat=now
        )
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            result = await mgr.extend_lease("w1", "hid", owner="impostor", lease_s=60, now=now)
        assert result is None
        mlog.warning.assert_called_once_with(
            "hijack_heartbeat_denied_owner_mismatch worker_id=%s hijack_id=%s current=%s attempted=%s",
            "w1",
            "hid",
            "real",
            "impostor",
        )


class TestExpiryTelemetry:
    """``cleanup_expired``: expired events for rest + dashboard, args pinned."""

    @pytest.mark.asyncio
    async def test_rest_expiry_logs_expired_event(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        # Past REST lease, no dashboard owner -> only the rest-expired event.
        st.hijack_session = HijackSession(hijack_id="hid", owner="o", lease_expires_at=time.monotonic() - 1)
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            changed = await mgr.cleanup_expired("w1")
        assert changed is True
        mlog.info.assert_called_once_with(EVENT_HIJACK_EXPIRED, worker_id="w1", hijack_type="rest")

    @pytest.mark.asyncio
    async def test_dashboard_expiry_logs_expired_event(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        # Past dashboard lease, no REST session -> only the dashboard event.
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            changed = await mgr.cleanup_expired("w1")
        assert changed is True
        mlog.info.assert_called_once_with(EVENT_HIJACK_EXPIRED, worker_id="w1", hijack_type="dashboard")
