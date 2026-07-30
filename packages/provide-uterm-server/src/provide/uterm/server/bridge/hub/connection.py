#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ConnectionManager: worker/browser connection lifecycle for TermHub.

Owns the *connection-churn* surface that previously lived inline on
:class:`provide.uterm.server.bridge.hub.connections._ConnectionMixin`. Splitting
it out of the mixin pile gives the worker- and browser-lifecycle paths a
service-class home with explicit dependencies (the shared hub lock, the
worker registry, the rate limiter, the resume-token store) and matches
the Phase 5 :class:`MessageRouter` pattern of a single back-reference to
the composing :class:`TermHub`. The companion
:class:`provide.uterm.server.bridge.hub.presence.PresenceManager` owns the
read-only presence query surface (role resolution, ``can_send_input``,
``register_browser_state_snapshot``, ``request_snapshot`` /
``request_analysis``); methods needing both lifecycle mutations *and* a
presence-shaped notification stay here and chain through the hub facade.

Scope:

* REST rate-limit gate plumbing (``hub.limiter``) + rejection log.
* Worker WS lifecycle: ``register_worker`` / ``is_active_worker`` /
  ``set_worker_tunnel_flag`` / ``set_worker_hello`` /
  ``update_last_snapshot`` / ``deregister_worker``.
* Browser WS lifecycle: ``register_browser`` /
  ``activate_browser_broadcasts`` / ``cleanup_browser_disconnect`` plus the
  private helpers ``_scan_events_for_resume`` / ``_update_lock_state``.
* Hijack-clearing lifecycle: ``disconnect_worker`` (+ its
  ``_event_bus_close`` helper) and ``force_release_hijack`` — clears any
  active hijack and emits the follow-up ``resume`` control frame plus a
  hijack-state broadcast via the hub facade. The *bodies* live in
  :mod:`provide.uterm.server.bridge.hub.connection_hijack` (module-level
  functions taking the manager as their first arg); the methods here are
  thin wrappers, moving the line bulk + mutants there while keeping the
  public method surface on :class:`ConnectionManager`.

