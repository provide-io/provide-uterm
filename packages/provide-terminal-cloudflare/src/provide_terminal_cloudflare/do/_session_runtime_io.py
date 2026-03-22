#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""I/O, broadcast, and alarm mixin for SessionRuntime.

Extracted from ``session_runtime.py`` to keep file sizes under 500 LOC.
Provides ``_SessionRuntimeIoMixin`` with request helpers, hijack state
broadcast, worker I/O, and the alarm handler.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

try:
    from provide_terminal_cloudflare.bridge.hijack import HijackSession
    from provide_terminal_cloudflare.cf_types import CFWebSocket
    from provide_terminal_cloudflare.do.persistence import clear_lease as _clear_lease
    from provide_terminal_cloudflare.do.persistence import persist_lease as _persist_lease
    from provide_terminal_cloudflare.state.registry import KV_REFRESH_S, update_kv_session
    from provide_terminal_cloudflare.state.store import LeaseRecord
except Exception:
    from bridge.hijack import HijackSession  # type: ignore[import-not-found]  # noqa: TC002
    from cf_types import CFWebSocket  # type: ignore[import-not-found]  # noqa: TC002
    from do.persistence import clear_lease as _clear_lease  # type: ignore[import-not-found]
    from do.persistence import persist_lease as _persist_lease  # type: ignore[import-not-found]
    from state.registry import KV_REFRESH_S, update_kv_session  # type: ignore[import-not-found]
    from state.store import LeaseRecord  # type: ignore[import-not-found]


logger = logging.getLogger(__name__)

_MAX_REQUEST_BODY = 65_536  # 64 KB — guard against memory exhaustion in DO sandbox


