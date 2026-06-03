#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""HijackLeaseManager: lease/ownership state machine for TermHub workers.

Owns the per-worker hijack lease arbitration that previously lived
inline on ``_OwnershipMixin``. Wraps the core
:class:`provide.uterm.bridge.coordinator.HijackCoordinator` semantics
with the multi-worker, browser-WS-vs-REST dispatch layer that the
FastAPI hub needs:

* REST hijack acquire / heartbeat / release / events.
* Dashboard WebSocket hijack acquire / touch / release / dead-socket
  cleanup.
* Lease-expiry sweep that emits resume frames when *both* slots go idle.

This module is mutation-enforced at killed==100 (489/489): its dedicated
``tests/bridge/hub/test_lease_kill_*.py`` suites pin every return value, state
mutation, control frame, and observability call, so a surviving mutant means a
behaviour went unasserted. Keep that bar when editing.

Lock semantics are unchanged from the pre-refactor mixin: every public
method that needs cross-field atomicity acquires the shared
``TermHub._lock``. The manager holds a reference to the same
``asyncio.Lock`` instance the hub uses elsewhere — no new lock is
introduced and no re-entrancy / ordering is altered. The hub remains
the source of truth for cross-cutting side-effects (broadcast, event
append, prune-if-idle, structured-event callback, metric callback);
those are injected as callables so this module avoids any hub import.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

from provide.telemetry import get_logger, get_tracer
from provide.uterm.server.bridge.hub.ext import (
    EVENT_HIJACK_ACQUIRED,
    EVENT_HIJACK_EXPIRED,
    EVENT_HIJACK_RELEASED,
)
from provide.uterm.server.bridge.models import HijackSession

if TYPE_CHECKING:
    import asyncio

    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub.registry import WorkerRegistry
    from provide.uterm.server.bridge.models import WorkerTermState

logger = get_logger(__name__)
tracer = get_tracer(__name__)


