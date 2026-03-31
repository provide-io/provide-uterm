#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI tunnel routes — binary-framed WebSocket endpoint.

Registers ``/tunnel/{worker_id}`` which accepts the binary tunnel protocol
and bridges it to TermHub (the same hub used by the legacy text protocol).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Annotated, Any, cast

from provide.telemetry import get_logger

try:
    from fastapi import APIRouter, Path, WebSocket, WebSocketDisconnect
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required: pip install 'provide-terminal[websocket]'") from _e

from provide.terminal.hijack.frames import (
    make_term_frame,
    make_worker_connected_frame,
    make_worker_disconnected_frame,
)
from provide.terminal.tunnel.protocol import (
    CHANNEL_DATA,
    decode_control,
    decode_frame,
)

if TYPE_CHECKING:
    from provide.terminal.hijack.hub import TermHub

logger = get_logger(__name__)
_HIJACK_CLEANUP_INTERVAL_S = 1.0


async def _periodic_hijack_cleanup(hub: TermHub, worker_id: str) -> None:  # pragma: no cover — cancelled on disconnect
    """Run lease cleanup on a fixed cadence while a WS handler is active."""
    while True:
        await asyncio.sleep(_HIJACK_CLEANUP_INTERVAL_S)
        await hub.cleanup_expired_hijack(worker_id)


def register_tunnel_routes(hub: TermHub, router: APIRouter) -> None:
    """Attach the ``/tunnel/{worker_id}`` WebSocket route to *router*."""

    @router.websocket("/tunnel/{worker_id}")
    async def ws_tunnel(
        websocket: WebSocket,
        worker_id: Annotated[str, Path(pattern=r"^[\w\-]+$")],
    ) -> None:
        # Auth: accept worker_bearer_token OR per-session tunnel tokens.
        auth_header = websocket.headers.get("authorization", "")
        provided = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
        worker_token = hub.worker_token()
        app = cast("Any", websocket.scope.get("app"))
        tunnel_tokens = cast(
            "dict[str, dict[str, str]]", getattr(getattr(app, "state", object()), "uterm_tunnel_tokens", {})
        )
        session_token = str(tunnel_tokens.get(worker_id, {}).get("worker_token", ""))
        valid = False
        if worker_token is not None and secrets.compare_digest(provided, worker_token):
            valid = True
        if session_token and secrets.compare_digest(provided, session_token):
            valid = True
        if (worker_token is not None or session_token) and not valid:
            await websocket.accept()
            await websocket.close(code=1008, reason="authentication required")
            return

        await websocket.accept()
        prev_was_hijacked = await hub.register_worker(worker_id, websocket)
        registry = getattr(getattr(app, "state", object()), "uterm_registry", None)
        if registry is not None:
            await registry.set_tunnel_connected(worker_id, True)
        logger.info("tunnel_worker_connected worker_id=%s", worker_id)
        if prev_was_hijacked:  # pragma: no cover — tested via hub unit tests
            hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
            await hub.broadcast_hijack_state(worker_id)
        await hub.broadcast(worker_id, cast("dict[str, Any]", make_worker_connected_frame(worker_id)))

        cleanup_task = asyncio.create_task(_periodic_hijack_cleanup(hub, worker_id))
        try:
            while True:
                raw = await websocket.receive_bytes()
                if len(raw) < 2:
                    continue
                if not await hub.is_active_worker(worker_id, websocket):  # pragma: no cover
                    with suppress(Exception):
                        await websocket.close()
                    break

                frame = decode_frame(raw)

                if frame.is_control:
                    await _handle_control(hub, websocket, worker_id, frame.payload)
                elif frame.is_eof:
                    logger.info("tunnel_eof worker_id=%s channel=%d", worker_id, frame.channel)
                elif frame.channel >= CHANNEL_DATA and frame.payload:
                    text = frame.payload.decode("utf-8", errors="replace")
                    await hub.broadcast(
                        worker_id,
                        cast("dict[str, Any]", make_term_frame(text, ts=time.time())),
                    )
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover
            logger.warning("tunnel_ws_error worker_id=%s error=%s", worker_id, exc)
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            should_broadcast, was_hijacked = await hub.deregister_worker(worker_id, websocket)
            if registry is not None:
                await registry.set_tunnel_connected(worker_id, False)
            if should_broadcast:
                hub.metric("ws_disconnect_total")
                hub.metric("ws_disconnect_worker_total")
                if was_hijacked:  # pragma: no cover — tested via hub unit tests
                    hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
                    await hub.broadcast_hijack_state(worker_id)
                await hub.broadcast(
                    worker_id,
                    cast("dict[str, Any]", make_worker_disconnected_frame(worker_id)),
                )
            logger.info("tunnel_worker_disconnected worker_id=%s", worker_id)


async def _handle_control(
    hub: TermHub,
    _websocket: WebSocket,
    worker_id: str,
    payload: bytes,
) -> None:
    """Handle a control channel message from the tunnel agent."""
    try:
        msg = decode_control(payload)
    except Exception:
        logger.warning("tunnel_bad_control worker_id=%s", worker_id)
        return

    msg_type = msg.get("type")

    if msg_type == "open":
        mode = msg.get("input_mode", "open")
        if mode in ("hijack", "open"):
            await hub.set_worker_hello_mode(worker_id, mode)
        logger.info(
            "tunnel_open worker_id=%s tunnel_type=%s term_size=%s",
            worker_id,
            msg.get("tunnel_type"),
            msg.get("term_size"),
        )
    elif msg_type == "resize":
        logger.debug("tunnel_resize worker_id=%s %dx%d", worker_id, msg.get("cols", 0), msg.get("rows", 0))
    elif msg_type == "close":
        logger.info("tunnel_close worker_id=%s channel=%d", worker_id, msg.get("channel", 1))
    elif msg_type == "snapshot":
        screen = str(msg.get("screen", ""))
        snapshot: dict[str, Any] = {"type": "snapshot", "screen": screen, "ts": time.time()}
        await hub.update_last_snapshot(worker_id, snapshot)
        await hub.broadcast(worker_id, snapshot)
