#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ConnectionManager: worker/browser connection lifecycle for TermHub.

Owns the *connection-churn* surface that previously lived inline on
:class:`provide.uterm.bridge.hub.connections._ConnectionMixin`. Splitting
it out of the mixin pile gives the worker- and browser-lifecycle paths a
service-class home with explicit dependencies (the shared hub lock, the
worker registry, the rate limiter, the resume-token store) and matches
the Phase 5 :class:`MessageRouter` pattern of a single back-reference to
the composing :class:`TermHub`. The companion
:class:`provide.uterm.bridge.hub.presence.PresenceManager` owns the
read-only presence query surface (role resolution, ``can_send_input``,
``register_browser_state_snapshot``, ``request_snapshot`` /
``request_analysis``); methods that need both lifecycle mutations *and*
a presence-shaped notification (only ``force_release_hijack`` today)
stay here and chain through the hub facade for the broadcast side.

Scope:

* REST rate-limit gate plumbing (delegating to ``hub.limiter``) plus the
  observability log on rejection.
* Worker WS lifecycle: ``register_worker`` / ``is_active_worker`` /
  ``set_worker_tunnel_flag`` / ``set_worker_hello`` /
  ``update_last_snapshot`` / ``deregister_worker``.
* Browser WS lifecycle: ``register_browser`` /
  ``activate_browser_broadcasts`` / ``cleanup_browser_disconnect`` plus
  the two private helpers ``_scan_events_for_resume`` and
  ``_update_lock_state`` that participate in the disconnect handler.
* ``force_release_hijack`` — clears any active hijack and emits the
  follow-up ``resume`` control frame plus a hijack-state broadcast via
  the hub facade.

Hot-path note: ``register_browser`` / ``cleanup_browser_disconnect`` are
called per-session (browser attach / detach) — not in the per-frame hot
path. The mixin shim cost is therefore irrelevant at this layer. Lock
semantics are intentionally preserved verbatim from the mixin
implementation: the manager uses the *hub's* ``asyncio.Lock`` (accessed
via the back reference) so concurrent connection churn keeps
serialising against the same object that the rest of the hub uses.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger, get_tracer
from provide.uterm.bridge.hub.ext import (
    EVENT_RATE_LIMIT_TRIGGERED,
    EVENT_SESSION_DISCONNECTED,
    EVENT_SESSION_REGISTERED,
)
from provide.uterm.bridge.models import WorkerTermState

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.bridge.hub.core import TermHub

logger = get_logger(__name__)
tracer = get_tracer(__name__)


