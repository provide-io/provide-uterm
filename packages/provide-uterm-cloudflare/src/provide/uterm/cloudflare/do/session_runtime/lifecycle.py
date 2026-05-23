#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""WebSocket lifecycle handlers for SessionRuntime.

Provides ``webSocketOpen``, ``webSocketMessage``, ``webSocketClose``, and
``webSocketError`` — the four hibernation-aware Durable Object hooks that
drive browser/worker/raw socket bookkeeping.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any, cast

from provide.uterm.bridge.contracts import (
    CURRENT_PROTOCOL_VERSION,
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    PREFERRED_PROTOCOL_VERSION,
)

if TYPE_CHECKING:
    from provide.uterm.cloudflare.api.ws_routes import handle_socket_message
    from provide.uterm.cloudflare.cf_types import CFWebSocket
    from provide.uterm.cloudflare.contracts import RuntimeProtocol
    from provide.uterm.cloudflare.do.ushell import on_browser_connected
    from provide.uterm.cloudflare.state.registry import update_kv_session
else:
    try:
        from provide.uterm.cloudflare.api.ws_routes import handle_socket_message
        from provide.uterm.cloudflare.cf_types import CFWebSocket
        from provide.uterm.cloudflare.do.ushell import on_browser_connected
        from provide.uterm.cloudflare.state.registry import update_kv_session
    except Exception:  # pragma: no cover
        from api.ws_routes import handle_socket_message  # type: ignore[import-not-found,no-redef]
        from cf_types import CFWebSocket  # type: ignore[import-not-found,no-redef]  # noqa: TC002
        from do.ushell import on_browser_connected  # type: ignore[import-not-found,no-redef]
        from state.registry import update_kv_session  # type: ignore[import-not-found,no-redef]

logger = logging.getLogger(__name__)


