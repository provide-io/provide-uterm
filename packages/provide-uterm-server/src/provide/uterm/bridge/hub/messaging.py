#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import contextlib
import statistics
import time
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger
from provide.uterm.bridge.frames import make_hijack_state_frame, make_worker_disconnected_frame
from provide.uterm.bridge.hub.redaction import StreamRedactor

if TYPE_CHECKING:
    from fastapi import APIRouter, WebSocket

    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.bridge.frames import HijackStateFrame
    from provide.uterm.bridge.hub.resume import ResumeTokenStore

logger = get_logger(__name__)


class HubMessagingMixin:
    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append a timestamped event to the worker's event ring buffer and return it."""
        payload = data or {}
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return {"seq": 0, "ts": time.time(), "type": event_type, "data": payload}
            st.event_seq += 1
            evt: dict[str, Any] = {"seq": st.event_seq, "ts": time.time(), "type": event_type, "data": payload}
            st.events.append(evt)
            st.min_event_seq = int(st.events[0]["seq"])
        if self._event_bus is not None:
            self._event_bus._enqueue(worker_id, evt)
        return evt

    async def broadcast(self, worker_id: str, msg: dict[str, Any]) -> None:
        """Send *msg* to all browser WebSockets registered for *worker_id*."""
        from provide.uterm.bridge.hub.core import _encode_browser_frame

        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            browsers_with_roles = list(st.browsers.items())

        dead: set[WebSocket] = set()
        is_term_data = msg.get("type") == "term"
        raw_data = str(msg.get("data", "")) if is_term_data else ""

        # Pre-encode for all browsers (except when redaction is needed)
        encoded_default = _encode_browser_frame(msg)

        for ws, _role in browsers_with_roles:
            try:
                final_payload = encoded_default
                if is_term_data and self._output_policy_gate:
                    context = await self.prepare_policy_context(ws, worker_id, action="output")
                    rules = await self._output_policy_gate.get_redaction_rules(context)
                    if rules:
                        redactor = StreamRedactor(rules)
                        redacted_data = redactor.redact(raw_data)
                        final_payload = _encode_browser_frame({"type": "term", "data": redacted_data})
                await ws.send_text(final_payload)
            except Exception as exc:
                logger.debug("broadcast_send_failed worker_id=%s: %s", worker_id, exc)
                dead.add(ws)
        if dead:
            changed = await self.remove_dead_browsers(worker_id, dead)
            if changed:
                await self.broadcast_hijack_state(worker_id)

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
        """Send a hijack_state message to each browser; return the set of dead sockets."""
        from provide.uterm.bridge.hub.core import _encode_browser_frame, _mono_to_wall

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
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            browsers = list(st.browsers.keys())
            hijack_owner = st.hijack_owner
            is_hijacked = self.is_hijacked(st)
            is_dashboard = self.is_dashboard_hijack_active(st)
            is_rest = self.has_valid_rest_lease(st)
            input_mode = st.input_mode
            lease_expires_at = (
                st.hijack_session.lease_expires_at
                if is_rest and st.hijack_session is not None
                else st.hijack_owner_expires_at
            )

        dead = await self._send_hijack_state_to(
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
            await self.remove_dead_browsers(worker_id, dead)
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is None:
                    return
                survivors = list(st2.browsers.keys())
                is_h2 = self.is_hijacked(st2)
                is_dashboard2 = self.is_dashboard_hijack_active(st2)
                is_rest2 = self.has_valid_rest_lease(st2)
                hijack_owner2 = st2.hijack_owner
                input_mode2 = st2.input_mode
                lease2 = (
                    st2.hijack_session.lease_expires_at
                    if is_rest2 and st2.hijack_session is not None
                    else st2.hijack_owner_expires_at
                )
            await self._send_hijack_state_to(
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
        """Send *msg* to the worker WebSocket; returns False if no worker is connected."""
        from provide.uterm.bridge.hub.core import _encode_worker_frame

        if source and msg.get("type") == "input":
            self._record_keystroke(source)

        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None or st.worker_ws is None:
                return False
            ws = st.worker_ws
        try:
            await ws.send_text(_encode_worker_frame(msg))
            return True
        except Exception as exc:
            logger.debug("send_worker_failed worker_id=%s: %s", worker_id, exc)
            async with self._lock:
                st2 = self._workers.get(worker_id)
                if st2 is not None and st2.worker_ws is ws:  # pragma: no branch
                    st2.worker_ws = None
            return False

    def _record_keystroke(self, source: Any) -> None:
        """Record the timing of a keystroke from a browser."""
        if source not in self._keystroke_timestamps:
            self._keystroke_timestamps[source] = deque(maxlen=50)
        self._keystroke_timestamps[source].append(time.monotonic())

    def _get_heuristics(self, source: Any) -> dict[str, float]:
        """Return behavioral metrics for the given browser."""
        timestamps = self._keystroke_timestamps.get(source)
        if not timestamps or len(timestamps) < 2:
            return {"cps": 0.0, "jitter": 0.0}

        duration = timestamps[-1] - timestamps[0]
        cps = (len(timestamps) - 1) / duration if duration > 0 else 0.0
        intervals = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
        jitter = statistics.variance(intervals) if len(intervals) > 1 else 0.0
        return {"cps": cps, "jitter": jitter}

    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]:
        """Clear heuristic state and call parent cleanup."""
        self._keystroke_timestamps.pop(ws, None)
        self._input_buffers.pop(ws, None)
        self._hold_buffers.pop(ws, None)
        return await super().cleanup_browser_disconnect(worker_id, ws, owned_hijack)

    async def remove_dead_browsers(self, worker_id: str, dead: set[WebSocket]) -> bool:
        """Clear input buffers for dead browsers and call parent cleanup."""
        for ws in dead:
            self._input_buffers.pop(ws, None)
            self._hold_buffers.pop(ws, None)
        return await super().remove_dead_browsers(worker_id, dead)

    async def _run_behavioral_audit_loop(self) -> None:
        """Periodically audit active connections for behavioral anomalies."""
        while True:
            await asyncio.sleep(self._behavioral_audit_interval_s)
            try:
                await self._audit_all_browsers()
            except Exception:
                logger.exception("behavioral_audit_loop_error")

    async def _audit_all_browsers(self) -> None:
        """Iterate all active browsers and evaluate behavioral heuristics."""
        from fastapi import status

        from provide.uterm.bridge.hub.ext import ConnectionHeuristics

        async with self._lock:
            all_browsers = [(worker_id, ws) for worker_id, st in self._workers.items() for ws in st.browsers]

        for worker_id, ws in all_browsers:
            heuristics_data = self._get_heuristics(ws)
            heuristics = ConnectionHeuristics(
                cps=heuristics_data["cps"],
                jitter=heuristics_data["jitter"],
                timestamp=time.time(),
            )
            context = await self.prepare_policy_context(ws, worker_id, action="behavioral_audit")
            decision = await self._behavioral_audit_gate.audit_connection(
                heuristics, context, self._behavioral_thresholds
            )
            if decision.action == "deny":
                logger.warning(
                    "behavioral_audit_denied worker_id=%s reason=%s",
                    worker_id,
                    decision.reason or "anomaly detected",
                )
                with contextlib.suppress(Exception):
                    await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason=decision.reason or "Behavioral anomaly")

    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Deregister the worker WS and notify the EventBus on disconnect."""
        should_broadcast, was_hijacked = await super().deregister_worker(worker_id, ws)
        if should_broadcast and self._event_bus is not None:
            self._event_bus.close_worker(worker_id)
        return should_broadcast, was_hijacked

    async def prune_if_idle(self, worker_id: str) -> None:
        """Remove worker state when no connections or leases remain."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            if st.worker_ws is None and not st.browsers and st.hijack_owner is None and st.hijack_session is None:
                del self._workers[worker_id]
                logger.debug("pruned idle worker_id=%s", worker_id)

    async def hijack_state_msg_for(self, worker_id: str, ws: WebSocket) -> HijackStateFrame:
        """Build a hijack_state dict for *ws*, setting owner='me' if *ws* holds the lease."""
        from provide.uterm.bridge.hub.core import _mono_to_wall

        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return make_hijack_state_frame(
                    hijacked=False,
                    owner=None,
                    lease_expires_at=None,
                    input_mode="hijack",
                )
            is_dashboard = self.is_dashboard_hijack_active(st)
            is_rest = self.has_valid_rest_lease(st)
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
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False, "not_found"
            if mode == "open" and self.is_hijacked(st):
                return False, "active_hijack"
            st.input_mode = mode
        await self.broadcast(worker_id, {"type": "input_mode_changed", "input_mode": mode, "ts": time.time()})
        await self.broadcast_hijack_state(worker_id)
        return True, None

    async def disconnect_worker(self, worker_id: str) -> bool:
        """Programmatically disconnect the worker WS. Returns True if a worker was connected."""
        ws: WebSocket | None = None
        async with self._lock:
            st = self._workers.get(worker_id)
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

    async def get_idle_candidates(self, timeout_s: float) -> list[tuple[str, float]]:
        """Return ``(worker_id, last_activity_at)`` for workers with no browsers idle beyond *timeout_s*."""
        now = time.monotonic()
        async with self._lock:
            return [
                (wid, st.last_activity_at)
                for wid, st in self._workers.items()
                if not st.browsers and (now - st.last_activity_at) > timeout_s
            ]

    @property
    def resume_store(self) -> ResumeTokenStore | None:
        """Public accessor for the resume token store."""
        return self._resume_store

    async def set_browser_role(self, worker_id: str, ws: WebSocket, role: str) -> None:
        """Update the role for *ws* in *worker_id*'s browser set."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is not None and ws in st.browsers:  # pragma: no branch
                st.browsers[ws] = role

    async def try_reclaim_hijack(self, worker_id: str, ws: WebSocket) -> bool:
        """Attempt to acquire hijack ownership for *ws* if the session is unhijacked."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if (
                st is not None
                and st.worker_ws is not None
                and st.input_mode != "open"
                and st.hijack_owner is None
                and not self.is_hijacked(st)
            ):
                st.hijack_owner = ws
                st.hijack_owner_expires_at = time.monotonic() + self._dashboard_hijack_lease_s
                return True
        return False

    async def get_worker_browser_role(self, worker_id: str, ws: WebSocket) -> str | None:
        """Return the role assigned to *ws* for *worker_id*, or ``None`` if not found."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return None
            return st.browsers.get(ws)

    async def get_last_snapshot(self, worker_id: str) -> dict[str, Any] | None:
        """Return the most recent snapshot for *worker_id*, or ``None`` if not registered."""
        async with self._lock:
            st = self._workers.get(worker_id)
            return None if st is None else st.last_snapshot

    async def browser_count(self, worker_id: str) -> int:
        """Return the number of browser WebSockets currently connected for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            return 0 if st is None else len(st.browsers)

    async def browser_count_total(self) -> int:
        """Return the total number of browser WebSockets connected across all workers."""
        async with self._lock:
            return sum(len(st.browsers) for st in self._workers.values())

    async def get_recent_events(self, worker_id: str, limit: int) -> list[dict[str, Any]]:
        """Return the most recent events for *worker_id* (up to *limit*, clamped to 1-500)."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return []
            return list(st.events)[-max(1, min(limit, 500)) :]

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
