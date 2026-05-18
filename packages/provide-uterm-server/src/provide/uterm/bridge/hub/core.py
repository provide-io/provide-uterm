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

from provide.uterm.bridge.hub.approvalflow import HubApprovalFlowMixin
from provide.uterm.bridge.hub.approvals import InMemoryApprovalStore
from provide.uterm.bridge.hub.connections import _ConnectionMixin
from provide.uterm.bridge.hub.ext import (
    BehavioralAuditGate,
    BehavioralThresholds,
    NoOpBehavioralAuditGate,
    NoOpPolicyGate,
    OutputPolicyGate,
    PolicyGate,
)
from provide.uterm.bridge.hub.messaging import HubMessagingMixin
from provide.uterm.bridge.hub.ownership import _HijackOwnershipMixin
from provide.uterm.bridge.hub.polling import _PollingMixin
from provide.uterm.bridge.hub.resume import ResumeSession, ResumeTokenStore
from provide.uterm.bridge.hub.state import HubStateMixin
from provide.uterm.bridge.ratelimit import TokenBucket
from provide.uterm.control_channel import encode_control, encode_data

if TYPE_CHECKING:
    from provide.uterm.bridge.hub.event_bus import EventBus
    from provide.uterm.bridge.identity import IdentityProvider
    from provide.uterm.bridge.models import WorkerTermState

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
    HubApprovalFlowMixin,
    HubMessagingMixin,
    HubStateMixin,
    _PollingMixin,
    _HijackOwnershipMixin,
    _ConnectionMixin,
):
    """In-memory registry for terminal WebSocket connections."""

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
        self._rest_acquire_per_client: dict[str, TokenBucket] = {}
        self._rest_send_per_client: dict[str, TokenBucket] = {}
        self._event_deque_maxlen = max(1, int(event_deque_maxlen))
        self._resume_store = resume_store
        self._resume_ttl_s = max(1.0, float(resume_ttl_s))
        self._on_resume = on_resume
        self._ws_to_resume_token: dict[WebSocket, str] = {}
        self._startup_pending_browsers: set[WebSocket] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._event_bus = event_bus
        self.ws_idle_timeout_s = max(10.0, float(ws_idle_timeout_s))
        self._keystroke_timestamps: dict[Any, Any] = {}
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

        if not isinstance(self._behavioral_audit_gate, NoOpBehavioralAuditGate):
            audit_task = asyncio.create_task(self._run_behavioral_audit_loop())
            self._background_tasks.add(audit_task)
            audit_task.add_done_callback(self._background_tasks.discard)

    @property
    def identity_provider(self) -> IdentityProvider | None:
        """Public accessor for the configured identity provider."""
        return self._identity_provider

    async def set_worker_hello_mode(self, worker_id: str, mode: str) -> bool:
        """Backward-compatible wrapper for worker hello mode handling."""
        # Narrow the str arg to InputMode at the wrapper boundary; reject
        # unknown values so the cast on the next line is sound.
        if mode not in ("hijack", "open"):
            raise ValueError(f"invalid input mode: {mode!r}")
        return await self.set_worker_hello(worker_id, mode)  # type: ignore[arg-type]
