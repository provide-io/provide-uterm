#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux TermHub mixin — presence routing and control transfer."""

from __future__ import annotations

from typing import Any

try:
    from provide.terminal.deckmux import (
        PresenceStore,
        TransferManager,
        generate_color,
        generate_name,
    )
    from provide.terminal.deckmux._names import generate_initials
    from provide.terminal.deckmux._protocol import (
        MSG_CONTROL_REQUEST,
        MSG_PRESENCE_UPDATE,
        MSG_QUEUED_INPUT,
        make_control_transfer,
        make_presence_leave,
    )

    _HAS_DECKMUX = True
except ImportError:  # pragma: no cover
    _HAS_DECKMUX = False


class DeckMuxMixin:
    """Mixin for TermHub to handle DeckMux presence messages.

    Expects the host class to provide:
    - broadcast(worker_id, msg) — send to all browsers
    - _workers dict with WorkerTermState entries
    - _lock for thread safety
    """

    def _deckmux_init(self) -> None:
        """Initialise DeckMux state containers. Call from host ``__init__``."""
        self._presence_stores: dict[str, PresenceStore] = {}
        self._transfer_managers: dict[str, TransferManager] = {}

    def _get_presence_store(self, worker_id: str) -> PresenceStore:
        if worker_id not in self._presence_stores:
            self._presence_stores[worker_id] = PresenceStore()
        return self._presence_stores[worker_id]

    def _get_transfer_manager(
        self,
        worker_id: str,
        config: dict[str, Any] | None = None,
    ) -> TransferManager:
        if worker_id not in self._transfer_managers:
            idle_s = (config or {}).get("auto_transfer_idle_s", 30)
            queue_mode = (config or {}).get("keystroke_queue", "display")
            self._transfer_managers[worker_id] = TransferManager(
                auto_transfer_idle_s=idle_s,
                keystroke_queue_mode=queue_mode,
            )
        return self._transfer_managers[worker_id]

    async def deckmux_on_browser_connect(
        self,
        worker_id: str,
        ws: Any,
        role: str,
        principal: Any = None,
    ) -> dict[str, Any] | None:
        """Called when a browser connects. Returns presence_sync message to send."""
        store = self._get_presence_store(worker_id)

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
        return result

    async def deckmux_on_browser_disconnect(
        self,
        worker_id: str,
        ws: Any,
    ) -> None:
        """Called when a browser disconnects. Broadcasts presence_leave."""
        store = self._get_presence_store(worker_id)
        user_id = str(id(ws))
        removed = store.remove(user_id)
        if removed:
            msg = make_presence_leave(user_id)
            await self.broadcast(worker_id, msg)  # type: ignore[attr-defined]

    async def deckmux_handle_message(
        self,
        worker_id: str,
        ws: Any,
        msg: dict[str, Any],
    ) -> None:
        """Route a DeckMux message from a browser."""
        msg_type = msg.get("type")
        store = self._get_presence_store(worker_id)
        user_id = str(id(ws))

        if msg_type == MSG_PRESENCE_UPDATE:
            fields = {
                k: msg[k]
                for k in (
                    "scroll_line",
                    "scroll_range",
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
                await self.broadcast(worker_id, update_msg)  # type: ignore[attr-defined]

                # Reset auto-transfer warning if owner is active
                tm = self._get_transfer_manager(worker_id)
                if user.is_owner and fields.get("typing"):
                    tm.reset_warning()

        elif msg_type == MSG_QUEUED_INPUT:
            raw_keys = msg.get("keys", "")
            tm = self._get_transfer_manager(worker_id)
            display = tm.queue_keystroke(user_id, raw_keys)
            # Update user's queued_keys for broadcast
            store.update(user_id, queued_keys=display)
            user = store.get(user_id)
            if user:
                update_msg = user.to_dict()
                update_msg["type"] = MSG_PRESENCE_UPDATE
                await self.broadcast(worker_id, update_msg)  # type: ignore[attr-defined]

        elif msg_type == MSG_CONTROL_REQUEST:
            owner = store.get_owner()
            if owner is None:
                # No one has control — grant immediately
                store.set_owner(user_id)
                tm = self._get_transfer_manager(worker_id)
                transfer = tm.build_transfer_message("", user_id, "handover")
                await self.broadcast(worker_id, transfer)  # type: ignore[attr-defined]
            elif owner.user_id == user_id:
                # Requester is already the owner — release control
                store.clear_owner()
                transfer = make_control_transfer(user_id, "", "handover")
                await self.broadcast(worker_id, transfer)  # type: ignore[attr-defined]
            # else: another user owns — ignore the request

    def deckmux_cleanup(self, worker_id: str) -> None:
        """Clean up DeckMux state for a session."""
        self._presence_stores.pop(worker_id, None)
        self._transfer_managers.pop(worker_id, None)
