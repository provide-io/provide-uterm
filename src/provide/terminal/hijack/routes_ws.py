#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocket terminal routes for the hijack hub.

Registers:
- ``/ws/worker/{bot_id}/term``  — worker → hub (terminal output, snapshots)
- ``/ws/bot/{bot_id}/term``     — browser → hub (dashboard viewer + hijack control)
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

try:
    from fastapi import APIRouter, WebSocket, WebSocketDisconnect
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-terminal[websocket]'") from _e

import logging

from provide.terminal.hijack.models import BotTermState, extract_prompt_id

if TYPE_CHECKING:
    from provide.terminal.hijack.hub import TermHub

logger = logging.getLogger(__name__)


def register_ws_routes(hub: TermHub, router: APIRouter) -> None:
    """Attach WebSocket terminal routes to *router*."""

    @router.websocket("/ws/worker/{bot_id}/term")
    async def ws_worker_term(websocket: WebSocket, bot_id: str) -> None:
        await websocket.accept()
        async with hub._lock:
            st = hub._bots.setdefault(bot_id, BotTermState())
            st.worker_ws = websocket
        logger.info("term_worker_connected bot_id=%s", bot_id)
        await hub._request_snapshot(bot_id)

        try:
            while True:
                await hub._cleanup_expired_hijack(bot_id)
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception as exc:  # noqa: S112
                    logger.debug("ws_worker_bad_json bot_id=%s: %s", bot_id, exc)
                    continue
                mtype = msg.get("type")
                if mtype == "term":
                    data = msg.get("data", "")
                    if data:
                        await hub._broadcast(bot_id, {"type": "term", "data": data, "ts": msg.get("ts", time.time())})
                elif mtype == "snapshot":
                    snapshot: dict[str, Any] = {
                        "type": "snapshot",
                        "screen": msg.get("screen", ""),
                        "cursor": msg.get("cursor", {"x": 0, "y": 0}),
                        "cols": int(msg.get("cols", 80) or 80),
                        "rows": int(msg.get("rows", 25) or 25),
                        "screen_hash": msg.get("screen_hash", ""),
                        "cursor_at_end": bool(msg.get("cursor_at_end", True)),
                        "has_trailing_space": bool(msg.get("has_trailing_space", False)),
                        "prompt_detected": msg.get("prompt_detected"),
                        "ts": msg.get("ts", time.time()),
                    }
                    async with hub._lock:
                        st2 = hub._bots.get(bot_id)
                        if st2 is not None:
                            st2.last_snapshot = snapshot
                    await hub._broadcast(bot_id, snapshot)
                    await hub._append_event(
                        bot_id,
                        "snapshot",
                        {"prompt_id": extract_prompt_id(snapshot), "screen_hash": snapshot.get("screen_hash")},
                    )
                elif mtype == "analysis":
                    await hub._broadcast(
                        bot_id,
                        {
                            "type": "analysis",
                            "formatted": msg.get("formatted", ""),
                            "raw": msg.get("raw"),
                            "ts": msg.get("ts", time.time()),
                        },
                    )
                elif mtype == "status":
                    await hub._broadcast(bot_id, msg)
                    await hub._append_event(bot_id, "worker_status", {"status": msg})
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover
            logger.warning("term_worker_ws_error bot_id=%s error=%s", bot_id, exc)
        finally:
            async with hub._lock:
                st3 = hub._bots.get(bot_id)
                if st3 is not None and st3.worker_ws is websocket:
                    st3.worker_ws = None
            logger.info("term_worker_disconnected bot_id=%s", bot_id)
            await hub._prune_if_idle(bot_id)

    @router.websocket("/ws/bot/{bot_id}/term")
    async def ws_browser_term(websocket: WebSocket, bot_id: str) -> None:
        await websocket.accept()
        # Capture all startup state atomically while registering the browser.
        async with hub._lock:
            st = hub._bots.setdefault(bot_id, BotTermState())
            st.browsers.add(websocket)
            is_hijacked = hub._is_hijacked(st)
            hijacked_by_me = hub._is_dashboard_hijack_active(st) and st.hijack_owner is websocket
            initial_snapshot = st.last_snapshot  # captured under lock to avoid stale read

        await websocket.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "bot_id": bot_id,
                    "can_hijack": True,
                    "hijacked": is_hijacked,
                    "hijacked_by_me": hijacked_by_me,
                },
                ensure_ascii=True,
            )
        )
        await websocket.send_text(json.dumps(await hub._hijack_state_msg_for(bot_id, websocket), ensure_ascii=True))

        if initial_snapshot is not None:
            await websocket.send_text(json.dumps(initial_snapshot, ensure_ascii=True))
        else:
            await hub._request_snapshot(bot_id)

        try:
            while True:
                await hub._cleanup_expired_hijack(bot_id)
                raw = await websocket.receive_text()
                try:
                    msg_b: dict[str, Any] = json.loads(raw)
                except Exception as exc:  # noqa: S112
                    logger.debug("ws_browser_bad_json bot_id=%s: %s", bot_id, exc)
                    continue
                mtype = msg_b.get("type")

                if mtype == "snapshot_req":
                    # Atomically verify ownership and extend lease in one lock block.
                    await hub._touch_if_owner(bot_id, websocket)
                    await hub._request_snapshot(bot_id)

                elif mtype == "analyze_req":
                    await hub._touch_if_owner(bot_id, websocket)
                    await hub._request_analysis(bot_id)

                elif mtype == "heartbeat":
                    lease_expires_at = await hub._touch_if_owner(bot_id, websocket)
                    if lease_expires_at is not None:
                        await websocket.send_text(
                            json.dumps(
                                {"type": "heartbeat_ack", "lease_expires_at": lease_expires_at, "ts": time.time()},
                                ensure_ascii=True,
                            )
                        )
                        await hub._broadcast_hijack_state(bot_id)

                elif mtype == "hijack_request":
                    acquired, err = await hub._try_acquire_ws_hijack(bot_id, websocket)
                    if acquired:
                        ok = await hub._send_worker(
                            bot_id,
                            {
                                "type": "control",
                                "action": "pause",
                                "owner": "dashboard",
                                "lease_s": 0,
                                "ts": time.time(),
                            },
                        )
                        if not ok:
                            # Atomically verify we still own the hijack and clear
                            # it. If a concurrent request stole ownership between
                            # _try_acquire_ws_hijack and here, _try_release_ws_hijack
                            # returns (False, ...) and we skip the notify — preventing
                            # a spurious on_hijack_changed(enabled=False) while another
                            # client legitimately holds the lease.
                            released, rest_active = await hub._try_release_ws_hijack(bot_id, websocket)
                            if released and not rest_active:
                                hub._notify_hijack_changed(bot_id, enabled=False, owner=None)
                            await websocket.send_text(
                                json.dumps(
                                    {"type": "error", "message": "No worker connected for this bot."}, ensure_ascii=True
                                )
                            )
                            await hub._broadcast_hijack_state(bot_id)
                            continue
                        await hub._broadcast_hijack_state(bot_id)
                        hub._notify_hijack_changed(bot_id, enabled=True, owner="dashboard")
                        await hub._append_event(bot_id, "hijack_acquired", {"owner": "dashboard_ws"})
                    else:
                        msg_text = (
                            "No worker connected for this bot."
                            if err == "no_worker"
                            else "Already hijacked by another client."
                        )
                        await websocket.send_text(json.dumps({"type": "error", "message": msg_text}, ensure_ascii=True))
                        await websocket.send_text(
                            json.dumps(await hub._hijack_state_msg_for(bot_id, websocket), ensure_ascii=True)
                        )

                elif mtype == "hijack_step":
                    if await hub._touch_if_owner(bot_id, websocket) is not None:
                        ok = await hub._send_worker(
                            bot_id,
                            {
                                "type": "control",
                                "action": "step",
                                "owner": "dashboard",
                                "lease_s": 0,
                                "ts": time.time(),
                            },
                        )
                        if not ok:
                            await websocket.send_text(
                                json.dumps(
                                    {"type": "error", "message": "No worker connected for this bot."}, ensure_ascii=True
                                )
                            )
                        else:
                            await hub._append_event(bot_id, "hijack_step", {"owner": "dashboard_ws"})

                elif mtype == "hijack_release":
                    # Atomically check ownership and clear in one lock block to
                    # prevent a concurrent hijack_request stealing ownership
                    # between _is_owner() and _set_hijack_owner(None).
                    # rest_active is captured inside the same lock block to
                    # avoid a post-release TOCTOU on _is_rest_session_active.
                    released, rest_active = await hub._try_release_ws_hijack(bot_id, websocket)
                    if released:
                        await hub._send_worker(
                            bot_id,
                            {
                                "type": "control",
                                "action": "resume",
                                "owner": "dashboard",
                                "lease_s": 0,
                                "ts": time.time(),
                            },
                        )
                        await hub._broadcast_hijack_state(bot_id)
                        if not rest_active:
                            hub._notify_hijack_changed(bot_id, enabled=False, owner=None)
                        await hub._append_event(bot_id, "hijack_released", {"owner": "dashboard_ws"})

                elif mtype == "ping":
                    pass  # keepalive — TCP ACK is sufficient, no response needed

                elif mtype == "input":
                    if await hub._touch_if_owner(bot_id, websocket) is not None:
                        data = msg_b.get("data", "")
                        if data:
                            ok = await hub._send_worker(bot_id, {"type": "input", "data": data, "ts": time.time()})
                            if not ok:
                                await websocket.send_text(
                                    json.dumps(
                                        {"type": "error", "message": "Worker connection lost."}, ensure_ascii=True
                                    )
                                )
                            else:
                                await hub._append_event(
                                    bot_id, "hijack_send", {"owner": "dashboard_ws", "keys": data[:120]}
                                )

        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover
            logger.warning("term_browser_ws_error bot_id=%s error=%s", bot_id, exc)
        finally:
            # Atomically detect ownership, capture REST-session liveness, and clear
            # the owner — all in one lock block to avoid the TOCTOU window where
            # _is_owner() returns True but another coroutine steals hijack_owner
            # (or vice-versa), and to avoid a second lock round-trip for
            # _is_rest_session_active after the owner has already been cleared.
            was_owner = False
            rest_still_active = False
            async with hub._lock:
                st3 = hub._bots.get(bot_id)
                was_owner = st3 is not None and hub._is_dashboard_hijack_active(st3) and st3.hijack_owner is websocket
                if st3 is not None:
                    st3.browsers.discard(websocket)
                    if was_owner:
                        st3.hijack_owner = None
                        st3.hijack_owner_expires_at = None
                        rest_still_active = hub._is_rest_session_active(st3)
            if was_owner:
                await hub._send_worker(
                    bot_id,
                    {"type": "control", "action": "resume", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
                )
                await hub._broadcast_hijack_state(bot_id)
                if not rest_still_active:
                    hub._notify_hijack_changed(bot_id, enabled=False, owner=None)
                await hub._append_event(bot_id, "hijack_released", {"owner": "dashboard_ws_disconnect"})
            await hub._prune_if_idle(bot_id)
