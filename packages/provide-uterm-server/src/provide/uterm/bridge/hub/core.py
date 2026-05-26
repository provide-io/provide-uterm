#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TermHub: in-memory registry for terminal WebSocket connections."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger

try:
    from fastapi import WebSocket
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for TermHub: pip install 'provide-uterm[websocket]'") from _e

from provide.uterm.bridge.hub.approvals import InMemoryApprovalStore
from provide.uterm.bridge.hub.connection import ConnectionManager
from provide.uterm.bridge.hub.ext import (
    BehavioralAuditGate,
    BehavioralThresholds,
    NoOpBehavioralAuditGate,
    NoOpPolicyGate,
    OutputPolicyGate,
    PolicyDecision,
    PolicyGate,
)
from provide.uterm.bridge.hub.lease import HijackLeaseManager
from provide.uterm.bridge.hub.limiter import RateLimiter
from provide.uterm.bridge.hub.polling_service import PollingCoordinator
from provide.uterm.bridge.hub.presence import PresenceManager
from provide.uterm.bridge.hub.registry import WorkerRegistry
from provide.uterm.bridge.hub.resume import ResumeSession, ResumeTokenStore
from provide.uterm.bridge.hub.router import MessageRouter
from provide.uterm.bridge.hub.store import StateStore
from provide.uterm.control_channel import encode_control, encode_data

if TYPE_CHECKING:
    from collections import deque

    from fastapi import APIRouter

    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.bridge.frames import HijackStateFrame
    from provide.uterm.bridge.hub.event_bus import EventBus
    from provide.uterm.bridge.hub.ext import PolicyContext
    from provide.uterm.bridge.identity import IdentityProvider
    from provide.uterm.bridge.models import HijackSession, WorkerTermState
    from provide.uterm.bridge.ratelimit import TokenBucket

logger = get_logger(__name__)

HijackStateCallback = Callable[[str, bool, str | None], Awaitable[None] | None]
BrowserRoleResolver = Callable[[WebSocket, str], str | None | Awaitable[str | None]]
MetricCallback = Callable[[str, int], None]
WorkerEmptyCallback = Callable[[str], Coroutine[Any, Any, None]]
ResumeCallback = Callable[[str, ResumeSession], Awaitable[bool]]


def _encode_browser_frame(msg: dict[str, Any]) -> str:
    if str(msg.get("type") or "") == "term":
        return encode_data(str(msg.get("data") or ""))
    return encode_control(msg)


def _encode_worker_frame(msg: dict[str, Any]) -> str:
    if str(msg.get("type") or "") == "input":
        return encode_data(str(msg.get("data") or ""))
    return encode_control(msg)


def _mono_to_wall(mono_ts: float | None) -> float | None:
    """Convert a monotonic timestamp to wall-clock for external consumers."""
    if mono_ts is None:
        return None
    return time.time() + (mono_ts - time.monotonic())


class BrowserRoleResolutionError(RuntimeError):
    """Raised when a browser-role resolver fails and the WS should be rejected."""


