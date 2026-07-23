#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""I/O, broadcast, and alarm mixin for SessionRuntime.

Provides ``_SessionRuntimeIoMixin`` with request helpers, hijack state
broadcast, worker I/O, and the alarm handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_tracer

from provide.uterm.control_channel import encode_control_frame, encode_terminal_data

if TYPE_CHECKING:
    from provide.uterm.cloudflare.bridge.hijack import HijackSession
    from provide.uterm.cloudflare.cf_types import CFWebSocket
    from provide.uterm.cloudflare.do._webhooks import fire_webhooks
    from provide.uterm.cloudflare.do.persistence import clear_lease as _clear_lease
    from provide.uterm.cloudflare.do.persistence import persist_lease as _persist_lease
    from provide.uterm.cloudflare.do.persistence import schedule_alarm
    from provide.uterm.cloudflare.state.registry import KV_REFRESH_S, update_kv_session
    from provide.uterm.cloudflare.state.store import LeaseRecord
else:
    try:
        from provide.uterm.cloudflare.bridge.hijack import HijackSession
        from provide.uterm.cloudflare.cf_types import CFWebSocket
        from provide.uterm.cloudflare.do._webhooks import fire_webhooks
        from provide.uterm.cloudflare.do.persistence import clear_lease as _clear_lease
        from provide.uterm.cloudflare.do.persistence import persist_lease as _persist_lease
        from provide.uterm.cloudflare.do.persistence import schedule_alarm
        from provide.uterm.cloudflare.state.registry import KV_REFRESH_S, update_kv_session
        from provide.uterm.cloudflare.state.store import LeaseRecord
    except Exception:  # pragma: no cover
        from bridge.hijack import HijackSession
        from cf_types import CFWebSocket  # noqa: TC002
        from do._webhooks import fire_webhooks
        from do.persistence import clear_lease as _clear_lease
        from do.persistence import persist_lease as _persist_lease
        from do.persistence import schedule_alarm
        from state.registry import KV_REFRESH_S, update_kv_session
        from state.store import LeaseRecord

from .flow_control import PAUSE as _FLOW_PAUSE

if TYPE_CHECKING:
    from provide.uterm.cloudflare.contracts import RuntimeProtocol

    from .flow_control import FlowController


logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

_MAX_REQUEST_BODY = 65_536  # 64 KB — guard against memory exhaustion in DO sandbox

