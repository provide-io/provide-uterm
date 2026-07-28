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

import time
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.server.bridge.frames import make_hijack_state_frame
from provide.uterm.server.bridge.hub.redaction import StreamRedactor
from provide.uterm.server.bridge.hub.router_behavioral import (
    audit_all_browsers as _audit_all_browsers_impl,
)
from provide.uterm.server.bridge.hub.router_behavioral import (
    forget_browser as _forget_browser_impl,
)
from provide.uterm.server.bridge.hub.router_behavioral import (
    get_heuristics as _get_heuristics_impl,
)
from provide.uterm.server.bridge.hub.router_behavioral import (
    record_keystroke as _record_keystroke_impl,
)
from provide.uterm.server.bridge.hub.router_behavioral import (
    run_behavioral_audit_loop as _run_behavioral_audit_loop_impl,
)
from provide.uterm.server.bridge.hub.router_broadcast import (
    broadcast as _broadcast_impl,
)
from provide.uterm.server.bridge.hub.router_broadcast import (
    broadcast_hijack_state as _broadcast_hijack_state_impl,
)
from provide.uterm.server.bridge.hub.router_broadcast import (
    send_hijack_state_to as _send_hijack_state_to_impl,
)
from provide.uterm.server.bridge.hub.router_broadcast import (
    send_worker as _send_worker_impl,
)

# Re-exported so the ``router_impl`` namespace (and tests importing these from
# it) is unchanged after the redaction helpers moved to ``router_redaction``.
from provide.uterm.server.bridge.hub.router_redaction import (
    _REDACT_MAX_DEPTH,  # noqa: F401  (re-export for namespace stability)
    _redact_frame_fields,
    _redact_value,
)

if TYPE_CHECKING:
    from collections import deque

    from fastapi import WebSocket

    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.server.bridge.frames import HijackStateFrame
    from provide.uterm.server.bridge.hub.core import TermHub

logger = get_logger(__name__)

