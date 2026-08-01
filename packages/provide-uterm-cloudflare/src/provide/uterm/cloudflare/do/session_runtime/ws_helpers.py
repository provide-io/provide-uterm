#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""WebSocket helper mixin for SessionRuntime.

Provides socket keying, role resolution, registration, and send helpers.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from provide.uterm.control_channel import encode_control_frame, encode_terminal_data

if TYPE_CHECKING:
    from provide.uterm.cloudflare.cf_types import CFWebSocket

logger = logging.getLogger(__name__)
_ATTACHMENT_PREFIX = "uterm-v2:"


class _WsHelperMixin:
    """Mixin providing WebSocket helper methods for SessionRuntime."""

    if TYPE_CHECKING:
        worker_ws: CFWebSocket | None
        worker_id: str
        browser_sockets: dict[str, CFWebSocket]
        browser_hijack_owner: dict[str, str]
        browser_resume_tokens: dict[str, str]
        raw_sockets: dict[str, CFWebSocket]
        hijack: Any
        store: Any
        meta: dict[str, Any]

        def _restore_state(self) -> None: ...

    def _attachment_data(self, ws: CFWebSocket) -> dict[str, str]:
        """Decode the durable v2 attachment, returning an empty dict for legacy sockets."""
        try:
            attachment = ws.deserializeAttachment()
            if hasattr(attachment, "to_py"):
                attachment = attachment.to_py()
            if isinstance(attachment, str) and attachment.startswith(_ATTACHMENT_PREFIX):
                value = json.loads(attachment.removeprefix(_ATTACHMENT_PREFIX))
                if isinstance(value, dict):
                    return {str(key): str(item) for key, item in value.items() if item is not None}
        except Exception as exc:
            logger.debug("failed to decode socket attachment: %s", exc)
        return {}

    def _serialize_socket_attachment(
        self,
        ws: CFWebSocket,
        *,
        role: str,
        browser_role: str,
        socket_id: str,
        resume_token: str | None = None,
        worker_generation: str | None = None,
        hijack_id: str | None = None,
    ) -> None:
        value = {
            "role": role,
            "browser_role": browser_role,
            "worker_id": self.worker_id,
            "socket_id": socket_id,
            "resume_token": resume_token,
            "worker_generation": worker_generation,
            "hijack_id": hijack_id,
        }
        ws.serializeAttachment(_ATTACHMENT_PREFIX + json.dumps(value, separators=(",", ":"), sort_keys=True))

    def _set_browser_ownership_attachment(
        self,
        ws: CFWebSocket,
        hijack_id: str | None,
        *,
        resume_token: str | None = None,
        browser_role: str | None = None,
    ) -> None:
        data = self._attachment_data(ws)
        with contextlib.suppress(Exception):
            self._serialize_socket_attachment(
                ws,
                role="browser",
                browser_role=browser_role or data.get("browser_role") or self._socket_browser_role(ws),
                socket_id=data.get("socket_id") or self.ws_key(ws),
                resume_token=resume_token
                or data.get("resume_token")
                or self.browser_resume_tokens.get(self.ws_key(ws)),
                hijack_id=hijack_id,
            )

    def _socket_worker_generation(self, ws: CFWebSocket) -> str | None:
        attached = self._attachment_data(ws).get("worker_generation")
        if attached:
            return attached
        fallback = getattr(ws, "_ut_worker_generation", None)
        return fallback if isinstance(fallback, str) and fallback else None

    def _restore_browser_identity(self, ws: CFWebSocket) -> None:
        """Rebuild browser token and ownership maps from durable attachment state."""
        data = self._attachment_data(ws)
        ws_id = self.ws_key(ws)
        self.browser_sockets[ws_id] = ws
        token = data.get("resume_token")
        if not token:
            return
        record = self.store.get_resume_token(token)
        if record is None or record.get("worker_id") != self.worker_id:
            return
        self.browser_resume_tokens[ws_id] = token
        active = self.hijack.session
        if active is not None and active.owner == f"browser:{token}" and data.get("hijack_id") == active.hijack_id:
            self.browser_hijack_owner[ws_id] = active.hijack_id

    def ws_key(self, ws: CFWebSocket) -> str:
        attached_id = self._attachment_data(ws).get("socket_id")
        if attached_id:
            return attached_id
        try:
            existing = getattr(ws, "_ut_ws_key", None)
            if isinstance(existing, str) and existing:
                return existing
        except Exception:
            existing = None

        key = f"{time.time_ns()}_{secrets.token_hex(4)}"
        with contextlib.suppress(Exception):
            ws._ut_ws_key = key  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
        return key

    def _socket_role(self, ws: CFWebSocket) -> str:
        """Return the socket type: ``"browser"``, ``"worker"``, or ``"raw"``."""
        try:
            attachment = ws.deserializeAttachment()
            if isinstance(attachment, str):
                data = self._attachment_data(ws)
                if data.get("role") in {"browser", "worker", "raw"}:
                    return data["role"]
                if attachment in {"browser", "worker", "raw"}:
                    return attachment  # legacy plain-string format
                # Format: "type:browser_role" or "type:browser_role:worker_id"
                parts = attachment.split(":", 2)
                if parts[0] in {"browser", "worker", "raw"}:
                    return parts[0]
            role = None
            if hasattr(attachment, "get"):
                role = attachment.get("role")
            if role is None and hasattr(attachment, "role"):
                role = attachment.role
            if role is None and hasattr(attachment, "to_py"):
                try:
                    py_attachment = attachment.to_py()
                    if isinstance(py_attachment, str):
                        role = py_attachment
                    elif isinstance(py_attachment, dict):
                        role = py_attachment.get("role")
                except Exception:
                    role = None
            if isinstance(role, str) and role in {"browser", "worker", "raw"}:
                return role
        except Exception:
            role = None
        if role is None:
            candidate = getattr(ws, "_ut_role", None)
            if isinstance(candidate, str):
                return candidate
        return "browser"

    def _socket_browser_role(self, ws: CFWebSocket) -> str:
        """Return the JWT-resolved browser role from the socket attachment.

        Defaults to ``"admin"`` in ``none``/``dev`` mode (open access).  In
        ``jwt`` mode, falls back to ``"viewer"`` (fail-closed) when the
        attachment cannot be read — e.g. after hibernation for a connection
        whose ``serializeAttachment`` call raised at connect time.
        """
        try:
            data = self._attachment_data(ws)
            if data.get("browser_role") in {"admin", "operator", "viewer"}:
                return data["browser_role"]
            attachment = ws.deserializeAttachment()
            if isinstance(attachment, str):
                # Attachment format: "type:browser_role:worker_id" (3 fields).
                # Use split(":", 2) so parts[1] is the bare role, not "role:worker_id".
                parts = attachment.split(":", 2)
                if len(parts) >= 2 and parts[1] in {"admin", "operator", "viewer"}:
                    return parts[1]
        except Exception as exc:
            logger.debug("failed to deserialize browser role attachment: %s", exc)
        # Instance-attribute fallback (set in fetch() when serializeAttachment raises).
        # This attribute is NOT preserved across hibernation, so it will be absent
        # on hibernation-resume paths.
        role = getattr(ws, "_ut_browser_role", None)
        if isinstance(role, str) and role in {"admin", "operator", "viewer"}:
            return role
        # Fail-closed: in jwt mode grant only viewer; in open-access modes grant admin.
        # Warn in jwt mode — this path means the role was not recoverable post-hibernation.
        # In none/dev mode every caller is already treated as admin by design
        # (no per-socket role granularity), so the admin fallback is intentional,
        # not a privilege escalation.
        if self.config.jwt.mode not in {"none", "dev"}:  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
            logger.warning("browser role unavailable (post-hibernation fallback), defaulting to viewer")
        return "admin" if self.config.jwt.mode in {"none", "dev"} else "viewer"  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]

    def _socket_worker_id(self, ws: CFWebSocket) -> str:
        """Return the worker_id from the socket attachment (stored at connect time).

        Falls back to ``self.worker_id`` when not encoded in the attachment
        (e.g. legacy connections, test sockets without serialized attachment).
        """
        try:
            data = self._attachment_data(ws)
            if data.get("worker_id"):
                return data["worker_id"]
            attachment = ws.deserializeAttachment()
            if isinstance(attachment, str):
                parts = attachment.split(":", 2)
                if len(parts) >= 3 and parts[2]:
                    return parts[2]
        except Exception as exc:
            logger.debug("failed to deserialize worker_id from attachment: %s", exc)
        worker_id: str = self.worker_id
        return worker_id

    def _restore_worker_id_from_socket(self, ws: CFWebSocket) -> None:
        """Recover an unnamed DO's storage key before a hibernation callback."""
        attached_worker_id = self._socket_worker_id(ws)
        if self.worker_id != "default" or attached_worker_id == "default":
            return
        self.worker_id = attached_worker_id
        if self.meta.get("display_name") == "default":
            self.meta["display_name"] = attached_worker_id
        self._restore_state()

    def _register_socket(self, ws: CFWebSocket, role: str) -> None:
        ws_id = self.ws_key(ws)
        if role == "worker":
            self.worker_ws = ws
            return
        if role == "raw":
            self.raw_sockets[ws_id] = ws
            return
        self.browser_sockets[ws_id] = ws

    def _remove_ws(self, ws: CFWebSocket) -> None:
        """Remove *ws* from all socket registries (worker, browser, raw)."""
        ws_id = self.ws_key(ws)
        if ws is self.worker_ws:
            self.worker_ws = None
        self.browser_sockets.pop(ws_id, None)
        self.raw_sockets.pop(ws_id, None)
        self.browser_hijack_owner.pop(ws_id, None)
        self.browser_resume_tokens.pop(ws_id, None)
        self._flow.forget(ws_id)  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]

    async def send_ws(self, ws: CFWebSocket, payload: dict[str, Any]) -> None:
        frame_type = str(payload.get("type") or "")
        if frame_type in {"input", "term"}:
            await self._send_text(ws, encode_terminal_data(str(payload.get("data", ""))))
            return
        await self._send_text(ws, encode_control_frame(payload))

    async def _send_text(self, ws: CFWebSocket, payload: str) -> None:
        result = ws.send(payload)
        if inspect.isawaitable(result):
            await result

    async def _maybe_send_presence_sync(self, ws: CFWebSocket, *, exclude_self: bool = False) -> None:
        """Send a ``presence_sync`` frame if the session has presence enabled.

        Called after the hello message on browser connect.  ``exclude_self``
        should be ``True`` in ``webSocketOpen`` (the socket is already
        registered, so we exclude it from the peer list) and ``False`` in
        ``fetch()`` (the socket is not yet in the registry).
        """
        if not self.meta.get("presence"):
            return
        exclude_ws: CFWebSocket | None = ws if exclude_self else None
        connected_ids = self._get_presence_browser_ids(exclude_ws=exclude_ws)
        await self.send_ws(
            ws,
            {
                "type": "presence_sync",
                "users": [{"user_id": uid} for uid in connected_ids],
                "config": {
                    "auto_transfer_idle_s": self.config.deckmux_auto_transfer_idle_s,  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
                    "keystroke_queue": self.config.deckmux_keystroke_queue,  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
                },
                "ts": time.time(),
            },
        )

    def _get_presence_browser_ids(self, *, exclude_ws: CFWebSocket | None) -> list[str]:
        """Return ws_key IDs for all currently connected browser sockets.

        Used to build ``presence_sync`` payloads on connect.  When
        ``exclude_ws`` is provided (e.g. the just-connected socket in
        ``webSocketOpen``), that socket is omitted from the list so the
        joining browser only sees already-connected peers.
        """
        exclude_key = self.ws_key(exclude_ws) if exclude_ws is not None else None
        try:
            all_ws = list(self.ctx.getWebSockets())  # type: ignore[attr-defined]  # ty:ignore[unresolved-attribute]
        except Exception:
            all_ws = []
        if not all_ws:
            all_ws = list(self.browser_sockets.values())
        ids: list[str] = []
        for candidate in all_ws:
            if self._socket_role(candidate) != "browser":
                continue
            key = self.ws_key(candidate)
            if key == exclude_key:
                continue
            ids.append(key)
        return ids
