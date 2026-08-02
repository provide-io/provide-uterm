#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TermHub: in-memory registry for terminal WebSocket connections.

TermHub composes nine service classes — registry, limiter, approval_store,
lease, router, connection_mgr, presence_mgr, state, polling — and exposes
their methods as ``hub.<method>(...)`` for back-compat. See the package
docstring at :mod:`provide.uterm.server.bridge.hub` for the full service map.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

try:
    from fastapi import WebSocket  # noqa: TC002 — runtime import drives the friendly ImportError below
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for TermHub: pip install 'provide-uterm[websocket]'") from _e

import provide.uterm.server.bridge.hub.core_delegates_connection as _conn
import provide.uterm.server.bridge.hub.core_delegates_lease as _lease_d
import provide.uterm.server.bridge.hub.core_orchestration as _orch
from provide.uterm.server.bridge.hub.approvals import InMemoryApprovalStore
from provide.uterm.server.bridge.hub.connection import ConnectionManager
from provide.uterm.server.bridge.hub.event_bus import EventBus
from provide.uterm.server.bridge.hub.ext import (
    BehavioralAuditGate,
    BehavioralThresholds,
    NoOpBehavioralAuditGate,
    NoOpPolicyGate,
    OutputPolicyGate,
    PolicyDecision,
    PolicyGate,
    TelemetrySink,
)
from provide.uterm.server.bridge.hub.lease import HijackLeaseManager
from provide.uterm.server.bridge.hub.limiter import RateLimiter
from provide.uterm.server.bridge.hub.polling_service import PollingCoordinator
from provide.uterm.server.bridge.hub.presence import PresenceManager
from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.hub.router import MessageRouter
from provide.uterm.server.bridge.hub.store import StateStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import APIRouter

    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.server.bridge.frames import HijackStateFrame
    from provide.uterm.server.bridge.hub.core_helpers import (
        BrowserRoleResolver,
        HijackStateCallback,
        MetricCallback,
        ResumeCallback,
        WorkerEmptyCallback,
    )
    from provide.uterm.server.bridge.hub.ext import PolicyContext
    from provide.uterm.server.bridge.hub.resume import ResumeTokenStore
    from provide.uterm.server.bridge.identity import IdentityProvider
    from provide.uterm.server.bridge.models import HijackSession, WorkerTermState

    OnBrowserMessage = Callable[["TermHub", WebSocket, str, str, dict[str, Any], bool], Awaitable[bool]]


# Upper bound on how long a resuming socket waits for the *previous* socket's
# disconnect bookkeeping to commit (see ``TermHub.wait_resume_token_ready``).
# Mirrors the Go port's ``lifecycleOperationTimeout``
# (``hub/lifecycle_reservation.go``). The work being waited on is a single
# resume-store write performed under the hub lock, so five seconds is orders of
# magnitude of headroom; it also stays far below the resume-token TTL (300 s
# default) and the WS idle timeout, so a latch that somehow never gets released
# costs one slow resume rather than a hung socket.
RESUME_TOKEN_DETACH_TIMEOUT_S = 5.0


