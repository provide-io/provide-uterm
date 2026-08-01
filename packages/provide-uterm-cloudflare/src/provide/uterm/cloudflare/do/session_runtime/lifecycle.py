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
        _deleted_at: float | None
        ws_key: Any
        _socket_role: Any
        browser_sockets: dict[str, CFWebSocket]
        _register_socket: Any
        broadcast_worker_frame: Any
        worker_id: str
        env: Any
        hijack: Any
        input_mode: str
        meta: dict[str, Any]
        raw_sockets: dict[str, CFWebSocket]
        last_snapshot: dict[str, Any] | None
        _send_text: Any
        config: Any
        store: Any
        browser_resume_tokens: dict[str, str]
        send_ws: Any
        _maybe_send_presence_sync: Any
        send_hijack_state: Any
        _ushell: Any
        _socket_browser_role: Any
        lifecycle_state: str
        _socket_worker_id: Any
        _restore_worker_id_from_socket: Any
        _remove_ws: Any
        broadcast_to_browsers: Any
        browser_hijack_owner: dict[str, str]
        push_worker_input: Any
        register_worker_socket: Any
        activate_worker_socket: Any
        _restore_browser_identity: Any
        _set_browser_ownership_attachment: Any
        unregister_worker_socket: Any
        remove_browser_socket: Any
        broadcast_hijack_state: Any

    async def webSocketOpen(self, ws: CFWebSocket) -> None:  # noqa: N802
        self._restore_worker_id_from_socket(ws)
        if self._deleted_at is not None:
            with contextlib.suppress(Exception):
                ws.close(1001, "session deleted")
            return
        ws_id = self.ws_key(ws)
        role = self._socket_role(ws)
        # Check before _register_socket so we can detect fetch()-initialized browser sockets.
        # fetch() sends hello before the 101 response; webSocketOpen() fires after the upgrade.
        # For the normal (non-hibernation) path the socket is already registered — skip hello.
        already_initialized = ws_id in self.browser_sockets
        if role == "worker":
            if not await self.activate_worker_socket(ws):
                return
            self.lifecycle_state = "running"
            await self.broadcast_worker_frame(
                {"type": "worker_connected", "worker_id": self.worker_id, "ts": time.time()}
            )
            await update_kv_session(
                self.env,
                self.worker_id,
                connected=True,
                hijacked=self.hijack.session is not None,
                input_mode=self.input_mode,
                meta=self.meta,
            )
        elif role == "raw":
            self._register_socket(ws, role)
            self.raw_sockets[ws_id] = ws
            if self.last_snapshot is not None and isinstance(self.last_snapshot.get("screen"), str):
                await self._send_text(ws, str(self.last_snapshot.get("screen")))
        else:
            self._restore_browser_identity(ws)
            browser_role = self._socket_browser_role(ws)
            if not already_initialized:
                # Hibernation-restore path: fetch() did not run for this connection, so
                # send hello here.  For normal upgrades fetch() already sent it.
                _resume_on = bool(getattr(self.config, "resume_enabled", True))
                _open_resume_token = self.browser_resume_tokens.get(ws_id, "") if _resume_on else ""
                if _resume_on and not _open_resume_token:
                    _open_resume_token = secrets.token_urlsafe(32)
                    _open_resume_ttl = float(getattr(self.config, "resume_ttl_s", 300))
                    self.store.create_resume_token(_open_resume_token, self.worker_id, browser_role, _open_resume_ttl)
                    self.browser_resume_tokens[ws_id] = _open_resume_token
                    self._set_browser_ownership_attachment(ws, self.browser_hijack_owner.get(ws_id))
                hello: dict[str, object] = {
                    "type": "hello",
                    "worker_id": self.worker_id,
                    "worker_online": self.worker_ws is not None or self._ushell is not None,
                    # can_hijack and role reflect the JWT-resolved browser role.
                    "can_hijack": browser_role == "admin",
                    "input_mode": self.input_mode,
                    "role": browser_role,
                    "hijack_control": "ws",
                    "hijack_step_supported": True,
                    "resume_supported": _resume_on,
                    "presence_enabled": bool(self.meta.get("presence")),
                    "protocol_version": CURRENT_PROTOCOL_VERSION,
                    "protocol": {
                        "selected": PREFERRED_PROTOCOL_VERSION,
                        "server_min": MIN_PROTOCOL_VERSION,
                        "server_max": MAX_PROTOCOL_VERSION,
                    },
                    "ts": time.time(),
                }
                if _resume_on:
                    hello["resume_token"] = _open_resume_token
                await self.send_ws(ws, hello)
            await self._maybe_send_presence_sync(ws, exclude_self=True)
            await self.send_hijack_state(ws)
            if self.last_snapshot is not None:
                await self.send_ws(ws, self.last_snapshot)
            # For ushell sessions, broadcast worker_connected + welcome on first browser join.
            await on_browser_connected(self)

    async def webSocketMessage(self, ws: CFWebSocket, message: Any) -> None:  # noqa: N802
        self._restore_worker_id_from_socket(ws)
        if self._deleted_at is not None:
            with contextlib.suppress(Exception):
                ws.close(1001, "session deleted")
            return
        role = self._socket_role(ws)
        if role == "worker":
            if not await self.activate_worker_socket(ws):
                return
        else:
            if role == "browser":
                self._restore_browser_identity(ws)
            else:
                self._register_socket(ws, role)
        if role == "raw":
            payload = (
                message.decode("latin-1", errors="replace") if isinstance(message, (bytes, bytearray)) else str(message)
            )
            await self.push_worker_input(payload)
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
        self._restore_worker_id_from_socket(ws)
        deleted = self._deleted_at is not None
        # Use _socket_role() instead of `ws is self.worker_ws` — after hibernation,
        # self.worker_ws is None so the identity check would always be False.
        role = self._socket_role(ws)
        wid = self._socket_worker_id(ws)
        if role == "browser" and not deleted:
            ws_id = self.ws_key(ws)
            if self.meta.get("presence"):
                await self.broadcast_to_browsers({"type": "presence_leave", "user_id": ws_id, "ts": time.time()})
        released_hijack = False
        current_worker_closed = False
        if role == "browser":
            released_hijack = await self.remove_browser_socket(ws)
        elif role == "worker":
            current_worker_closed = await self.unregister_worker_socket(ws)
            self._remove_ws(ws)
        else:
            self._remove_ws(ws)
        if released_hijack:
            await self.broadcast_hijack_state()
        if role == "worker" and current_worker_closed:
            if not deleted:
                self.lifecycle_state = "stopped"
                await self.broadcast_worker_frame({"type": "worker_disconnected", "worker_id": wid, "ts": time.time()})
            await update_kv_session(self.env, wid, connected=False)

    async def webSocketError(self, ws: CFWebSocket, error: Any) -> None:  # noqa: N802
        self._restore_worker_id_from_socket(ws)
        role = self._socket_role(ws)
        wid = self._socket_worker_id(ws)
        logger.warning("ws_error worker_id=%s role=%s error=%s", wid, role, error)
        deleted = self._deleted_at is not None
        if role == "browser" and not deleted:
            ws_id = self.ws_key(ws)
            if self.meta.get("presence"):
                await self.broadcast_to_browsers({"type": "presence_leave", "user_id": ws_id, "ts": time.time()})
        released_hijack = False
        current_worker_closed = False
        if role == "browser":
            released_hijack = await self.remove_browser_socket(ws)
        elif role == "worker":
            current_worker_closed = await self.unregister_worker_socket(ws)
            self._remove_ws(ws)
        else:
            self._remove_ws(ws)
        if released_hijack:
            await self.broadcast_hijack_state()
        if role == "worker" and current_worker_closed:
            if not deleted:
                self.lifecycle_state = "error"
                await self.broadcast_worker_frame({"type": "worker_disconnected", "worker_id": wid, "ts": time.time()})
            await update_kv_session(self.env, wid, connected=False)
