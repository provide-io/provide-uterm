#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from provide.terminal.bridge.hub.core import (
    BrowserRoleResolutionError,
    BrowserRoleResolver,
    HijackStateCallback,
    ResumeCallback,
    TermHub,
)
from provide.terminal.bridge.hub.event_bus import EventBus
from provide.terminal.bridge.hub.ext import (
    NoOpPolicyGate,
    PolicyContext,
    PolicyDecision,
    PolicyGate,
)
from provide.terminal.bridge.hub.resume import (
    ControlPlaneResumeStore,
    InMemoryResumeStore,
    ResumeSession,
    ResumeTokenStore,
)

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.terminal.bridge.models import WorkerTermState


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
    async def register_worker(self, worker_id: str, ws: WebSocket) -> bool: ...
    async def is_active_worker(self, worker_id: str, ws: WebSocket) -> bool: ...
    async def set_worker_hello_mode(self, worker_id: str, mode: str) -> bool: ...
    async def update_last_snapshot(self, worker_id: str, snapshot: dict[str, Any]) -> None: ...
    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]: ...
    async def register_browser(self, worker_id: str, ws: WebSocket, role: str) -> dict[str, Any]: ...
    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]: ...
    async def resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str: ...

    # -- Messaging & broadcast -------------------------------------------------
    async def send_worker(self, worker_id: str, msg: dict[str, Any], *, source: Any = None) -> bool: ...
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