class TermHub:
    """In-memory registry for terminal WebSocket connections."""

    # Thin no-mixin delegators to the owning service (heavier bodies in the
    # ``core_delegates_*`` / ``core_orchestration`` / ``core_helpers`` siblings).
    # Lease/router hooks dispatch via ``self._hub.<method>`` so monkey-patched hub names still intercept.
    snapshot_matches = staticmethod(PollingCoordinator.snapshot_matches)

    async def wait_for_snapshot(self, worker_id: str, timeout_ms: int = 1500) -> dict[str, Any] | None:
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
        return await self.polling.wait_for_guard(
            worker_id,
            expect_prompt_id=expect_prompt_id,
            expect_regex=expect_regex,
            timeout_ms=timeout_ms,
            poll_interval_ms=poll_interval_ms,
        )

    @property
    def event_bus(self) -> EventBus | None:
        return self._event_bus

    @event_bus.setter
    def event_bus(self, value: EventBus | None) -> None:
        self._event_bus = value
        self._operation_event_bus = EventBus() if value is not None else None

    def _watch_authorized_operation_output(self, worker_id: str, *, max_queue_bytes: int | None = None) -> Any:
        """Open the private raw stream reserved for supervised operations.

        This stream is intentionally separate from :attr:`event_bus`, which
        backs diagnostic APIs, SSE, webhooks, and MCP tools and therefore only
        receives redacted content events. No route exposes this private stream.
        """
        if self._operation_event_bus is None:
            return None
        return self._operation_event_bus.watch(
            worker_id,
            event_types=["term", "snapshot"],
            max_queue_bytes=max_queue_bytes,
        )

    def _buffer_and_get_command(self, ws: WebSocket, data: str) -> str | None:
        return self.state.buffer_and_get_command(ws, data)

    async def shutdown(self) -> None:
        await self.state.shutdown()

    async def touch_activity(self, worker_id: str) -> None:
        await self.state.touch_activity(worker_id)

    def metric(self, name: str, value: int = 1) -> None:
        self.state.metric(name, value)

    clamp_lease = staticmethod(StateStore.clamp_lease)
    has_valid_rest_lease = staticmethod(StateStore.has_valid_rest_lease)
    is_dashboard_hijack_active = staticmethod(StateStore.is_dashboard_hijack_active)

    def is_hijacked(self, st: WorkerTermState) -> bool:
        return self.state.is_hijacked(st)

    async def _get(self, worker_id: str) -> WorkerTermState:
        return await self.state.get_or_create(worker_id)

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        self.state.notify_hijack_changed(worker_id, enabled=enabled, owner=owner)

    async def _resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        return await self.state.resolve_role_for_browser(ws, worker_id)

    async def prepare_policy_context(self, ws: WebSocket, worker_id: str, action: str | None = None) -> PolicyContext:
        return await self.state.prepare_policy_context(ws, worker_id, action)

    def _map_roles(self, principal: Any) -> frozenset[str]:
        return self.state._map_roles(principal)

    @staticmethod
    def _compute_lease_expirations(st: Any, now: float) -> tuple[bool, bool]:
        return HijackLeaseManager.compute_lease_expirations(st, now)

    async def _expire_leases_under_lock(self, worker_id: str, now: float) -> tuple[bool, bool, bool] | None:
        return await self.lease._expire_leases_under_lock(worker_id, now)

    async def _recheck_and_resume(self, worker_id: str, now: float) -> None:
        await self.lease._recheck_and_resume(worker_id, now)

    async def cleanup_expired_hijack(self, worker_id: str) -> bool:
        return await _lease_d.cleanup_expired_hijack(self, worker_id)  # ty:ignore[invalid-argument-type]

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> HijackSession | None:
        return await _lease_d.get_rest_session(self, worker_id, hijack_id)  # ty:ignore[invalid-argument-type]

    async def try_acquire_rest_hijack(
        self,
        worker_id: str,
        *,
        owner: str,
        lease_s: int,
        hijack_id: str,
        now: float,
    ) -> tuple[bool, str | None]:
        return await _lease_d.try_acquire_rest_hijack(
            self,  # ty:ignore[invalid-argument-type]
            worker_id,
            owner=owner,
            lease_s=lease_s,
            hijack_id=hijack_id,
            now=now,
        )

    async def try_acquire_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, str | None]:
        return await _lease_d.try_acquire_ws_hijack(self, worker_id, ws)  # ty:ignore[invalid-argument-type]

    async def touch_hijack_owner(self, worker_id: str, lease_s: int | None = None) -> float | None:
        return await self.lease.touch_owner(worker_id, lease_s)

    async def touch_if_owner(self, worker_id: str, ws: WebSocket) -> float | None:
        return await self.lease.touch_if_owner(worker_id, ws)

    async def try_release_ws_hijack(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        return await _lease_d.try_release_ws_hijack(self, worker_id, ws)  # ty:ignore[invalid-argument-type]

    async def extend_hijack_lease(
        self, worker_id: str, hijack_id: str, owner: str, lease_s: int, now: float
    ) -> float | None:
        return await self.lease.extend_lease(worker_id, hijack_id, owner, lease_s, now)

    async def get_fresh_hijack_expiry(self, worker_id: str, hijack_id: str, fallback: float) -> float:
        return await self.lease.get_fresh_expiry(worker_id, hijack_id, fallback)

    async def get_hijack_events_data(
        self,
        worker_id: str,
        hijack_id: str,
        hs: HijackSession,
        after_seq: int,
        limit: int,
    ) -> dict[str, Any]:
        return await self.lease.get_events_data(worker_id, hijack_id, hs, after_seq, limit)

    async def check_hijack_valid(self, worker_id: str, hijack_id: str) -> bool:
        return await self.lease.check_valid(worker_id, hijack_id)

    async def release_rest_hijack(self, worker_id: str, hijack_id: str) -> tuple[bool, bool]:
        return await self.lease.release_rest(worker_id, hijack_id)

    async def check_still_hijacked(self, worker_id: str) -> bool:
        return await self.lease.still_hijacked(worker_id)

    async def is_input_open_mode(self, worker_id: str) -> bool:
        return await self.lease.is_input_open_mode(worker_id)

    async def prepare_browser_input(self, worker_id: str, ws: WebSocket) -> bool:
        return await self.lease.prepare_browser_input(worker_id, ws)

    async def send_owned_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        browser_ws: WebSocket | None = None,
        rest_hijack_id: str | None = None,
        ownership_generation: int | None = None,
        source: Any = None,
    ) -> tuple[bool, str | None]:
        return await self.lease.send_owned_worker(
            worker_id,
            msg,
            browser_ws=browser_ws,
            rest_hijack_id=rest_hijack_id,
            ownership_generation=ownership_generation,
            source=source,
        )

    async def run_owned_browser_operation(
        self,
        worker_id: str,
        operation: Any,
        *,
        browser_ws: WebSocket,
        ownership_generation: int,
        source: Any = None,
    ) -> tuple[tuple[bool, str | None] | None, str | None]:
        return await self.lease.run_owned_browser_operation(
            worker_id,
            operation,
            browser_ws=browser_ws,
            ownership_generation=ownership_generation,
            source=source,
        )

    async def capture_browser_ownership(self, worker_id: str, ws: WebSocket) -> int | None:
        return await self.lease.capture_browser_ownership(worker_id, ws)

    async def capture_dashboard_ownership(self, worker_id: str, ws: WebSocket) -> int | None:
        return await self.lease.capture_dashboard_ownership(worker_id, ws)

    async def send_worker_if_unowned(self, worker_id: str, msg: dict[str, Any]) -> bool:
        return await self.lease.send_worker_if_unowned(worker_id, msg)

    def allow_rest_acquire_for(self, client_id: str) -> bool:
        return self.connection_mgr.allow_rest_acquire_for(client_id)

    def allow_rest_send_for(self, client_id: str) -> bool:
        return self.connection_mgr.allow_rest_send_for(client_id)

    def worker_token(self) -> str | None:
        return self.connection_mgr.worker_token()

    async def register_worker(self, worker_id: str, ws: WebSocket, *, is_tunnel_worker: bool = False) -> bool:
        return await _conn.register_worker(  # ty:ignore[invalid-argument-type]
            self, worker_id, ws, is_tunnel_worker=is_tunnel_worker
        )

    async def is_active_worker(self, worker_id: str, ws: WebSocket) -> bool:
        return await self.connection_mgr.is_active_worker(worker_id, ws)

    async def set_worker_tunnel_flag(self, worker_id: str, value: bool) -> None:
        await self.connection_mgr.set_worker_tunnel_flag(worker_id, value)

    async def set_worker_hello(self, worker_id: str, mode: InputMode, protocol_version: int | None = None) -> bool:
        return await self.connection_mgr.set_worker_hello(worker_id, mode, protocol_version)

    async def update_last_snapshot(self, worker_id: str, snapshot: dict[str, Any]) -> None:
        await self.connection_mgr.update_last_snapshot(worker_id, snapshot)

    async def register_browser(
        self, worker_id: str, ws: WebSocket, role: str, *, defer_broadcast: bool = False
    ) -> dict[str, Any]:
        return await _conn.register_browser(self, worker_id, ws, role, defer_broadcast=defer_broadcast)  # ty:ignore[invalid-argument-type]

    async def activate_browser_broadcasts(self, worker_id: str, ws: WebSocket) -> None:
        await self.connection_mgr.activate_browser_broadcasts(worker_id, ws)

    async def register_browser_state_snapshot(self, worker_id: str, ws: WebSocket) -> dict[str, Any]:
        return await self.presence_mgr.register_browser_state_snapshot(worker_id, ws)

    async def resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        return await self.presence_mgr.resolve_role_for_browser(ws, worker_id)

    def can_send_input(self, st: WorkerTermState, ws: WebSocket) -> bool:
        return self.presence_mgr.can_send_input(st, ws)

    async def request_snapshot(self, worker_id: str) -> None:
        await self.presence_mgr.request_snapshot(worker_id)

    async def request_analysis(self, worker_id: str) -> None:
        await self.presence_mgr.request_analysis(worker_id)

    async def force_release_hijack(self, worker_id: str) -> bool:
        return await self.connection_mgr.force_release_hijack(worker_id)

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.router.append_event(worker_id, event_type, data)

    async def commit_snapshot_event(
        self,
        worker_id: str,
        snapshot: dict[str, Any],
        *,
        expected_worker: WebSocket | None = None,
    ) -> dict[str, Any] | None:
        return await self.router.commit_snapshot_event(worker_id, snapshot, expected_worker=expected_worker)

    async def broadcast(self, worker_id: str, msg: dict[str, Any]) -> None:
        await self.router.broadcast(worker_id, msg)

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        await self.router.broadcast_hijack_state(worker_id)

    async def send_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        source: Any = None,
        expected_worker: WebSocket | None = None,
    ) -> bool:
        return await self.router.send_worker(worker_id, msg, source=source, expected_worker=expected_worker)

    async def hijack_state_msg_for(self, worker_id: str, ws: WebSocket) -> HijackStateFrame:
        return await self.router.hijack_state_msg_for(worker_id, ws)

    async def set_input_mode(self, worker_id: str, mode: InputMode) -> tuple[bool, str | None]:
        return await self.router.set_input_mode(worker_id, mode)

    async def disconnect_worker(self, worker_id: str) -> bool:
        return await self.connection_mgr.disconnect_worker(worker_id)

    async def prune_if_idle(self, worker_id: str) -> None:
        await self.router.prune_if_idle(worker_id)

    async def get_idle_candidates(self, timeout_s: float) -> list[tuple[str, float]]:
        return await self.router.get_idle_candidates(timeout_s)

    async def set_browser_role(self, worker_id: str, ws: WebSocket, role: str) -> None:
        await self.router.set_browser_role(worker_id, ws, role)

    async def try_reclaim_hijack(self, worker_id: str, ws: WebSocket) -> bool:
        return await self.router.try_reclaim_hijack(worker_id, ws)

    async def try_reclaim_hijack_status(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        return await self.router.try_reclaim_hijack_status(worker_id, ws)

    async def get_worker_browser_role(self, worker_id: str, ws: WebSocket) -> str | None:
        return await self.router.get_worker_browser_role(worker_id, ws)

    async def get_last_snapshot(self, worker_id: str, recipient: Any = None) -> dict[str, Any] | None:
        # *recipient* set ⇒ role-scoped redaction (M5); see MessageRouter.get_last_snapshot.
        return await self.router.get_last_snapshot(worker_id, recipient)

    async def redact_snapshot_for_recipient(
        self, worker_id: str, snapshot: dict[str, Any], recipient: Any
    ) -> dict[str, Any]:
        return await self.router.redact_snapshot_for_recipient(worker_id, snapshot, recipient)

    async def browser_count(self, worker_id: str) -> int:
        return await self.router.browser_count(worker_id)

    async def browser_count_total(self) -> int:
        return await self.router.browser_count_total()

    async def get_recent_events(self, worker_id: str, limit: int) -> list[dict[str, Any]]:
        return await self.router.get_recent_events(worker_id, limit)

    def _record_keystroke(self, source: Any) -> None:
        self.router.record_keystroke(source)

    def _get_heuristics(self, source: Any) -> dict[str, float]:
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
        await self.router.audit_all_browsers()

    async def _run_behavioral_audit_loop(self) -> None:
        await self.router.run_behavioral_audit_loop()

    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]:
        return await _conn.cleanup_browser_disconnect(self, worker_id, ws, owned_hijack)  # ty:ignore[invalid-argument-type]

    async def remove_dead_browsers(self, worker_id: str, dead: set[WebSocket]) -> bool:
        return await _conn.remove_dead_browsers(self, worker_id, dead)  # ty:ignore[invalid-argument-type]

    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        return await _conn.deregister_worker(self, worker_id, ws)  # ty:ignore[invalid-argument-type]

    @property
    def resume_store(self) -> ResumeTokenStore | None:
        return self._resume_store

    # -- Resume-token detach latches ------------------------------------
    #
    # A resume token is bound to exactly one browser socket. Its ownership flag
    # (``was_hijack_owner``) is written by that socket's *disconnect*
    # bookkeeping, which runs on the socket's own task — a promptly reconnecting
    # browser could therefore read the flag before it was written and silently
    # skip its lease reclaim. Each bound token gets an ``asyncio.Event`` latch,
    # armed under ``_lock`` when the token is bound and set under ``_lock``
    # *after* the disconnect bookkeeping has committed, so a resume can order
    # itself behind the socket it is replacing. Port of ``resumeTokenDetached``
    # in the Go hub (``hub/lifecycle_reservation.go``).

    def _bind_resume_token_locked(self, ws: WebSocket, token: str) -> None:
        """Bind *token* to *ws* and arm its detach latch.

        The caller must hold ``self._lock``. Any token previously bound to *ws*
        is superseded, so its latch is released here — otherwise a socket that
        resumes into a replacement token would strand the connect-time token's
        latch (and any waiter on it) until the process exits.
        """
        self._detach_resume_token_locked(self._ws_to_resume_token.get(ws))
        self._ws_to_resume_token[ws] = token
        self._resume_token_detached.setdefault(token, asyncio.Event())

    def _detach_resume_token_locked(self, token: str | None) -> None:
        """Release *token*'s detach latch, if it has one.

        The caller must hold ``self._lock``. Tolerates ``None`` (nothing was
        bound) and an unknown token (already detached), mirroring the Go port's
        ``detachResumeTokenLocked``.
        """
        if token is None:
            return
        latch = self._resume_token_detached.pop(token, None)
        if latch is not None:
            latch.set()

    async def wait_resume_token_ready(self, token: str, ws: WebSocket) -> bool:
        """Wait until *token* is safe for *ws* to resume with.

        Returns immediately when *token* is still bound to *ws* itself (nothing
        to order against). Otherwise waits for the socket that held it to finish
        its disconnect bookkeeping, so a subsequent ``resume_store.get(token)``
        observes the committed ``was_hijack_owner`` flag.

        Returns ``True`` when the token is ready and ``False`` when the wait hit
        :data:`RESUME_TOKEN_DETACH_TIMEOUT_S`. The bound is a liveness guard
        only: callers proceed either way (with whatever the store says), so a
        lost latch degrades to the pre-latch race instead of hanging the socket.
        """
        async with self._lock:
            if self._ws_to_resume_token.get(ws) == token:
                return True
            latch = self._resume_token_detached.get(token)
        if latch is None:
            return True
        try:
            await asyncio.wait_for(latch.wait(), RESUME_TOKEN_DETACH_TIMEOUT_S)
        except TimeoutError:
            return False
        return True

    def create_router(self, *, extra_route_registrars: list[Any] | None = None) -> APIRouter:
        return _orch.create_router(self, extra_route_registrars=extra_route_registrars)  # ty:ignore[invalid-argument-type]

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
        # Non-input control frames get a smaller separate anti-flood budget.
        browser_control_rate_limit_per_sec: float = 10,
        rest_acquire_rate_limit_per_sec: float = 5,
        rest_send_rate_limit_per_sec: float = 20,
        worker_token: str | None = None,
        worker_frame_on_invalid: Literal["drop", "reject"] = "drop",
        event_deque_maxlen: int = 2000,
        resume_store: ResumeTokenStore | None = None,
        resume_ttl_s: float = 300,
        on_resume: ResumeCallback | None = None,
        allow_stale_owner_role_resume: bool = False,
        event_bus: EventBus | None = None,
        ws_idle_timeout_s: float = 14400.0,
        policy_gate: PolicyGate | None = None,
        identity_provider: IdentityProvider | None = None,
        delegate_roles: bool = True,
        output_policy_gate: OutputPolicyGate | None = None,
        behavioral_audit_gate: BehavioralAuditGate | None = None,
        behavioral_thresholds: BehavioralThresholds | None = None,
        behavioral_audit_interval_s: float = 30.0,
        telemetry_sink: TelemetrySink | None = None,
        max_buffer_chars: int = 40_000,
        max_event_data_chars: int = 8192,
        max_connections_per_principal: int = 25,
        max_workers: int = 10000,
    ) -> None:
        self._lock = asyncio.Lock()
        # Service objects own the impl; ``_workers`` / ``_rest_*`` / … stay as property shims.
        self.registry = WorkerRegistry()
        self._on_hijack_changed = on_hijack_changed
        self._on_metric = on_metric
        self._resolve_browser_role = resolve_browser_role
        self.on_worker_empty: WorkerEmptyCallback | None = on_worker_empty
        self._worker_token = worker_token
        # Finding #5d: malformed-worker-frame policy — ``"drop"`` vs ``"reject"`` (WS close 1003); see websockets_impl.py.
        self.worker_frame_on_invalid: Literal["drop", "reject"] = worker_frame_on_invalid
        self.lease = HijackLeaseManager(
            registry=self.registry,
            lock=self._lock,
            dashboard_hijack_lease_s=int(dashboard_hijack_lease_s),
            hub=self,
        )
        self.max_ws_message_bytes = max(1024, int(max_ws_message_bytes))
        self.max_input_chars = max(100, int(max_input_chars))
        self.max_buffer_chars = max(self.max_input_chars, int(max_buffer_chars))
        self.max_event_data_chars = max(256, int(max_event_data_chars))
        self.browser_rate_limit_per_sec = float(browser_rate_limit_per_sec)
        self.browser_control_rate_limit_per_sec = max(0.1, float(browser_control_rate_limit_per_sec))
        self.limiter = RateLimiter(
            rest_acquire_rate=float(rest_acquire_rate_limit_per_sec),
            rest_send_rate=float(rest_send_rate_limit_per_sec),
        )
        self._event_deque_maxlen = max(1, int(event_deque_maxlen))
        self._resume_store = resume_store
        self._resume_ttl_s = max(1.0, float(resume_ttl_s))
        self._on_resume = on_resume
        self.allow_stale_owner_role_resume = bool(allow_stale_owner_role_resume)
        self._ws_to_resume_token: dict[WebSocket, str] = {}
        # token -> latch set once the socket holding it has committed its
        # disconnect bookkeeping (see _bind_resume_token_locked).
        self._resume_token_detached: dict[str, asyncio.Event] = {}
        self._startup_pending_browsers: set[WebSocket] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._event_bus = event_bus
        # Raw terminal content is available only to server-owned supervised
        # operations. Diagnostic/untrusted EventBus consumers continue to see
        # the write-time-redacted ring payloads.
        self._operation_event_bus = EventBus() if event_bus is not None else None
        self.ws_idle_timeout_s = max(10.0, float(ws_idle_timeout_s))
        self._policy_gate = policy_gate or NoOpPolicyGate()
        self._input_buffers: dict[WebSocket, str] = {}
        self._hold_buffers: dict[WebSocket, str] = {}
        self.approval_store = InMemoryApprovalStore()
        self.approval_store.subscribe_expired(self._handle_expired_approval)
        self._paused_browsers: set[WebSocket] = set()
        self._on_browser_message: OnBrowserMessage | None = None
        self._identity_provider = identity_provider
        self._delegate_roles = delegate_roles
        self._output_policy_gate = output_policy_gate
        self._behavioral_audit_gate = behavioral_audit_gate or NoOpBehavioralAuditGate()
        self._behavioral_thresholds = behavioral_thresholds or BehavioralThresholds()
        self._behavioral_audit_interval_s = max(1.0, float(behavioral_audit_interval_s))
        self._telemetry_sink: TelemetrySink | None = telemetry_sink
        # Per-principal browser quota (concrete principals) + global worker_id cap (bounds OOM; reconnects exempt).
        self.max_connections_per_principal = max(1, int(max_connections_per_principal))
        self.max_workers = max(1, int(max_workers))
        self._principal_browser_counts: dict[str, int] = {}
        self._ws_principal: dict[Any, str] = {}  # WebSocket → principal subject_id (disconnect decrement)
        self.router = MessageRouter(self)  # ty:ignore[invalid-argument-type]  # built first: state/polling/connection reuse it
        self.state = StateStore(self)  # ty:ignore[invalid-argument-type]
        self.polling = PollingCoordinator(self)  # ty:ignore[invalid-argument-type]
        self.connection_mgr = ConnectionManager(self)  # ty:ignore[invalid-argument-type]
        self.presence_mgr = PresenceManager(self)

        if not isinstance(self._behavioral_audit_gate, NoOpBehavioralAuditGate):
            audit_task = asyncio.create_task(self._run_behavioral_audit_loop())
            self._background_tasks.add(audit_task)
            audit_task.add_done_callback(self._background_tasks.discard)

    @property
    def identity_provider(self) -> IdentityProvider | None:
        return self._identity_provider

    async def set_worker_hello_mode(self, worker_id: str, mode: str) -> bool:
        return await _orch.set_worker_hello_mode(self, worker_id, mode)  # ty:ignore[invalid-argument-type]

    async def emit_telemetry(
        self,
        event_type: str,
        *,
        worker_id: str,
        principal: str | None = None,
        role: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await _orch.emit_telemetry(
            self,  # ty:ignore[invalid-argument-type]
            event_type,
            worker_id=worker_id,
            principal=principal,
            role=role,
            metadata=metadata,
        )

    # Approval flow -- orchestration body in ``core_orchestration.resolve_approval``.

    async def resolve_approval(
        self,
        worker_id: str,
        request_id: str,
        decision: PolicyDecision,
        command: str,
        *,
        approval_request: Any | None = None,
    ) -> tuple[bool, str | None]:
        return await _orch.resolve_approval(  # ty:ignore[invalid-argument-type]
            self,
            worker_id,
            request_id,
            decision,
            command,
            approval_request=approval_request,
        )

    async def _handle_expired_approval(self, request: Any) -> None:
        await _orch.handle_expired_approval(self, request)  # ty:ignore[invalid-argument-type]
