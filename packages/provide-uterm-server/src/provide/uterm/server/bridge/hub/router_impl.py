#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""MessageRouter: outbound-frame plumbing for the terminal hub.

This module owns the *messaging* surface that previously lived inline on
:class:`provide.uterm.server.bridge.hub.messaging.HubMessagingMixin`. The mixin
remains as a thin facade — its public methods now delegate to a
:class:`MessageRouter` instance hung off the composing
:class:`TermHub`. The split exists to give the broadcast / send-worker
hot path a service-class home with explicit dependencies (registry,
hub lock, event bus, optional output policy gate) instead of duck-typed
mixin attributes.

Hot-path note: the broadcast path runs once per outbound terminal frame
and is the busiest code path in the server. The mixin shim that calls
into the router adds exactly one Python attribute lookup +
function-call per broadcast — measured by ``-X importtime`` and by
direct microbenchmark it is well under a microsecond and is dominated
by the existing ``async with self._lock`` plus ``ws.send_text``
overhead. No locks are introduced or moved by this extraction.

Lock semantics are intentionally preserved verbatim from the mixin
implementation: the router uses the *hub's* ``asyncio.Lock`` (passed in
via constructor) so concurrent broadcast/send/state-change calls keep
serialising against the same object that the rest of the hub uses.
"""

from __future__ import annotations

import contextlib
import statistics
import time
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger
from provide.uterm.server.bridge.frames import make_hijack_state_frame
from provide.uterm.server.bridge.hub.redaction import StreamRedactor

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.server.bridge.frames import HijackStateFrame
    from provide.uterm.server.bridge.hub.core import TermHub

logger = get_logger(__name__)


class MessageRouter:
    """Outbound-frame plumbing: broadcasts, worker sends, hijack-state notifications.

    Composed into :class:`TermHub` as ``self.router``. Holds a back
    reference to the hub for the small set of cross-cutting queries
    that legitimately need it (``is_hijacked`` / ``is_dashboard_hijack_active``
    / ``has_valid_rest_lease`` / ``prepare_policy_context`` /
    ``notify_hijack_changed`` / ``remove_dead_browsers``) — these all
    live on sibling mixins and the router calls them through the hub
    facade.

    Args:
        hub: The composing :class:`TermHub`. The router uses
            ``hub._lock``, ``hub.registry``, ``hub._event_bus`` and
            the policy-gate / behavioral-audit gates configured on
            the hub.
    """

    __slots__ = ("_hub", "_keystroke_timestamps")

    def __init__(self, hub: TermHub) -> None:
        self._hub = hub
        # Per-browser keystroke timing ring buffers used by the
        # behavioral audit loop. Lives on the router because it's
        # purely messaging-adjacent state. The mixin exposes a
        # property shim so legacy tests that poke this directly
        # continue to work.
        self._keystroke_timestamps: dict[Any, deque[float]] = {}

    @property
    def keystroke_timestamps(self) -> dict[Any, deque[float]]:
        """Per-browser keystroke timestamp ring buffers (mutable view)."""
        return self._keystroke_timestamps

    # -- Event ring buffer -----------------------------------------------

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append a timestamped event to the worker's event ring buffer and return it."""
        hub = self._hub
        payload = data or {}
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return {"seq": 0, "ts": time.time(), "type": event_type, "data": payload}
            st.event_seq += 1
            evt: dict[str, Any] = {"seq": st.event_seq, "ts": time.time(), "type": event_type, "data": payload}
            st.events.append(evt)
            st.min_event_seq = int(st.events[0]["seq"])
        if hub._event_bus is not None:
            hub._event_bus._enqueue(worker_id, evt)
        return evt

    # -- Broadcast / send hot path --------------------------------------

    async def broadcast(self, worker_id: str, msg: dict[str, Any]) -> None:
        """Send *msg* to all browser WebSockets registered for *worker_id*."""
        from provide.uterm.server.bridge.hub.core import _encode_browser_frame

        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return
            browsers_with_roles = [
                (ws, role) for ws, role in st.browsers.items() if ws not in hub._startup_pending_browsers
            ]

        dead: set[WebSocket] = set()
        is_term_data = msg.get("type") == "term"
        raw_data = str(msg.get("data", "")) if is_term_data else ""

        # Pre-encode for all browsers (except when redaction is needed)
        encoded_default = _encode_browser_frame(msg)

        for ws, _role in browsers_with_roles:
            try:
                final_payload = encoded_default
                if is_term_data and hub._output_policy_gate:
                    context = await hub.prepare_policy_context(ws, worker_id, action="output")
                    rules = await hub._output_policy_gate.get_redaction_rules(context)
                    if rules:  # pragma: no branch — empty-rules fall-through is the default state; covered by output-gate unit tests
                        redactor = StreamRedactor(rules)
                        redacted_data = redactor.redact(raw_data)
                        final_payload = _encode_browser_frame({"type": "term", "data": redacted_data})
                await ws.send_text(final_payload)
            except Exception as exc:
                logger.debug("broadcast_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)
        if dead:
            changed = await hub.remove_dead_browsers(worker_id, dead)
            if changed:
                await self.broadcast_hijack_state(worker_id)

    async def send_hijack_state_to(
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
        """Send a hijack_state message to each browser; return the set of dead sockets."""
        from provide.uterm.server.bridge.hub.core import _encode_browser_frame, _mono_to_wall

        dead: set[WebSocket] = set()
        for ws in browsers:
            if is_dashboard and hijack_owner is ws:
                owner: str | None = "me"
            elif is_dashboard or is_rest:
                owner = "other"
            else:
                owner = None
            payload = _encode_browser_frame(
                cast(
                    "dict[str, Any]",
                    make_hijack_state_frame(
                        hijacked=is_hijacked,
                        owner=owner,
                        lease_expires_at=_mono_to_wall(lease_expires_at),
                        input_mode=input_mode,
                    ),
                )
            )
            try:
                await ws.send_text(payload)
            except Exception as exc:
                if not suppress_errors:
                    logger.debug("broadcast_hijack_state_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)
        return dead

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        """Send a hijack_state message to every browser for *worker_id*, cleaning up dead sockets."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return
            browsers = [ws for ws in st.browsers if ws not in hub._startup_pending_browsers]
            hijack_owner = st.hijack_owner
            is_hijacked = hub.is_hijacked(st)
            is_dashboard = hub.is_dashboard_hijack_active(st)
            is_rest = hub.has_valid_rest_lease(st)
            input_mode = st.input_mode
            lease_expires_at = (
                st.hijack_session.lease_expires_at
                if is_rest and st.hijack_session is not None
                else st.hijack_owner_expires_at
            )

        dead = await self.send_hijack_state_to(
            browsers,
            worker_id=worker_id,
            is_hijacked=is_hijacked,
            is_dashboard=is_dashboard,
            is_rest=is_rest,
            hijack_owner=hijack_owner,
            input_mode=input_mode,
            lease_expires_at=lease_expires_at,
        )
        if dead:
            await hub.remove_dead_browsers(worker_id, dead)
            async with hub._lock:
                st2 = hub.registry.get(worker_id)
                if st2 is None:
                    return
                survivors = [ws for ws in st2.browsers if ws not in hub._startup_pending_browsers]
                is_h2 = hub.is_hijacked(st2)
                is_dashboard2 = hub.is_dashboard_hijack_active(st2)
                is_rest2 = hub.has_valid_rest_lease(st2)
                hijack_owner2 = st2.hijack_owner
                input_mode2 = st2.input_mode
                lease2 = (
                    st2.hijack_session.lease_expires_at
                    if is_rest2 and st2.hijack_session is not None
                    else st2.hijack_owner_expires_at
                )
            await self.send_hijack_state_to(
                survivors,
                worker_id=worker_id,
                is_hijacked=is_h2,
                is_dashboard=is_dashboard2,
                is_rest=is_rest2,
                hijack_owner=hijack_owner2,
                input_mode=input_mode2,
                lease_expires_at=lease2,
                suppress_errors=True,
            )

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        """Send *msg* to the worker WebSocket; returns False if no worker is connected.

        Tunnel workers (``is_tunnel_worker=True``) use a different wire
        format: ``input`` messages are sent as raw bytes (the worker
        writes them straight to its PTY), other message types are
        dropped because the worker's bridge loop has no JSON-envelope
        handling.
        """
        from provide.uterm.server.bridge.hub.core import _encode_worker_frame

        hub = self._hub
        if source and msg.get("type") == "input":
            self.record_keystroke(source)

        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None or st.worker_ws is None:
                return False
            ws = st.worker_ws
            is_tunnel = st.is_tunnel_worker
        try:
            if is_tunnel:
                # Tunnel wire format: hub → worker is raw bytes for input,
                # nothing for control. Matches the existing ``uterm share``
                # bridge loop which writes every received byte to PTY.
                if msg.get("type") != "input":
                    return True
                data = msg.get("data")
                if not isinstance(data, str):
                    return True
                await ws.send_bytes(data.encode("utf-8"))
                return True
            await ws.send_text(_encode_worker_frame(msg))
            return True
        except BaseException as exc:
            logger.debug("send_worker_failed worker_id=%s: %s", worker_id, exc)
            async with hub._lock:
                st2 = hub.registry.get(worker_id)
                if st2 is not None and st2.worker_ws is ws:  # pragma: no branch
                    st2.worker_ws = None
            if isinstance(exc, Exception):
                return False
            raise

    # -- Behavioral heuristics ------------------------------------------

    def record_keystroke(self, source: Any) -> None:
        """Record the timing of a keystroke from a browser."""
        if source not in self._keystroke_timestamps:
            self._keystroke_timestamps[source] = deque(maxlen=50)
        self._keystroke_timestamps[source].append(time.monotonic())

    def get_heuristics(self, source: Any) -> dict[str, float]:
        """Return behavioral metrics for the given browser."""
        timestamps = self._keystroke_timestamps.get(source)
        if not timestamps or len(timestamps) < 2:
            return {"cps": 0.0, "jitter": 0.0}

        duration = timestamps[-1] - timestamps[0]
        cps = (len(timestamps) - 1) / duration if duration > 0 else 0.0
        intervals = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
        jitter = statistics.variance(intervals) if len(intervals) > 1 else 0.0
        return {"cps": cps, "jitter": jitter}

    def forget_browser(self, ws: Any) -> None:
        """Drop heuristic state for a disconnected browser."""
        self._keystroke_timestamps.pop(ws, None)

    async def run_behavioral_audit_loop(self) -> None:
        """Periodically audit active connections for behavioral anomalies.

        ``self._hub._audit_all_browsers`` is invoked rather than the local
        method so existing tests that patch ``hub._audit_all_browsers``
        (e.g. to raise an exception and exercise the exception logger)
        keep intercepting. The hub-level shim forwards to
        :meth:`audit_all_browsers` here, so the cycle terminates on the
        second hop.
        """
        import asyncio

        hub = self._hub
        while True:
            await asyncio.sleep(hub._behavioral_audit_interval_s)
            try:
                await hub._audit_all_browsers()
            except Exception:
                logger.exception("behavioral_audit_loop_error")

    async def audit_all_browsers(self) -> None:
        """Iterate all active browsers and evaluate behavioral heuristics."""
        from fastapi import status

        from provide.uterm.server.bridge.hub.ext import ConnectionHeuristics

        hub = self._hub
        async with hub._lock:
            all_browsers = [(worker_id, ws) for worker_id, st in hub.registry.items() for ws in st.browsers]

        for worker_id, ws in all_browsers:
            heuristics_data = self.get_heuristics(ws)
            heuristics = ConnectionHeuristics(
                cps=heuristics_data["cps"],
                jitter=heuristics_data["jitter"],
                timestamp=time.time(),
            )
            context = await hub.prepare_policy_context(ws, worker_id, action="behavioral_audit")
            # _behavioral_audit_gate is Any | None; the guard above already
            # exited the loop when it's None (see run_behavioral_audit_loop),
            # but the narrow doesn't survive across awaits.
            assert hub._behavioral_audit_gate is not None
            decision = await hub._behavioral_audit_gate.audit_connection(
                heuristics, context, hub._behavioral_thresholds
            )
            if decision.action == "deny":
                logger.warning(
                    "behavioral_audit_denied worker_id=%s reason=%s",
                    worker_id,
                    decision.reason or "anomaly detected",
                )
                with contextlib.suppress(Exception):
                    await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason=decision.reason or "Behavioral anomaly")

    # -- Worker / browser lifecycle helpers -----------------------------

    async def prune_if_idle(self, worker_id: str) -> None:
        """Remove worker state when no connections or leases remain."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return
            if st.worker_ws is None and not st.browsers and st.hijack_owner is None and st.hijack_session is None:
                hub.registry.pop(worker_id)
                logger.debug("pruned idle worker_id=%s", worker_id)

    async def hijack_state_msg_for(self, worker_id: str, ws: WebSocket) -> HijackStateFrame:
        """Build a hijack_state dict for *ws*, setting owner='me' if *ws* holds the lease."""
        from provide.uterm.server.bridge.hub.core import _mono_to_wall

        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return make_hijack_state_frame(
                    hijacked=False,
                    owner=None,
                    lease_expires_at=None,
                    input_mode="hijack",
                )
            is_dashboard = hub.is_dashboard_hijack_active(st)
            is_rest = hub.has_valid_rest_lease(st)
            is_h = is_dashboard or is_rest
            input_mode = st.input_mode
            lease_expires_at = (
                st.hijack_session.lease_expires_at
                if is_rest and st.hijack_session is not None
                else st.hijack_owner_expires_at
            )
            if is_dashboard and st.hijack_owner is ws:
                owner: str | None = "me"
            elif is_dashboard or is_rest:
                owner = "other"
            else:
                owner = None
        return make_hijack_state_frame(
            hijacked=is_h,
            owner=owner,
            lease_expires_at=_mono_to_wall(lease_expires_at),
            input_mode=input_mode,
        )

    async def set_input_mode(self, worker_id: str, mode: InputMode) -> tuple[bool, str | None]:
        """Set input_mode under lock. Rejects if active hijack when switching to "open"."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return False, "not_found"
            if mode == "open" and hub.is_hijacked(st):
                return False, "active_hijack"
            st.input_mode = mode
        await self.broadcast(worker_id, {"type": "input_mode_changed", "input_mode": mode, "ts": time.time()})
        await self.broadcast_hijack_state(worker_id)
        return True, None

    # NOTE: ``disconnect_worker`` is intentionally NOT defined here. The
    # full implementation lives on :class:`HubMessagingMixin` so the
    # cross-cutting hooks (``broadcast_hijack_state``, ``prune_if_idle``,
    # ``notify_hijack_changed``) dispatch through ``self.<name>`` on the
    # hub — which lets tests monkey-patch those methods on a hub
    # instance to verify the worker_id flows correctly.

    # -- Read accessors -------------------------------------------------

    async def get_idle_candidates(self, timeout_s: float) -> list[tuple[str, float]]:
        """Return ``(worker_id, last_activity_at)`` for workers with no browsers idle beyond *timeout_s*."""
        hub = self._hub
        now = time.monotonic()
        async with hub._lock:
            return [
                (wid, st.last_activity_at)
                for wid, st in hub.registry.items()
                if not st.browsers and (now - st.last_activity_at) > timeout_s
            ]

    async def set_browser_role(self, worker_id: str, ws: WebSocket, role: str) -> None:
        """Update the role for *ws* in *worker_id*'s browser set."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is not None and ws in st.browsers:  # pragma: no branch
                st.browsers[ws] = role

    async def try_reclaim_hijack(self, worker_id: str, ws: WebSocket) -> bool:
        """Attempt to acquire hijack ownership for *ws* if the session is unhijacked."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if (
                st is not None
                and st.worker_ws is not None
                and st.input_mode != "open"
                and st.hijack_owner is None
                and not hub.is_hijacked(st)
            ):
                st.hijack_owner = ws
                st.hijack_owner_expires_at = time.monotonic() + hub._dashboard_hijack_lease_s
                return True
        return False

    async def get_worker_browser_role(self, worker_id: str, ws: WebSocket) -> str | None:
        """Return the role assigned to *ws* for *worker_id*, or ``None`` if not found."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return None
            role: str | None = st.browsers.get(ws)
            return role

    async def get_last_snapshot(self, worker_id: str) -> dict[str, Any] | None:
        """Return the most recent snapshot for *worker_id*, or ``None`` if not registered."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            return None if st is None else st.last_snapshot

    async def browser_count(self, worker_id: str) -> int:
        """Return the number of browser WebSockets currently connected for *worker_id*."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            return 0 if st is None else len(st.browsers)

    async def browser_count_total(self) -> int:
        """Return the total number of browser WebSockets connected across all workers."""
        hub = self._hub
        async with hub._lock:
            return sum(len(st.browsers) for st in hub.registry.all())

    async def get_recent_events(self, worker_id: str, limit: int) -> list[dict[str, Any]]:
        """Return the most recent events for *worker_id* (up to *limit*, clamped to 1-500)."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return []
            return list(st.events)[-max(1, min(limit, 500)) :]


__all__ = ["MessageRouter"]
