#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""I/O, broadcast, and alarm mixin for SessionRuntime.

Provides ``_SessionRuntimeIoMixin`` with request helpers, hijack state
broadcast, worker I/O, and the alarm handler.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_tracer

from provide.uterm.control_channel import encode_control, encode_data

try:
    from provide.uterm.cloudflare.bridge.hijack import HijackSession
    from provide.uterm.cloudflare.cf_types import CFWebSocket
    from provide.uterm.cloudflare.do._webhooks import fire_webhooks
    from provide.uterm.cloudflare.do.persistence import clear_lease as _clear_lease
    from provide.uterm.cloudflare.do.persistence import persist_lease as _persist_lease
    from provide.uterm.cloudflare.state.registry import KV_REFRESH_S, update_kv_session
    from provide.uterm.cloudflare.state.store import LeaseRecord
except Exception:  # pragma: no cover
    from bridge.hijack import HijackSession  # type: ignore[import-not-found,no-redef]
    from cf_types import CFWebSocket  # type: ignore[import-not-found,no-redef]  # noqa: TC002
    from do._webhooks import fire_webhooks  # type: ignore[import-not-found,no-redef]
    from do.persistence import clear_lease as _clear_lease  # type: ignore[import-not-found,no-redef]
    from do.persistence import persist_lease as _persist_lease  # type: ignore[no-redef]
    from state.registry import KV_REFRESH_S, update_kv_session  # type: ignore[import-not-found,no-redef]
    from state.store import LeaseRecord  # type: ignore[import-not-found,no-redef]


logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

_MAX_REQUEST_BODY = 65_536  # 64 KB — guard against memory exhaustion in DO sandbox


def _mono_to_wall(mono: float | None) -> float | None:
    """Convert a monotonic timestamp to wall-clock for API/WS responses."""
    if mono is None:
        return None
    return mono + (time.time() - time.monotonic())


def _wall_to_mono(wall: float) -> float:
    """Convert a persisted wall-clock timestamp back to a monotonic timestamp.

    Persisted leases are stored as wall-clock so they survive a DO restart; the
    in-memory HijackSession uses monotonic time for countdown/alarm scheduling.
    """
    return wall - (time.time() - time.monotonic())


