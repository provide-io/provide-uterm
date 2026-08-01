#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TermHub package — bridge hub registry plus its composed service classes.

Phase 7 of refactor #16 finished the migration from six "Hub*Mixin"
parents on :class:`TermHub` to a single class composing nine service
attributes. Every public ``hub.<method>(...)`` call site is preserved:
the methods live on :class:`TermHub` directly and forward to the
appropriate service. The map of services owned by every ``TermHub``
instance:

* ``hub.registry`` — :class:`WorkerRegistry` (``registry.py``). The
  worker-id → :class:`WorkerTermState` map; owns ``add``/``get``/
  ``remove``/``iter_workers``.
* ``hub.limiter`` — :class:`RateLimiter` (``limiter.py``). The global
  + per-client REST acquire / send token buckets and the LRU eviction
  cache.
* ``hub.approval_store`` — :class:`InMemoryApprovalStore`
  (``approvals.py``). Pending and resolved approval requests; the
  approval-flow orchestration lives directly on :class:`TermHub`.
* ``hub.lease`` — :class:`HijackLeaseManager` (``lease.py``). The
  hijack lease state machine (REST + dashboard WS), expiry sweeps,
  resume control frames.
* ``hub.router`` — :class:`MessageRouter` (``router.py``). The
  broadcast / send_worker hot path plus the behavioral-heuristics ring
  buffer.
* ``hub.connection_mgr`` — :class:`ConnectionManager`
  (``connection.py``). Worker/browser register/deregister, REST
  rate-limit gates, force_release_hijack lifecycle. The hijack-clearing
  method bodies (``disconnect_worker`` / ``_event_bus_close`` /
  ``force_release_hijack``) live in ``connection_hijack.py``.
* ``hub.presence_mgr`` — :class:`PresenceManager` (``presence.py``).
  Read-only browser presence queries (``can_send_input``, role
  resolution) and worker-bound presence control frames
  (``request_snapshot``, ``request_analysis``).
* ``hub.state`` — :class:`StateStore` (``store.py``). Worker
  heartbeats, per-browser input buffer, hijack-state predicates,
  metric / on_hijack_changed callback fan-out, browser-role resolution,
  policy-context plumbing.
* ``hub.polling`` — :class:`PollingCoordinator`
  (``polling_service.py``). Snapshot polling helpers
  (``snapshot_matches``, ``wait_for_snapshot``, ``wait_for_guard``).

:class:`TermHub` itself has **no mixin parents** as of Phase 7b. The
legacy ``connections.py`` module survives only as a back-compat
re-export shim for ``_REST_CLIENT_CACHE_MAX``,
``_REST_CLIENT_EVICT_COUNT`` and ``shutdown_background_tasks``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from provide.uterm.server.bridge.hub.core import (
    BrowserRoleResolutionError,
    BrowserRoleResolver,
    HijackStateCallback,
    ResumeCallback,
    TermHub,
)
from provide.uterm.server.bridge.hub.event_bus import EventBus
from provide.uterm.server.bridge.hub.ext import (
    NoOpPolicyGate,
    PolicyContext,
    PolicyDecision,
    PolicyGate,
)
from provide.uterm.server.bridge.hub.resume import (
    ControlPlaneResumeStore,
    InMemoryResumeStore,
    ResumeSession,
    ResumeTokenStore,
)

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.models import WorkerTermState


