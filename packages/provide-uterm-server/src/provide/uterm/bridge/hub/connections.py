#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Thin compatibility facade over :class:`ConnectionManager` + :class:`PresenceManager`.

This mixin used to own all the hub's worker/browser connection lifecycle
plumbing. Phase 6 of refactor #16 extracted the bulk of the logic into
two service classes:

* :class:`provide.uterm.bridge.hub.connection.ConnectionManager` — owns
  worker register/deregister, browser register/cleanup, REST rate-limit
  gates, and the ``force_release_hijack`` lifecycle path.
* :class:`provide.uterm.bridge.hub.presence.PresenceManager` — owns the
  read-only browser presence queries (``can_send_input``, role
  resolution, browser-state snapshot) and worker-bound presence-driven
  control frames (``request_snapshot`` / ``request_analysis``).

The mixin remains because the existing TermHub composes its sibling
mixins via multiple inheritance and a handful of methods participate in
cooperative MRO chains (``cleanup_browser_disconnect``,
``deregister_worker``, ``remove_dead_browsers`` on the messaging mixin).
The non-cooperative methods now forward straight to ``self.connection_mgr``
or ``self.presence_mgr`` — one attribute lookup + function-call per
delegation, which is negligible compared to the existing
``async with self._lock`` + ``ws.send_text`` overhead on these paths.

The legacy module-level ``_REST_CLIENT_CACHE_MAX`` /
``_REST_CLIENT_EVICT_COUNT`` re-exports stay in place so existing
imports from ``provide.uterm.bridge.hub.connections`` keep working;
canonical definitions live in :mod:`provide.uterm.bridge.hub.limiter`.

``request_snapshot`` and ``force_release_hijack`` are implemented inline
(rather than via the service back-reference) so the test-only pattern
of monkey-patching them on a hub instance keeps working — the WS routes
and ``provide-uterm-server``'s session registry both dispatch through
``self.<name>`` so instance-level overrides are honored.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.bridge.hub.limiter import (
    REST_CLIENT_CACHE_MAX as _REST_CLIENT_CACHE_MAX,
)
from provide.uterm.bridge.hub.limiter import (
    REST_CLIENT_EVICT_COUNT as _REST_CLIENT_EVICT_COUNT,
)

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.bridge.hub.connection import ConnectionManager
    from provide.uterm.bridge.hub.presence import PresenceManager
    from provide.uterm.bridge.models import WorkerTermState

logger = get_logger(__name__)


async def shutdown_background_tasks(task_set: set[asyncio.Task[Any]]) -> int:
    """Cancel and await all pending background tasks. Returns count cancelled."""
    tasks = list(task_set)
    if not tasks:
        return 0
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    task_set.clear()
    return sum(1 for r in results if isinstance(r, (asyncio.CancelledError, Exception)))


# Re-exported from :mod:`provide.uterm.bridge.hub.limiter` so existing
# call sites that import these from ``connections`` keep working. The
# canonical definitions live in the limiter module now.
__all__ = ["_REST_CLIENT_CACHE_MAX", "_REST_CLIENT_EVICT_COUNT", "shutdown_background_tasks"]


