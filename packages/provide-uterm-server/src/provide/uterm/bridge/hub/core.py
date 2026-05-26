#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TermHub: in-memory registry for terminal WebSocket connections."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

try:
    from fastapi import WebSocket
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for TermHub: pip install 'provide-uterm[websocket]'") from _e

from provide.uterm.bridge.hub.approvals import InMemoryApprovalStore
from provide.uterm.bridge.hub.connection import ConnectionManager
from provide.uterm.bridge.hub.connections import _ConnectionMixin
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
from provide.uterm.bridge.hub.messaging import HubMessagingMixin
from provide.uterm.bridge.hub.ownership import _HijackOwnershipMixin
from provide.uterm.bridge.hub.polling_service import PollingCoordinator
from provide.uterm.bridge.hub.presence import PresenceManager
from provide.uterm.bridge.hub.registry import WorkerRegistry
from provide.uterm.bridge.hub.resume import ResumeSession, ResumeTokenStore
from provide.uterm.bridge.hub.router import MessageRouter
from provide.uterm.bridge.hub.state import HubStateMixin
from provide.uterm.bridge.hub.store import StateStore
from provide.uterm.control_channel import encode_control, encode_data

if TYPE_CHECKING:
    from provide.uterm.bridge.hub.event_bus import EventBus
    from provide.uterm.bridge.identity import IdentityProvider
    from provide.uterm.bridge.models import WorkerTermState
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


class TermHub(
    HubMessagingMixin,
    HubStateMixin,
    _HijackOwnershipMixin,
    _ConnectionMixin,
):
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
