#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TermHub: in-memory registry for terminal WebSocket connections.

Requires the ``websocket`` extra::

    pip install 'provide-terminal[websocket]'
"""

from __future__ import annotations

import asyncio
import inspect
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from provide.terminal.bridge.contracts import InputMode
    from provide.terminal.bridge.hub.event_bus import EventBus
    from provide.terminal.bridge.identity import IdentityProvider, Principal

from provide.telemetry import get_logger

try:
    from fastapi import APIRouter, WebSocket, WebSocketException, status
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for TermHub: pip install 'provide-terminal[websocket]'") from _e

import contextlib

from provide.terminal.bridge.frames import HijackStateFrame, make_hijack_state_frame, make_worker_disconnected_frame
from provide.terminal.bridge.hub.approvals import InMemoryApprovalStore
from provide.terminal.bridge.hub.connections import _ConnectionMixin
from provide.terminal.bridge.hub.ext import (
    BehavioralAuditGate,
    BehavioralThresholds,
    ConnectionHeuristics,
    NoOpBehavioralAuditGate,
    NoOpPolicyGate,
    OutputPolicyGate,
    PolicyContext,
    PolicyDecision,
    PolicyGate,
)
from provide.terminal.bridge.hub.ownership import _HijackOwnershipMixin
from provide.terminal.bridge.hub.polling import _PollingMixin
from provide.terminal.bridge.hub.redaction import StreamRedactor
from provide.terminal.bridge.hub.resume import ResumeSession, ResumeTokenStore
from provide.terminal.bridge.models import WorkerTermState
from provide.terminal.bridge.ratelimit import TokenBucket
from provide.terminal.control_channel import encode_control, encode_data

logger = get_logger(__name__)
# Callback type aliases
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


class TermHub(_PollingMixin, _HijackOwnershipMixin, _ConnectionMixin):
    """In-memory registry for terminal WebSocket connections.

    Manages the lifecycle of worker / browser terminal streams and hijack leases.

    Args:
        on_hijack_changed: ``(worker_id, hijacked, owner) -> None`` fired on any
            hijack state change (async or sync).
        dashboard_hijack_lease_s: Default WS hijack lease duration in seconds.
        resolve_browser_role: ``(ws, worker_id) -> str | None`` — returns
            ``"viewer"``, ``"operator"``, or ``"admin"`` for each browser; ``None``
            defaults to ``"viewer"``. Raise :class:`BrowserRoleResolutionError`
            to close the socket with 1008.
        policy_gate: Optional :class:`PolicyGate` for input interception.
        identity_provider: Optional :class:`IdentityProvider` for principal resolution.
        delegate_roles: If True, trust roles returned by IdP. If False, map from claims.
    """

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
        rest_acquire_rate_limit_per_sec: float = 5,
        rest_send_rate_limit_per_sec: float = 20,
        worker_token: str | None = None,
        event_deque_maxlen: int = 2000,
        resume_store: ResumeTokenStore | None = None,
        resume_ttl_s: float = 300,
        on_resume: ResumeCallback | None = None,
        event_bus: EventBus | None = None,
        ws_idle_timeout_s: float = 300.0,
        policy_gate: PolicyGate | None = None,
        identity_provider: IdentityProvider | None = None,
        delegate_roles: bool = True,
        output_policy_gate: OutputPolicyGate | None = None,
        behavioral_audit_gate: BehavioralAuditGate | None = None,
        behavioral_thresholds: BehavioralThresholds | None = None,
        behavioral_audit_interval_s: float = 30.0,
    ) -> None:
        self._lock = asyncio.Lock()
        self._workers: dict[str, WorkerTermState] = {}
        self._on_hijack_changed = on_hijack_changed
        self._on_metric = on_metric
        self._resolve_browser_role = resolve_browser_role
        self.on_worker_empty: WorkerEmptyCallback | None = on_worker_empty
        self._worker_token = worker_token
        self._dashboard_hijack_lease_s = max(1, min(int(dashboard_hijack_lease_s), 600))
        self.max_ws_message_bytes = max(1024, int(max_ws_message_bytes))
        self.max_input_chars = max(100, int(max_input_chars))
        self.browser_rate_limit_per_sec = float(browser_rate_limit_per_sec)
        self._rest_acquire_rate = max(0.1, float(rest_acquire_rate_limit_per_sec))
        self._rest_send_rate = max(0.1, float(rest_send_rate_limit_per_sec))
        self._rest_acquire_bucket = TokenBucket(self._rest_acquire_rate)
        self._rest_send_bucket = TokenBucket(self._rest_send_rate)
        # Per-client buckets; oldest half evicted when cache exceeds _REST_CLIENT_CACHE_MAX.
        self._rest_acquire_per_client: dict[str, TokenBucket] = {}
        self._rest_send_per_client: dict[str, TokenBucket] = {}
        self._event_deque_maxlen = max(1, int(event_deque_maxlen))
        self._resume_store = resume_store
        self._resume_ttl_s = max(1.0, float(resume_ttl_s))
        self._on_resume = on_resume
        self._ws_to_resume_token: dict[WebSocket, str] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._event_bus: EventBus | None = event_bus
        self.ws_idle_timeout_s = max(10.0, float(ws_idle_timeout_s))
        self._keystroke_timestamps: dict[Any, deque[float]] = {}
        self._policy_gate = policy_gate or NoOpPolicyGate()
        self._input_buffers: dict[WebSocket, str] = {}
        self._hold_buffers: dict[WebSocket, str] = {}
        self._approval_store = InMemoryApprovalStore()
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

        # Start background behavioral audit loop
        if not isinstance(self._behavioral_audit_gate, NoOpBehavioralAuditGate):
            audit_task = asyncio.create_task(self._run_behavioral_audit_loop())
            self._background_tasks.add(audit_task)
            audit_task.add_done_callback(self._background_tasks.discard)

    @property
    def event_bus(self) -> EventBus | None:
        """Public accessor for the EventBus instance (None if not configured)."""
        return self._event_bus

    def _buffer_and_get_command(self, ws: WebSocket, data: str) -> str | None:
        """Accumulate input for *ws* and return the command if a newline is received."""
        buf = self._input_buffers.get(ws, "") + data
        if "\r" in buf or "\n" in buf:
            self._input_buffers.pop(ws, None)
            return buf
        self._input_buffers[ws] = buf
        return None

    async def shutdown(self) -> None:
        """Cancel all background tasks for graceful shutdown."""
        from provide.terminal.bridge.hub.connections import shutdown_background_tasks

        count = await shutdown_background_tasks(self._background_tasks)
        if count:
            logger.info("hub_shutdown cancelled %d background tasks", count)

    async def touch_activity(self, worker_id: str) -> None:
        """Update the last-activity timestamp for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is not None:  # pragma: no branch
                st.last_activity_at = time.monotonic()

    def metric(self, name: str, value: int = 1) -> None:
        """Emit a named metric via the configured on_metric callback."""
        callback = self._on_metric
        if callback is None:
            return
        try:
            callback(name, int(value))
        except Exception as exc:  # pragma: no cover - defensive only
            logger.warning("metric_callback_failed metric=%s error=%s", name, exc)

    @staticmethod
    def clamp_lease(lease_s: int) -> int:
        """Clamp a lease duration to [1, 3600] seconds."""
        return max(1, min(int(lease_s), 3600))

    @staticmethod
    def has_valid_rest_lease(st: WorkerTermState) -> bool:
        """Return True if *st* has an unexpired REST hijack session."""
        hs = st.hijack_session
        return hs is not None and hs.lease_expires_at > time.monotonic()

    @staticmethod
    def is_dashboard_hijack_active(st: WorkerTermState) -> bool:
        """Return True if a dashboard WS hijack owner exists and its lease has not expired."""
        if st.hijack_owner is None:
            return False
        if st.hijack_owner_expires_at is None:
            return True
        return st.hijack_owner_expires_at > time.monotonic()

    def is_hijacked(self, st: WorkerTermState) -> bool:
        """Return True if *st* is under any active hijack (dashboard WS or REST)."""
        return self.is_dashboard_hijack_active(st) or self.has_valid_rest_lease(st)

    async def _get(self, worker_id: str) -> WorkerTermState:
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                st = WorkerTermState()
                self._workers[worker_id] = st
            return st

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        """Fire the on_hijack_changed callback (sync or async) without blocking."""
        cb = self._on_hijack_changed
        if cb is None:
            return
        result = cb(worker_id, enabled, owner)
        if inspect.isawaitable(result):
            task: asyncio.Task[object] = asyncio.create_task(result)  # type: ignore[arg-type]
            task.add_done_callback(
                lambda t: (
                    logger.warning("on_hijack_changed callback raised worker_id=%s error=%s", worker_id, t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
            )

    async def _resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        role = "viewer"
        resolver = self._resolve_browser_role
        if resolver is None:
            return role
        try:
            resolved_role = resolver(ws, worker_id)
            if inspect.isawaitable(resolved_role):
                try:
                    resolved_role = await asyncio.wait_for(resolved_role, timeout=5.0)
                except TimeoutError as exc:
                    logger.warning("resolve_browser_role_timeout worker_id=%s", worker_id)
                    self.metric("browser_role_resolution_timeout")
                    raise BrowserRoleResolutionError(worker_id) from exc
        except (BrowserRoleResolutionError, WebSocketException):
            # Re-raise so the caller sees the original close code / error type.
            raise
        except Exception as exc:
            logger.warning("resolve_browser_role_failed worker_id=%s error=%s", worker_id, exc)
            raise BrowserRoleResolutionError(worker_id) from exc
        if isinstance(resolved_role, str) and resolved_role in {"viewer", "operator", "admin"}:
            return resolved_role
        if resolved_role is not None:
            logger.warning("resolve_browser_role_invalid worker_id=%s role=%r", worker_id, resolved_role)
        return role

    async def prepare_policy_context(self, ws: WebSocket, worker_id: str, action: str | None = None) -> PolicyContext:
        """Create a PolicyContext for the given browser WebSocket and worker."""
        async with self._lock:
            st = self._workers.get(worker_id)
            role = st.browsers.get(ws) if st else None

        principal = None
        if self._identity_provider:
            principal = await self._identity_provider.resolve_principal(ws)
        else:
            # Fallback for backward compatibility during migration
            principal = getattr(getattr(ws, "state", None), "uterm_principal", None)

        if principal:
            # Override role from IdP if available
            roles = self._map_roles(principal)
            # PolicyContext expects a single role string for now, we pick primary
            if roles:
                # Prefer highest privileged role for context
                if "admin" in roles:
                    role = "admin"
                elif "operator" in roles:
                    role = "operator"
                else:
                    role = "viewer"

        client_id = "anonymous"
        if principal:
            client_id = str(principal.subject_id) if hasattr(principal, "subject_id") else str(principal)

        metadata = {"principal": principal} if principal else {}
        return PolicyContext(
            worker_id=worker_id,
            client_id=client_id,
            role=role,
            action=action,
            metadata=metadata,
        )

    def _map_roles(self, principal: Principal) -> frozenset[str]:
        if self._delegate_roles:
            return principal.roles

        # Simple hardcoded mapping for now (can be expanded to rules engine)
        mapped_roles = set()
        claims = principal.claims or {}
        if claims.get("admin") or claims.get("is_admin"):
            mapped_roles.add("admin")
        elif claims.get("operator"):
            mapped_roles.add("operator")

        if not mapped_roles:
            mapped_roles.add("viewer")
        return frozenset(mapped_roles)

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append a timestamped event to the worker's event ring buffer and return it.

        For ``event_type="snapshot"`` the *data* dict contains ``prompt_id``,
        ``screen_hash``, and ``screen`` (the full screen text).  Pattern filters
        on the EventBus rely on ``data["screen"]`` being populated.
        """
        payload = data or {}
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return {"seq": 0, "ts": time.time(), "type": event_type, "data": payload}
            st.event_seq += 1
            evt: dict[str, Any] = {"seq": st.event_seq, "ts": time.time(), "type": event_type, "data": payload}
            st.events.append(evt)
            # Set min_event_seq after append so it always reflects events[0].seq,
            # which is correct whether or not the deque was full before the append.
            st.min_event_seq = int(st.events[0]["seq"])
        # EventBus fanout is outside the lock — put_nowait never blocks.
        if self._event_bus is not None:
            self._event_bus._enqueue(worker_id, evt)
        return evt

    async def broadcast(self, worker_id: str, msg: dict[str, Any]) -> None:
        """Send *msg* to all browser WebSockets registered for *worker_id*."""
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return
            # We copy browsers and their roles for per-connection redaction
            browsers_with_roles = list(st.browsers.items())

        dead: set[WebSocket] = set()
        is_term_data = msg.get("type") == "term"
        raw_data = str(msg.get("data", "")) if is_term_data else ""

        for ws, _role in browsers_with_roles:
            try:
                final_payload = _encode_browser_frame(msg)

                # Apply output redaction if enabled and this is terminal data
                if is_term_data and self._output_policy_gate:
                    context = await self.prepare_policy_context(ws, worker_id, action="output")
                    rules = await self._output_policy_gate.get_redaction_rules(context)
                    if rules:
                        redactor = StreamRedactor(rules)
                        redacted_data = redactor.redact(raw_data)
                        # Re-encode for this specific browser
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
        dead: set[WebSocket] = set()
        for ws in browsers:
            if is_dashboard and hijack_owner is ws:
                owner: str | None = "me"
            elif is_dashboard or is_rest:
                owner = "other"
            else:
                owner = None
            payload = encode_control(
                cast(
                    "dict[str, Any]",
                    make_hijack_state_frame(
                        hijacked=is_hijacked,
                        owner=owner,
                        lease_expires_at=lease_expires_at,
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
            lease_expires_at=_mono_to_wall(lease_expires_at),
        )
        if dead:
            await self.remove_dead_browsers(worker_id, dead)
            # Re-read updated state and send to survivors directly — avoids recursion
            # when multiple browsers die simultaneously.
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
                lease_expires_at=_mono_to_wall(lease2),
                suppress_errors=True,
            )

    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool:
        """Send *msg* to the worker WebSocket; returns False if no worker is connected."""
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

        # Characters Per Second (CPS) over the window
        duration = timestamps[-1] - timestamps[0]
        cps = (len(timestamps) - 1) / duration if duration > 0 else 0.0

        # Jitter (Variance of inter-keystroke timing)
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
        async with self._lock:
            # Snapshot worker/browser mapping to avoid holding lock during HTTP calls
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
                # For simplicity, we just close the socket if behavior is denied.
                # A more advanced flow could trigger a Global Hold.
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
        """Set input_mode under lock. Rejects if active hijack when switching to "open".

        Returns:
            ``(True, None)`` on success.
            ``(False, "not_found")`` if worker not registered.
            ``(False, "active_hijack")`` if a hijack is active and mode is "open".
        """
        async with self._lock:
            st = self._workers.get(worker_id)
            if st is None:
                return False, "not_found"
            if mode == "open" and self.is_hijacked(st):
                return False, "active_hijack"
            st.input_mode = mode
        await self.broadcast(
            worker_id,
            {"type": "input_mode_changed", "input_mode": mode, "ts": time.time()},
        )
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
            # Clear hijack state atomically
            was_hijacked = st.hijack_session is not None or st.hijack_owner is not None
            st.hijack_session = None
            st.hijack_owner = None
            st.hijack_owner_expires_at = None
        # Close WS outside lock
        try:
            await ws.close()
        except Exception as exc:
            logger.debug("disconnect_worker close error worker_id=%s: %s", worker_id, exc)
        await self.broadcast(
            worker_id,
            cast("dict[str, Any]", make_worker_disconnected_frame(worker_id)),
        )
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
        """Attempt to acquire hijack ownership for *ws* if the session is unhijacked.

        Returns True if ownership was successfully set.
        """
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
        """Create and return a FastAPI ``APIRouter`` with all terminal routes registered.

        *extra_route_registrars* is a list of callables ``(hub, router) -> None``
        that register additional routes (e.g. tunnel routes). This avoids a hard
        import dependency on the tunnel package.
        """
        from provide.terminal.bridge.routes.rest import register_rest_routes
        from provide.terminal.bridge.routes.websockets import register_ws_routes

        router = APIRouter()
        register_rest_routes(self, router)
        register_ws_routes(self, router)
        for registrar in extra_route_registrars or []:
            registrar(self, router)
        return router

    async def resolve_approval(self, worker_id: str, request_id: str, decision: PolicyDecision, command: str) -> None:
        """Resolve a pending approval and resume the worker if approved."""
        # 1. Handle Fan-Out Approvals
        req = self._approval_store.get(request_id)
        if req and getattr(req, "is_fanout", False):
            if decision.action == "allow":
                fo_ctrl = getattr(self, "fan_out_controller", None)
                if fo_ctrl:
                    # Execute the broadcast across the group
                    await fo_ctrl.release_approved_command(request_id)
            elif decision.action == "deny":
                logger.info(
                    "fanout_approval_rejected request_id=%s group_id=%s",
                    request_id,
                    getattr(req, "group_id", "unknown"),
                )
                fo_ctrl = getattr(self, "fan_out_controller", None)
                if fo_ctrl:
                    # Prune the pending command state
                    await asyncio.to_thread(fo_ctrl._on_approval_expired, request_id)
            return

        # 2. Handle Single-Session Approvals
        st = await self._get(worker_id)

        if decision.action == "allow":
            await self.send_worker(worker_id, {"type": "input", "data": command, "ts": time.time()})
        elif decision.action == "deny":
            # Inject rejection message into the terminal stream
            from provide.terminal.control_channel import encode_data

            msg = f"\\r\\x1b[31m[REJECTED] Command '{command.strip()}' blocked by Admin.\\x1b[0m"
            if decision.reason:
                msg += f" \\x1b[33mReason: {decision.reason}\\x1b[0m"
            msg += "\\r"
            for ws in list(st.browsers.keys()):
                await ws.send_text(encode_data(msg))

        # Unpause browsers and broadcast resolution
        for ws in list(st.browsers.keys()):
            if ws in self._paused_browsers:
                self._paused_browsers.discard(ws)
                # Playback buffered input if approved
                if decision.action == "allow" and ws in self._hold_buffers:
                    buffered_data = self._hold_buffers.pop(ws)
                    if self._on_browser_message:

                        async def _playback(
                            hub: TermHub,
                            ws: WebSocket,
                            worker_id: str,
                            role: str,
                            msg: dict[str, Any],
                            owned_hijack: bool,
                        ) -> None:
                            if hub._on_browser_message:
                                await hub._on_browser_message(hub, ws, worker_id, role, msg, owned_hijack)

                        task = asyncio.create_task(
                            _playback(
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