class _ConnectionMixin:
    """Compatibility facade forwarding to :attr:`TermHub.connection_mgr` / ``presence_mgr``.

    The two service classes (:class:`ConnectionManager` and
    :class:`PresenceManager`) own the actual implementation. This mixin
    exists only so the legacy ``hub.<method>(...)`` call sites — and the
    ``super().<method>(...)`` cooperative chains on
    :class:`HubMessagingMixin` — keep working unchanged.

    All forwarded methods are thin pass-throughs. ``request_snapshot``
    and ``force_release_hijack`` are implemented inline (calling through
    ``self.<name>`` via the services) so monkey-patching them on a hub
    instance still works — tests in ``test_hub_polling_coverage``,
    ``test_misc_coverage`` and ``provide-uterm-server/tests/server``
    rely on that pattern.
    """

    # Type-only declarations; runtime initialisation lives in TermHub.__init__.
    connection_mgr: ConnectionManager
    presence_mgr: PresenceManager

    # -- Rate limiting --------------------------------------------------

    def allow_rest_acquire_for(self, client_id: str) -> bool:
        """Per-client REST acquire rate limit (also checks the global bucket)."""
        return self.connection_mgr.allow_rest_acquire_for(client_id)

    def allow_rest_send_for(self, client_id: str) -> bool:
        """Per-client REST send/step rate limit (also checks the global bucket)."""
        return self.connection_mgr.allow_rest_send_for(client_id)

    # -- Token access ---------------------------------------------------

    def worker_token(self) -> str | None:
        """Return the configured worker bearer token (read-only)."""
        return self.connection_mgr.worker_token()

    # -- Worker connection lifecycle ------------------------------------

    async def register_worker(self, worker_id: str, ws: WebSocket) -> bool:
        """Register *ws* as the active worker for *worker_id*."""
        return await self.connection_mgr.register_worker(worker_id, ws)

    async def is_active_worker(self, worker_id: str, ws: WebSocket) -> bool:
        """Return True if *ws* is still the registered worker for *worker_id*."""
        return await self.connection_mgr.is_active_worker(worker_id, ws)

    async def set_worker_tunnel_flag(self, worker_id: str, value: bool) -> None:
        """Mark whether ``worker_id``'s worker WS uses the tunnel wire format."""
        await self.connection_mgr.set_worker_tunnel_flag(worker_id, value)

    async def set_worker_hello(self, worker_id: str, mode: InputMode, protocol_version: int | None = None) -> bool:
        """Process a ``worker_hello`` message: set input_mode and persist protocol version."""
        return await self.connection_mgr.set_worker_hello(worker_id, mode, protocol_version)

    async def update_last_snapshot(self, worker_id: str, snapshot: dict[str, Any]) -> None:
        """Store *snapshot* as the most recent snapshot for *worker_id*."""
        await self.connection_mgr.update_last_snapshot(worker_id, snapshot)

    async def deregister_worker(self, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
        """Clear *ws* as the active worker if it is still current."""
        return await self.connection_mgr.deregister_worker(worker_id, ws)

    # -- Browser connection lifecycle ------------------------------------

    async def register_browser(
        self, worker_id: str, ws: WebSocket, role: str, *, defer_broadcast: bool = False
    ) -> dict[str, Any]:
        """Register *ws* as a browser for *worker_id* and return initial state."""
        return await self.connection_mgr.register_browser(worker_id, ws, role, defer_broadcast=defer_broadcast)

    async def activate_browser_broadcasts(self, worker_id: str, ws: WebSocket) -> None:
        """Allow broadcasts to a browser after its startup frames have been sent."""
        await self.connection_mgr.activate_browser_broadcasts(worker_id, ws)

    async def cleanup_browser_disconnect(self, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]:
        """Handle a browser WS disconnect atomically."""
        return await self.connection_mgr.cleanup_browser_disconnect(worker_id, ws, owned_hijack)

    # -- Presence-shaped queries ----------------------------------------

    async def register_browser_state_snapshot(self, worker_id: str, ws: WebSocket) -> dict[str, Any]:
        """Return current browser state without re-registering (resume helper)."""
        return await self.presence_mgr.register_browser_state_snapshot(worker_id, ws)

    async def resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        """Public wrapper around the hub's ``_resolve_role_for_browser`` callback."""
        return await self.presence_mgr.resolve_role_for_browser(ws, worker_id)

    def can_send_input(self, st: WorkerTermState, ws: WebSocket) -> bool:
        """Check if *ws* can send input to the worker (open mode or hijack owner).

        In open mode, viewers are excluded — only operators and admins may send.
        """
        return self.presence_mgr.can_send_input(st, ws)

    # -- Worker-bound presence control frames ---------------------------
    # Implemented inline (not via service back-reference) so the
    # test-only pattern of monkey-patching ``hub.request_snapshot`` keeps
    # working. The service still owns the canonical implementation;
    # the shim just dispatches through it.

    async def request_snapshot(self, worker_id: str) -> None:
        """Send a ``snapshot_req`` control frame to the worker (no-op if no worker connected)."""
        await self.presence_mgr.request_snapshot(worker_id)

    async def request_analysis(self, worker_id: str) -> None:
        """Send an ``analyze_req`` control frame to the worker (no-op if no worker connected)."""
        await self.presence_mgr.request_analysis(worker_id)

    # -- Hijack-clearing lifecycle --------------------------------------
    # Implemented inline (not via service back-reference) so the
    # test-only pattern of monkey-patching ``hub.force_release_hijack``
    # in ``tests/server/test_registry.py`` and
    # ``tests/server/test_coverage_gaps.py`` keeps working.

    async def force_release_hijack(self, worker_id: str) -> bool:
        """Forcibly clear any active hijack for *worker_id* and send a resume control frame."""
        return await self.connection_mgr.force_release_hijack(worker_id)