class _LeaseHubCallbacks(Protocol):
    """Subset of :class:`TermHub` the lease manager calls back into.

    Defined as a structural protocol so the manager can be unit-tested
    with a fake hub without dragging the full TermHub surface in.
    """

    def is_hijacked(self, st: WorkerTermState) -> bool: ...
    def is_dashboard_hijack_active(self, st: WorkerTermState) -> bool: ...
    def has_valid_rest_lease(self, st: WorkerTermState) -> bool: ...
    def can_send_input(self, st: WorkerTermState, ws: WebSocket) -> bool: ...
    def metric(self, name: str, value: int = ...) -> None: ...
    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None: ...
    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool: ...
    async def broadcast_hijack_state(self, worker_id: str) -> None: ...
    async def append_event(
        self, worker_id: str, event_type: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...
    async def prune_if_idle(self, worker_id: str) -> None: ...
    async def _recheck_and_resume(self, worker_id: str, now: float) -> None: ...


class HijackLeaseManager:
    """Multi-worker hijack lease state machine for TermHub.

    Args:
        registry: Shared worker registry — the manager reads worker
            state through ``registry._workers`` (matching the existing
            mixin semantics). New code should prefer the explicit
            accessors on :class:`WorkerRegistry`.
        lock: The hub's shared ``asyncio.Lock``. Reused as-is to keep
            lock-ordering invariants identical to the pre-refactor
            mixin; do *not* allocate a fresh lock here.
        dashboard_hijack_lease_s: TTL for dashboard WS leases. Clamped
            into ``[1, 600]`` to match the prior hub-side clamp.
        hub: Callback surface for cross-cutting side-effects
            (broadcast, append_event, send_worker, prune_if_idle,
            metric, notify_hijack_changed, plus the state-predicate
            helpers ``is_hijacked``, ``is_dashboard_hijack_active``,
            ``has_valid_rest_lease``, ``can_send_input``). Held by
            reference so updates on the hub propagate without rewiring.
    """

    __slots__ = ("_dashboard_hijack_lease_s", "_hub", "_lock", "_registry")

    def __init__(
        self,
        registry: WorkerRegistry,
        lock: asyncio.Lock,
        dashboard_hijack_lease_s: int,
        hub: _LeaseHubCallbacks,
    ) -> None:
        self._registry = registry
        self._lock = lock
        self._dashboard_hijack_lease_s = max(1, min(int(dashboard_hijack_lease_s), 600))
        self._hub = hub

    # -- Tunable accessors -------------------------------------------------

    @property
    def dashboard_hijack_lease_s(self) -> int:
        """Configured dashboard-WS lease TTL (seconds)."""
        return self._dashboard_hijack_lease_s

    @dashboard_hijack_lease_s.setter
    def dashboard_hijack_lease_s(self, value: int) -> None:
        self._dashboard_hijack_lease_s = max(1, min(int(value), 600))

    # -- Helpers (no lock) -------------------------------------------------

    @staticmethod
    def compute_lease_expirations(st: Any, now: float) -> tuple[bool, bool]:
        """Return ``(browser_expired, rest_expired)`` without mutating state."""
        lease = st.lease
        rest_expired = lease.session is not None and lease.session.lease_expires_at <= now
        browser_expired = lease.ws is not None and lease.ws_expires_at is not None and lease.ws_expires_at <= now
        return browser_expired, rest_expired

    # -- Expiry sweep ------------------------------------------------------

    async def _expire_leases_under_lock(self, worker_id: str, now: float) -> tuple[bool, bool, bool] | None:
        """Expire stale leases under lock; return (rest, dashboard, resume) or None."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is None:
                return None
            lease = st.lease
            if lease.is_idle:
                return None
            rest_expired, dashboard_expired = lease.expire(now)
            if rest_expired or dashboard_expired:
                st.apply_lease(lease)
            should_resume = (rest_expired or dashboard_expired) and lease.is_idle
        return rest_expired, dashboard_expired, should_resume

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        """Verify no concurrent hijack appeared; send resume if clear."""
        async with self._lock:
            st2 = self._registry._workers.get(worker_id)
            if st2 is not None and self._hub.is_hijacked(st2):  # pragma: no branch
                return
        await self._hub.send_worker(
            worker_id,
            {"type": "control", "action": "resume", "owner": "lease-expired", "lease_s": 0, "ts": now},
        )
        self._hub.notify_hijack_changed(worker_id, enabled=False, owner=None)

    async def cleanup_expired(self, worker_id: str) -> bool:
        """Expire any stale REST or dashboard leases; emit resume on full release.

        Inter-step hooks (``_recheck_and_resume``, ``append_event``,
        ``broadcast_hijack_state``, ``prune_if_idle``, ``metric``) are
        dispatched via ``self._hub.<method>`` rather than directly so that
        mutation-killing tests which patch the hub-level names (e.g.
        ``hub._recheck_and_resume``, ``hub.append_event``) continue to
        intercept the calls after the orchestration moved into the
        service. The hub-level shims forward back to this manager, so the
        cycle terminates on the second hop.
        """
        now = time.monotonic()
        result = await self._expire_leases_under_lock(worker_id, now)
        if result is None:
            return False
        rest_expired, dashboard_expired, should_resume = result
        if not rest_expired and not dashboard_expired:
            return False
        self._hub.metric("hijack_lease_expiries_total")
        if should_resume:
            await self._hub._recheck_and_resume(worker_id, now)
        if rest_expired:
            await self._hub.append_event(worker_id, "hijack_lease_expired")
            logger.info(EVENT_HIJACK_EXPIRED, worker_id=worker_id, hijack_type="rest")
        if dashboard_expired:
            await self._hub.append_event(worker_id, "hijack_owner_expired")
            logger.info(EVENT_HIJACK_EXPIRED, worker_id=worker_id, hijack_type="dashboard")
        await self._hub.broadcast_hijack_state(worker_id)
        await self._hub.prune_if_idle(worker_id)
        return True

    # -- REST session lookup ----------------------------------------------

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession | None:
        """Return the active REST session matching *hijack_id*, or None."""
        await self.cleanup_expired(worker_id)
        return await self._get_rest_session_no_cleanup(worker_id, hijack_id)

    async def _get_rest_session_no_cleanup(self, worker_id: str, hijack_id: str) -> HijackSession | None:
        """Same as :meth:`get_rest_session` but skips the cleanup pass.

        Used by the ownership-mixin shim so it can route the cleanup
        call through ``self.cleanup_expired_hijack`` (preserving mutation
        tests that patch the public method) without doing the work twice.
        """
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is None:
                return None
            hs = st.hijack_session
            if hs is None or hs.lease_expires_at <= time.monotonic() or hs.hijack_id != hijack_id:
                return None
            return hs

    # -- Acquire -----------------------------------------------------------

    async def try_acquire_rest(
        self,
        worker_id: str,
        *,
        owner: str,
        lease_s: int,
        hijack_id: str,
        now: float,
    ) -> tuple[bool, str | None]:
        """Reserve a REST hijack, pause the worker, then finalise the lease.

        The worker-pause ``send_text`` runs OUTSIDE the hub lock: holding the single
        global lock across the send would let one backpressured worker stall every
        other hub operation. The slot is reserved under the lock (``hijack_pending``)
        so concurrent acquires still see it as taken, the pause is sent lock-free, the
        lease is finalised under the lock, and the ``finally`` rolls the reservation
        back on send failure / cancellation / a worker that vanished mid-send.
        """
        with tracer.start_as_current_span(
            "uterm.hijack.acquire.rest", attributes={"worker_id": worker_id, "owner": owner}
        ):
            from provide.uterm.server.bridge.hub.core import _encode_worker_frame

            # Phase 1 — reserve under the lock (in-memory only).
            async with self._lock:
                st = self._registry._workers.get(worker_id)
                if st is None or st.worker_ws is None:
                    return False, "no_worker"
                if st.input_mode == "open":
                    return False, "open_mode"
                if (
                    self._hub.is_dashboard_hijack_active(st)
                    or self._hub.has_valid_rest_lease(st)
                    or st.hijack_pending is not None
                ):
                    return False, "already_hijacked"
                worker_ws = st.worker_ws
                st.hijack_pending = hijack_id

            try:
                # Phase 2 — pause the worker OUTSIDE the lock.
                try:
                    await worker_ws.send_text(
                        _encode_worker_frame(
                            {
                                "type": "control",
                                "action": "pause",
                                "owner": owner,
                                "hijack_id": hijack_id,
                                "ts": time.time(),
                            }
                        )
                    )
                except Exception as exc:
                    logger.debug("pause_worker_failed worker_id=%s: %s", worker_id, exc)
                    async with self._lock:
                        st = self._registry._workers.get(worker_id)
                        if st is not None and st.worker_ws is worker_ws:
                            st.worker_ws = None
                    return False, "no_worker"

                # Phase 3 — finalise under the lock (unless cancelled / superseded).
                async with self._lock:
                    st = self._registry._workers.get(worker_id)
                    if st is None or st.hijack_pending != hijack_id:
                        return False, "no_worker"
                    st.hijack_session = HijackSession(
                        hijack_id=hijack_id,
                        owner=owner,
                        acquired_at=now,
                        lease_expires_at=now + lease_s,
                        last_heartbeat=now,
                    )
                    st.hijack_pending = None
            finally:
                # Roll back a still-outstanding reservation (send failure, cancellation,
                # or a worker that vanished mid-send). On success phase 3 already cleared
                # it, so this is a no-op; a concurrent acquire's reservation (different
                # hijack_id) is left untouched.
                async with self._lock:
                    st = self._registry._workers.get(worker_id)
                    if st is not None and st.hijack_pending == hijack_id:
                        st.hijack_pending = None

            logger.info(EVENT_HIJACK_ACQUIRED, worker_id=worker_id, hijack_type="rest", owner=owner, lease_s=lease_s)
            return True, None

    async def try_acquire_ws(self, worker_id: str, ws: WebSocket) -> tuple[bool, str | None]:
        """Atomically check availability and set the dashboard WS hijack owner."""
        with tracer.start_as_current_span("uterm.hijack.acquire.ws", attributes={"worker_id": worker_id}):
            async with self._lock:
                st = self._registry._workers.get(worker_id)
                if st is None or st.worker_ws is None:
                    return False, "no_worker"
                if self._hub.is_dashboard_hijack_active(st) or self._hub.has_valid_rest_lease(st):
                    return False, "already_hijacked"
                ttl = self._dashboard_hijack_lease_s
                st.hijack_owner = ws
                st.hijack_owner_expires_at = time.monotonic() + ttl
            logger.info(EVENT_HIJACK_ACQUIRED, worker_id=worker_id, hijack_type="dashboard", lease_s=ttl)
            return True, None

    # -- Touch / heartbeat -------------------------------------------------

    async def touch_owner(self, worker_id: str, lease_s: int | None = None) -> float | None:
        """Extend the dashboard WS hijack lease; returns new expiry or None."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is None or st.hijack_owner is None:
                return None
            ttl = self._dashboard_hijack_lease_s if lease_s is None else max(1, min(int(lease_s), 600))
            st.hijack_owner_expires_at = time.monotonic() + ttl
            return st.hijack_owner_expires_at

    async def touch_if_owner(self, worker_id: str, ws: WebSocket) -> float | None:
        """Atomically verify WS ownership and extend lease; returns new expiry or None."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is None or not self._hub.is_dashboard_hijack_active(st) or st.hijack_owner is not ws:
                return None
            st.hijack_owner_expires_at = time.monotonic() + self._dashboard_hijack_lease_s
            return st.hijack_owner_expires_at

    # -- Release -----------------------------------------------------------

    async def try_release_ws(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Atomically verify ownership and clear in a single lock block."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is None or not self._hub.is_dashboard_hijack_active(st) or st.hijack_owner is not ws:
                rest_active = st is not None and self._hub.has_valid_rest_lease(st)
                return False, rest_active
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
            rest_active = self._hub.has_valid_rest_lease(st)
        logger.info(EVENT_HIJACK_RELEASED, worker_id=worker_id, hijack_type="dashboard")
        return True, rest_active

    async def remove_dead_browsers(self, worker_id: str, dead: set[WebSocket]) -> bool:
        """Remove *dead* browser sockets under lock; resume if owner was dead."""
        notify_hijack_off = False
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is not None:
                for ws in dead:
                    st.browsers.pop(ws, None)
                    if self._hub.is_dashboard_hijack_active(st) and st.hijack_owner is ws:
                        st.hijack_owner = None
                        st.hijack_owner_expires_at = None
                        notify_hijack_off = not self._hub.has_valid_rest_lease(st)
        if notify_hijack_off:
            # Re-check: a concurrent acquire may have written a new session
            # between the lock release above and _send_worker below.
            async with self._lock:
                _st2 = self._registry._workers.get(worker_id)
                if _st2 is not None and self._hub.is_hijacked(_st2):  # pragma: no branch
                    notify_hijack_off = False
        if notify_hijack_off:
            await self._hub.send_worker(
                worker_id,
                {"type": "control", "action": "resume", "owner": "dead-socket", "lease_s": 0, "ts": time.time()},
            )
            self._hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
        return notify_hijack_off

    async def extend_lease(self, worker_id: str, hijack_id: str, owner: str, lease_s: int, now: float) -> float | None:
        """Extend the REST hijack lease. Returns new expiry or None."""
        with tracer.start_as_current_span(
            "uterm.hijack.heartbeat", attributes={"worker_id": worker_id, "owner": owner}
        ):
            async with self._lock:
                st = self._registry._workers.get(worker_id)
                if st is None or st.hijack_session is None or st.hijack_session.hijack_id != hijack_id:
                    return None
                if st.hijack_session.owner != owner:
                    logger.warning(
                        "hijack_heartbeat_denied_owner_mismatch worker_id=%s hijack_id=%s current=%s attempted=%s",
                        worker_id,
                        hijack_id,
                        st.hijack_session.owner,
                        owner,
                    )
                    self._hub.metric("hijack_heartbeat_denied_owner_mismatch")
                    return None
                st.hijack_session.last_heartbeat = now
                st.hijack_session.lease_expires_at = now + lease_s
                return st.hijack_session.lease_expires_at

    async def get_fresh_expiry(self, worker_id: str, hijack_id: str, fallback: float) -> float:
        """Re-read the current lease expiry under lock."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is not None and st.hijack_session is not None and st.hijack_session.hijack_id == hijack_id:
                return st.hijack_session.lease_expires_at
        return fallback

    async def get_events_data(
        self,
        worker_id: str,
        hijack_id: str,
        hs: HijackSession,
        after_seq: int,
        limit: int,
    ) -> dict[str, Any]:
        """Return the events payload for a REST hijack events endpoint."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is None:  # pragma: no cover
                return {
                    "rows": [],
                    "latest_seq": 0,
                    "min_event_seq": 0,
                    "fresh_expires": hs.lease_expires_at,
                }
            rows = [evt for evt in list(st.events) if int(evt.get("seq", 0)) > after_seq][:limit]
            latest_seq = st.event_seq
            min_event_seq = st.min_event_seq
            fresh_expires = (
                st.hijack_session.lease_expires_at
                if st.hijack_session is not None and st.hijack_session.hijack_id == hijack_id
                else hs.lease_expires_at
            )
        return {
            "rows": rows,
            "latest_seq": latest_seq,
            "min_event_seq": min_event_seq,
            "fresh_expires": fresh_expires,
        }

    async def check_valid(self, worker_id: str, hijack_id: str) -> bool:
        """Return True if the REST hijack session is still valid."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            return (
                st is not None
                and st.hijack_session is not None
                and st.hijack_session.hijack_id == hijack_id
                and st.hijack_session.lease_expires_at > time.monotonic()
            )

    async def release_rest(self, worker_id: str, hijack_id: str) -> tuple[bool, bool]:
        """Atomically clear the REST hijack session."""
        with tracer.start_as_current_span("uterm.hijack.release", attributes={"worker_id": worker_id}):
            async with self._lock:
                st = self._registry._workers.get(worker_id)
                if st is None or st.hijack_session is None or st.hijack_session.hijack_id != hijack_id:
                    return False, False
                st.hijack_session = None
                should_resume = not self._hub.is_dashboard_hijack_active(st)
            return True, should_resume

    async def still_hijacked(self, worker_id: str) -> bool:
        """Return True if any hijack (REST or dashboard WS) is currently active."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is None:
                return False
            return bool(self._hub.is_hijacked(st))

    async def is_input_open_mode(self, worker_id: str) -> bool:
        """Return True if the worker is in open input mode."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            return st is not None and st.input_mode == "open"

    async def prepare_browser_input(self, worker_id: str, ws: WebSocket) -> bool:
        """Check if ws may send input; also extend the dashboard lease if ws is owner."""
        async with self._lock:
            st = self._registry._workers.get(worker_id)
            if st is None:
                return False
            allowed: bool = bool(self._hub.can_send_input(st, ws))
            if self._hub.is_dashboard_hijack_active(st) and st.hijack_owner is ws:
                st.hijack_owner_expires_at = time.monotonic() + self._dashboard_hijack_lease_s
            return allowed


__all__ = ["HijackLeaseManager"]