class _SessionRuntimeIoMixin:
    """Mixin providing request helpers, broadcast, worker I/O, and alarm for SessionRuntime."""

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    async def request_json(self, request: object) -> dict[str, Any]:
        body = await request.text()  # type: ignore[attr-defined]
        if not body:
            return {}
        if len(body) > _MAX_REQUEST_BODY:
            logger.warning("request_json: body too large (%d bytes), rejecting", len(body))
            return {}
        value = json.loads(body)
        if not isinstance(value, dict):
            return {}
        return value

    def persist_lease(self, session: HijackSession | None) -> None:
        _persist_lease(self.store, self.ctx, self.worker_id, session, LeaseRecord)  # type: ignore[attr-defined]

    def clear_lease(self) -> None:
        _clear_lease(self.store, self.worker_id)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Hijack state broadcast
    # ------------------------------------------------------------------

    async def send_hijack_state(self, ws: CFWebSocket) -> None:
        ws_id = self.ws_key(ws)  # type: ignore[attr-defined]
        session = self.hijack.session  # type: ignore[attr-defined]
        owner = None
        if session is not None:
            owner = "me" if self.browser_hijack_owner.get(ws_id) == session.hijack_id else "other"  # type: ignore[attr-defined]
        await self.send_ws(  # type: ignore[attr-defined]
            ws,
            {
                "type": "hijack_state",
                "hijacked": session is not None,
                "owner": owner,
                "lease_expires_at": (session.lease_expires_at if session is not None else None),
                "ts": time.time(),
            },
        )

    async def broadcast_hijack_state(self) -> None:
        for ws_id, ws in list(self.browser_sockets.items()):  # type: ignore[attr-defined]
            try:
                await self.send_hijack_state(ws)
            except Exception:
                self.browser_sockets.pop(ws_id, None)  # type: ignore[attr-defined]
                self.browser_hijack_owner.pop(ws_id, None)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Worker I/O
    # ------------------------------------------------------------------

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        if self.worker_ws is None:  # type: ignore[attr-defined]
            return False
        await self.send_ws(  # type: ignore[attr-defined]
            self.worker_ws,  # type: ignore[attr-defined]
            {"type": "control", "action": action, "owner": owner, "lease_s": lease_s, "ts": time.time()},
        )
        return True

    async def push_worker_input(self, data: str) -> bool:
        if self.worker_ws is None:  # type: ignore[attr-defined]
            return False
        await self.send_ws(self.worker_ws, {"type": "input", "data": data, "ts": time.time()})  # type: ignore[attr-defined]
        return True

    async def broadcast_to_browsers(self, payload: dict[str, Any]) -> None:
        # After CF hibernation, in-memory dicts are reset. Use ctx.getWebSockets()
        # to enumerate all live sockets. In local pywrangler dev, ctx.getWebSockets()
        # returns [] (no hibernation state) — fall back to the in-memory dict when empty.
        try:
            all_ws = list(self.ctx.getWebSockets())  # type: ignore[attr-defined]
        except Exception:
            all_ws = []
        if not all_ws:
            all_ws = list(self.browser_sockets.values())  # type: ignore[attr-defined]
        for ws in all_ws:
            if self._socket_role(ws) != "browser":  # type: ignore[attr-defined]
                continue
            ws_id = self.ws_key(ws)  # type: ignore[attr-defined]
            try:
                await self.send_ws(ws, payload)  # type: ignore[attr-defined]
            except Exception:
                self.browser_sockets.pop(ws_id, None)  # type: ignore[attr-defined]
                self.browser_hijack_owner.pop(ws_id, None)  # type: ignore[attr-defined]

    async def broadcast_worker_frame(self, payload: dict[str, Any]) -> None:
        self.store.append_event(self.worker_id, str(payload.get("type") or "event"), payload)  # type: ignore[attr-defined]
        await self.broadcast_to_browsers(payload)

        text_payload: str | None = None
        frame_type = str(payload.get("type") or "")
        if frame_type == "term":
            text_payload = str(payload.get("data") or "")
        elif frame_type == "snapshot":
            screen = payload.get("screen")
            text_payload = str(screen) if screen is not None else ""
        elif frame_type == "worker_connected":
            text_payload = "\r\n[worker connected]\r\n"
        elif frame_type == "worker_disconnected":
            text_payload = "\r\n[worker disconnected]\r\n"

        if text_payload is None:
            return

        for ws_id, ws in list(self.raw_sockets.items()):  # type: ignore[attr-defined]
            try:
                await self._send_text(ws, text_payload)  # type: ignore[attr-defined]
            except Exception:
                self.raw_sockets.pop(ws_id, None)  # type: ignore[attr-defined]

    async def alarm(self) -> None:
        now = time.time()
        session = self.hijack.session  # type: ignore[attr-defined]
        if session is not None and session.lease_expires_at <= now:
            logger.info("alarm: auto-releasing expired lease owner=%s", session.owner)
            self.hijack.release(session.hijack_id)  # type: ignore[attr-defined]
            self.clear_lease()
            with contextlib.suppress(Exception):
                await self.push_worker_control("resume", owner="lease_expired", lease_s=0)
            await self.broadcast_hijack_state()
        if self.worker_ws is not None:  # type: ignore[attr-defined]
            await update_kv_session(
                self.env,  # type: ignore[attr-defined]
                self.worker_id,  # type: ignore[attr-defined]
                connected=True,
                hijacked=self.hijack.session is not None,  # type: ignore[attr-defined]
                input_mode=self.input_mode,  # type: ignore[attr-defined]
            )
            if (_s := getattr(self.ctx, "storage", None)) is not None and callable(getattr(_s, "setAlarm", None)):  # type: ignore[attr-defined]
                _s.setAlarm(int((now + KV_REFRESH_S) * 1000))
        elif self.hijack.session is not None:  # type: ignore[attr-defined]
            if (_s := getattr(self.ctx, "storage", None)) is not None and callable(getattr(_s, "setAlarm", None)):  # type: ignore[attr-defined]
                _s.setAlarm(int(self.hijack.session.lease_expires_at * 1000))  # type: ignore[attr-defined]
