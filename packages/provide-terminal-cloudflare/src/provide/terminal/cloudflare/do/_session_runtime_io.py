#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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
    from provide.terminal.cloudflare.bridge.hijack import HijackSession
    from provide.terminal.cloudflare.cf_types import CFWebSocket
    from provide.terminal.cloudflare.do._webhooks import fire_webhooks
    from provide.terminal.cloudflare.do.persistence import clear_lease as _clear_lease
    from provide.terminal.cloudflare.do.persistence import persist_lease as _persist_lease
    from provide.terminal.cloudflare.state.registry import KV_REFRESH_S, update_kv_session
    from provide.terminal.cloudflare.state.store import LeaseRecord
except Exception:  # pragma: no cover
    from bridge.hijack import HijackSession  # type: ignore[import-not-found]
    from cf_types import CFWebSocket  # type: ignore[import-not-found]  # noqa: TC002
    from do._webhooks import fire_webhooks  # type: ignore[import-not-found]
    from do.persistence import clear_lease as _clear_lease  # type: ignore[import-not-found]
    from do.persistence import persist_lease as _persist_lease  # type: ignore[import-not-found]
    from state.registry import KV_REFRESH_S, update_kv_session  # type: ignore[import-not-found]
    from state.store import LeaseRecord  # type: ignore[import-not-found]


logger = logging.getLogger(__name__)

_MAX_REQUEST_BODY = 65_536  # 64 KB — guard against memory exhaustion in DO sandbox


def _mono_to_wall(mono: float | None) -> float | None:
    """Convert a monotonic timestamp to wall-clock for API/WS responses."""
    if mono is None:
        return None
    return mono + (time.time() - time.monotonic())


class _SessionRuntimeIoMixin:
    """Mixin providing request helpers, broadcast, worker I/O, and alarm for SessionRuntime."""

    # ------------------------------------------------------------------
    # State restore (called from SessionRuntime.__init__)
    # ------------------------------------------------------------------

    def _restore_state(self) -> None:
        saved_meta = self.store.load_session_meta(self.worker_id)  # type: ignore[attr-defined]
        if saved_meta is not None:
            self.meta = saved_meta  # type: ignore[attr-defined]
            self._meta_loaded = True  # type: ignore[attr-defined]
        row = self.store.load_session(self.worker_id)  # type: ignore[attr-defined]
        if row is None:
            return
        deleted_at = row.get("deleted_at")
        if deleted_at is not None:
            self._deleted_at = float(deleted_at)  # type: ignore[attr-defined]
            self.lifecycle_state = "deleted"  # type: ignore[attr-defined]
            return
        hijack_id = row.get("hijack_id")
        owner = row.get("owner")
        lease_expires_at = row.get("lease_expires_at")
        if (
            isinstance(hijack_id, str)
            and isinstance(owner, str)
            and isinstance(lease_expires_at, (float, int))
            and float(lease_expires_at) > time.monotonic()
        ):
            self.hijack._session = HijackSession(  # type: ignore[attr-defined]
                hijack_id=hijack_id,
                owner=owner,
                lease_expires_at=float(lease_expires_at),
            )
        snapshot = row.get("last_snapshot")
        if isinstance(snapshot, dict):
            self.last_snapshot = snapshot  # type: ignore[attr-defined]
        stored_mode = row.get("input_mode")
        if stored_mode in {"hijack", "open"}:
            self.input_mode = stored_mode  # type: ignore[attr-defined]

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
                "lease_expires_at": (_mono_to_wall(session.lease_expires_at) if session is not None else None),
                "input_mode": self.input_mode,  # type: ignore[attr-defined]
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
        # ushell acknowledges control frames as no-ops (always returns True).
        if self._ushell is not None:  # type: ignore[attr-defined]
            await self._ushell.handle_control(action)  # type: ignore[attr-defined]
            return True
        if self.worker_ws is None:  # type: ignore[attr-defined]
            return False
        await self.send_ws(  # type: ignore[attr-defined]
            self.worker_ws,  # type: ignore[attr-defined]
            {"type": "control", "action": action, "owner": owner, "lease_s": lease_s, "ts": time.time()},
        )
        return True

    async def push_worker_input(self, data: str) -> bool:
        # Route input to ushell when active; fall back to external worker WS.
        if self._ushell is not None:  # type: ignore[attr-defined]
            frames = await self._ushell.handle_input(data)  # type: ignore[attr-defined]
            for frame in frames:
                await self.broadcast_to_browsers(frame)
            return True
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
        event = self.store.append_event(self.worker_id, str(payload.get("type") or "event"), payload)  # type: ignore[attr-defined]
        await self.broadcast_to_browsers(payload)
        await fire_webhooks(self, event)  # type: ignore[arg-type]

        text_payload: str | None = None
        frame_type = str(payload.get("type") or "")
        if frame_type == "term":
            text_payload = str(payload.get("data") or "")
        elif frame_type == "snapshot":
            screen = payload.get("screen")
            text_payload = str(screen) if screen is not None else ""
            self.last_snapshot = payload  # type: ignore[attr-defined]
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
        mono_now = time.monotonic()
        wall_now = time.time()
        session = self.hijack.session  # type: ignore[attr-defined]
        if session is not None and session.lease_expires_at <= mono_now:
            logger.info("alarm: auto-releasing expired lease owner=%s", session.owner)
            self.hijack.release(session.hijack_id)  # type: ignore[attr-defined]
            self.clear_lease()
            with contextlib.suppress(Exception):
                await self.push_worker_control("resume", owner="lease_expired", lease_s=0)
            await self.broadcast_hijack_state()
        if self.worker_ws is not None or self._ushell is not None:  # type: ignore[attr-defined]
            await update_kv_session(
                self.env,  # type: ignore[attr-defined]
                self.worker_id,  # type: ignore[attr-defined]
                connected=True,
                hijacked=self.hijack.session is not None,  # type: ignore[attr-defined]
                input_mode=self.input_mode,  # type: ignore[attr-defined]
            )
            if (_s := getattr(self.ctx, "storage", None)) is not None and callable(getattr(_s, "setAlarm", None)):  # type: ignore[attr-defined]
                _s.setAlarm(int((wall_now + KV_REFRESH_S) * 1000))
        elif self.hijack.session is not None:  # type: ignore[attr-defined]
            if (_s := getattr(self.ctx, "storage", None)) is not None and callable(getattr(_s, "setAlarm", None)):  # type: ignore[attr-defined]
                _s.setAlarm(int(_mono_to_wall(self.hijack.session.lease_expires_at) * 1000))  # type: ignore[attr-defined]
