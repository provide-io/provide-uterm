#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PresenceManager: browser-presence queries and worker-presence control frames.

Owns the *read-only presence surface* that previously lived inline on
:class:`provide.uterm.server.bridge.hub.connections._ConnectionMixin`. Splitting it
out of the connection lifecycle module gives the "who is here, in what
role, and what can they do" questions a service-class home with explicit
dependencies (the shared hub lock, the worker registry, the lifecycle
state queries) and matches the Phase 5 :class:`MessageRouter` pattern of
a single back-reference to the composing :class:`TermHub`.

Scope:

* Browser role resolution and the cached browser-state snapshot used by
  the resume handshake.
* Per-browser send-authorization (``can_send_input``) — the only
  presence query that does *not* take the hub lock because it operates
  on an already-captured :class:`WorkerTermState` reference.
* Worker-bound presence-driven control frames (``snapshot_req``,
  ``analyze_req``) — these poke the worker for a fresh snapshot or
  analysis result, which is a presence-shaped operation even though it
  goes out over the worker socket rather than a browser socket.

Hot-path note: ``can_send_input`` runs on every browser input frame. The
implementation is intentionally unchanged from the pre-extraction inline
version — pure dict lookup plus a couple of boolean checks, no
allocations, no lock. The mixin shim adds one attribute lookup +
function-call which is negligible at this layer (the per-frame critical
path is dominated by the encoder and ``ws.send_text``).

Lock semantics are intentionally preserved verbatim from the mixin
implementation: the manager uses the *hub's* ``asyncio.Lock`` (accessed
via the back reference) so concurrent presence queries keep serialising
against the same object that the rest of the hub uses.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub.core import TermHub
    from provide.uterm.server.bridge.models import WorkerTermState

logger = get_logger(__name__)


class PresenceManager:
    """Read-only browser-presence queries and worker-presence control frames.

    Composed into :class:`TermHub` as ``self.presence_mgr``. Holds a back
    reference to the hub for the small set of cross-cutting queries that
    legitimately need it (``is_dashboard_hijack_active``,
    ``_resolve_role_for_browser``, ``send_worker``, ``is_hijacked``).

    Args:
        hub: The composing :class:`TermHub`. The manager uses
            ``hub._lock``, ``hub.registry``, the hijack-state predicates
            on ``hub`` and the role-resolver callback configured on the
            hub.
    """

    __slots__ = ("_hub",)

    def __init__(self, hub: TermHub) -> None:
        self._hub = hub

    # -- Browser presence queries ---------------------------------------

    async def register_browser_state_snapshot(self, worker_id: str, ws: WebSocket) -> dict[str, Any]:
        """Return current browser state without re-registering.

        Used after a resume to get updated hello fields.
        """
        hub = self._hub
        async with hub._lock:
            st = hub.registry.get(worker_id)
            if st is None:
                return {
                    "is_hijacked": False,
                    "hijacked_by_me": False,
                    "worker_online": False,
                    "input_mode": "hijack",
                }
            return {
                "is_hijacked": hub.is_hijacked(st),
                "hijacked_by_me": hub.is_dashboard_hijack_active(st) and st.hijack_owner is ws,
                "worker_online": st.worker_ws is not None,
                "input_mode": st.input_mode,
            }

    async def resolve_role_for_browser(self, ws: WebSocket, worker_id: str) -> str:
        """Public wrapper around the hub's ``_resolve_role_for_browser`` callback."""
        return await self._hub._resolve_role_for_browser(ws, worker_id)

    def can_send_input(self, st: WorkerTermState, ws: WebSocket) -> bool:
        """Check if *ws* can send input to the worker (open mode or hijack owner).

        In open mode, viewers are excluded — only operators and admins may send.
        """
        hub = self._hub
        if st.input_mode == "open":
            role = st.browsers.get(ws, "viewer")
            return role in ("operator", "admin")
        return hub.is_dashboard_hijack_active(st) and st.hijack_owner is ws

    # -- Worker-bound presence control frames ---------------------------

    async def request_snapshot(self, worker_id: str) -> None:
        """Send a ``snapshot_req`` control frame to the worker (no-op if no worker connected)."""
        await self._hub.send_worker(worker_id, {"type": "snapshot_req", "req_id": str(uuid.uuid4()), "ts": time.time()})

    async def request_analysis(self, worker_id: str) -> None:
        """Send an ``analyze_req`` control frame to the worker (no-op if no worker connected)."""
        await self._hub.send_worker(worker_id, {"type": "analyze_req", "req_id": str(uuid.uuid4()), "ts": time.time()})


__all__ = ["PresenceManager"]
