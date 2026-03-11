#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""WebSocket helper mixin for SessionRuntime.

Extracted from ``session_runtime.py`` to keep file sizes under 500 LOC.
Provides socket keying, role resolution, registration, and send helpers.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)


class _WsHelperMixin:
    """Mixin providing WebSocket helper methods for SessionRuntime."""

    def ws_key(self, ws: Any) -> str:
        try:
            existing = getattr(ws, "_ut_ws_key", None)
            if isinstance(existing, str) and existing:
                return existing
        except Exception:
            existing = None

        key = f"{time.time_ns()}_{secrets.token_hex(4)}"
        with contextlib.suppress(Exception):
            ws._ut_ws_key = key
        return key

    def _socket_role(self, ws: Any) -> str:
        """Return the socket type: ``"browser"``, ``"worker"``, or ``"raw"``."""
        try:
            attachment = ws.deserializeAttachment()
            if isinstance(attachment, str):
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

    def _socket_browser_role(self, ws: Any) -> str:
        """Return the JWT-resolved browser role from the socket attachment.

        Defaults to ``"admin"`` in ``none``/``dev`` mode (open access).  In
        ``jwt`` mode, falls back to ``"viewer"`` (fail-closed) when the
        attachment cannot be read — e.g. after hibernation for a connection
        whose ``serializeAttachment`` call raised at connect time.
        """
        try:
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
        if self.config.jwt.mode not in {"none", "dev"}:  # type: ignore[attr-defined]
            logger.warning("browser role unavailable (post-hibernation fallback), defaulting to viewer")
        return "admin" if self.config.jwt.mode in {"none", "dev"} else "viewer"  # type: ignore[attr-defined]

    def _socket_worker_id(self, ws: Any) -> str:
        """Return the worker_id from the socket attachment (stored at connect time).

        Falls back to ``self.worker_id`` when not encoded in the attachment
        (e.g. legacy connections, test sockets without serialized attachment).
        """
        try:
            attachment = ws.deserializeAttachment()
            if isinstance(attachment, str):
                parts = attachment.split(":", 2)
                if len(parts) >= 3 and parts[2]:
                    return parts[2]
        except Exception as exc:
            logger.debug("failed to deserialize worker_id from attachment: %s", exc)
        return self.worker_id  # type: ignore[attr-defined]

    def _register_socket(self, ws: Any, role: str) -> None:
        ws_id = self.ws_key(ws)
        if role == "worker":
            self.worker_ws = ws  # type: ignore[attr-defined]
            return
        if role == "raw":
            self.raw_sockets[ws_id] = ws  # type: ignore[attr-defined]
            return
        self.browser_sockets[ws_id] = ws  # type: ignore[attr-defined]

    def _remove_ws(self, ws: Any) -> None:
        """Remove *ws* from all socket registries (worker, browser, raw)."""
        ws_id = self.ws_key(ws)
        if ws is self.worker_ws:  # type: ignore[attr-defined]
            self.worker_ws = None  # type: ignore[attr-defined]
        self.browser_sockets.pop(ws_id, None)  # type: ignore[attr-defined]
        self.raw_sockets.pop(ws_id, None)  # type: ignore[attr-defined]
        self.browser_hijack_owner.pop(ws_id, None)  # type: ignore[attr-defined]

    async def send_ws(self, ws: Any, payload: dict[str, Any]) -> None:
        await self._send_text(ws, json.dumps(payload, ensure_ascii=True))

    async def _send_text(self, ws: Any, payload: str) -> None:
        result = ws.send(payload)
        if inspect.isawaitable(result):
            await result