class TermHub:
    """In-memory registry for terminal WebSocket connections."""

    # -- PollingCoordinator delegates (Phase 7b: ex-_PollingMixin) -----------
    # The coordinator owns the actual implementation; ``hub.polling`` is
    # the canonical handle. These class-level pass-throughs keep the
    # legacy ``hub.<name>(...)`` call surface intact without an extra
    # mixin in the MRO. ``snapshot_matches`` is exposed as a
    # ``staticmethod`` so ``TermHub.snapshot_matches(...)`` keeps working.

    snapshot_matches = staticmethod(PollingCoordinator.snapshot_matches)

    async def wait_for_snapshot(self, worker_id: str, timeout_ms: int = 1500) -> dict[str, Any] | None:
        """Poll for a fresh snapshot from *worker_id*, waiting up to *timeout_ms* ms."""
        return await self.polling.wait_for_snapshot(worker_id, timeout_ms)

    async def wait_for_guard(
        self,
        worker_id: str,
        *,
        expect_prompt_id: str | None,
        expect_regex: str | None,
        timeout_ms: int,
        poll_interval_ms: int,
    ) -> tuple[bool, dict[str, Any] | None, str | None]:
        """Poll until the snapshot satisfies prompt-id/regex guards or *timeout_ms* elapses."""
        return await self.polling.wait_for_guard(
            worker_id,
            expect_prompt_id=expect_prompt_id,
            expect_regex=expect_regex,
            timeout_ms=timeout_ms,
            poll_interval_ms=poll_interval_ms,
        )

    # -- StateStore delegates (Phase 7b: ex-HubStateMixin) -----------------
    # The store (``self.state``) owns the actual implementation. The
    # methods below are thin pass-throughs that keep the legacy
    # ``hub.<name>(...)`` call surface intact without an extra mixin in
    # the MRO. Tests still monkey-patch ``shutdown`` and
    # ``notify_hijack_changed`` on hub instances — that pattern keeps
    # working because instance attributes shadow class methods regardless
    # of where the method lives in the class hierarchy.

    @property
    def event_bus(self) -> EventBus | None:
        """Public accessor for the EventBus instance (None if not configured)."""
        return self._event_bus

    @event_bus.setter
    def event_bus(self, value: EventBus | None) -> None:
        """Backward-compatible setter used by tests and app wiring."""
        self._event_bus = value

    def _buffer_and_get_command(self, ws: WebSocket, data: str) -> str | None:
        """Accumulate input for *ws* and return the command if a newline is received."""
        return self.state.buffer_and_get_command(ws, data)

    async def shutdown(self) -> None:
        """Cancel all background tasks for graceful shutdown."""
        await self.state.shutdown()

    async def touch_activity(self, worker_id: str) -> None:
        """Update the last-activity timestamp for *worker_id*."""
        await self.state.touch_activity(worker_id)

    def metric(self, name: str, value: int = 1) -> None:
        """Emit a named metric via the configured on_metric callback."""
        self.state.metric(name, value)

    # ``staticmethod`` wrappers around the canonical implementations on
    # :class:`StateStore` so legacy ``hub.clamp_lease(...)`` /
    # ``TermHub.is_dashboard_hijack_active(st)`` call sites keep working.
    clamp_lease = staticmethod(StateStore.clamp_lease)
    has_valid_rest_lease = staticmethod(StateStore.has_valid_rest_lease)
    is_dashboard_hijack_active = staticmethod(StateStore.is_dashboard_hijack_active)

    def is_hijacked(self, st: WorkerTermState) -> bool:
        """Return True if *st* is under any active hijack (dashboard WS or REST)."""
        return self.state.is_hijacked(st)

    async def _get(self, worker_id: str) -> WorkerTermState:
        """Return the existing :class:`WorkerTermState` for *worker_id* or create one."""
        return await self.state.get_or_create(worker_id)

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        """Fire the on_hijack_changed callback (sync or async) without blocking."""
        self.state.notify_hijack_changed(worker_id, enabled=enabled, owner=owner)

    async def _resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        """Resolve a browser role via the configured callback; defaults to "viewer"."""
        return await self.state.resolve_role_for_browser(ws, worker_id)

    async def prepare_policy_context(self, ws: WebSocket, worker_id: str, action: str | None = None) -> PolicyContext:
        """Create a :class:`PolicyContext` for the given browser WebSocket and worker."""
        return await self.state.prepare_policy_context(ws, worker_id, action)

    def _map_roles(self, principal: Any) -> frozenset[str]:
        """Map an identity-provider principal to a frozen set of hub roles."""
        return self.state._map_roles(principal)

    # -- HijackLeaseManager delegates (Phase 7b: ex-_OwnershipMixin) --------
    # ``self.lease`` (HijackLeaseManager) owns the hijack state machine.
    # The pure-forward methods below keep the legacy ``hub.<name>(...)``
    # call surface intact without an extra mixin in the MRO. Tests
    # monkey-patch ``_recheck_and_resume`` and ``cleanup_expired_hijack``
    # on hub instances; instance attributes shadow class methods so the
    # patches keep working after the move.
    #
    # ``cleanup_expired_hijack`` keeps its inline orchestration body
    # (rather than forwarding straight to the lease service) so mutation
    # tests that patch ``hub._recheck_and_resume`` still see the call go
    # through ``self._recheck_and_resume``. ``get_rest_session`` routes
    # the expiry sweep through ``self.cleanup_expired_hijack`` for the
    # same reason.

    @staticmethod
    def _compute_lease_expirations(st: Any, now: float) -> tuple[bool, bool]:
        """Return ``(browser_expired, rest_expired)`` without mutating state."""
        return HijackLeaseManager.compute_lease_expirations(st, now)

    async def _expire_leases_under_lock(self, worker_id: str, now: float) -> tuple[bool, bool, bool] | None:
        """Expire stale leases under lock — forwards to :attr:`lease`."""
        return await self.lease._expire_leases_under_lock(worker_id, now)

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        """Re-check under lock and send resume — forwards to :attr:`lease`."""
        await self.lease._recheck_and_resume(worker_id, now)

    async def cleanup_expired_hijack(self, worker_id: str) -> bool:
        """Expire stale REST/dashboard leases; emit resume if fully released."""
        from provide.uterm.bridge.hub.ext import EVENT_HIJACK_EXPIRED
        from provide.uterm.bridge.hub.lease import logger as _lease_logger

        now = time.monotonic()
        result = await self._expire_leases_under_lock(worker_id, now)
        if result is None:
            return False
        rest_expired, dashboard_expired, should_resume = result
        if not rest_expired and not dashboard_expired:
            return False
        self.metric("hijack_lease_expiries_total")
        if should_resume:
            await self._recheck_and_resume(worker_id, now)
        if rest_expired:
            await self.append_event(worker_id, "hijack_lease_expired")
            _lease_logger.info(EVENT_HIJACK_EXPIRED, worker_id=worker_id, hijack_type="rest")
        if dashboard_expired:
            await self.append_event(worker_id, "hijack_owner_expired")
            _lease_logger.info(EVENT_HIJACK_EXPIRED, worker_id=worker_id, hijack_type="dashboard")
        await self.broadcast_hijack_state(worker_id)
        await self.prune_if_idle(worker_id)
        return True

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession | None:
        """Return the active REST session for *hijack_id* or None."""
        await self.cleanup_expired_hijack(worker_id)
        return await self.lease._get_rest_session_no_cleanup(worker_id, hijack_id)

    async def try_acquire_rest_hijack(
        self,
        worker_id: str,
        *,
        owner: str,
        lease_s: int,
        hijack_id: str,
        now: float,
    ) -> tuple[bool, str | None]:
        """Atomically check availability and create a REST hijack session."""
        return await self.lease.try_acquire_rest(worker_id, owner=owner, lease_s=lease_s, hijack_id=hijack_id, now=now)

    async def try_acquire_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, str | None]:
        """Atomically check availability and set the dashboard WS hijack owner."""
        return await self.lease.try_acquire_ws(worker_id, ws)

    async def touch_hijack_owner(self, worker_id: str, lease_s: int | None = None) -> float | None:
        """Extend the dashboard WS hijack lease."""
        return await self.lease.touch_owner(worker_id, lease_s)

    async def touch_if_owner(self, worker_id: str, ws: WebSocket) -> float | None:
        """Atomically verify WS ownership and extend lease."""
        return await self.lease.touch_if_owner(worker_id, ws)

    async def try_release_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Atomically verify ownership and clear in a single lock block."""
        return await self.lease.try_release_ws(worker_id, ws)

    async def extend_hijack_lease(
        self, worker_id: str, hijack_id: str, owner: str, lease_s: int, now: float
    ) -> float | None:
        """Extend the REST hijack lease."""
        return await self.lease.extend_lease(worker_id, hijack_id, owner, lease_s, now)

    async def get_fresh_hijack_expiry(self, worker_id: str, hijack_id: str, fallback: float) -> float:
        """Re-read the current lease expiry under lock."""
        return await self.lease.get_fresh_expiry(worker_id, hijack_id, fallback)

    async def get_hijack_events_data(
        self,
        worker_id: str,
        hijack_id: str,
        hs: HijackSession,
        after_seq: int,
        limit: int,
    ) -> dict[str, Any]:
        """Return the events payload for a REST hijack events endpoint."""
        return await self.lease.get_events_data(worker_id, hijack_id, hs, after_seq, limit)

    async def check_hijack_valid(self, worker_id: str, hijack_id: str) -> bool:
        """Return True if the REST hijack session is still valid."""
        return await self.lease.check_valid(worker_id, hijack_id)

    async def release_rest_hijack(self, worker_id: str, hijack_id: str) -> tuple[bool, bool]:
        """Atomically clear the REST hijack session."""
        return await self.lease.release_rest(worker_id, hijack_id)

    async def check_still_hijacked(self, worker_id: str) -> bool:
        """Return True if any hijack (REST or dashboard WS) is currently active."""
        return await self.lease.still_hijacked(worker_id)

    async def is_input_open_mode(self, worker_id: str) -> bool:
        """Return True if the worker is in open input mode."""
        return await self.lease.is_input_open_mode(worker_id)

    async def prepare_browser_input(self, worker_id: str, ws: WebSocket) -> bool:
        """Check if ws may send input; extends dashboard lease if ws is owner."""
        return await self.lease.prepare_browser_input(worker_id, ws)

    # -- ConnectionManager / PresenceManager delegates (Phase 7b:
    #    ex-_ConnectionMixin) -------------------------------------------
    # ``self.connection_mgr`` (ConnectionManager) owns the worker/browser
    # register/deregister + REST rate-limit gate paths, and the
    # ``force_release_hijack`` lifecycle. ``self.presence_mgr``
    # (PresenceManager) owns the read-only browser presence queries and
    # the worker-bound presence control frames. The methods below keep
    # the legacy ``hub.<name>(...)`` call surface intact without an extra
    # mixin in the MRO. Tests monkey-patch ``request_snapshot`` and
    # ``force_release_hijack`` on hub instances; instance attributes
    # shadow class methods so the patches keep working after the move.

    def allow_rest_acquire_for(self, client_id: str) -> bool:
        """Per-client REST acquire rate limit (also checks the global bucket)."""
        return self.connection_mgr.allow_rest_acquire_for(client_id)

    def allow_rest_send_for(self, client_id: str) -> bool:
        """Per-client REST send/step rate limit (also checks the global bucket)."""
        return self.connection_mgr.allow_rest_send_for(client_id)

    def worker_token(self) -> str | None:
        """Return the configured worker bearer token (read-only)."""
        return self.connection_mgr.worker_token()

    async def register_worker(self, worker_id: str, ws: WebSocket) -> bool:
        """Register *ws* as the active worker for *worker_id*."""
        return await self.connection_mgr.register_worker(worker_id, ws)

    async def is_active_worker(self, worker_id: str, ws: WebSocket) -> bool:
        """Return True if *ws* is still the registered worker for *worker_id*."""
        return await self.connection_mgr.is_active_worker(worker_id, ws)

    async def set_worker_tunnel_flag(self, worker_id: str, value: bool) -> None:
        """Mark whether ``worker_id``'s worker WS uses the tunnel wire format."""
        await self.connection_mgr.set_worker_tunnel_flag(worker_id, value)

    async def set_worker_hello(self, worker_id: str, mode: InputMode, protocol_version: int | None = None) -> bool:
        """Process a ``worker_hello`` message: set input_mode and persist protocol version."""
        return await self.connection_mgr.set_worker_hello(worker_id, mode, protocol_version)

    async def update_last_snapshot(self, worker_id: str, snapshot: dict[str, Any]) -> None:
        """Store *snapshot* as the most recent snapshot for *worker_id*."""
        await self.connection_mgr.update_last_snapshot(worker_id, snapshot)

    async def register_browser(
        self, worker_id: str, ws: WebSocket, role: str, *, defer_broadcast: bool = False
    ) -> dict[str, Any]:
        """Register *ws* as a browser for *worker_id* and return initial state."""
        return await self.connection_mgr.register_browser(worker_id, ws, role, defer_broadcast=defer_broadcast)

    async def activate_browser_broadcasts(self, worker_id: str, ws: WebSocket) -> None:
        """Allow broadcasts to a browser after its startup frames have been sent."""
        await self.connection_mgr.activate_browser_broadcasts(worker_id, ws)

    async def register_browser_state_snapshot(self, worker_id: str, ws: WebSocket) -> dict[str, Any]:
        """Return current browser state without re-registering (resume helper)."""
        return await self.presence_mgr.register_browser_state_snapshot(worker_id, ws)

    async def resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        """Public wrapper around the hub's ``_resolve_role_for_browser`` callback."""
        return await self.presence_mgr.resolve_role_for_browser(ws, worker_id)

    def can_send_input(self, st: WorkerTermState, ws: WebSocket) -> bool:
        """Check if *ws* can send input to the worker (open mode or hijack owner)."""
        return self.presence_mgr.can_send_input(st, ws)

    async def request_snapshot(self, worker_id: str) -> None:
        """Send a ``snapshot_req`` control frame to the worker (no-op if no worker)."""
        await self.presence_mgr.request_snapshot(worker_id)

    async def request_analysis(self, worker_id: str) -> None:
        """Send an ``analyze_req`` control frame to the worker (no-op if no worker)."""
        await self.presence_mgr.request_analysis(worker_id)

    async def force_release_hijack(self, worker_id: str) -> bool:
        """Forcibly clear any active hijack for *worker_id* and send a resume frame."""
        return await self.connection_mgr.force_release_hijack(worker_id)

    # -- MessageRouter delegates (Phase 7b: ex-HubMessagingMixin) -----------
    # ``self.router`` (MessageRouter) owns the broadcast / send_worker hot
    # path plus the behavioral-heuristics ring buffer. The methods below
    # keep the legacy ``hub.<name>(...)`` call surface intact. Tests
    # monkey-patch broadcast / broadcast_hijack_state / send_worker /
    # set_input_mode / append_event / get_idle_candidates / etc.; instance
    # attributes shadow class methods so those patches keep working.
    #
    # ``disconnect_worker`` and ``_run_behavioral_audit_loop`` keep their
    # inline implementations so the test-only monkey-patch pattern on
    # ``broadcast_hijack_state`` / ``prune_if_idle`` / ``notify_hijack_changed``
    # / ``_audit_all_browsers`` keeps intercepting via ``self.<name>``.
    # The exception loggers stay on this module for the same reason.

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append a timestamped event to the worker's event ring buffer and return it."""
        return await self.router.append_event(worker_id, event_type, data)

    async def broadcast(self, worker_id: str, msg: dict[str, Any]) -> None:
        """Send *msg* to all browser WebSockets registered for *worker_id*."""
        await self.router.broadcast(worker_id, msg)

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        """Send a hijack_state message to every browser for *worker_id*."""
        await self.router.broadcast_hijack_state(worker_id)

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        """Send *msg* to the worker WebSocket; returns False if no worker is connected."""
        return await self.router.send_worker(worker_id, msg, source=source)

    async def hijack_state_msg_for(self, worker_id: str, ws: WebSocket) -> HijackStateFrame:
        """Build a hijack_state dict for *ws*, setting owner='me' if *ws* holds the lease."""
        return await self.router.hijack_state_msg_for(worker_id, ws)

    async def set_input_mode(self, worker_id: str, mode: InputMode) -> tuple[bool, str | None]:
        """Set input_mode under lock. Rejects if active hijack when switching to "open"."""
        return await self.router.set_input_mode(worker_id, mode)

    async def disconnect_worker(self, worker_id: str) -> bool:
        """Programmatically disconnect the worker WS. Returns True if a worker was connected."""
        from provide.uterm.bridge.frames import make_worker_disconnected_frame

        ws: WebSocket | None = None
        async with self._lock:
            st = self.registry.get(worker_id)
            if st is None or st.worker_ws is None:
                return False
            ws = st.worker_ws
            st.worker_ws = None
            was_hijacked = st.hijack_session is not None or st.hijack_owner is not None
            st.hijack_session = None
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
        try:
            await ws.close()
        except Exception as exc:
            logger.debug("disconnect_worker close error worker_id=%s: %s", worker_id, exc)
        await self.broadcast(worker_id, cast("dict[str, Any]", make_worker_disconnected_frame(worker_id)))
        if was_hijacked:
            self.notify_hijack_changed(worker_id, enabled=False, owner=None)
            await self.broadcast_hijack_state(worker_id)
        if self._event_bus is not None:
            self._event_bus.close_worker(worker_id)
        await self.prune_if_idle(worker_id)
        return True

    async def prune_if_idle(self, worker_id: str) -> None:
        """Remove worker state when no connections or leases remain."""
        await self.router.prune_if_idle(worker_id)

    async def get_idle_candidates(self, timeout_s: float) -> list[tuple[str, float]]:
        """Return ``(worker_id, last_activity_at)`` for workers idle beyond *timeout_s*."""
        return await self.router.get_idle_candidates(timeout_s)

    async def set_browser_role(self, worker_id: str, ws: WebSocket, role: str) -> None:
        """Update the role for *ws* in *worker_id*'s browser set."""
        await self.router.set_browser_role(worker_id, ws, role)

    async def try_reclaim_hijack(self, worker_id: str, ws: WebSocket) -> bool:
        """Attempt to acquire hijack ownership for *ws* if the session is unhijacked."""
        return await self.router.try_reclaim_hijack(worker_id, ws)

    async def get_worker_browser_role(self, worker_id: str, ws: WebSocket) -> str | None:
        """Return the role assigned to *ws* for *worker_id*, or ``None`` if not found."""
        return await self.router.get_worker_browser_role(worker_id, ws)

    async def get_last_snapshot(self, worker_id: str) -> dict[str, Any] | None:
        """Return the most recent snapshot for *worker_id*, or ``None`` if not registered."""
        return await self.router.get_last_snapshot(worker_id)

    async def browser_count(self, worker_id: str) -> int:
        """Return the number of browser WebSockets currently connected for *worker_id*."""
        return await self.router.browser_count(worker_id)

    async def browser_count_total(self) -> int:
        """Return the total number of browser WebSockets connected across all workers."""
        return await self.router.browser_count_total()

    async def get_recent_events(self, worker_id: str, limit: int) -> list[dict[str, Any]]:
        """Return the most recent events for *worker_id* (up to *limit*, clamped to 1-500)."""
        return await self.router.get_recent_events(worker_id, limit)

    def _record_keystroke(self, source: Any) -> None:
        """Record the timing of a keystroke from a browser."""
        self.router.record_keystroke(source)

    def _get_heuristics(self, source: Any) -> dict[str, float]:
        """Return behavioral metrics for the given browser."""
        return self.router.get_heuristics(source)

    async def _send_hijack_state_to(
        self,
        browsers: list[WebSocket],
        *,
        worker_id: str,
        is_hijacked: bool,
        is_dashboard: bool,
        is_rest: bool,
        hijack_owner: WebSocket | None,
        input_mode: str,
        lease_expires_at: float | None,
        suppress_errors: bool = False,
    ) -> set[WebSocket]:
        """Forward to :meth:`MessageRouter.send_hijack_state_to`."""
        return await self.router.send_hijack_state_to(
            browsers,
            worker_id=worker_id,
            is_hijacked=is_hijacked,
            is_dashboard=is_dashboard,
            is_rest=is_rest,
            hijack_owner=hijack_owner,
            input_mode=input_mode,
            lease_expires_at=lease_expires_at,
            suppress_errors=suppress_errors,
        )

    async def _audit_all_browsers(self) -> None:
        """Iterate all active browsers and evaluate behavioral heuristics."""
        await self.router.audit_all_browsers()

    async def _run_behavioral_audit_loop(self) -> None:
        """Periodically audit active connections for behavioral anomalies."""
        while True:
            await asyncio.sleep(self._behavioral_audit_interval_s)
            try:
                await self._audit_all_browsers()
            except Exception:
                logger.exception("behavioral_audit_loop_error")

    @property
    def _keystroke_timestamps(self) -> dict[Any, deque[float]]:
        """Back-compat view of the router's keystroke ring buffer map."""
        return self.router.keystroke_timestamps

    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]:
        """Clear heuristic state and call into the connection manager."""
        self.router.forget_browser(ws)
        self._input_buffers.pop(ws, None)
        self._hold_buffers.pop(ws, None)
        return await self.connection_mgr.cleanup_browser_disconnect(worker_id, ws, owned_hijack)

    async def remove_dead_browsers(self, worker_id: str, dead: set[WebSocket]) -> bool:
        """Clear input buffers for dead browsers and call into the lease manager."""
        for ws in dead:
            self._input_buffers.pop(ws, None)
            self._hold_buffers.pop(ws, None)
            self._startup_pending_browsers.discard(ws)
        return await self.lease.remove_dead_browsers(worker_id, dead)

    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Deregister the worker WS and notify the EventBus on disconnect."""
        should_broadcast, was_hijacked = await self.connection_mgr.deregister_worker(worker_id, ws)
        if should_broadcast and self._event_bus is not None:
            self._event_bus.close_worker(worker_id)
        return should_broadcast, was_hijacked

    @property
    def resume_store(self) -> ResumeTokenStore | None:
        """Public accessor for the resume token store."""
        return self._resume_store

    def create_router(self, *, extra_route_registrars: list[Any] | None = None) -> APIRouter:
        """Create and return a FastAPI ``APIRouter`` with all terminal routes registered."""
        from fastapi import APIRouter

        from provide.uterm.bridge.routes.rest import register_rest_routes
        from provide.uterm.bridge.routes.websockets import register_ws_routes

        router = APIRouter()
        register_rest_routes(self, router)
        register_ws_routes(self, router)
        for registrar in extra_route_registrars or []:
            registrar(self, router)
        return router

    def __init__(
        self,
        on_hijack_changed: HijackStateCallback | None = None,
        on_metric: MetricCallback | None = None,
        dashboard_hijack_lease_s: int = 45,
        *,
        resolve_browser_role: BrowserRoleResolver | None = None,
        on_worker_empty: WorkerEmptyCallback | None = None,
        max_ws_message_bytes: int = 1_048_576,
        max_input_chars: int = 10_000,
        browser_rate_limit_per_sec: float = 30,
        # Non-input control frames (hijack_request, presence_update, resume,
        # queued_input, control_request) are budgeted separately from input
        # keystrokes. The cap is intentionally smaller — a legitimate UI
        # emits at most a few control frames per second; higher rates are a
        # client-side bug or an abuse attempt. The budget protects the hub
        # from a hostile browser flooding hijack_requests or presence_updates.
        browser_control_rate_limit_per_sec: float = 10,
        rest_acquire_rate_limit_per_sec: float = 5,
        rest_send_rate_limit_per_sec: float = 20,
        worker_token: str | None = None,
        event_deque_maxlen: int = 2000,
        resume_store: ResumeTokenStore | None = None,
        resume_ttl_s: float = 300,
        on_resume: ResumeCallback | None = None,
        event_bus: EventBus | None = None,
        ws_idle_timeout_s: float = 14400.0,
        policy_gate: PolicyGate | None = None,
        identity_provider: IdentityProvider | None = None,
        delegate_roles: bool = True,
        output_policy_gate: OutputPolicyGate | None = None,
        behavioral_audit_gate: BehavioralAuditGate | None = None,
        behavioral_thresholds: BehavioralThresholds | None = None,
        behavioral_audit_interval_s: float = 30.0,
    ) -> None:
        self._lock = asyncio.Lock()
        # WorkerRegistry owns the worker map; the legacy ``_workers``
        # attribute is exposed as a property below so existing mixin
        # code can continue to use mapping operations unchanged while
        # the phased refactor migrates call sites to ``self.registry``.
        self.registry = WorkerRegistry()
        self._on_hijack_changed = on_hijack_changed
        self._on_metric = on_metric
        self._resolve_browser_role = resolve_browser_role
        self.on_worker_empty: WorkerEmptyCallback | None = on_worker_empty
        self._worker_token = worker_token
        # HijackLeaseManager owns the hijack state machine; legacy
        # ``_dashboard_hijack_lease_s`` is exposed via a property shim
        # below so existing mixin and test code that reads it as an
        # attribute keeps working.
        self.lease = HijackLeaseManager(
            registry=self.registry,
            lock=self._lock,
            dashboard_hijack_lease_s=int(dashboard_hijack_lease_s),
            hub=self,
        )
        self.max_ws_message_bytes = max(1024, int(max_ws_message_bytes))
        self.max_input_chars = max(100, int(max_input_chars))
        self.browser_rate_limit_per_sec = float(browser_rate_limit_per_sec)
        self.browser_control_rate_limit_per_sec = max(0.1, float(browser_control_rate_limit_per_sec))
        # RateLimiter owns the per-purpose REST token buckets; legacy
        # ``_rest_*`` attributes are exposed as property shims below so
        # existing mixin and test code that pokes the buckets directly
        # continues to work while the phased refactor migrates call
        # sites to ``self.limiter`` accessors.
        self.limiter = RateLimiter(
            rest_acquire_rate=float(rest_acquire_rate_limit_per_sec),
            rest_send_rate=float(rest_send_rate_limit_per_sec),
        )
        self._event_deque_maxlen = max(1, int(event_deque_maxlen))
        self._resume_store = resume_store
        self._resume_ttl_s = max(1.0, float(resume_ttl_s))
        self._on_resume = on_resume
        self._ws_to_resume_token: dict[WebSocket, str] = {}
        self._startup_pending_browsers: set[WebSocket] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._event_bus = event_bus
        self.ws_idle_timeout_s = max(10.0, float(ws_idle_timeout_s))
        self._policy_gate = policy_gate or NoOpPolicyGate()
        self._input_buffers: dict[WebSocket, str] = {}
        self._hold_buffers: dict[WebSocket, str] = {}
        # InMemoryApprovalStore owns pending/resolved approval requests.
        # The legacy ``_approval_store`` attribute is exposed as a
        # property+setter shim below so existing mixin code, route
        # handlers, the FanOutController, and tests can continue to
        # read/write the store unchanged while the phased refactor
        # migrates call sites to ``self.approval_store``.
        self.approval_store = InMemoryApprovalStore()
        self._paused_browsers: set[WebSocket] = set()
        self._on_browser_message: (
            Callable[[TermHub, WebSocket, str, str, dict[str, Any], bool], Awaitable[bool]] | None
        ) = None
        self._identity_provider = identity_provider
        self._delegate_roles = delegate_roles
        self._output_policy_gate = output_policy_gate
        self._behavioral_audit_gate = behavioral_audit_gate or NoOpBehavioralAuditGate()
        self._behavioral_thresholds = behavioral_thresholds or BehavioralThresholds()
        self._behavioral_audit_interval_s = max(1.0, float(behavioral_audit_interval_s))
        # MessageRouter owns the broadcast / send_worker hot path plus
        # the behavioral-heuristics ring buffer; ``HubMessagingMixin``
        # is now a thin facade forwarding to this service. The router
        # is constructed last because it holds a back-reference to the
        # hub for cross-mixin calls (``is_hijacked``,
        # ``prepare_policy_context`` etc.).
        self.router = MessageRouter(self)
        # StateStore owns the worker-state heartbeat (``touch_activity``),
        # the per-browser line buffer, the hijack-state predicates, the
        # metric / on_hijack_changed callback fan-out, and browser-role
        # resolution + policy-context plumbing. ``HubStateMixin`` is now
        # a thin facade forwarding to this service — see
        # :mod:`provide.uterm.bridge.hub.store`.
        self.state = StateStore(self)
        # PollingCoordinator owns the snapshot polling helpers
        # (``snapshot_matches``, ``wait_for_snapshot``, ``wait_for_guard``).
        # ``_PollingMixin`` is now a thin facade forwarding to this
        # service — see :mod:`provide.uterm.bridge.hub.polling_service`.
        self.polling = PollingCoordinator(self)
        # ConnectionManager owns worker/browser register/deregister,
        # rate-limit gate plumbing and the ``force_release_hijack``
        # lifecycle path; PresenceManager owns the read-only browser
        # presence queries (``can_send_input``, role resolution,
        # browser-state snapshot) and worker-bound presence control
        # frames (``request_snapshot`` / ``request_analysis``). Both are
        # back-referenced via the hub for cross-cutting calls
        # (``is_hijacked``, ``send_worker``, ``broadcast_hijack_state``,
        # ``notify_hijack_changed``, ``_resolve_role_for_browser``).
        # ``_ConnectionMixin`` is now a thin facade forwarding to these
        # services — see :mod:`provide.uterm.bridge.hub.connections`.
        self.connection_mgr = ConnectionManager(self)
        self.presence_mgr = PresenceManager(self)

        if not isinstance(self._behavioral_audit_gate, NoOpBehavioralAuditGate):
            audit_task = asyncio.create_task(self._run_behavioral_audit_loop())
            self._background_tasks.add(audit_task)
            audit_task.add_done_callback(self._background_tasks.discard)

    @property
    def _workers(self) -> dict[str, WorkerTermState]:
        """Back-compat view of the worker map owned by :attr:`registry`.

        Mixins and tests still index/iterate ``self._workers`` directly;
        this property forwards to the registry's underlying dict so the
        Phase 1 extraction is non-functional. New code should prefer
        :attr:`registry` accessors.
        """
        return self.registry._workers

    @_workers.setter
    def _workers(self, value: dict[str, WorkerTermState]) -> None:
        """Replace the worker map wholesale (back-compat for tests)."""
        self.registry._workers = value

    # -- RateLimiter back-compat shims -----------------------------------
    # These forward the legacy ``_rest_*`` attributes to :attr:`limiter`.
    # Tests still poke individual buckets directly (force ``_tokens = 0``
    # to simulate exhaustion, swap in ``MagicMock``s, pre-populate the
    # per-client dict to exercise eviction); the shims keep that surface
    # alive while ownership moves into the service.

    @property
    def _rest_acquire_bucket(self) -> TokenBucket:
        return self.limiter.rest_acquire_bucket

    @_rest_acquire_bucket.setter
    def _rest_acquire_bucket(self, bucket: TokenBucket) -> None:
        self.limiter.rest_acquire_bucket = bucket

    @property
    def _rest_send_bucket(self) -> TokenBucket:
        return self.limiter.rest_send_bucket

    @_rest_send_bucket.setter
    def _rest_send_bucket(self, bucket: TokenBucket) -> None:
        self.limiter.rest_send_bucket = bucket

    @property
    def _rest_acquire_per_client(self) -> dict[str, TokenBucket]:
        return self.limiter.rest_acquire_per_client

    @_rest_acquire_per_client.setter
    def _rest_acquire_per_client(self, value: dict[str, TokenBucket]) -> None:
        self.limiter.rest_acquire_per_client = value

    @property
    def _rest_send_per_client(self) -> dict[str, TokenBucket]:
        return self.limiter.rest_send_per_client

    @_rest_send_per_client.setter
    def _rest_send_per_client(self, value: dict[str, TokenBucket]) -> None:
        self.limiter.rest_send_per_client = value

    # -- HijackLeaseManager back-compat shim ----------------------------
    # ``_dashboard_hijack_lease_s`` is still read as a plain attribute by
    # :class:`HubMessagingMixin.try_reclaim_hijack` and by tests; forward
    # to :attr:`lease` so the service owns the canonical value.

    @property
    def _dashboard_hijack_lease_s(self) -> int:
        return self.lease.dashboard_hijack_lease_s

    @_dashboard_hijack_lease_s.setter
    def _dashboard_hijack_lease_s(self, value: int) -> None:
        self.lease.dashboard_hijack_lease_s = value

    @property
    def identity_provider(self) -> IdentityProvider | None:
        """Public accessor for the configured identity provider."""
        return self._identity_provider

    @property
    def _approval_store(self) -> InMemoryApprovalStore:
        """Back-compat alias for :attr:`approval_store`.

        Mixin code, route handlers, the FanOutController, and several
        tests still reference ``self._approval_store`` directly; this
        property forwards to the canonical attribute so the Phase 3
        extraction is non-functional. New code should prefer
        :attr:`approval_store`.
        """
        return self.approval_store

    @_approval_store.setter
    def _approval_store(self, store: InMemoryApprovalStore) -> None:
        """Replace the approval store wholesale (back-compat for tests)."""
        self.approval_store = store

    async def set_worker_hello_mode(self, worker_id: str, mode: str) -> bool:
        """Backward-compatible wrapper for worker hello mode handling."""
        # Narrow the str arg to InputMode at the wrapper boundary; reject
        # unknown values so the cast on the next line is sound.
        if mode not in ("hijack", "open"):
            raise ValueError(f"invalid input mode: {mode!r}")
        return await self.set_worker_hello(worker_id, mode)  # type: ignore[arg-type]

    # -- Approval flow (Phase 7b: ex-HubApprovalFlowMixin) ------------------
    # The orchestration logic that surrounds the approval-store CRUD
    # surface (worker resume, browser rejection notice, paused-browser
    # playback, approval_resolved control-frame fanout) lives directly on
    # TermHub now. The mixin held no patched methods; moving it on shrinks
    # the MRO by one parent without affecting the public call surface.

    async def resolve_approval(
        self,
        worker_id: str,
        request_id: str,
        decision: PolicyDecision,
        command: str,
    ) -> None:
        """Resolve a pending approval and resume the worker if approved."""
        req = self.approval_store.get(request_id)
        if req and getattr(req, "is_fanout", False):
            if decision.action == "allow":
                fo_ctrl = getattr(self, "fan_out_controller", None)
                if fo_ctrl:
                    await fo_ctrl.release_approved_command(request_id)
            elif decision.action == "deny":
                logger.info(
                    "fanout_approval_rejected request_id=%s group_id=%s",
                    request_id,
                    getattr(req, "group_id", "unknown"),
                )
                fo_ctrl = getattr(self, "fan_out_controller", None)
                if fo_ctrl:
                    await asyncio.to_thread(fo_ctrl._on_approval_expired, request_id)
            return

        st = await self._get(worker_id)

        if decision.action == "allow":
            await self.send_worker(worker_id, {"type": "input", "data": command, "ts": time.time()})
        elif decision.action == "deny":
            msg = f"\\r\\x1b[31m[REJECTED] Command '{command.strip()}' blocked by Admin.\\x1b[0m"
            if decision.reason:
                msg += f" \\x1b[33mReason: {decision.reason}\\x1b[0m"
            msg += "\\r"
            for ws in list(st.browsers.keys()):
                await ws.send_text(encode_data(msg))

        for ws in list(st.browsers.keys()):
            if ws in self._paused_browsers:
                self._paused_browsers.discard(ws)
                if decision.action == "allow" and ws in self._hold_buffers:
                    buffered_data = self._hold_buffers.pop(ws)
                    if self._on_browser_message:  # pragma: no branch — _on_browser_message is wired by app factory; no-handler case is a unit-test artifact

                        async def playback(
                            hub: TermHub,
                            browser_ws: WebSocket,
                            current_worker_id: str,
                            role: str,
                            msg: dict[str, str],
                            owned_hijack: bool,
                        ) -> None:
                            if (
                                hub._on_browser_message
                            ):  # pragma: no branch — entered only when set; recheck inside closure is defensive
                                await hub._on_browser_message(
                                    hub, browser_ws, current_worker_id, role, msg, owned_hijack
                                )

                        task = asyncio.create_task(
                            playback(
                                self,
                                ws,
                                worker_id,
                                st.browsers.get(ws, "viewer"),
                                {"type": "input", "data": buffered_data},
                                False,
                            )
                        )
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)

            await ws.send_text(
                _encode_browser_frame(
                    {
                        "type": "approval_resolved",
                        "outcome": "approved" if decision.action == "allow" else "rejected",
                        "request_id": request_id,
                    }
                )
            )