# Per-browser send timeout in broadcast(). A viewer whose receive window is
# stalled is treated as dead and pruned rather than head-of-line-blocking the
# worker-output fanout indefinitely.
_BROADCAST_SEND_TIMEOUT_S = 5.0


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

    # Content event types whose payloads carry terminal output and must be
    # redacted at write time before landing in the ring buffer. Other event
    # types (hijack_*, input_send, worker_status, ...) carry control metadata,
    # not scraped terminal output, and pass through unredacted.
    _REDACTED_EVENT_TYPES = frozenset({"term", "snapshot", "analysis"})

    __slots__ = ("_event_redactor", "_hub", "_keystroke_timestamps")

    def __init__(self, hub: TermHub) -> None:
        self._hub = hub
        # Per-browser keystroke timing ring buffers used by the
        # behavioral audit loop. Lives on the router because it's
        # purely messaging-adjacent state. The mixin exposes a
        # property shim so legacy tests that poke this directly
        # continue to work.
        self._keystroke_timestamps: dict[Any, deque[float]] = {}
        # Server-default redactor applied to event content at WRITE time so the
        # ring buffer (read by the events API / watch / MCP tools) is never an
        # unredacted egress for what the live broadcast scrubs. Events are
        # role-agnostic, so a single shared default-ruleset redactor is correct
        # and is built once (lazily on first content event, then reused).
        self._event_redactor: StreamRedactor | None = None

    @property
    def keystroke_timestamps(self) -> dict[Any, deque[float]]:
        """Per-browser keystroke timestamp ring buffers (mutable view)."""
        return self._keystroke_timestamps

    # -- Event ring buffer -----------------------------------------------

    def _redact_event_payload(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Redact terminal-content fields of a content event payload (write-time).

        Treats the payload as a frame by tagging it with ``type`` and reusing
        :func:`_redact_frame_fields` (so it benefits from the same string-field
        and structured-value recursion as the broadcast path), then strips the
        synthetic ``type`` key back off. Uses the server-default ruleset — events
        are role-agnostic and redacted once. Non-content event types are not
        passed here.

        A ``term`` event whose ``data`` is not a string (e.g. ``{"data": 42}``)
        is returned verbatim: ``_redact_frame_fields``' term branch ``str()``-
        coerces ``data``, which would change the stored type, so we skip it to
        preserve the legacy ring contract. snapshot/analysis content fields are
        strings in every real producer, so they always go through the redactor.
        Never mutates the input payload.
        """
        if event_type == "term" and not isinstance(payload.get("data"), str):
            return payload
        if self._event_redactor is None:
            from provide.uterm.server.bridge.hub.redaction_defaults import default_rules

            self._event_redactor = StreamRedactor(default_rules())
        redacted = _redact_frame_fields({"type": event_type, **payload}, self._event_redactor)
        return {k: v for k, v in redacted.items() if k != "type"}

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append a timestamped event to the worker's event ring buffer and return it.

        Content events (term / snapshot / analysis) have their terminal-output
        fields redacted with the server-default ruleset BEFORE being stored, so
        the ring buffer — read by the events API, ``/events/watch`` and the MCP
        events tools — is never an unredacted egress for what the live broadcast
        scrubs. Redaction runs before the term-data char cap below.

        For ``event_type == "term"`` the stored ring copy's ``data["data"]``
        field is truncated to ``hub.max_event_data_chars``.  This bounds the
        per-event memory footprint in the ring; the live broadcast path
        (``hub.broadcast``) sends the full payload independently and is
        unaffected.
        """
        hub = self._hub
        payload = data or {}
        # Redact content at write time (before truncation) so a secret near the
        # truncation boundary is removed regardless of where the cap would cut.
        if event_type in self._REDACTED_EVENT_TYPES:
            payload = self._redact_event_payload(event_type, payload)
        if event_type == "term" and isinstance(payload.get("data"), str):
            cap = hub.max_event_data_chars
            raw = payload["data"]
            if len(raw) > cap:
                payload = {**payload, "data": raw[:cap]}
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

    async def commit_snapshot_event(self, worker_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Atomically store a snapshot and append its correlated ring event."""
        hub = self._hub
        payload = dict(snapshot)
        frame_type = payload.pop("type", "snapshot")
        payload = {"type": frame_type, **self._redact_event_payload("snapshot", payload)}
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return {**payload, "event_seq": 0}
            st.event_seq += 1
            committed = {**payload, "event_seq": st.event_seq}
            evt: dict[str, Any] = {
                "seq": st.event_seq,
                "ts": time.time(),
                "type": "snapshot",
                "data": committed,
            }
            st.last_snapshot = committed
            st.events.append(evt)
            st.min_event_seq = int(st.events[0]["seq"])
        if hub._event_bus is not None:
            hub._event_bus._enqueue(worker_id, evt)
        return committed

    # -- Broadcast / send hot path --------------------------------------
    # Thin wrappers over :mod:`router_broadcast` (fan-out / redaction / worker
    # send); method surface and ``hub._lock`` semantics are unchanged.

    async def broadcast(self, worker_id: str, msg: dict[str, Any]) -> None:
        """Send *msg* to all browser WebSockets registered for *worker_id*."""
        await _broadcast_impl(self, worker_id, msg)  # ty:ignore[invalid-argument-type]

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
        return await _send_hijack_state_to_impl(
            self,  # ty:ignore[invalid-argument-type]
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

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        """Send a hijack_state message to every browser for *worker_id*, cleaning up dead sockets."""
        await _broadcast_hijack_state_impl(self, worker_id)  # ty:ignore[invalid-argument-type]

    async def send_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        source: Any = None,
        expected_worker: WebSocket | None = None,
    ) -> bool:
        """Send *msg* to the worker WebSocket; returns False if no worker is connected.

        Tunnel workers (``is_tunnel_worker=True``) use the binary tunnel
        protocol: ``input`` messages are sent as raw UTF-8 PTY bytes, HTTP
        inspect controls are sent on ``CHANNEL_HTTP``, and other message
        types are dropped because the worker's bridge loop has no JSON
        envelope handling.
        """
        return await _send_worker_impl(  # ty:ignore[invalid-argument-type]
            self, worker_id, msg, source=source, expected_worker=expected_worker
        )

    # -- Behavioral heuristics ------------------------------------------
    # Thin wrappers over :mod:`router_behavioral` (keystroke timing / audit);
    # method surface is unchanged.

    def record_keystroke(self, source: Any) -> None:
        """Record the timing of a keystroke from a browser."""
        _record_keystroke_impl(self, source)  # ty:ignore[invalid-argument-type]

    def get_heuristics(self, source: Any) -> dict[str, float]:
        """Return behavioral metrics for the given browser."""
        return _get_heuristics_impl(self, source)  # ty:ignore[invalid-argument-type]

    def forget_browser(self, ws: Any) -> None:
        """Drop heuristic state for a disconnected browser."""
        _forget_browser_impl(self, ws)  # ty:ignore[invalid-argument-type]

    async def run_behavioral_audit_loop(self) -> None:
        """Periodically audit active connections for behavioral anomalies."""
        await _run_behavioral_audit_loop_impl(self)  # ty:ignore[invalid-argument-type]

    async def audit_all_browsers(self) -> None:
        """Iterate all active browsers and evaluate behavioral heuristics."""
        await _audit_all_browsers_impl(self)  # ty:ignore[invalid-argument-type]

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
        """Set input_mode under lock. Rejects if active hijack when switching to "open".

        Every caller of this is an authenticated route — the two session routes
        and the worker-control route, which requires ``session.control.mode``. So
        reaching here means somebody *decided* the mode, and the decision is
        recorded: a worker's ``worker_hello`` may afterwards raise the mode but
        never lower it back. See
        :attr:`~provide.uterm.server.bridge.models.WorkerTermState.input_mode_set_by_operator`.
        """
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return False, "not_found"
            if mode == "open" and hub.is_hijacked(st):
                return False, "active_hijack"
            st.input_mode = mode
            st.input_mode_set_by_operator = True
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
        reclaimed, _competing_owner = await self.try_reclaim_hijack_status(worker_id, ws)
        return reclaimed

    async def try_reclaim_hijack_status(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Return ``(reclaimed, competing_owner)`` from one fenced observation."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return False, False
            state = st
            fence = st.owned_input_fence
        async with fence:
            async with hub._lock:
                st = hub.registry.get(worker_id)
                if (
                    st is state
                    and st.worker_ws is not None
                    and st.input_mode != "open"
                    and st.hijack_owner is None
                    and not hub.is_hijacked(st)
                ):
                    st.hijack_owner = ws
                    st.hijack_owner_expires_at = time.monotonic() + hub.lease.dashboard_hijack_lease_s
                    st.ownership_generation += 1
                    return True, False
                return False, st is state and hub.is_hijacked(st)

    async def get_worker_browser_role(self, worker_id: str, ws: WebSocket) -> str | None:
        """Return the role assigned to *ws* for *worker_id*, or ``None`` if not found."""
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return None
            role: str | None = st.browsers.get(ws)
            return role

    async def redact_snapshot_for_recipient(
        self, worker_id: str, snapshot: dict[str, Any], recipient: Any
    ) -> dict[str, Any]:
        """Return a recipient-role-redacted COPY of *snapshot* (M5 read-path parity).

        Applies the SAME output-redaction policy as the live broadcast path
        (:meth:`broadcast`), scoped to *recipient*'s role via
        ``prepare_policy_context`` + ``get_redaction_rules``. *recipient* is the
        connecting browser ``ws`` (WS initial_snapshot) or the requesting
        ``Request`` (REST ``/snapshot``) — both expose the principal that
        ``prepare_policy_context`` resolves.

        Returns *snapshot* unchanged when the output gate is inactive or yields
        no rules; otherwise returns a redacted copy built by
        :func:`_redact_frame_fields`. The input *snapshot* is never mutated, so
        the stored ``last_snapshot`` is safe to redact-on-read repeatedly with
        different recipient roles. Callers must only invoke this when the gate
        is active (it re-checks defensively).
        """
        hub = self._hub
        gate = hub._output_policy_gate
        if gate is None:  # pragma: no cover — callers guard on gate; defensive
            return snapshot
        context = await hub.prepare_policy_context(recipient, worker_id, action="output")
        rules = await gate.get_redaction_rules(context)
        if not rules:
            return snapshot
        # Force the snapshot frame type so _redact_frame_fields' field map fires
        # even if a stored snapshot somehow lacks an explicit ``type`` key (the
        # redactor only redacts frames whose ``type`` is ``"snapshot"``). This is
        # a copy — the stored last_snapshot is never mutated. Setting the key
        # when it is already "snapshot" is a harmless idempotent overwrite.
        to_redact = {**snapshot, "type": "snapshot"}
        return _redact_frame_fields(to_redact, StreamRedactor(rules))

    async def get_last_snapshot(self, worker_id: str, recipient: Any = None) -> dict[str, Any] | None:
        """Return the most recent snapshot for *worker_id*, or ``None`` if not registered.

        When *recipient* is provided AND an output-redaction policy is active,
        the returned snapshot is a role-scoped REDACTED copy (M5: the REST
        ``/snapshot`` and WS initial-snapshot reads must not bypass the policy
        the broadcast path enforces). With no recipient or no gate the raw
        stored snapshot is returned (broadcast source / no-policy default). The
        stored ``last_snapshot`` is never mutated.
        """
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            snapshot = None if st is None else st.last_snapshot
        if snapshot is None or recipient is None or hub._output_policy_gate is None:
            return snapshot
        return await self.redact_snapshot_for_recipient(worker_id, snapshot, recipient)

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


__all__ = ["MessageRouter", "_redact_frame_fields", "_redact_value"]
