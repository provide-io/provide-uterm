#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Thin compatibility facade over :class:`MessageRouter`.

This mixin used to own all the hub's outbound-frame plumbing. Phase 5
of refactor #16 extracted the bulk of the logic into
:class:`provide.uterm.bridge.hub.router.MessageRouter`; the mixin
remains because the existing TermHub composes its sibling mixins via
multiple inheritance and a small number of methods participate in
cooperative MRO chains (``remove_dead_browsers``,
``cleanup_browser_disconnect``, ``deregister_worker``) that must stay
on the hub's class hierarchy.

The non-cooperative methods (``broadcast``, ``send_worker``,
``broadcast_hijack_state``, ``append_event``, ``set_input_mode``,
``disconnect_worker`` and friends) now forward straight to
``self.router``. This keeps every existing public call site —
``hub.broadcast(...)``, ``hub.send_worker(...)``, etc. — working
unchanged while the implementation lives in the service class.

The legacy underscore-prefixed helpers (``_record_keystroke``,
``_get_heuristics``, ``_send_hijack_state_to``, ``_audit_all_browsers``,
``_run_behavioral_audit_loop``) and the ``_keystroke_timestamps`` dict
are exposed as compatibility shims because tests poke them directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from collections import deque

    from fastapi import APIRouter, WebSocket

    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.bridge.frames import HijackStateFrame
    from provide.uterm.bridge.hub.core import TermHub
    from provide.uterm.bridge.hub.resume import ResumeTokenStore
    from provide.uterm.bridge.hub.router import MessageRouter

logger = get_logger(__name__)


class HubMessagingMixin:
    """Compatibility facade that forwards messaging calls to :attr:`TermHub.router`.

    The router (:class:`MessageRouter`) owns the actual implementation.
    Two methods remain here in their full form because they participate
    in cooperative MRO chains across sibling mixins:
    ``cleanup_browser_disconnect`` and ``remove_dead_browsers`` both
    call ``super()`` to chain into the connection/ownership mixins.
    ``deregister_worker`` is here for the same reason (super-call into
    the connection mixin) plus its event-bus side-effect.

    The ``create_router`` method (FastAPI ``APIRouter`` factory — not to
    be confused with :class:`MessageRouter`) stays on the mixin because
    it composes route registrars over the hub itself.

    All the underscore-prefixed attributes declared on this class are
    *type-only* hints for mypy-strict; runtime initialisation lives in
    :meth:`TermHub.__init__`.
    """

    # Shared state (initialised in TermHub.__init__).
    _input_buffers: dict[Any, str]
    _hold_buffers: dict[Any, str]
    _startup_pending_browsers: set[Any]
    _event_bus: Any | None
    _resume_store: ResumeTokenStore | None
    router: MessageRouter

    # -- Router-backed delegates ----------------------------------------
    # Each of these is a thin pass-through to ``self.router``. The mixin
    # exists only so the legacy ``hub.<method>`` call sites keep working;
    # the router owns the implementation. The forwarding cost is one
    # attribute lookup + one function call, which is negligible against
    # the existing async-with-lock + ``ws.send_text`` overhead.

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
        """Programmatically disconnect the worker WS. Returns True if a worker was connected.

        Implemented inline (rather than forwarding to the router) so
        the test-only pattern of monkey-patching the hub-level
        ``broadcast_hijack_state`` / ``prune_if_idle`` / ``notify_hijack_changed``
        hooks keeps working — those are dispatched via ``self.<name>``
        so instance-level overrides are honored. The close-exception
        log also stays on this module's logger
        (``provide.uterm.bridge.hub.messaging``) because tests patch
        that import path directly.
        """
        from provide.uterm.bridge.frames import make_worker_disconnected_frame

        hub = cast("TermHub", self)
        ws: WebSocket | None = None
        async with hub._lock:
            st = hub.registry.get(worker_id)
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
            hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
            await self.broadcast_hijack_state(worker_id)
        if hub._event_bus is not None:
            hub._event_bus.close_worker(worker_id)
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

    # -- Legacy underscore-prefixed shims --------------------------------
    # Tests reach into these directly; keep them callable on the hub.

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
        """Periodically audit active connections for behavioral anomalies.

        Implemented inline (rather than forwarding to the router) so
        the test-only pattern of monkey-patching
        ``hub._audit_all_browsers`` to inject a fault keeps working —
        the loop dispatches through ``self._audit_all_browsers()`` so
        the override is honored. The exception log stays on this
        module's logger (``provide.uterm.bridge.hub.messaging``) for
        the same back-compat reason.
        """
        import asyncio

        hub = cast("TermHub", self)
        while True:
            await asyncio.sleep(hub._behavioral_audit_interval_s)
            try:
                await self._audit_all_browsers()
            except Exception:
                logger.exception("behavioral_audit_loop_error")

    @property
    def _keystroke_timestamps(self) -> dict[Any, deque[float]]:
        """Back-compat view of the router's keystroke ring buffer map."""
        return self.router.keystroke_timestamps

    # -- MRO-cooperative methods (stay on the mixin) ---------------------

    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]:
        """Clear heuristic state and call parent cleanup."""
        self.router.forget_browser(ws)
        self._input_buffers.pop(ws, None)
        self._hold_buffers.pop(ws, None)
        # cooperative MRO super-call — defined on a sibling mixin.
        return await super().cleanup_browser_disconnect(worker_id, ws, owned_hijack)  # type: ignore[misc, no-any-return]

    async def remove_dead_browsers(self, worker_id: str, dead: set[WebSocket]) -> bool:
        """Clear input buffers for dead browsers and call parent cleanup."""
        for ws in dead:
            self._input_buffers.pop(ws, None)
            self._hold_buffers.pop(ws, None)
            self._startup_pending_browsers.discard(ws)
        # cooperative MRO super-call — defined on a sibling mixin.
        return await super().remove_dead_browsers(worker_id, dead)  # type: ignore[misc, no-any-return]

    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Deregister the worker WS and notify the EventBus on disconnect."""
        # cooperative MRO super-call — defined on a sibling mixin.
        should_broadcast, was_hijacked = await super().deregister_worker(worker_id, ws)  # type: ignore[misc]
        hub = cast("TermHub", self)
        if should_broadcast and hub._event_bus is not None:
            hub._event_bus.close_worker(worker_id)
        return should_broadcast, was_hijacked

    # -- Misc public surface --------------------------------------------

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
        # The route registrars are typed for ``TermHub`` (the composing
        # class). At runtime ``self`` IS a TermHub when this mixin method
        # runs; the cast tells mypy that's the contract.
        hub = cast("TermHub", self)
        register_rest_routes(hub, router)
        register_ws_routes(hub, router)
        for registrar in extra_route_registrars or []:
            registrar(self, router)
        return router