This module is mutation-enforced at killed==100 (the hijack-clearing
bodies' mutants now live in ``connection_hijack.py``, also enforced): the
``tests/bridge/hub/test_connection_kill_*.py`` suites pin every return
value, state mutation, quota-counter update, and observability call (the
documented equivalents are in ``mutation_equivalents.toml``).

Lock semantics are preserved verbatim: the manager uses the *hub's*
``asyncio.Lock`` (via the back reference) so concurrent connection churn
keeps serialising against the same object the rest of the hub uses.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger, get_tracer
from provide.uterm.server.bridge.hub import connection_hijack
from provide.uterm.server.bridge.hub.ext import (
    EVENT_RATE_LIMIT_TRIGGERED,
    EVENT_SESSION_DISCONNECTED,
    EVENT_SESSION_REGISTERED,
)
from provide.uterm.server.bridge.models import WorkerTermState

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.server.bridge.hub.core import TermHub

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
                # --- Global worker-registration cap (OOM bound) ---
                # Only reject a brand-NEW worker_id once the map is full. A
                # reconnecting, already-registered worker_id (CF DO rotation,
                # manager restart, network blip) MUST always be allowed — the
                # ``worker_id not in _workers`` guard preserves reconnects (see
                # the lease-preservation note below).
                if worker_id not in hub.registry._workers and len(hub.registry._workers) >= hub.max_workers:
                    from fastapi import WebSocketException

                    raise WebSocketException(
                        code=1008,
                        reason="worker capacity exceeded",
                    )
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
            # The False arc here exits the async-with directly; coverage.py 7.14
            # mis-attributes that __aexit__ arc on Python 3.11 (3.12+ tracks it
            # fine), so it falsely reports the branch as partial. The branch IS
            # exercised — see test_set_worker_tunnel_flag_noop_for_unknown_worker.
            if st is not None:  # pragma: no branch
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
            # A hello may raise the mode, never lower it. Two reasons to refuse,
            # and both are needed: a lease is actually held, or somebody has
            # explicitly decided the mode through an authenticated route. The
            # second is the window the lease check alone left open — an operator
            # sets `hijack` and then acquires, and a hello landing between those
            # two steps used to revert the mode, so the acquire was refused for
            # being in open mode. The operator's only clue was a failure that
            # looked like their own mistake.
            #
            # The decision flag is what makes this expressible at all:
            # `input_mode` defaults to `hijack`, so refusing every lowering would
            # refuse every worker that legitimately announces `open`.
            would_lower = mode == "open" and st.input_mode == "hijack"
            if would_lower and (st.input_mode_set_by_operator or hub.is_hijacked(st)):
                logger.warning(
                    "worker_hello_mode_blocked worker_id=%s — a hello may not lower a decided mode to open",
                    worker_id,
                )
                return False
            st.input_mode = mode
            # Same Python-3.11 coverage.py async-with __aexit__ arc quirk as in
            # set_worker_tunnel_flag: the False arc (no protocol_version) falls
            # through to the post-with `return True`; 3.11 mis-attributes it. The
            # branch IS exercised — see test_worker_hello_without_protocol_version_*.
            if protocol_version is not None:  # pragma: no branch
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

    @staticmethod
    def _browser_principal_subject_id(ws: Any) -> str | None:
        """Return the principal subject_id for quota tracking, or ``None`` if exempt.

        Returns ``None`` (exempt — no count) for:
        * WebSockets with no ``state.uterm_principal`` attribute.
        * Principals with ``subject_id == "anonymous"``.
        Only concrete, non-anonymous human principals are counted.
        """
        principal = getattr(getattr(ws, "state", None), "uterm_principal", None)
        if principal is None:
            return None
        subject_id = getattr(principal, "subject_id", None)
        if not isinstance(subject_id, str) or subject_id == "anonymous" or not subject_id:
            return None
        return subject_id

    async def register_browser(
        self, worker_id: str, ws: WebSocket, role: str, *, defer_broadcast: bool = False
    ) -> dict[str, Any]:
        """Register *ws* as a browser for *worker_id* and return initial state.

        Returns a dict with keys: ``is_hijacked``, ``hijacked_by_me``,
        ``worker_online``, ``input_mode``, ``initial_snapshot``,
        and optionally ``resume_token``.

        Raises ``WebSocketException(1008)`` when the authenticated principal has
        reached ``hub.max_connections_per_principal`` concurrent browser
        connections.  Anonymous principals and connections without a principal
        are exempt from the quota (the auth layer handles them separately).
        """
        hub = self._hub
        with tracer.start_as_current_span("uterm.browser.register", attributes={"worker_id": worker_id, "role": role}):
            resume_token: str | None = None
            async with hub._lock:
                # --- Per-principal browser connection quota (BROWSER-only) ---
                # The quota gate runs BEFORE minting the resume token so a
                # rejected connection never orphans a token in the resume store
                # (which would otherwise linger until TTL/retention).
                subject_id = self._browser_principal_subject_id(ws)
                if subject_id is not None:
                    current = hub._principal_browser_counts.get(subject_id, 0)
                    if current >= hub.max_connections_per_principal:
                        from fastapi import WebSocketException

                        raise WebSocketException(
                            code=1008,
                            reason="too many connections",
                        )
                    hub._principal_browser_counts[subject_id] = current + 1
                    hub._ws_principal[ws] = subject_id
                # -----------------------------------------------------------
                # Everything past the increment is wrapped so a raise here
                # (e.g. ``resume_store.create()`` throwing an sqlite IO error or
                # CancelledError) does NOT leak the per-principal quota slot.
                # ``register_browser`` is awaited OUTSIDE the WS route's
                # try/finally, so a raise here would otherwise skip the paired
                # decrement in ``cleanup_browser_disconnect`` and — with no
                # reaper for ``_principal_browser_counts`` — eventually lock the
                # principal out at 1008. On ANY exception we roll the increment
                # back (mirroring ``_update_lock_state``'s decrement) and re-raise.
                try:
                    # Mint the resume token only once the quota gate has passed.
                    if hub._resume_store is not None:
                        resume_token = await hub._resume_store.create(worker_id, role, hub._resume_ttl_s)
                        hub._ws_to_resume_token[ws] = resume_token
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
                except BaseException:
                    self._rollback_browser_quota(ws)
                    raise
            # Redact the connect-time snapshot OUTSIDE the lock (the policy
            # context build re-acquires hub._lock). This is the same role-scoped
            # output-redaction the broadcast path applies; without it the initial
            # snapshot would ship raw screen/raw_tail/prompt_detected to the
            # browser, bypassing the policy (M5). redact_snapshot_for_recipient
            # returns a COPY, so the stored st.last_snapshot is not mutated.
            _snapshot = initial_state["initial_snapshot"]
            if _snapshot is not None and hub._output_policy_gate is not None:
                initial_state["initial_snapshot"] = await hub.redact_snapshot_for_recipient(
                    worker_id,
                    # ty calls this redundant; mypy (warn_redundant_casts=true) requires it.
                    cast("dict[str, Any]", _snapshot),  # ty: ignore[redundant-cast]
                    ws,
                )
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

    def _rollback_browser_quota(self, ws: Any) -> None:
        """Undo the per-principal quota increment for *ws* (M6 atomicity).

        Called from :meth:`register_browser` when a setup line raises after the
        increment. Pops ``_ws_principal[ws]`` and decrements
        ``_principal_browser_counts`` for that subject, popping it to zero —
        the exact inverse of the increment and a mirror of the decrement in
        :meth:`_update_lock_state`. Net effect: a failed register leaves the
        count exactly as it was before the attempt. Must run under ``hub._lock``
        (the caller already holds it).
        """
        hub = self._hub
        # Also drop any resume-token map entry minted before the raise so it
        # doesn't orphan in memory (the route's disconnect cleanup, which
        # normally pops it, never runs when register_browser itself raises).
        hub._ws_to_resume_token.pop(ws, None)
        subject_id = hub._ws_principal.pop(ws, None)
        if subject_id is not None:
            remaining = hub._principal_browser_counts.get(subject_id, 0) - 1
            if remaining <= 0:
                hub._principal_browser_counts.pop(subject_id, None)
            else:
                hub._principal_browser_counts[subject_id] = remaining

    @staticmethod
    def _scan_events_for_resume(st: Any) -> bool:
        """Scan event history to determine if a resume is still needed on browser disconnect.

        Returns ``True`` if a resume control frame should be sent (no prior expiry
        or release event was found in the history that would have already sent one).
        Scans backwards; stops at the first hijack lifecycle event encountered.
        """
        for evt in reversed(st.events):
            t = evt.get("type")
            if not isinstance(t, str):
                continue
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
        # Decrement per-principal browser count if this ws was counted.
        subject_id = hub._ws_principal.pop(ws, None)
        if subject_id is not None:
            remaining = hub._principal_browser_counts.get(subject_id, 0) - 1
            if remaining <= 0:
                hub._principal_browser_counts.pop(subject_id, None)
            else:
                hub._principal_browser_counts[subject_id] = remaining
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
        """Programmatically disconnect the worker WS (body in :mod:`connection_hijack`)."""
        return await connection_hijack.disconnect_worker(self, worker_id)  # ty:ignore[invalid-argument-type]

    def _event_bus_close(self, worker_id: str) -> None:
        """Indirect EventBus close, read on the hub each call (body in :mod:`connection_hijack`)."""
        connection_hijack._event_bus_close(self, worker_id)  # ty:ignore[invalid-argument-type]

    async def force_release_hijack(self, worker_id: str) -> bool:
        """Forcibly clear any active hijack and send a resume frame (body in :mod:`connection_hijack`)."""
        return await connection_hijack.force_release_hijack(self, worker_id)  # ty:ignore[invalid-argument-type]


__all__ = ["ConnectionManager"]