@runtime_checkable
class TermHubProtocol(Protocol):
    """Structural protocol for TermHub — use for type hints and testing mocks.

    Code that depends on TermHub should annotate parameters as
    ``TermHubProtocol`` instead of ``TermHub`` to allow injection of
    alternative implementations (stubs, mocks, decorators).
    """

    # -- Core state management -------------------------------------------------
    max_ws_message_bytes: int
    max_input_chars: int
    browser_rate_limit_per_sec: float
    # Finding #5d: malformed-worker-frame policy ("drop" | "reject").
    worker_frame_on_invalid: str

    @property
    def event_bus(self) -> EventBus | None: ...

    def metric(self, name: str, value: int = ...) -> None: ...
    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = ...) -> None: ...
    def worker_token(self) -> str | None: ...
    def allow_rest_acquire_for(self, client_id: str) -> bool:
        """Return True if *client_id* passes both the global and per-client acquire rate limit."""
        ...

    def allow_rest_send_for(self, client_id: str) -> bool:
        """Return True if *client_id* passes both the global and per-client send/step rate limit."""
        ...

    def can_send_input(self, st: WorkerTermState, ws: WebSocket) -> bool: ...

    # -- Worker / browser connection lifecycle ---------------------------------
    async def register_worker(self, worker_id: str, ws: WebSocket, *, is_tunnel_worker: bool = ...) -> bool: ...
    async def is_active_worker(self, worker_id: str, ws: WebSocket) -> bool: ...
    async def set_worker_hello_mode(self, worker_id: str, mode: str) -> bool: ...
    async def update_last_snapshot(self, worker_id: str, snapshot: dict[str, Any]) -> None: ...
    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]: ...
    async def register_browser(
        self, worker_id: str, ws: WebSocket, role: str, *, defer_broadcast: bool = ...
    ) -> dict[str, Any]: ...
    async def activate_browser_broadcasts(self, worker_id: str, ws: WebSocket) -> None: ...
    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]: ...
    async def resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str: ...

    # -- Messaging & broadcast -------------------------------------------------
    async def send_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        source: Any = None,
        expected_worker: WebSocket | None = None,
    ) -> bool: ...
    async def send_worker_if_unowned(self, worker_id: str, msg: dict[str, Any]) -> bool: ...
    async def capture_browser_ownership(self, worker_id: str, ws: WebSocket) -> int | None: ...
    async def capture_dashboard_ownership(self, worker_id: str, ws: WebSocket) -> int | None: ...
    async def try_reclaim_hijack_status(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]: ...
    async def broadcast(self, worker_id: str, msg: dict[str, Any]) -> None: ...
    async def broadcast_hijack_state(self, worker_id: str) -> None: ...
    async def hijack_state_msg_for(self, worker_id: str, ws: WebSocket) -> dict[str, Any]: ...
    async def append_event(
        self, worker_id: str, event_type: str, data: dict[str, Any] | None = ...
    ) -> dict[str, Any]: ...
    async def request_snapshot(self, worker_id: str) -> None: ...
    async def request_analysis(self, worker_id: str) -> None: ...

    # -- Snapshots & events ----------------------------------------------------
    async def wait_for_snapshot(self, worker_id: str, timeout_ms: int = ...) -> dict[str, Any] | None: ...
    async def get_last_snapshot(self, worker_id: str) -> dict[str, Any] | None: ...
    async def get_recent_events(self, worker_id: str, limit: int) -> list[dict[str, Any]]: ...
    async def browser_count(self, worker_id: str) -> int: ...
    async def browser_count_total(self) -> int: ...

    # -- Hijack state ----------------------------------------------------------
    async def cleanup_expired_hijack(self, worker_id: str) -> bool: ...
    async def check_still_hijacked(self, worker_id: str) -> bool: ...
    async def force_release_hijack(self, worker_id: str) -> bool: ...
    async def prune_if_idle(self, worker_id: str) -> None: ...
    async def disconnect_worker(self, worker_id: str) -> bool: ...
    async def set_input_mode(self, worker_id: str, mode: str) -> tuple[bool, str | None]: ...


__all__ = [
    "BrowserRoleResolutionError",
    "BrowserRoleResolver",
    "EventBus",
    "HijackStateCallback",
    "ControlPlaneResumeStore",
    "InMemoryResumeStore",
    "NoOpPolicyGate",
    "PolicyContext",
    "PolicyGate",
    "ResumeCallback",
    "ResumeSession",
    "ResumeTokenStore",
    "TermHub",
    "TermHubProtocol",
]
