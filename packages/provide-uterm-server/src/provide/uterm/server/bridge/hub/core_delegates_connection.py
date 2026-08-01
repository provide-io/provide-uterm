#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Connection-delegate bodies extracted from :class:`TermHub`.

``self.connection_mgr`` (:class:`ConnectionManager`) owns the
worker/browser register/deregister paths. The :class:`TermHub` methods
that wrap a connection call *and* emit lifecycle telemetry, clear
per-WebSocket hub-level buffers, or notify the EventBus keep a one-line
wrapper on the class — preserving the no-mixin ``hub.<name>(...)`` call
surface — while their bodies live here as module-level functions taking
``hub`` as the first argument.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub.core_impl import TermHub


async def register_worker(hub: TermHub, worker_id: str, ws: WebSocket, *, is_tunnel_worker: bool = False) -> bool:
    """Register *ws* as the active worker for *worker_id*."""
    result = await hub.connection_mgr.register_worker(worker_id, ws, is_tunnel_worker=is_tunnel_worker)
    await hub.emit_telemetry("session.registered", worker_id=worker_id, metadata={"session_type": "worker"})
    return result


async def register_browser(
    hub: TermHub, worker_id: str, ws: WebSocket, role: str, *, defer_broadcast: bool = False
) -> dict[str, Any]:
    """Register *ws* as a browser for *worker_id* and return initial state."""
    result = await hub.connection_mgr.register_browser(worker_id, ws, role, defer_broadcast=defer_broadcast)
    await hub.emit_telemetry(
        "session.registered",
        worker_id=worker_id,
        role=role,
        metadata={"session_type": "browser"},
    )
    return result


async def cleanup_browser_disconnect(hub: TermHub, worker_id: str, ws: WebSocket, owned_hijack: bool) -> dict[str, Any]:
    """Clear heuristic state and call into the connection manager."""
    hub.router.forget_browser(ws)
    hub._input_buffers.pop(ws, None)
    hub._hold_buffers.pop(ws, None)
    hub._paused_browsers.discard(ws)
    result = await hub.connection_mgr.cleanup_browser_disconnect(worker_id, ws, owned_hijack)
    await hub.emit_telemetry("session.disconnected", worker_id=worker_id, metadata={"session_type": "browser"})
    return result


async def remove_dead_browsers(hub: TermHub, worker_id: str, dead: set[WebSocket]) -> bool:
    """Clear per-browser state for dead browsers and call into the lease manager."""
    for ws in dead:
        # Forget the router-side heuristic state too, mirroring the graceful
        # cleanup_browser_disconnect path — otherwise a browser pruned via the
        # dead-socket path leaks its keystroke_timestamps entry forever.
        hub.router.forget_browser(ws)
        hub._input_buffers.pop(ws, None)
        hub._hold_buffers.pop(ws, None)
        hub._startup_pending_browsers.discard(ws)
        hub._paused_browsers.discard(ws)
    return await hub.lease.remove_dead_browsers(worker_id, dead)


async def deregister_worker(hub: TermHub, worker_id: str, ws: WebSocket) -> tuple[bool, bool]:
    """Deregister the worker WS and notify the EventBus on disconnect."""
    should_broadcast, was_hijacked = await hub.connection_mgr.deregister_worker(worker_id, ws)
    if should_broadcast and hub._event_bus is not None:
        hub._event_bus.close_worker(worker_id)
    return should_broadcast, was_hijacked


__all__ = [
    "cleanup_browser_disconnect",
    "deregister_worker",
    "register_browser",
    "register_worker",
    "remove_dead_browsers",
]
