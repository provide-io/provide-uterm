#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing unit tests for :meth:`HijackLeaseManager.try_acquire_rest`.

These pin every observable effect of the REST acquire path so that any
operator/constant/boundary mutation flips a concrete assertion:

* The exact ``(bool, reason)`` return tuple for every failure branch
  (``no_worker`` via ``st is None`` and via ``st.worker_ws is None``,
  ``open_mode``, ``already_hijacked`` via the dashboard predicate and via
  the REST predicate) and ``(True, None)`` on success.
* The pause control frame dispatched to ``st.worker_ws`` (decoded from the
  ``_encode_worker_frame`` text): every field — ``type``, ``action``,
  ``owner``, ``hijack_id`` and the presence of ``ts``.
* The created :class:`HijackSession`: every field — ``hijack_id``,
  ``owner``, ``acquired_at == now``, ``lease_expires_at == now + lease_s``
  and ``last_heartbeat == now``.
* The send-failure path: ``st.worker_ws`` cleared to ``None`` and the
  ``(False, "no_worker")`` return.

The harness mirrors ``test_lease.py`` exactly (``_FakeHub`` callbacks,
``_make_state``, ``_make_manager``).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

from provide.uterm.server.bridge.hub.ext import EVENT_HIJACK_ACQUIRED
from provide.uterm.server.bridge.hub.lease import HijackLeaseManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.models import HijackSession, WorkerTermState
from tests.bridge.control_channel_helpers import decode_control_payload


class _FakeHub:
    """Minimal :class:`_LeaseHubCallbacks` impl for unit tests (mirrors test_lease.py)."""

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
    """Create a registered worker state with a live worker_ws (AsyncMock)."""
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


def _captured_pause_frame(st: WorkerTermState) -> dict[str, Any]:
    """Decode the single control frame passed to ``st.worker_ws.send_text``."""
    send_text = st.worker_ws.send_text  # type: ignore[union-attr]
    assert send_text.await_count == 1, f"expected exactly one send_text, got {send_text.await_count}"
    payload = send_text.await_args.args[0]
    return decode_control_payload(payload)


