#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Thin compatibility facade over :class:`StateStore`.

Phase 7 of refactor #16 extracted the bulk of the original
``HubStateMixin`` into :class:`provide.uterm.bridge.hub.store.StateStore`.
The mixin remains because the existing TermHub composes its sibling
mixins via multiple inheritance and the following methods are still
addressed as ``hub.<name>(...)`` from route handlers, REST helpers,
tests and the other mixins:

* ``shutdown`` — graceful background-task teardown.
* ``touch_activity`` — per-frame worker heartbeat (called from the
  worker-WS reader hot path).
* ``metric`` — metric callback fan-out.
* ``clamp_lease`` / ``has_valid_rest_lease`` /
  ``is_dashboard_hijack_active`` / ``is_hijacked`` — small pure
  predicates used by the lease manager and the messaging router.
* ``_get`` — back-compat helper that returns or creates a
  :class:`WorkerTermState`; preserved on the mixin because the
  approval-flow mixin and the test suite both reference it directly.
* ``notify_hijack_changed`` — invoked from the connection mixin and
  the lease manager when a hijack lifecycle event needs to fire the
  configured callback. **Tests monkey-patch this on the hub instance;
  the shim keeps the dispatch path going through ``self.<name>``.**
* ``prepare_policy_context`` — used by route handlers and the
  approval flow; called as ``hub.prepare_policy_context(...)``.
* ``_resolve_role_for_browser`` — private hook called by the
  connection mixin's browser-registration path; tests still invoke
  it directly to drive resolver branches.
* ``_buffer_and_get_command`` — invoked by tests directly; the route
  layer uses the same buffer dict via this helper.
* ``event_bus`` property + setter — exposed for tests and app wiring.

All forwarded methods are one-line pass-throughs to ``self.state`` (the
composed :class:`StateStore`); the type-only attribute declarations
match the type-only shape of the other mixin facades in this package.

Lock semantics are intentionally preserved verbatim from the
pre-extraction implementation: the store uses the *hub's*
``asyncio.Lock`` (accessed via its back reference) so concurrent state
mutations keep serialising against the same object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.bridge.hub.store import StateStore

if TYPE_CHECKING:
    import asyncio

    from fastapi import WebSocket

    from provide.uterm.bridge.hub.event_bus import EventBus
    from provide.uterm.bridge.hub.ext import PolicyContext
    from provide.uterm.bridge.models import WorkerTermState

logger = get_logger(__name__)


class HubStateMixin:
    """Compatibility facade that forwards state queries to :attr:`TermHub.state`.

    The store (:class:`StateStore`) owns the actual implementation.
    This mixin exists only so the legacy ``hub.<method>`` call sites —
    plus the test-only monkey-patch pattern for ``notify_hijack_changed``
    — keep working unchanged.
    """

    # Shared state (initialised in TermHub.__init__).
    _lock: asyncio.Lock
    _workers: dict[str, Any]
    _input_buffers: dict[Any, str]
    _background_tasks: set[Any]
    _event_bus: EventBus | None
    _on_metric: Any | None
    _on_hijack_changed: Any | None
    _resolve_browser_role: Any | None
    _identity_provider: Any | None
    _delegate_roles: bool
    state: StateStore

    # -- Event-bus accessor ---------------------------------------------

    @property
    def event_bus(self) -> EventBus | None:
        """Public accessor for the EventBus instance (None if not configured)."""
        return self._event_bus

    @event_bus.setter
    def event_bus(self, value: EventBus | None) -> None:
        """Backward-compatible setter used by tests and app wiring."""
        self._event_bus = value

    # -- Forwarded helpers ----------------------------------------------

    def _buffer_and_get_command(self, ws: WebSocket, data: str) -> str | None:
        """Accumulate input for *ws* and return the command if a newline is received."""
        return self.state.buffer_and_get_command(ws, data)

    async def shutdown(self) -> None:
        """Cancel all background tasks for graceful shutdown."""
        await self.state.shutdown()

    async def touch_activity(self, worker_id: str) -> None:
        """Update the last-activity timestamp for *worker_id*."""
        await self.state.touch_activity(worker_id)

    def metric(self, name: str, value: int = 1) -> None:
        """Emit a named metric via the configured on_metric callback."""
        self.state.metric(name, value)

    # -- Static helpers re-exposed via the store -------------------------
    # ``staticmethod`` wrappers around the canonical implementations on
    # :class:`StateStore` so legacy ``hub.clamp_lease(...)`` /
    # ``TermHub.is_dashboard_hijack_active(st)`` call sites keep working.

    clamp_lease = staticmethod(StateStore.clamp_lease)
    has_valid_rest_lease = staticmethod(StateStore.has_valid_rest_lease)
    is_dashboard_hijack_active = staticmethod(StateStore.is_dashboard_hijack_active)

    def is_hijacked(self, st: WorkerTermState) -> bool:
        """Return True if *st* is under any active hijack (dashboard WS or REST)."""
        return self.state.is_hijacked(st)

    async def _get(self, worker_id: str) -> WorkerTermState:
        """Return the existing :class:`WorkerTermState` for *worker_id* or create one."""
        return await self.state.get_or_create(worker_id)

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        """Fire the on_hijack_changed callback (sync or async) without blocking.

        Implemented inline (via the back-reference) so the test-only
        pattern of monkey-patching ``hub.notify_hijack_changed`` keeps
        working — the rest of the hub dispatches through ``self.<name>``
        so instance-level overrides are honored.
        """
        self.state.notify_hijack_changed(worker_id, enabled=enabled, owner=owner)

    async def _resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        """Resolve a browser role via the configured callback; defaults to "viewer"."""
        return await self.state.resolve_role_for_browser(ws, worker_id)

    async def prepare_policy_context(self, ws: WebSocket, worker_id: str, action: str | None = None) -> PolicyContext:
        """Create a :class:`PolicyContext` for the given browser WebSocket and worker."""
        return await self.state.prepare_policy_context(ws, worker_id, action)

    def _map_roles(self, principal: Any) -> frozenset[str]:
        """Map an identity-provider principal to a frozen set of hub roles."""
        return self.state._map_roles(principal)


__all__ = ["HubStateMixin"]
