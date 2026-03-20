#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Browser WebSocket message dispatch for the hijack hub.

Called by ``ws_browser_term`` in ``websockets.py`` for each parsed browser frame.
Returns the updated ``owned_hijack`` flag (True = this browser holds the hijack
lease, False = it does not).
"""

from __future__ import annotations

import json
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

from provide.terminal.hijack.models import VALID_ROLES

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.terminal.hijack.hub import TermHub
else:
    WebSocket = Any

logger = get_logger(__name__)


async def handle_browser_message(
    hub: TermHub,
    ws: WebSocket,
    worker_id: str,
    role: str,
    msg_b: dict[str, Any],
    owned_hijack: bool,
) -> bool:
    """Dispatch one parsed browser WS message.

    Returns the updated value of ``owned_hijack`` (unchanged if the message
    type does not affect ownership).
    """
    mtype = msg_b.get("type")

    if mtype == "snapshot_req":
        is_owner = await hub.touch_if_owner(worker_id, ws) is not None
        if is_owner:
            await hub.request_snapshot(worker_id)
        else:
            # Non-owner viewers may request snapshots only when no hijack is
            # active — forwarding during an active hijack disrupts the owner's
            # wait_for_guard prompt detection.
            if not await hub.check_still_hijacked(worker_id):
                await hub.request_snapshot(worker_id)

    elif mtype == "analyze_req":
        if await hub.touch_if_owner(worker_id, ws) is not None:
            await hub.request_analysis(worker_id)

    elif mtype == "heartbeat":
        lease_expires_at = await hub.touch_if_owner(worker_id, ws)
        if lease_expires_at is not None:
            await ws.send_text(
                json.dumps(
                    {"type": "heartbeat_ack", "lease_expires_at": lease_expires_at, "ts": time.time()},
                    ensure_ascii=True,
                )
            )
            await hub.broadcast_hijack_state(worker_id)

    elif mtype == "hijack_request":
        return await _handle_hijack_request(hub, ws, worker_id, role, owned_hijack)

    elif mtype == "hijack_step":
        if await hub.touch_if_owner(worker_id, ws) is not None:
            ok = await hub.send_worker(
                worker_id,
                {"type": "control", "action": "step", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
            )
            if not ok:
                await ws.send_text(
                    json.dumps({"type": "error", "message": "No worker connected for this session."}, ensure_ascii=True)
                )
            else:
                hub.metric("hijack_steps_total")
                await hub.append_event(worker_id, "hijack_step", {"owner": "dashboard_ws"})

    elif mtype == "hijack_release":
        return await _handle_hijack_release(hub, ws, worker_id, owned_hijack)

    elif mtype == "ping":
        with suppress(Exception):
            await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}, ensure_ascii=True))

    elif mtype == "input":
        await _handle_input(hub, ws, worker_id, msg_b)

    return owned_hijack


async def _handle_hijack_request(
    hub: TermHub,
    ws: WebSocket,
    worker_id: str,
    role: str,
    owned_hijack: bool,
) -> bool:
    """Process a hijack_request message; returns updated owned_hijack flag."""
    # Only admins can hijack.
    if role != "admin":
        await ws.send_text(json.dumps({"type": "error", "message": "Hijack requires admin role."}, ensure_ascii=True))
        return owned_hijack
    # Reject in open mode — no exclusive ownership.
    if await hub.is_input_open_mode(worker_id):
        await ws.send_text(
            json.dumps({"type": "error", "message": "Hijack not available in open input mode."}, ensure_ascii=True)
        )
        return owned_hijack
    # Send pause to the worker *before* writing ownership — mirrors REST
    # hijack_acquire so that concurrent acquires see the worker as free
    # while the network send is in flight.
    pause_sent = await hub.send_worker(
        worker_id,
        {"type": "control", "action": "pause", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
    )
    if not pause_sent:
        await ws.send_text(
            json.dumps({"type": "error", "message": "No worker connected for this session."}, ensure_ascii=True)
        )
        await ws.send_text(json.dumps(await hub.hijack_state_msg_for(worker_id, ws), ensure_ascii=True))
        return owned_hijack
    # Worker is paused — now atomically check-and-set ownership.
    acquired, err = await hub.try_acquire_ws_hijack(worker_id, ws)
    if not acquired:
        if err == "already_hijacked":
            hub.metric("hijack_conflicts_total")
        # Compensating resume. Skip for "already_hijacked": sending resume
        # would unpause the legitimate owner's session.
        if err != "already_hijacked":
            await hub.send_worker(
                worker_id,
                {"type": "control", "action": "resume", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
            )
        msg_text = (
            "No worker connected for this session." if err == "no_worker" else "Already hijacked by another client."
        )
        await ws.send_text(json.dumps({"type": "error", "message": msg_text}, ensure_ascii=True))
        await ws.send_text(json.dumps(await hub.hijack_state_msg_for(worker_id, ws), ensure_ascii=True))
        return owned_hijack
    await hub.broadcast_hijack_state(worker_id)
    hub.metric("hijack_acquires_total")
    hub.notify_hijack_changed(worker_id, enabled=True, owner="dashboard")
    await hub.append_event(worker_id, "hijack_acquired", {"owner": "dashboard_ws"})
    return True  # owned_hijack = True


async def _handle_hijack_release(
    hub: TermHub,
    ws: WebSocket,
    worker_id: str,
    owned_hijack: bool,
) -> bool:
    """Process a hijack_release message; returns updated owned_hijack flag."""
    # Atomically check ownership and clear in one lock block to prevent a
    # concurrent hijack_request stealing ownership between check and clear.
    # rest_active is captured inside the same lock block to avoid TOCTOU
    # on _is_rest_session_active after the owner has been cleared.
    released, rest_active = await hub.try_release_ws_hijack(worker_id, ws)
    if released:
        _do_resume = not rest_active
        if _do_resume and await hub.check_still_hijacked(worker_id):
            # Re-check: a concurrent hijack_acquire may have written a new
            # session between try_release_ws_hijack and _send_worker.
            _do_resume = False
        if _do_resume:
            await hub.send_worker(
                worker_id,
                {"type": "control", "action": "resume", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
            )
        await hub.broadcast_hijack_state(worker_id)
        if _do_resume:
            hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
        hub.metric("hijack_releases_total")
        await hub.append_event(worker_id, "hijack_released", {"owner": "dashboard_ws"})
        return False  # owned_hijack = False
    return owned_hijack


async def _handle_input(
    hub: TermHub,
    ws: WebSocket,
    worker_id: str,
    msg_b: dict[str, Any],
) -> None:
    """Process an input message from the browser."""
    can_send = await hub.prepare_browser_input(worker_id, ws)
    if not can_send:
        return
    data = msg_b.get("data", "")
    if data and len(data) > hub.max_input_chars:
        await ws.send_text(json.dumps({"type": "error", "message": "Input too long."}, ensure_ascii=True))
    elif data:
        ok = await hub.send_worker(worker_id, {"type": "input", "data": data, "ts": time.time()})
        if not ok:
            await ws.send_text(json.dumps({"type": "error", "message": "Worker connection lost."}, ensure_ascii=True))
        else:
            await hub.append_event(worker_id, "input_send", {"owner": "dashboard_ws", "keys": data[:120]})


async def _handle_resume(
    hub: TermHub,
    ws: WebSocket,
    worker_id: str,
    role: str,
    msg_b: dict[str, Any],
    owned_hijack: bool,
) -> bool:
    """Process a resume message from the browser. Returns updated owned_hijack."""
    store = hub._resume_store
    if store is None:
        return owned_hijack

    old_token = msg_b.get("token", "")
    if not old_token:
        return owned_hijack

    session = store.get(old_token)
    if session is None or session.worker_id != worker_id:
        return owned_hijack

    # Optional application-level validation
    if hub._on_resume is not None and not await hub._on_resume(old_token, session):
        return owned_hijack

    # Valid resume — revoke old token
    store.revoke(old_token)

    # Restore role if different and valid
    new_role = role
    can_hijack = role == "admin"
    if session.role != role and session.role in VALID_ROLES:
        async with hub._lock:
            st = hub._workers.get(worker_id)
            if st is not None:  # pragma: no branch
                st.browsers[ws] = session.role
        new_role = session.role
        can_hijack = new_role == "admin"

    # Reclaim hijack if was owner and no current hijack
    if session.was_hijack_owner:
        async with hub._lock:
            st = hub._workers.get(worker_id)
            if st is not None and st.hijack_owner is None and not hub.is_hijacked(st):  # pragma: no branch
                st.hijack_owner = ws
                st.hijack_owner_expires_at = time.time() + hub._dashboard_hijack_lease_s
                owned_hijack = True

    # Issue new token for this connection
    new_token = store.create(worker_id, new_role, hub._resume_ttl_s)
    hub._ws_to_resume_token[ws] = new_token

    # Re-read state and send updated hello
    _resumed_state = await hub.register_browser_state_snapshot(worker_id, ws)
    await ws.send_text(
        json.dumps(
            {
                "type": "hello",
                "worker_id": worker_id,
                "can_hijack": can_hijack,
                "hijacked": _resumed_state.get("is_hijacked", False),
                "hijacked_by_me": _resumed_state.get("hijacked_by_me", False),
                "worker_online": _resumed_state.get("worker_online", False),
                "input_mode": _resumed_state.get("input_mode", "hijack"),
                "role": new_role,
                "hijack_control": "ws",
                "hijack_step_supported": True,
                "capabilities": {
                    "hijack_control": "ws",
                    "hijack_step_supported": True,
                },
                "resume_supported": True,
                "resume_token": new_token,
                "resumed": True,
            },
            ensure_ascii=True,
        )
    )
    await ws.send_text(json.dumps(await hub.hijack_state_msg_for(worker_id, ws), ensure_ascii=True))
    logger.info("ws_browser_resumed worker_id=%s role=%s hijack=%s", worker_id, new_role, owned_hijack)
    return owned_hijack