class _LifecycleMixin:
    """Mixin providing Durable Object WebSocket lifecycle handlers for SessionRuntime."""

    if TYPE_CHECKING:
        worker_ws: CFWebSocket | None

    async def webSocketOpen(self, ws: CFWebSocket) -> None:  # noqa: N802
        if self._deleted_at is not None:  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                ws.close(1001, "session deleted")
            return
        ws_id = self.ws_key(ws)  # type: ignore[attr-defined]
        role = self._socket_role(ws)  # type: ignore[attr-defined]
        # Check before _register_socket so we can detect fetch()-initialized browser sockets.
        # fetch() sends hello before the 101 response; webSocketOpen() fires after the upgrade.
        # For the normal (non-hibernation) path the socket is already registered — skip hello.
        already_initialized = ws_id in self.browser_sockets  # type: ignore[attr-defined]
        self._register_socket(ws, role)  # type: ignore[attr-defined]
        if role == "worker":
            self.worker_ws = ws
            self.lifecycle_state = "running"
            await self.broadcast_worker_frame(  # type: ignore[attr-defined]
                {"type": "worker_connected", "worker_id": self.worker_id, "ts": time.time()}  # type: ignore[attr-defined]
            )
            await update_kv_session(
                self.env,  # type: ignore[attr-defined]
                self.worker_id,  # type: ignore[attr-defined]
                connected=True,
                hijacked=self.hijack.session is not None,  # type: ignore[attr-defined]
                input_mode=self.input_mode,  # type: ignore[attr-defined]
                meta=self.meta,  # type: ignore[attr-defined]
            )
        elif role == "raw":
            self.raw_sockets[ws_id] = ws  # type: ignore[attr-defined]
            if self.last_snapshot is not None and isinstance(self.last_snapshot.get("screen"), str):  # type: ignore[attr-defined]
                await self._send_text(ws, str(self.last_snapshot.get("screen")))  # type: ignore[attr-defined]
        else:
            self.browser_sockets[ws_id] = ws  # type: ignore[attr-defined]
            browser_role = self._socket_browser_role(ws)  # type: ignore[attr-defined]
            if not already_initialized:
                # Hibernation-restore path: fetch() did not run for this connection, so
                # send hello here.  For normal upgrades fetch() already sent it.
                _open_resume_token = secrets.token_urlsafe(32)
                _open_resume_ttl = float(getattr(self.config, "resume_ttl_s", 300))  # type: ignore[attr-defined]
                self.store.create_resume_token(_open_resume_token, self.worker_id, browser_role, _open_resume_ttl)  # type: ignore[attr-defined]
                self.browser_resume_tokens[ws_id] = _open_resume_token  # type: ignore[attr-defined]
                await self.send_ws(  # type: ignore[attr-defined]
                    ws,
                    {
                        "type": "hello",
                        "worker_id": self.worker_id,  # type: ignore[attr-defined]
                        "worker_online": self.worker_ws is not None or self._ushell is not None,  # type: ignore[attr-defined]
                        # can_hijack and role reflect the JWT-resolved browser role.
                        "can_hijack": browser_role == "admin",
                        "input_mode": self.input_mode,  # type: ignore[attr-defined]
                        "role": browser_role,
                        "hijack_control": "rest",
                        "hijack_step_supported": True,
                        "resume_supported": True,
                        "resume_token": _open_resume_token,
                        "presence_enabled": bool(self.meta.get("presence")),  # type: ignore[attr-defined]
                        "protocol_version": CURRENT_PROTOCOL_VERSION,
                        "protocol": {
                            "selected": PREFERRED_PROTOCOL_VERSION,
                            "server_min": MIN_PROTOCOL_VERSION,
                            "server_max": MAX_PROTOCOL_VERSION,
                        },
                        "ts": time.time(),
                    },
                )
            await self._maybe_send_presence_sync(ws, exclude_self=True)  # type: ignore[attr-defined]
            await self.send_hijack_state(ws)  # type: ignore[attr-defined]
            if self.last_snapshot is not None:  # type: ignore[attr-defined]
                await self.send_ws(ws, self.last_snapshot)  # type: ignore[attr-defined]
            # For ushell sessions, broadcast worker_connected + welcome on first browser join.
            await on_browser_connected(self)

    async def webSocketMessage(self, ws: CFWebSocket, message: Any) -> None:  # noqa: N802
        if self._deleted_at is not None:  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                ws.close(1001, "session deleted")
            return
        role = self._socket_role(ws)  # type: ignore[attr-defined]
        self._register_socket(ws, role)  # type: ignore[attr-defined]
        if role == "raw":
            payload = (
                message.decode("latin-1", errors="replace") if isinstance(message, (bytes, bytearray)) else str(message)
            )
            await self.push_worker_input(payload)  # type: ignore[attr-defined]
            return

        # Tunnel protocol: binary frames from the tunnel agent (worker role).
        # In Pyodide, JS ArrayBuffer/Uint8Array arrives as a JsProxy, not Python bytes.
        # Convert via to_py() or to_bytes() before checking isinstance.
        _bin = message
        if hasattr(_bin, "to_py"):  # pragma: no cover — Pyodide JsProxy only
            _bin = _bin.to_py()
        elif hasattr(_bin, "to_bytes"):  # pragma: no cover — Pyodide JsProxy only
            _bin = _bin.to_bytes()
        if isinstance(_bin, (bytes, bytearray, memoryview)) and role == "worker":
            if TYPE_CHECKING:
                from provide.uterm.cloudflare.api.tunnel_routes import handle_tunnel_message
            else:
                try:
                    from provide.uterm.cloudflare.api.tunnel_routes import handle_tunnel_message
                except ImportError:  # pragma: no cover
                    from api.tunnel_routes import handle_tunnel_message  # type: ignore[import-not-found,no-redef]

            await handle_tunnel_message(cast("RuntimeProtocol", self), ws, bytes(_bin))
            return

        raw = message if isinstance(message, str) else str(message)
        await handle_socket_message(cast("RuntimeProtocol", self), ws, raw, is_worker=(role == "worker"))

    async def webSocketClose(self, ws: CFWebSocket, code: int, reason: str, was_clean: bool = True) -> None:  # noqa: N802
        _ = (code, reason, was_clean)
        deleted = self._deleted_at is not None  # type: ignore[attr-defined]
        # Use _socket_role() instead of `ws is self.worker_ws` — after hibernation,
        # self.worker_ws is None so the identity check would always be False.
        role = self._socket_role(ws)  # type: ignore[attr-defined]
        wid = self._socket_worker_id(ws)  # type: ignore[attr-defined]
        if role == "browser" and not deleted:
            ws_id = self.ws_key(ws)  # type: ignore[attr-defined]
            if self.meta.get("presence"):  # type: ignore[attr-defined]
                await self.broadcast_to_browsers(  # type: ignore[attr-defined]
                    {"type": "presence_leave", "user_id": ws_id, "ts": time.time()}
                )
            # Mark the resume token as hijack owner before removing socket, so the
            # browser can reclaim ownership on reconnect.
            if ws_id in self.browser_hijack_owner:  # type: ignore[attr-defined]
                token = self.browser_resume_tokens.get(ws_id)  # type: ignore[attr-defined]
                if token:
                    self.store.mark_resume_hijack_owner(token, True)  # type: ignore[attr-defined]
        self._remove_ws(ws)  # type: ignore[attr-defined]
        if role == "worker":
            if not deleted:
                self.lifecycle_state = "stopped"
                await self.broadcast_worker_frame(  # type: ignore[attr-defined]
                    {"type": "worker_disconnected", "worker_id": wid, "ts": time.time()}
                )
            await update_kv_session(self.env, wid, connected=False)  # type: ignore[attr-defined]

    async def webSocketError(self, ws: CFWebSocket, error: Any) -> None:  # noqa: N802
        role = self._socket_role(ws)  # type: ignore[attr-defined]
        wid = self._socket_worker_id(ws)  # type: ignore[attr-defined]
        logger.warning("ws_error worker_id=%s role=%s error=%s", wid, role, error)
        deleted = self._deleted_at is not None  # type: ignore[attr-defined]
        if role == "browser" and not deleted:
            ws_id = self.ws_key(ws)  # type: ignore[attr-defined]
            if self.meta.get("presence"):  # type: ignore[attr-defined]
                await self.broadcast_to_browsers(  # type: ignore[attr-defined]
                    {"type": "presence_leave", "user_id": ws_id, "ts": time.time()}
                )
            if ws_id in self.browser_hijack_owner:  # type: ignore[attr-defined]
                token = self.browser_resume_tokens.get(ws_id)  # type: ignore[attr-defined]
                if token:
                    self.store.mark_resume_hijack_owner(token, True)  # type: ignore[attr-defined]
        self._remove_ws(ws)  # type: ignore[attr-defined]
        if role == "worker":
            if not deleted:
                self.lifecycle_state = "error"
                await self.broadcast_worker_frame(  # type: ignore[attr-defined]
                    {"type": "worker_disconnected", "worker_id": wid, "ts": time.time()}
                )
            await update_kv_session(self.env, wid, connected=False)  # type: ignore[attr-defined]