class _SessionRuntimeIoMixin:
    """Mixin providing request helpers, broadcast, worker I/O, and alarm for SessionRuntime.

    The attributes below are *type-only* declarations describing what the
    composing ``SessionRuntime`` class initialises elsewhere — the mixin
    itself never assigns them. Matches the cross-mixin pattern used by
    the server-side hub mixins.
    """

    if TYPE_CHECKING:
        worker_id: str
        max_buffer_bytes: int
        _queue_bytes: int
        store: Any
        ctx: Any
        meta: Any
        _meta_loaded: bool
        _deleted_at: float
        lifecycle_state: str
        hijack: Any
        input_mode: str
        browser_sockets: Any
        browser_hijack_owner: Any
        worker_ws: Any
        _ushell: Any
        raw_sockets: Any
        env: Any
        last_snapshot: Any

        def ws_key(self, ws: Any) -> Any: ...
        def _socket_role(self, ws: Any) -> str: ...
        async def _send_text(self, ws: Any, text: str) -> None: ...
        async def send_ws(self, ws: CFWebSocket, frame: dict[str, object]) -> None: ...

    # ------------------------------------------------------------------
    # State restore (called from SessionRuntime.__init__)
    # ------------------------------------------------------------------

    def _restore_state(self) -> None:
        saved_meta = self.store.load_session_meta(self.worker_id)  # type: ignore[attr-defined]
        if saved_meta is not None:
            self.meta = saved_meta
            self._meta_loaded = True
        row = self.store.load_session(self.worker_id)  # type: ignore[attr-defined]
        if row is None:
            return
        deleted_at = row.get("deleted_at")
        if deleted_at is not None:
            self._deleted_at = float(deleted_at)
            self.lifecycle_state = "deleted"
            return
        hijack_id = row.get("hijack_id")
        owner = row.get("owner")
        lease_expires_at = row.get("lease_expires_at")
        # The persisted value is wall-clock (see persist_lease); compare against
        # wall time, then convert back to monotonic for the in-memory session.
        if (
            isinstance(hijack_id, str)
            and isinstance(owner, str)
            and isinstance(lease_expires_at, (float, int))
            and float(lease_expires_at) > time.time()
        ):
            self.hijack._session = HijackSession(  # type: ignore[attr-defined]
                hijack_id=hijack_id,
                owner=owner,
                lease_expires_at=_wall_to_mono(float(lease_expires_at)),
            )
        snapshot = row.get("last_snapshot")
        if isinstance(snapshot, dict):
            self.last_snapshot = snapshot
        stored_mode = row.get("input_mode")
        if stored_mode in {"hijack", "open"}:
            self.input_mode = stored_mode

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    async def request_json(self, request: Any) -> dict[str, Any]:
        # Require application/json so a CSRF "simple request" (text/plain or a form
        # encoding — both skip the CORS preflight) cannot deliver a parseable body.
        content_type = str(request.headers.get("Content-Type") or "").lower()  # type: ignore[attr-defined]
        if "application/json" not in content_type:
            logger.warning("request_json: rejected non-JSON content-type %r", content_type)
            return {}

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
        await self.send_ws(
            ws,
            {
                "type": "hijack_state",
                "hijacked": session is not None,
                "owner": owner,
                "lease_expires_at": (_mono_to_wall(session.lease_expires_at) if session is not None else None),
                "input_mode": self.input_mode,
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
        await self.send_ws(
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
                # Backpressure logic
                frame_type = str(payload.get("type") or "")
                encoded = (
                    encode_data(str(payload.get("data", "")))
                    if frame_type in {"input", "term"}
                    else encode_control(payload)
                )
                msg_len = len(encoded)
                if self._queue_bytes + msg_len > self.max_buffer_bytes:
                    logger.warning(
                        "cloudflare_runtime_buffer_full id=%s queue=%d msg=%d",
                        self.worker_id,
                        self._queue_bytes,
                        msg_len,
                    )
                    with tracer.start_as_current_span(
                        "uterm.buffer.full",
                        attributes={"worker_id": self.worker_id, "queue_bytes": self._queue_bytes, "msg_len": msg_len},
                    ):
                        pass
                    continue
                self._queue_bytes += msg_len
                try:
                    await self.send_ws(ws, payload)
                finally:
                    # Always release the reservation, even if send_ws raises —
                    # otherwise a transient send failure leaks _queue_bytes
                    # upward until every future broadcast trips the buffer-full
                    # guard and is dropped.
                    self._queue_bytes = max(0, self._queue_bytes - msg_len)
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
            self.last_snapshot = payload
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
                self.worker_id,
                connected=True,
                hijacked=self.hijack.session is not None,  # type: ignore[attr-defined]
                input_mode=self.input_mode,
            )
            if (_s := getattr(self.ctx, "storage", None)) is not None and callable(getattr(_s, "setAlarm", None)):  # type: ignore[attr-defined]
                _s.setAlarm(int((wall_now + KV_REFRESH_S) * 1000))
        elif self.hijack.session is not None:  # type: ignore[attr-defined]
            if (_s := getattr(self.ctx, "storage", None)) is not None and callable(getattr(_s, "setAlarm", None)):  # type: ignore[attr-defined]
                # ``self.hijack.session.lease_expires_at`` is a non-None float
                # (the surrounding branch already gated on ``session is not
                # None``), so ``_mono_to_wall`` cannot return None here.
                lease_wall = _mono_to_wall(self.hijack.session.lease_expires_at)  # type: ignore[attr-defined]
                assert lease_wall is not None
                _s.setAlarm(int(lease_wall * 1000))