class ConnectionManager:
    """Worker/browser connection lifecycle for TermHub.

    Composed into :class:`TermHub` as ``self.connection_mgr``. Holds a
    back reference to the hub for the cross-cutting queries that
    legitimately need it (``is_hijacked``, ``is_dashboard_hijack_active``,
    ``has_valid_rest_lease``, ``send_worker``, ``broadcast_hijack_state``,
    ``notify_hijack_changed``).

    Args:
        hub: The composing :class:`TermHub`. The manager uses
            ``hub._lock``, ``hub.registry``, ``hub.limiter``,
            ``hub._event_deque_maxlen``, ``hub._worker_token``,
            ``hub._resume_store`` / ``hub._resume_ttl_s`` /
            ``hub._ws_to_resume_token``, ``hub._startup_pending_browsers``,
            and ``hub._background_tasks``.
    """

    __slots__ = ("_hub",)

    def __init__(self, hub: TermHub) -> None:
        self._hub = hub

    # -- Rate limiting --------------------------------------------------

    def allow_rest_acquire_for(self, client_id: str) -> bool:
        """Per-client REST acquire rate limit (also checks the global bucket).

        Delegates the bucket composition and LRU-lite eviction to
        ``hub.limiter``; only the structured-event log on a rejection
        lives here so the hub keeps a single observability surface.
        """
        allowed = self._hub.limiter.allow_rest_acquire(client_id)
        if not allowed:
            logger.warning(EVENT_RATE_LIMIT_TRIGGERED, client_id=client_id, limit_type="rest_acquire")
        return allowed

    def allow_rest_send_for(self, client_id: str) -> bool:
        """Per-client REST send/step rate limit (also checks the global bucket).

        Delegates the bucket composition and LRU-lite eviction to
        ``hub.limiter`` (same strategy as :meth:`allow_rest_acquire_for`).
        """
        allowed = self._hub.limiter.allow_rest_send(client_id)
        if not allowed:
            logger.warning(EVENT_RATE_LIMIT_TRIGGERED, client_id=client_id, limit_type="rest_send")
        return allowed

    # -- Token access ---------------------------------------------------

    def worker_token(self) -> str | None:
        """Return the configured worker bearer token (read-only)."""
        return self._hub._worker_token

    # -- Worker connection lifecycle ------------------------------------

    async def register_worker(self, worker_id: str, ws: WebSocket) -> bool:
        """Register *ws* as the active worker for *worker_id*.

        Clears any stale hijack state from a previous crashed worker session.
        Returns ``True`` if a previous hijack was active (caller should broadcast
        a cleared-hijack notification), ``False`` otherwise.
        """
        hub = self._hub
        with tracer.start_as_current_span("uterm.worker.register", attributes={"worker_id": worker_id}):
            async with hub._lock:
                st = hub.registry._workers.setdefault(worker_id, WorkerTermState())
                st.events = deque(st.events, maxlen=hub._event_deque_maxlen)
                # Only clear hijack state when the EXISTING lease is
                # actually expired. Worker WS reconnects are routine for
                # passive supervised bots (Cloudflare DO rotation, manager
                # restart, network blip) and the framework's hijack lease
                # should survive a transient reconnect — clearing it
                # unconditionally meant a single CFDO "reconnecting..."
                # blip mid-run silently invalidated the holder's
                # hijack_id, every subsequent /send 404'd, and the
                # whole compare run cratered. Time-bounded expiry
                # (lease_expires_at) is already the security guarantee;
                # WS register is not a security event.
                _now_mono = time.monotonic()
                _expired = st.hijack_session is not None and st.hijack_session.lease_expires_at <= _now_mono
                prev_was_hijacked = _expired or (st.hijack_session is None and st.hijack_owner is not None)
                if _expired:
                    st.hijack_session = None
                if prev_was_hijacked:
                    st.hijack_owner = None
                    st.hijack_owner_expires_at = None
                st.worker_ws = ws
            logger.info(EVENT_SESSION_REGISTERED, worker_id=worker_id, session_type="worker")
            return prev_was_hijacked

    async def is_active_worker(self, worker_id: str, ws: WebSocket) -> bool:
        """Return True if *ws* is still the registered worker for *worker_id*."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            return st is not None and st.worker_ws is ws

    async def set_worker_tunnel_flag(self, worker_id: str, value: bool) -> None:
        """Mark whether ``worker_id``'s worker WS uses the tunnel wire format.

        See :class:`WorkerTermState.is_tunnel_worker` for the semantics
        (raw bytes for input, no DLE-framed JSON envelope). Called by
        :mod:`provide.uterm.tunnel.fastapi_routes` right after
        ``register_worker`` so :meth:`send_worker` can route outbound
        messages with the correct codec.
        """
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is not None:
                st.is_tunnel_worker = value

    async def set_worker_hello(self, worker_id: str, mode: InputMode, protocol_version: int | None = None) -> bool:
        """Process a ``worker_hello`` message: set input_mode and persist protocol version.

        Returns ``True`` if the mode was applied, ``False`` if the worker is no
        longer registered or if switching to ``"open"`` while a hijack lease is
        active (mode change is blocked in that case). When ``protocol_version`` is
        provided, it is recorded on the :class:`WorkerTermState` so downstream
        feature gates can query it via ``worker.protocol_version``.
        """
        hub = self._hub
        if protocol_version is not None:
            logger.info("worker_hello_protocol worker_id=%s version=%d", worker_id, protocol_version)
            if protocol_version < 1:
                logger.warning("worker_hello_legacy_protocol worker_id=%s version=%d", worker_id, protocol_version)

        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return False
            if mode == "open" and hub.is_hijacked(st):
                logger.warning(
                    "worker_hello_mode_blocked worker_id=%s — cannot switch to open while hijack active",
                    worker_id,
                )
                return False
            st.input_mode = mode
            if protocol_version is not None:
                st.protocol_version = protocol_version
        return True

    async def update_last_snapshot(self, worker_id: str, snapshot: dict[str, Any]) -> None:
        """Store *snapshot* as the most recent snapshot for *worker_id*."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is not None:  # pragma: no branch
                st.last_snapshot = snapshot

    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Clear *ws* as the active worker if it is still current.

        Returns ``(should_broadcast_disconnect, was_hijacked)``.
        ``should_broadcast_disconnect`` is ``True`` only when *ws* was the
        current worker (i.e. a replacement has not already taken over).
        """
        hub = self._hub
        with tracer.start_as_current_span("uterm.worker.deregister", attributes={"worker_id": worker_id}):
            async with hub._lock:
                st = hub.registry.get(worker_id)
                if st is None or st.worker_ws is not ws:
                    return False, False
                was_hijacked = st.hijack_session is not None or st.hijack_owner is not None
                st.worker_ws = None
                st.hijack_session = None
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
            return True, was_hijacked

    # -- Browser connection lifecycle ------------------------------------

    async def register_browser(
        self, worker_id: str, ws: WebSocket, role: str, *, defer_broadcast: bool = False
    ) -> dict[str, Any]:
        """Register *ws* as a browser for *worker_id* and return initial state.

        Returns a dict with keys: ``is_hijacked``, ``hijacked_by_me``,
        ``worker_online``, ``input_mode``, ``initial_snapshot``,
        and optionally ``resume_token``.
        """
        hub = self._hub
        with tracer.start_as_current_span("uterm.browser.register", attributes={"worker_id": worker_id, "role": role}):
            resume_token: str | None = None
            if hub._resume_store is not None:
                resume_token = await hub._resume_store.create(worker_id, role, hub._resume_ttl_s)
                hub._ws_to_resume_token[ws] = resume_token
            async with hub._lock:
                st = hub.registry._workers.setdefault(worker_id, WorkerTermState())
                st.browsers[ws] = role
                if defer_broadcast:
                    hub._startup_pending_browsers.add(ws)
                initial_state = {
                    "is_hijacked": hub.is_hijacked(st),
                    "hijacked_by_me": hub.is_dashboard_hijack_active(st) and st.hijack_owner is ws,
                    "worker_online": st.worker_ws is not None,
                    "input_mode": st.input_mode,
                    "initial_snapshot": st.last_snapshot,
                    "resume_token": resume_token,
                }
            logger.info(EVENT_SESSION_REGISTERED, worker_id=worker_id, session_type="browser", role=role)
            return initial_state

    async def activate_browser_broadcasts(self, worker_id: str, ws: WebSocket) -> None:
        """Allow broadcasts to a browser after its startup frames have been sent."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if (
                st is not None and ws in st.browsers
            ):  # pragma: no branch — race window during browser disconnect; defensive
                hub._startup_pending_browsers.discard(ws)

    @staticmethod
    def _scan_events_for_resume(st: Any) -> bool:
        """Scan event history to determine if a resume is still needed on browser disconnect.

        Returns ``True`` if a resume control frame should be sent (no prior expiry
        or release event was found in the history that would have already sent one).
        Scans backwards; stops at the first hijack lifecycle event encountered.
        """
        for evt in reversed(st.events):
            t = str(evt.get("type", ""))
            if t in {"hijack_owner_expired", "hijack_lease_expired"}:
                return False
            if t in {"hijack_acquired", "hijack_released"}:
                break
        return True

    def _update_lock_state(self, st: Any, ws: Any, owned_hijack: bool) -> tuple[bool, bool, bool]:
        """Apply disconnect state mutations to *st* and return outcome flags.

        Returns ``(was_owner, rest_still_active, resume_without_owner)``.
        Must be called while holding ``hub._lock``.
        """
        hub = self._hub
        was_owner = hub.is_dashboard_hijack_active(st) and st.hijack_owner is ws
        rest_still_active = False
        resume_without_owner = False
        st.browsers.pop(ws, None)
        if was_owner:
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
            rest_still_active = hub.has_valid_rest_lease(st)
        elif owned_hijack and st.worker_ws is not None and not hub.is_hijacked(st):  # pragma: no branch
            # Scan backwards for the most recent hijack-related event to determine
            # whether cleanup already sent a resume (lease/owner expired) or whether
            # a resume is still needed.  Checking only the last event is fragile
            # because a subsequent snapshot event can overwrite the expiry marker.
            resume_without_owner = self._scan_events_for_resume(st)
        return was_owner, rest_still_active, resume_without_owner

    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]:
        """Handle a browser WS disconnect atomically.

        Returns a dict with keys: ``was_owner``, ``resume_without_owner``,
        ``rest_still_active``.
        """
        hub = self._hub
        with tracer.start_as_current_span("uterm.browser.deregister", attributes={"worker_id": worker_id}):
            browser_count = -1
            async with hub._lock:
                st = hub.registry.get(worker_id)
                was_owner = False
                rest_still_active = False
                resume_without_owner = False
                if st is not None:  # pragma: no branch
                    was_owner, rest_still_active, resume_without_owner = self._update_lock_state(st, ws, owned_hijack)
                    browser_count = len(st.browsers)
            # Mark resume token with hijack ownership (if any) so a reconnecting
            # browser can reclaim the lease.  Do NOT revoke — the token must survive
            # until the browser reconnects or TTL expires.
            if hub._resume_store is not None:
                token = hub._ws_to_resume_token.pop(ws, None)
                if token and (was_owner or owned_hijack):
                    await hub._resume_store.mark_hijack_owner(token, True)
            hub._startup_pending_browsers.discard(ws)

            # Fire empty-browser callback outside the lock when the last browser left.
            on_empty = getattr(hub, "on_worker_empty", None)
            if browser_count == 0 and on_empty is not None:
                task = asyncio.create_task(on_empty(worker_id))
                hub._background_tasks.add(task)
                task.add_done_callback(hub._background_tasks.discard)
            logger.info(EVENT_SESSION_DISCONNECTED, worker_id=worker_id, session_type="browser")
            return {
                "was_owner": was_owner,
                "rest_still_active": rest_still_active,
                "resume_without_owner": resume_without_owner,
            }

    # -- Hijack-clearing lifecycle --------------------------------------

    async def disconnect_worker(self, worker_id: str) -> bool:
        """Programmatically disconnect the worker WS. Returns ``True`` if a worker was connected.

        Inter-step hooks (``broadcast``, ``notify_hijack_changed``,
        ``broadcast_hijack_state``, ``prune_if_idle``) are dispatched via
        ``self._hub.<method>`` so existing mutation-killing tests which
        patch the hub-level names continue to intercept them after the
        orchestration moved into the service. The hub-level methods are
        pure one-line delegators back to their owning services, so the
        cycle terminates on the second hop.
        """
        from provide.uterm.bridge.frames import make_worker_disconnected_frame

        hub = self._hub
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
        await hub.broadcast(worker_id, cast("dict[str, Any]", make_worker_disconnected_frame(worker_id)))
        if was_hijacked:
            hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
            await hub.broadcast_hijack_state(worker_id)
        if hub._event_bus is not None:
            self._event_bus_close(worker_id)
        await hub.prune_if_idle(worker_id)
        return True

    def _event_bus_close(self, worker_id: str) -> None:
        """Indirect close so the EventBus reference is read on the hub each call.

        Tests substitute ``hub._event_bus`` after construction; reading
        through the back-reference keeps that pattern working without
        plumbing a setter through this service.
        """
        bus = self._hub._event_bus
        if bus is not None:  # pragma: no branch
            bus.close_worker(worker_id)

    async def force_release_hijack(self, worker_id: str) -> bool:
        """Forcibly clear any active hijack for *worker_id* and send a resume control frame.

        Returns ``True`` if a hijack was active and was cleared, ``False`` otherwise.
        Typically called before switching input mode to ``"open"`` or on session teardown.
        """
        hub = self._hub
        owner = "server-forced"
        had_hijack = False
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return False
            if st.hijack_session is not None:
                owner = st.hijack_session.owner
                st.hijack_session = None
                had_hijack = True
            if hub.is_dashboard_hijack_active(st):  # pragma: no branch
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
                had_hijack = True
        if not had_hijack:
            return False
        await hub.send_worker(
            worker_id,
            {"type": "control", "action": "resume", "owner": owner, "lease_s": 0, "ts": time.time()},
        )
        hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
        await hub.broadcast_hijack_state(worker_id)
        return True


__all__ = ["ConnectionManager"]
