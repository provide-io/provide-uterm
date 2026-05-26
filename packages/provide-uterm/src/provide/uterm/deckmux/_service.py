#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMuxPresence: presence routing + control-transfer service.

Service-shaped extraction of ``DeckMuxMixin`` following the refactor #16
pattern used by :class:`HijackLeaseManager`, :class:`MessageRouter`,
:class:`ConnectionManager`, :class:`PresenceManager`, :class:`StateStore`
and :class:`PollingCoordinator` on the server-side hub.

The service owns the per-worker :class:`PresenceStore` and
:class:`TransferManager` containers and implements the four browser-
facing operations (``on_browser_connect``, ``on_browser_disconnect``,
``handle_message``, ``cleanup``). It holds a back reference to the host
hub for the broadcast call only — every other piece of state lives on
the service itself.

The back reference (rather than a captured callable) matches the Phase
7a/7b shape (:class:`StateStore`, :class:`PollingCoordinator`) and
preserves the existing test-construction pattern where ``_FakeHub``
calls ``_deckmux_init()`` *before* assigning ``self.broadcast``; the
broadcast attribute is resolved lazily at call time, not at service
construction time.

Wire format and lock semantics are preserved verbatim — every method
body is a line-for-line move from the prior mixin implementation, with
``self.broadcast(...)`` calls rewritten as ``self._hub.broadcast(...)``.
"""

from __future__ import annotations

from typing import Any

from provide.uterm.deckmux._names import (
    generate_color,
    generate_initials,
    generate_name,
)
from provide.uterm.deckmux._presence import PresenceStore
from provide.uterm.deckmux._protocol import (
    MSG_CONTROL_REQUEST,
    MSG_PRESENCE_UPDATE,
    MSG_QUEUED_INPUT,
    make_control_transfer,
    make_presence_leave,
)
from provide.uterm.deckmux._transfer import TransferManager


class DeckMuxPresence:
    """Presence + control-transfer service.

    Composed (or held) by a hub via ``DeckMuxMixin`` (or directly). Owns
    per-worker :class:`PresenceStore` and :class:`TransferManager`
    instances and routes the three browser-originated DeckMux messages
    plus the connect/disconnect lifecycle hooks.

    Args:
        hub: The composing host. The service only calls
            ``hub.broadcast(worker_id, msg)`` — looked up lazily so the
            attribute may be assigned to the hub *after*
            ``DeckMuxPresence`` is constructed (matches the existing
            ``_FakeHub`` test pattern).
    """

    __slots__ = ("_hub", "presence_stores", "transfer_managers")

    def __init__(self, hub: Any) -> None:
        self._hub = hub
        self.presence_stores: dict[str, PresenceStore] = {}
        self.transfer_managers: dict[str, TransferManager] = {}

    def get_presence_store(self, worker_id: str) -> PresenceStore:
        """Return (creating if needed) the presence store for *worker_id*."""
        if worker_id not in self.presence_stores:
            self.presence_stores[worker_id] = PresenceStore()
        return self.presence_stores[worker_id]

    def get_transfer_manager(
        self,
        worker_id: str,
        config: dict[str, Any] | None = None,
    ) -> TransferManager:
        """Return (creating if needed) the transfer manager for *worker_id*."""
        if worker_id not in self.transfer_managers:
            idle_s = (config or {}).get("auto_transfer_idle_s", 30)
            queue_mode = (config or {}).get("keystroke_queue", "display")
            self.transfer_managers[worker_id] = TransferManager(
                auto_transfer_idle_s=idle_s,
                keystroke_queue_mode=queue_mode,
            )
        return self.transfer_managers[worker_id]

    async def on_browser_connect(
        self,
        worker_id: str,
        ws: Any,
        role: str,
        principal: Any = None,
    ) -> dict[str, Any] | None:
        """Called when a browser connects. Returns presence_sync message to send."""
        store = self.get_presence_store(worker_id)

        # Generate identity
        user_id = str(id(ws))
        if principal and hasattr(principal, "subject_id"):
            name = getattr(principal, "display_name", "") or getattr(principal, "subject_id", "")
            user_id = getattr(principal, "subject_id", user_id)
        else:
            name = generate_name(user_id)

        color = generate_color(user_id, store.taken_colors())
        initials = generate_initials(name)

        # Prune users with no activity in the last 30 s (stale reconnect debris)
        store.prune_idle(30.0)

        store.add(user_id, name, color, role, initials)

        # Build sync payload for the joining browser
        config = {"auto_transfer_idle_s": 30, "keystroke_queue": "display"}
        result: dict[str, Any] = store.get_sync_payload(config)

        # Broadcast updated sync to all existing browsers so they see the new user.
        # addUser in the frontend is idempotent — re-joining existing users just updates them.
        if store.count > 1:
            await self._hub.broadcast(worker_id, result)

        return result

    async def on_browser_disconnect(
        self,
        worker_id: str,
        ws: Any,
        principal: Any = None,
    ) -> None:
        """Called when a browser disconnects. Broadcasts presence_leave."""
        store = self.get_presence_store(worker_id)
        user_id = str(id(ws))
        if principal and hasattr(principal, "subject_id"):
            user_id = getattr(principal, "subject_id", user_id)
        removed = store.remove(user_id)
        if removed:
            msg = make_presence_leave(user_id)
            await self._hub.broadcast(worker_id, msg)

    async def handle_message(
        self,
        worker_id: str,
        ws: Any,
        msg: dict[str, Any],
        principal: Any = None,
    ) -> None:
        """Route a DeckMux message from a browser."""
        msg_type = msg.get("type")
        store = self.get_presence_store(worker_id)
        # Resolve user_id the same way as on_browser_connect so the
        # store lookup succeeds (must match the ID used during add()).
        user_id = str(id(ws))
        if principal and hasattr(principal, "subject_id"):
            user_id = getattr(principal, "subject_id", user_id)

        if msg_type == MSG_PRESENCE_UPDATE:
            fields = {
                k: msg[k]
                for k in (
                    "scroll_line",
                    "scroll_range",
                    "total_lines",
                    "selection",
                    "pin",
                    "typing",
                    "cols",
                    "rows",
                )
                if k in msg
            }
            user = store.update(user_id, **fields)
            if user:
                # Broadcast to other browsers
                update_msg = user.to_dict()
                update_msg["type"] = MSG_PRESENCE_UPDATE
                await self._hub.broadcast(worker_id, update_msg)

                # Reset auto-transfer warning if owner is active
                tm = self.get_transfer_manager(worker_id)
                if user.is_owner and fields.get("typing"):
                    tm.reset_warning()

        elif msg_type == MSG_QUEUED_INPUT:
            raw_keys = msg.get("keys", "")
            tm = self.get_transfer_manager(worker_id)
            display = tm.queue_keystroke(user_id, raw_keys)
            # Update user's queued_keys for broadcast
            store.update(user_id, queued_keys=display)
            user = store.get(user_id)
            if user:
                update_msg = user.to_dict()
                update_msg["type"] = MSG_PRESENCE_UPDATE
                await self._hub.broadcast(worker_id, update_msg)

        elif msg_type == MSG_CONTROL_REQUEST:
            owner = store.get_owner()
            if owner is None:
                # No one has control — grant immediately
                store.set_owner(user_id)
                tm = self.get_transfer_manager(worker_id)
                transfer = tm.build_transfer_message("", user_id, "handover")
                await self._hub.broadcast(worker_id, transfer)
            elif owner.user_id == user_id:
                # Requester is already the owner — release control
                store.clear_owner()
                transfer = make_control_transfer(user_id, "", "handover")
                await self._hub.broadcast(worker_id, transfer)
            # else: another user owns — ignore the request

    def cleanup(self, worker_id: str) -> None:
        """Clean up DeckMux state for a session."""
        self.presence_stores.pop(worker_id, None)
        self.transfer_managers.pop(worker_id, None)
