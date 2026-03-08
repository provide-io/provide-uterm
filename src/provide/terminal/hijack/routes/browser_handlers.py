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
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.terminal.hijack.hub import TermHub
else:
    WebSocket = Any

logger = logging.getLogger(__name__)


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
        is_owner = await hub._touch_if_owner(worker_id, ws) is not None
        if is_owner:
            await hub._request_snapshot(worker_id)
        else:
            # Non-owner viewers may request snapshots only when no hijack is
            # active — forwarding during an active hijack disrupts the owner's
            # _wait_for_guard prompt detection.
            async with hub._lock:
                _st = hub._workers.get(worker_id)
                _hijack_active = _st is not None and hub._is_hijacked(_st)
            if not _hijack_active:
                await hub._request_snapshot(worker_id)

    elif mtype == "analyze_req":
        if await hub._touch_if_owner(worker_id, ws) is not None:
            await hub._request_analysis(worker_id)

    elif mtype == "heartbeat":
        lease_expires_at = await hub._touch_if_owner(worker_id, ws)
        if lease_expires_at is not None:
            await ws.send_text(
                json.dumps(
                    {"type": "heartbeat_ack", "lease_expires_at": lease_expires_at, "ts": time.time()},
                    ensure_ascii=True,
                )
            )
            await hub._broadcast_hijack_state(worker_id)

    elif mtype == "hijack_request":
        # Only admins can hijack.
        if role != "admin":
            await ws.send_text(
                json.dumps({"type": "error", "message": "Hijack requires admin role."}, ensure_ascii=True)
            )
            return owned_hijack
        # Reject in open mode — no exclusive ownership.
        async with hub._lock:
            _st_mode = hub._workers.get(worker_id)
            _is_open = _st_mode is not None and _st_mode.input_mode == "open"
        if _is_open:
            await ws.send_text(
                json.dumps({"type": "error", "message": "Hijack not available in open input mode."}, ensure_ascii=True)
            )
            return owned_hijack
        # Send pause to the worker *before* writing ownership — mirrors REST
        # hijack_acquire so that concurrent acquires see the worker as free
        # while the network send is in flight.
        pause_sent = await hub._send_worker(
            worker_id,
            {"type": "control", "action": "pause", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
        )
        if not pause_sent:
            await ws.send_text(
                json.dumps({"type": "error", "message": "No worker connected for this worker."}, ensure_ascii=True)
            )
            await ws.send_text(json.dumps(await hub._hijack_state_msg_for(worker_id, ws), ensure_ascii=True))
            return owned_hijack
        # Worker is paused — now atomically check-and-set ownership.
        acquired, err = await hub._try_acquire_ws_hijack(worker_id, ws)
        if not acquired:
            if err == "already_hijacked":
                hub._metric("hijack_conflicts_total")
            # Compensating resume. Skip for "already_hijacked": sending resume
            # would unpause the legitimate owner's session.
            if err != "already_hijacked":
                await hub._send_worker(
                    worker_id,
                    {"type": "control", "action": "resume", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
                )
            msg_text = (
                "No worker connected for this worker." if err == "no_worker" else "Already hijacked by another client."
            )
            await ws.send_text(json.dumps({"type": "error", "message": msg_text}, ensure_ascii=True))
            await ws.send_text(json.dumps(await hub._hijack_state_msg_for(worker_id, ws), ensure_ascii=True))
            return owned_hijack
        await hub._broadcast_hijack_state(worker_id)
        hub._metric("hijack_acquires_total")
        hub._notify_hijack_changed(worker_id, enabled=True, owner="dashboard")
        await hub._append_event(worker_id, "hijack_acquired", {"owner": "dashboard_ws"})
        return True  # owned_hijack = True

    elif mtype == "hijack_step":
        if await hub._touch_if_owner(worker_id, ws) is not None:
            ok = await hub._send_worker(
                worker_id,
                {"type": "control", "action": "step", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
            )
            if not ok:
                await ws.send_text(
                    json.dumps({"type": "error", "message": "No worker connected for this worker."}, ensure_ascii=True)
                )
            else:
                hub._metric("hijack_steps_total")
                await hub._append_event(worker_id, "hijack_step", {"owner": "dashboard_ws"})

    elif mtype == "hijack_release":
        # Atomically check ownership and clear in one lock block to prevent a
        # concurrent hijack_request stealing ownership between check and clear.
        # rest_active is captured inside the same lock block to avoid TOCTOU
        # on _is_rest_session_active after the owner has been cleared.
        released, rest_active = await hub._try_release_ws_hijack(worker_id, ws)
        if released:
            _do_resume = not rest_active
            if _do_resume:
                # Re-check: a concurrent hijack_acquire may have written a new
                # session between _try_release_ws_hijack and _send_worker.
                async with hub._lock:
                    _st = hub._workers.get(worker_id)
                    if _st is not None and hub._is_hijacked(_st):
                        _do_resume = False
            if _do_resume:
                await hub._send_worker(
                    worker_id,
                    {"type": "control", "action": "resume", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
                )
            await hub._broadcast_hijack_state(worker_id)
            if _do_resume:
                hub._notify_hijack_changed(worker_id, enabled=False, owner=None)
            hub._metric("hijack_releases_total")
            await hub._append_event(worker_id, "hijack_released", {"owner": "dashboard_ws"})
            return False  # owned_hijack = False

    elif mtype == "ping":
        pass  # keepalive — TCP ACK is sufficient, no response needed

    elif mtype == "input":
        can_send = False
        async with hub._lock:
            _st_input = hub._workers.get(worker_id)
            if _st_input is not None:
                can_send = hub._can_send_input(_st_input, ws)
                # Extend hijack lease if this browser is the owner.
                if hub._is_dashboard_hijack_active(_st_input) and _st_input.hijack_owner is ws:
                    _st_input.hijack_owner_expires_at = time.time() + hub._dashboard_hijack_lease_s
        if can_send:
            data = msg_b.get("data", "")
            if data and len(data) > hub.max_input_chars:
                await ws.send_text(json.dumps({"type": "error", "message": "Input too long."}, ensure_ascii=True))
            elif data:
                ok = await hub._send_worker(worker_id, {"type": "input", "data": data, "ts": time.time()})
                if not ok:
                    await ws.send_text(
                        json.dumps({"type": "error", "message": "Worker connection lost."}, ensure_ascii=True)
                    )
                else:
                    await hub._append_event(worker_id, "input_send", {"owner": "dashboard_ws", "keys": data[:120]})

    return owned_hijack