class TestTryAcquireRestSuccess:
    """The happy path: pause frame + session creation + (True, None)."""

    async def test_returns_true_none_on_success(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        registry.put("w1", _make_state())
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="hid", now=1000.0)
        assert result == (True, None)

    async def test_creates_session_with_all_fields(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        now = 1234.5
        lease_s = 77
        ok, err = await mgr.try_acquire_rest("w1", owner="alice", lease_s=lease_s, hijack_id="H-9", now=now)
        assert (ok, err) == (True, None)
        hs = st.hijack_session
        assert hs is not None
        assert isinstance(hs, HijackSession)
        assert hs.hijack_id == "H-9"
        assert hs.owner == "alice"
        assert hs.acquired_at == now
        assert hs.lease_expires_at == now + lease_s
        assert hs.last_heartbeat == now

    async def test_lease_expires_at_is_now_plus_lease_exactly(self) -> None:
        """Pins ``now + lease_s`` (catches +/- and multiplication mutations)."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        await mgr.try_acquire_rest("w1", owner="op", lease_s=30, hijack_id="h", now=500.0)
        assert st.hijack_session is not None
        # Distinct now/lease so now+lease, now-lease, now*lease, lease alone all differ.
        assert st.hijack_session.lease_expires_at == 530.0
        assert st.hijack_session.acquired_at == 500.0
        assert st.hijack_session.last_heartbeat == 500.0

    async def test_pause_frame_every_field(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        await mgr.try_acquire_rest("w1", owner="bob", lease_s=90, hijack_id="HID-42", now=10.0)
        frame = _captured_pause_frame(st)
        assert frame["type"] == "control"
        assert frame["action"] == "pause"
        assert frame["owner"] == "bob"
        assert frame["hijack_id"] == "HID-42"
        assert "ts" in frame
        assert isinstance(frame["ts"], (int, float))

    async def test_pause_frame_owner_tracks_owner_arg(self) -> None:
        """Owner field is the passed owner, not the hijack_id (kills field swap)."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        await mgr.try_acquire_rest("w1", owner="OWNER", lease_s=90, hijack_id="DIFFERENT", now=10.0)
        frame = _captured_pause_frame(st)
        assert frame["owner"] == "OWNER"
        assert frame["hijack_id"] == "DIFFERENT"
        assert frame["owner"] != frame["hijack_id"]

    async def test_pause_sent_exactly_once(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        registry.put("w1", st)
        await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert st.worker_ws.send_text.await_count == 1

    async def test_no_hub_side_effects_on_success(self) -> None:
        """try_acquire_rest does not broadcast / append / metric / notify."""
        mgr, registry, hub, _ = _make_manager()
        registry.put("w1", _make_state())
        await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert hub.send_worker_calls == []
        assert hub.broadcast_calls == []
        assert hub.events == []
        assert hub.metrics == []
        assert hub.notify_calls == []
        assert hub.prune_calls == []


class TestTryAcquireRestNoWorker:
    """Both ``no_worker`` branches return the exact tuple and create nothing."""

    async def test_no_worker_when_state_missing(self) -> None:
        mgr, _registry, _hub, _ = _make_manager()
        result = await mgr.try_acquire_rest("ghost", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert result == (False, "no_worker")

    async def test_no_worker_when_worker_ws_is_none(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.worker_ws = None
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert result == (False, "no_worker")
        # No session created on the failure branch.
        assert st.hijack_session is None

    async def test_no_worker_missing_state_does_not_register_anything(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        await mgr.try_acquire_rest("ghost", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert registry._workers.get("ghost") is None


class TestTryAcquireRestOpenMode:
    """``input_mode == "open"`` short-circuits with ``open_mode``."""

    async def test_open_mode_returns_open_mode(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.input_mode = "open"
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert result == (False, "open_mode")

    async def test_open_mode_creates_no_session_and_sends_nothing(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.input_mode = "open"
        registry.put("w1", st)
        await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert st.hijack_session is None
        assert st.worker_ws.send_text.await_count == 0

    async def test_hijack_mode_does_not_hit_open_branch(self) -> None:
        """A non-open mode proceeds to success (kills == vs != on input_mode)."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.input_mode = "hijack"
        registry.put("w1", st)
        ok, err = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert (ok, err) == (True, None)


class TestTryAcquireRestAlreadyHijacked:
    """Both ``already_hijacked`` branches (dashboard predicate / REST predicate)."""

    async def test_already_hijacked_via_active_dashboard(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() + 30
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert result == (False, "already_hijacked")
        assert st.hijack_session is None

    async def test_already_hijacked_via_valid_rest_lease(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="existing", owner="other", lease_expires_at=time.monotonic() + 30)
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert result == (False, "already_hijacked")
        # The existing session must be untouched (not overwritten).
        assert st.hijack_session is not None
        assert st.hijack_session.hijack_id == "existing"
        assert st.hijack_session.owner == "other"

    async def test_expired_dashboard_does_not_block_acquire(self) -> None:
        """An expired dashboard owner is NOT active -> acquire proceeds.

        Kills the boundary on ``hijack_owner_expires_at > now`` in the
        dashboard predicate (flipping it would treat this as active).
        """
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_owner = AsyncMock()
        st.hijack_owner_expires_at = time.monotonic() - 1
        registry.put("w1", st)
        ok, err = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=5.0)
        assert (ok, err) == (True, None)
        assert st.hijack_session is not None
        assert st.hijack_session.hijack_id == "h"

    async def test_expired_rest_lease_does_not_block_acquire(self) -> None:
        """An expired REST lease is NOT valid -> acquire proceeds and overwrites."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.hijack_session = HijackSession(hijack_id="stale", owner="old", lease_expires_at=time.monotonic() - 1)
        registry.put("w1", st)
        ok, err = await mgr.try_acquire_rest("w1", owner="new", lease_s=90, hijack_id="fresh", now=9.0)
        assert (ok, err) == (True, None)
        assert st.hijack_session is not None
        assert st.hijack_session.hijack_id == "fresh"
        assert st.hijack_session.owner == "new"


class TestTryAcquireRestSendFailure:
    """``send_text`` raising clears ``worker_ws`` and returns ``no_worker``."""

    async def test_send_failure_returns_no_worker(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.worker_ws.send_text = AsyncMock(side_effect=RuntimeError("socket dead"))
        registry.put("w1", st)
        result = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert result == (False, "no_worker")

    async def test_send_failure_clears_worker_ws(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.worker_ws.send_text = AsyncMock(side_effect=RuntimeError("boom"))
        registry.put("w1", st)
        await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert st.worker_ws is None

    async def test_send_failure_creates_no_session(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        st.worker_ws.send_text = AsyncMock(side_effect=RuntimeError("boom"))
        registry.put("w1", st)
        await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert st.hijack_session is None


class TestTryAcquireRestObservability:
    """Pin the exact telemetry of the success path: the span open + the log call.

    These kill mutmut mutations of the *arguments* to
    ``tracer.start_as_current_span(...)`` and ``logger.info(...)`` (event const
    -> None, kwargs -> None / removed, string case-flips, dict-key case-flips)
    that survive because no other assertion inspects the exact call.
    """

    async def test_success_opens_span_with_exact_name_and_attributes(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        registry.put("w1", _make_state())
        with (
            patch("provide.uterm.server.bridge.hub.lease.logger"),
            patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr,
        ):
            ok, err = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert (ok, err) == (True, None)
        mtr.start_as_current_span.assert_called_once_with(
            "uterm.hijack.acquire.rest",
            attributes={"worker_id": "w1", "owner": "op"},
        )

    async def test_success_logs_acquired_event_with_exact_kwargs(self) -> None:
        mgr, registry, _hub, _ = _make_manager()
        registry.put("w1", _make_state())
        with (
            patch("provide.uterm.server.bridge.hub.lease.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.lease.tracer"),
        ):
            ok, err = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="h", now=1.0)
        assert (ok, err) == (True, None)
        mlog.info.assert_called_once_with(
            EVENT_HIJACK_ACQUIRED,
            worker_id="w1",
            hijack_type="rest",
            owner="op",
            lease_s=90,
        )

    async def test_span_and_log_both_fire_exactly_on_success(self) -> None:
        """Both observability calls happen together on the happy path."""
        mgr, registry, _hub, _ = _make_manager()
        registry.put("w1", _make_state())
        with (
            patch("provide.uterm.server.bridge.hub.lease.logger") as mlog,
            patch("provide.uterm.server.bridge.hub.lease.tracer") as mtr,
        ):
            ok, err = await mgr.try_acquire_rest("w1", owner="carol", lease_s=12, hijack_id="hx", now=7.0)
        assert (ok, err) == (True, None)
        mtr.start_as_current_span.assert_called_once_with(
            "uterm.hijack.acquire.rest",
            attributes={"worker_id": "w1", "owner": "carol"},
        )
        mlog.info.assert_called_once_with(
            EVENT_HIJACK_ACQUIRED,
            worker_id="w1",
            hijack_type="rest",
            owner="carol",
            lease_s=12,
        )

    async def test_send_failure_logs_exact_debug(self) -> None:
        """The pause send-failure path logs the exact debug record (kills its arg mutations)."""
        mgr, registry, _hub, _ = _make_manager()
        st = _make_state()
        boom = RuntimeError("socket dead")
        st.worker_ws.send_text = AsyncMock(side_effect=boom)  # type: ignore[union-attr]
        registry.put("w1", st)
        with patch("provide.uterm.server.bridge.hub.lease.logger") as mlog:
            ok, err = await mgr.try_acquire_rest("w1", owner="op", lease_s=90, hijack_id="hid", now=1.0)
        assert (ok, err) == (False, "no_worker")
        mlog.debug.assert_called_once_with("pause_worker_failed worker_id=%s: %s", "w1", boom)
        mlog.info.assert_not_called()