# Cap on concurrent in-flight webhook-delivery tasks. Delivery is offloaded off
# the broadcast critical path (so a slow webhook can't stall the DO frame loop);
# this bounds how many can pile up before new ones are dropped — preventing an
# unbounded task/fetch backlog under a high-throughput stream.
_MAX_INFLIGHT_WEBHOOKS = 64


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
        _flow: FlowController
        _webhook_tasks: set[asyncio.Task[None]]
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
        saved_meta = self.store.load_session_meta(self.worker_id)
        if saved_meta is not None:
            self.meta = saved_meta
            self._meta_loaded = True
        row = self.store.load_session(self.worker_id)
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
            self.hijack._session = HijackSession(
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
        content_type = str(request.headers.get("Content-Type") or "").lower()
        if "application/json" not in content_type:
            logger.warning("request_json: rejected non-JSON content-type %r", content_type)
            return {}

        body = await request.text()
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
        _persist_lease(self.store, self.ctx, self.worker_id, session, LeaseRecord)

    def clear_lease(self) -> None:
        _clear_lease(self.store, self.worker_id)

    # ------------------------------------------------------------------
    # Hijack state broadcast
    # ------------------------------------------------------------------

    async def send_hijack_state(self, ws: CFWebSocket) -> None:
        ws_id = self.ws_key(ws)
        session = self.hijack.session
        owner = None
        if session is not None:
            owner = "me" if self.browser_hijack_owner.get(ws_id) == session.hijack_id else "other"
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
        for ws_id, ws in list(self.browser_sockets.items()):
            try:
                await self.send_hijack_state(ws)
            except Exception:
                self.browser_sockets.pop(ws_id, None)
                self.browser_hijack_owner.pop(ws_id, None)

    # ------------------------------------------------------------------
    # Worker I/O
    # ------------------------------------------------------------------

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        # ushell acknowledges control frames as no-ops (always returns True).
        if self._ushell is not None:
            await self._ushell.handle_control(action)
            return True
        if self.worker_ws is None:
            return False
        await self.send_ws(
            self.worker_ws,
            {"type": "control", "action": action, "owner": owner, "lease_s": lease_s, "ts": time.time()},
        )
        return True

    async def push_worker_input(self, data: str) -> bool:
        # Route input to ushell when active; fall back to external worker WS.
        if self._ushell is not None:
            frames = await self._ushell.handle_input(data)
            for frame in frames:
                await self.broadcast_to_browsers(frame)
            return True
        if self.worker_ws is None:
            return False
        await self.send_ws(self.worker_ws, {"type": "input", "data": data, "ts": time.time()})
        return True

    def _all_live_sockets(self) -> list[Any]:
        """Enumerate all live WebSockets, resilient to CF hibernation.

        After CF hibernation, in-memory dicts are reset, so ``ctx.getWebSockets()``
        is the source of truth. In local pywrangler dev it returns ``[]`` (no
        hibernation state) or is unavailable — fall back to the in-memory
        ``browser_sockets`` dict in that case.
        """
        try:
            all_ws = list(self.ctx.getWebSockets())
        except Exception:
            all_ws = []
        if not all_ws:
            all_ws = list(self.browser_sockets.values())
        return all_ws

    async def broadcast_to_browsers(self, payload: dict[str, Any]) -> None:
        all_ws = self._all_live_sockets()
        frame_type = str(payload.get("type") or "")
        for ws in all_ws:
            if self._socket_role(ws) != "browser":
                continue
            ws_id = self.ws_key(ws)
            # Tier B: drop high-volume term frames to a congested viewer so one slow
            # viewer can't stall the producer for everyone. It is resynced with a
            # fresh snapshot once it drains below low-water (see _apply_flow_control).
            if frame_type == "term" and self._flow.is_congested(ws_id):
                continue
            try:
                encoded = (
                    encode_terminal_data(str(payload.get("data", "")))
                    if frame_type in {"input", "term"}
                    else encode_control_frame(payload)
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
                    # Record bytes sent for ACK-driven backpressure (Tier A).
                    self._flow.on_sent(ws_id, msg_len)
                finally:
                    # Always release the reservation, even if send_ws raises —
                    # otherwise a transient send failure leaks _queue_bytes
                    # upward until every future broadcast trips the buffer-full
                    # guard and is dropped.
                    self._queue_bytes = max(0, self._queue_bytes - msg_len)
            except Exception:
                self.browser_sockets.pop(ws_id, None)
                self.browser_hijack_owner.pop(ws_id, None)
                self._flow.forget(ws_id)
        await self._apply_flow_control()

    async def _apply_flow_control(self) -> None:
        """Emit a producer pause/resume hint and resync any recovered viewers."""
        action = self._flow.decide(time.monotonic())
        if action is not None:
            await self._signal_worker_flow(action)
        # Tier B: a viewer that just drained below low-water had term frames
        # dropped while congested, so its screen is stale. Pull a fresh snapshot
        # from the worker (broadcast to all — an idempotent repaint) to resync it.
        if self._flow.take_recovered():
            await self._request_worker_snapshot()

    async def _request_worker_snapshot(self) -> None:
        """Ask the producer for a fresh full-screen snapshot (Tier B resync).

        For an in-DO ushell producer, handle_control returns the snapshot frames
        directly, which we broadcast (idempotent repaint). An external worker
        replies asynchronously over its WS → broadcast_worker_frame.
        """
        if self._ushell is not None:
            for frame in await self._ushell.handle_control("snapshot_request"):
                await self.broadcast_to_browsers(frame)
            return
        if self.worker_ws is None:
            return
        await self.send_ws(self.worker_ws, {"type": "control", "action": "snapshot_request", "ts": time.time()})

    async def _signal_worker_flow(self, action: str) -> None:
        """Send a Tier-A flow-control hint to the producer.

        Uses ``flow_pause``/``flow_resume`` — distinct from hijack pause/resume so
        the two never interfere. Routed to the in-DO ushell connector when present,
        otherwise to the external worker WS.
        """
        control = "flow_pause" if action == _FLOW_PAUSE else "flow_resume"
        if self._ushell is not None:
            await self._ushell.handle_control(control)
            return
        if self.worker_ws is None:
            return
        await self.send_ws(self.worker_ws, {"type": "control", "action": control, "ts": time.time()})

    async def note_browser_ack(self, ws_id: str, acked_bytes: int) -> None:
        """Record a browser's cumulative-bytes ACK and re-evaluate backpressure."""
        self._flow.on_ack(ws_id, acked_bytes, time.monotonic())
        await self._apply_flow_control()

    def _spawn_webhook_delivery(self, event: dict[str, Any]) -> None:
        """Deliver matching webhooks OFF the broadcast critical path.

        ``fire_webhooks`` awaits ``fetch`` inline; doing that here would stall the
        single-threaded DO event loop — and thus the entire PTY stream for this
        session — whenever a webhook URL is slow or blackholed. Instead we run
        delivery as a tracked background task. The set both keeps the task from
        being garbage-collected mid-flight and bounds how many can pile up: over
        ``_MAX_INFLIGHT_WEBHOOKS`` new deliveries are dropped rather than allowed
        to grow without bound under a high-throughput stream.
        """
        webhooks = self.store.load_webhooks(self.worker_id)
        if not webhooks:
            return  # common case — nothing configured, don't schedule a task
        if len(self._webhook_tasks) >= _MAX_INFLIGHT_WEBHOOKS:
            logger.warning(
                "webhook delivery backlog full (%d) — dropping delivery for event %s",
                _MAX_INFLIGHT_WEBHOOKS,
                event.get("type"),
            )
            return
        task = asyncio.create_task(fire_webhooks(cast("RuntimeProtocol", self), event, _webhooks=webhooks))
        self._webhook_tasks.add(task)
        task.add_done_callback(self._webhook_tasks.discard)

    async def broadcast_worker_frame(self, payload: dict[str, Any]) -> None:
        event = self.store.append_event(self.worker_id, str(payload.get("type") or "event"), payload)
        await self.broadcast_to_browsers(payload)
        self._spawn_webhook_delivery(event)

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

        for ws_id, ws in list(self.raw_sockets.items()):
            try:
                await self._send_text(ws, text_payload)
            except Exception:
                self.raw_sockets.pop(ws_id, None)

    async def alarm(self) -> None:
        mono_now = time.monotonic()
        wall_now = time.time()
        session = self.hijack.session
        if session is not None and session.lease_expires_at <= mono_now:
            logger.info("alarm: auto-releasing expired lease owner=%s", session.owner)
            self.hijack.release(session.hijack_id)
            self.clear_lease()
            with contextlib.suppress(Exception):
                await self.push_worker_control("resume", owner="lease_expired", lease_s=0)
            await self.broadcast_hijack_state()
        if self.worker_ws is not None or (self._ushell is not None and self._ushell_started):
            await update_kv_session(
                self.env,
                self.worker_id,
                connected=True,
                hijacked=self.hijack.session is not None,
                input_mode=self.input_mode,
                meta=self.meta,
            )
            schedule_alarm(self.ctx, wall_now + KV_REFRESH_S)
        elif self.hijack.session is not None:
            # ``self.hijack.session.lease_expires_at`` is a non-None float
            # (the surrounding branch already gated on ``session is not
            # None``), so ``_mono_to_wall`` cannot return None here.
            lease_wall = _mono_to_wall(self.hijack.session.lease_expires_at)
            assert lease_wall is not None
            schedule_alarm(self.ctx, lease_wall)
