#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Browser WebSocket message dispatch for the hijack hub.

Called by ``ws_browser_term`` in ``websockets.py`` for each parsed browser frame.
Returns the updated ``owned_hijack`` flag (True = this browser holds the hijack
lease, False = it does not).
"""

from __future__ import annotations

import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger
from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.frames import (
    BrowserInputFrame,
    make_error_frame,
    make_heartbeat_ack_frame,
    make_hello_frame,
    make_pong_frame,
)
from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
from provide.uterm.server.bridge.hub.ext import EVENT_RESUME_FAILED, NoOpPolicyGate
from provide.uterm.server.bridge.hub.semantics import CommandSplitter
from provide.uterm.server.bridge.models import VALID_ROLES

_ROLE_PRIORITY: dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub import TermHub
else:
    WebSocket = Any

logger = get_logger(__name__)

_HTTP_INSPECT_CONTROL_TYPES = frozenset(
    {
        "http_action",
        "http_intercept_toggle",
        "http_inspect_toggle",
    }
)


def _is_noop_policy_gate(gate: object) -> bool:
    """Return True for the default no-op policy gate, including module aliases."""
    if isinstance(gate, NoOpPolicyGate):
        return True
    gate_type = type(gate)
    return gate_type.__name__ == "NoOpPolicyGate" and gate_type.__module__.endswith(".bridge.hub.ext")


async def _handle_snapshot_req(hub: TermHub, ws: WebSocket, worker_id: str) -> None:
    """Handle snapshot_req message type."""
    is_owner = await hub.touch_if_owner(worker_id, ws) is not None
    if is_owner:
        await hub.request_snapshot(worker_id)
    else:
        # Non-owner viewers may request snapshots only when no hijack is
        # active — forwarding during an active hijack disrupts the owner's
        # wait_for_guard prompt detection.
        if not await hub.check_still_hijacked(worker_id):
            await hub.request_snapshot(worker_id)


async def _handle_analyze_req(hub: TermHub, ws: WebSocket, worker_id: str) -> None:
    """Handle analyze_req message type."""
    if await hub.touch_if_owner(worker_id, ws) is not None:
        await hub.request_analysis(worker_id)


async def _handle_heartbeat(hub: TermHub, ws: WebSocket, worker_id: str) -> None:
    """Handle heartbeat message type."""
    lease_expires_at = await hub.touch_if_owner(worker_id, ws)
    if lease_expires_at is not None:
        wall_expires = time.time() + (lease_expires_at - time.monotonic())
        await ws.send_text(encode_control_frame(make_heartbeat_ack_frame(wall_expires, ts=time.time())))
        await hub.broadcast_hijack_state(worker_id)


async def _handle_hijack_step(hub: TermHub, ws: WebSocket, worker_id: str) -> None:
    """Handle hijack_step message type."""
    generation = await hub.capture_dashboard_ownership(worker_id, ws)
    if generation is not None:
        ok, _reason = await hub.send_owned_worker(
            worker_id,
            {"type": "control", "action": "step", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
            browser_ws=ws,
            ownership_generation=generation,
        )
        if not ok:
            await ws.send_text(encode_control_frame(make_error_frame("No worker connected for this session.")))
        else:
            hub.metric("hijack_steps_total")
            await hub.append_event(worker_id, "hijack_step", {"owner": "dashboard_ws"})


async def _handle_http_inspect_control(
    hub: TermHub,
    ws: WebSocket,
    worker_id: str,
    msg_b: dict[str, Any],
) -> None:
    """Forward inspect/intercept control only when browser input is permitted."""
    generation = await hub.capture_browser_ownership(worker_id, ws)
    if generation is not None:
        await hub.send_owned_worker(
            worker_id,
            msg_b,
            browser_ws=ws,
            ownership_generation=generation,
        )


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
        await _handle_snapshot_req(hub, ws, worker_id)
    elif mtype == "analyze_req":
        await _handle_analyze_req(hub, ws, worker_id)
    elif mtype == "heartbeat":
        await _handle_heartbeat(hub, ws, worker_id)
    elif mtype == "hijack_request":
        return await _handle_hijack_request(hub, ws, worker_id, role, owned_hijack)
    elif mtype == "hijack_step":
        await _handle_hijack_step(hub, ws, worker_id)
    elif mtype == "hijack_release":
        return await _handle_hijack_release(hub, ws, worker_id, owned_hijack)
    elif mtype == "ping":
        with suppress(Exception):
            await ws.send_text(encode_control_frame(make_pong_frame(ts=time.time())))
    elif mtype == "input":
        await _handle_input(hub, ws, worker_id, msg_b)
    elif mtype in _HTTP_INSPECT_CONTROL_TYPES:
        await _handle_http_inspect_control(hub, ws, worker_id, msg_b)
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
        await ws.send_text(encode_control_frame(make_error_frame("Hijack requires admin role.")))
        return owned_hijack
    # Reject in open mode — no exclusive ownership.
    if await hub.is_input_open_mode(worker_id):
        await ws.send_text(encode_control_frame(make_error_frame("Hijack not available in open input mode.")))
        return owned_hijack
    # Send pause to the worker *before* writing ownership — mirrors REST
    # hijack_acquire so that concurrent acquires see the worker as free
    # while the network send is in flight.
    pause_sent = await hub.send_worker(
        worker_id,
        {"type": "control", "action": "pause", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
    )
    if not pause_sent:
        await ws.send_text(encode_control_frame(make_error_frame("No worker connected for this session.")))
        await ws.send_text(encode_control_frame(await hub.hijack_state_msg_for(worker_id, ws)))
        return owned_hijack
    # Worker is paused — now atomically check-and-set ownership.
    acquired, err = await hub.try_acquire_ws_hijack(worker_id, ws)
    if not acquired:
        if err == "already_hijacked":
            hub.metric("hijack_conflicts_total")
        # Compensating resume. Skip for "already_hijacked": sending resume
        # would unpause the legitimate owner's session.
        if err != "already_hijacked":
            await hub.send_worker_if_unowned(
                worker_id,
                {"type": "control", "action": "resume", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
            )
        msg_text = (
            "No worker connected for this session." if err == "no_worker" else "Already hijacked by another client."
        )
        await ws.send_text(encode_control_frame(make_error_frame(msg_text)))
        await ws.send_text(encode_control_frame(await hub.hijack_state_msg_for(worker_id, ws)))
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
            await hub.send_worker_if_unowned(
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
    *,
    bypass_pause: bool = False,
    ownership_generation_override: int | None = None,
    reserved_sender: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
) -> str:
    """Process an input message from the browser."""
    data = str(cast("BrowserInputFrame", msg_b).get("data", ""))
    if not data:
        return "ignored"

    if ws in hub._paused_browsers and not bypass_pause:
        new_hold = hub._hold_buffers.get(ws, "") + data
        if len(new_hold) > hub.max_buffer_chars:
            await ws.send_text(encode_control_frame(make_error_frame("Input too long.")))
            return "blocked"
        hub._hold_buffers[ws] = new_hold
        return "buffered"

    ownership_generation = ownership_generation_override
    if ownership_generation is None:
        ownership_generation = await hub.capture_browser_ownership(worker_id, ws)
    if ownership_generation is None:
        return "invalid_owner"

    if len(data) > hub.max_input_chars:
        await ws.send_text(encode_control_frame(make_error_frame("Input too long.")))
        return "blocked"

    context = await hub.prepare_policy_context(ws, worker_id, action="input")
    gate = hub._policy_gate
    is_complete_chunk = "\r" in data or "\n" in data

    if _is_noop_policy_gate(gate):
        worker_msg = {"type": "input", "data": data, "ts": time.time()}
        if reserved_sender is None:
            ok, error = await hub.send_owned_worker(
                worker_id,
                worker_msg,
                browser_ws=ws,
                ownership_generation=ownership_generation,
                source=ws,
            )
        else:
            ok = await reserved_sender(worker_msg)
            error = None if ok else "no_worker"
        if error == "invalid_owner":
            return "invalid_owner"
        if not ok:
            await ws.send_text(encode_control_frame(make_error_frame("Worker connection lost.")))
            return "send_failed"
        await hub.append_event(worker_id, "input_send", {"owner": "dashboard_ws", "keys": data[:120]})
        return "sent"

    if not is_complete_chunk and ws not in hub._input_buffers:
        decision = await gate.intercept_input(data, context)
        if decision.action == "hold":
            request_id = decision.request_id or str(uuid.uuid4())
            request = ApprovalRequest(
                id=request_id,
                worker_id=worker_id,
                submitter_id=str(context.client_id),
                command=data,
                status=ApprovalStatus.PENDING,
                created_at=time.time(),
                expires_at=time.time() + decision.timeout_s,
                origin_browser=ws,
                ownership_generation=ownership_generation,
            )
            if not hub.approval_store.add(request):
                await ws.send_text(encode_control_frame(make_error_frame("Approval request ID collision.")))
                return "collision"
            hub._paused_browsers.add(ws)
            from provide.uterm.server.bridge.hub.core import _encode_browser_frame

            st = await hub._get(worker_id)
            for b_ws in list(st.browsers.keys()):
                await b_ws.send_text(
                    _encode_browser_frame(
                        {
                            "type": "approval_pending",
                            "command": data,
                            "request_id": request_id,
                            "expires_at": request.expires_at,
                        }
                    )
                )
            return "held"

        if decision.action != "allow":
            logger.debug(
                "input_blocked_by_policy worker_id=%s action=%s reason=%s part=%s",
                worker_id,
                decision.action,
                decision.reason,
                data,
            )
            await ws.send_text(encode_control_frame(make_error_frame(f"Command part blocked by policy: {data}")))
            return "blocked"

    command = hub._buffer_and_get_command(ws, data)
    if command is None:
        return "buffered"

    splitter = CommandSplitter()
    parts = splitter.split(command)
    if len(parts) <= 1:
        parts = [command]
    for part in parts:
        part_decision = await gate.intercept_input(part, context)
        if part_decision.action == "hold":
            request_id = part_decision.request_id or str(uuid.uuid4())
            request = ApprovalRequest(
                id=request_id,
                worker_id=worker_id,
                submitter_id=str(context.client_id),
                command=command,
                status=ApprovalStatus.PENDING,
                created_at=time.time(),
                expires_at=time.time() + part_decision.timeout_s,
                origin_browser=ws,
                ownership_generation=ownership_generation,
            )
            if not hub.approval_store.add(request):
                await ws.send_text(encode_control_frame(make_error_frame("Approval request ID collision.")))
                return "collision"
            hub._paused_browsers.add(ws)
            from provide.uterm.server.bridge.hub.core import _encode_browser_frame

            st = await hub._get(worker_id)
            for b_ws in list(st.browsers.keys()):
                await b_ws.send_text(
                    _encode_browser_frame(
                        {
                            "type": "approval_pending",
                            "command": command,
                            "request_id": request_id,
                            "expires_at": request.expires_at,
                        }
                    )
                )
            return "held"
        if part_decision.action != "allow":
            logger.debug(
                "input_blocked_by_policy worker_id=%s action=%s reason=%s part=%s",
                worker_id,
                part_decision.action,
                part_decision.reason,
                part,
            )
            await ws.send_text(encode_control_frame(make_error_frame(f"Command part blocked by policy: {part}")))
            return "blocked"

    worker_msg = {"type": "input", "data": command, "ts": time.time()}
    if reserved_sender is None:
        ok, error = await hub.send_owned_worker(
            worker_id,
            worker_msg,
            browser_ws=ws,
            ownership_generation=ownership_generation,
            source=ws,
        )
    else:
        ok = await reserved_sender(worker_msg)
        error = None if ok else "no_worker"

    if error == "invalid_owner":
        return "invalid_owner"
    if not ok:
        await ws.send_text(encode_control_frame(make_error_frame("Worker connection lost.")))
        return "send_failed"
    await hub.append_event(worker_id, "input_send", {"owner": "dashboard_ws", "keys": command[:120]})
    return "sent"


def _select_resumed_role(role: str, session_role: str) -> tuple[str, bool]:
    """Select a resumed role without mutating browser state."""
    new_role = role
    if session_role in VALID_ROLES and _ROLE_PRIORITY.get(session_role, 0) <= _ROLE_PRIORITY.get(role, 0):
        new_role = session_role
    return new_role, new_role == "admin"


async def _rollback_reclaimed_hijack(hub: TermHub, ws: WebSocket, worker_id: str) -> None:
    """Release only *ws*'s provisional reclaim and compensate its pause."""
    released, rest_active = await hub.try_release_ws_hijack(worker_id, ws)
    if not released:
        return
    if not rest_active:
        await hub.send_worker_if_unowned(
            worker_id,
            {"type": "control", "action": "resume", "owner": "resume-rollback", "lease_s": 0, "ts": time.time()},
        )
    await hub.broadcast_hijack_state(worker_id)


async def _try_reclaim_hijack(
    hub: TermHub, ws: WebSocket, worker_id: str, session: Any, can_hijack: bool
) -> tuple[bool, bool, bool]:
    """Attempt to reclaim the hijack lease for a resuming session.

    Returns (owned_hijack, reclaimed_hijack).
    """
    if not (session.was_hijack_owner and can_hijack):
        return False, False, False
    reclaimed_hijack, competing_owner = await hub.try_reclaim_hijack_status(worker_id, ws)
    if not reclaimed_hijack:
        return False, False, competing_owner
    generation = await hub.capture_browser_ownership(worker_id, ws)
    pause_sent, _reason = await hub.send_owned_worker(
        worker_id,
        {"type": "control", "action": "pause", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
        browser_ws=ws,
        ownership_generation=generation,
    )
    if not pause_sent:
        await _rollback_reclaimed_hijack(hub, ws, worker_id)
        return False, False, False
    return True, True, False


async def _handle_resume(
    hub: TermHub,
    ws: WebSocket,
    worker_id: str,
    role: str,
    msg_b: dict[str, Any],
    owned_hijack: bool,
) -> bool:
    """Process a resume message from the browser. Returns updated owned_hijack."""
    store = hub.resume_store
    if store is None:
        return owned_hijack

    old_token = msg_b.get("token", "")
    if not isinstance(old_token, str) or not old_token:
        logger.warning(EVENT_RESUME_FAILED, worker_id=worker_id, reason="token_malformed")
        return owned_hijack

    # Order this resume behind the OLD socket's disconnect bookkeeping. That is
    # what writes ``was_hijack_owner`` onto the stored session, it runs on the
    # old socket's own task, and everything below — including the reclaim gate
    # in ``_try_reclaim_hijack`` — is decided from the value read here. Without
    # the wait a browser that reconnects promptly reads ``False`` and silently
    # comes back as a fenced viewer.  The wait is bounded; on expiry we proceed
    # with whatever the store already says (the pre-latch behaviour) rather than
    # failing a resume that is otherwise valid.
    if not await hub.wait_resume_token_ready(old_token, ws):
        logger.warning("ws_browser_resume_detach_wait_timeout worker_id=%s", worker_id)

    # Non-destructive lookup for the validation gates: a wrong-worker or
    # callback-rejected resume must NOT burn the single-use token, otherwise
    # the legitimate browser could no longer resume. The token is consumed
    # only on the success path, just before the new token is issued.
    session = await store.get(old_token)
    if session is None or session.worker_id != worker_id:
        logger.warning(EVENT_RESUME_FAILED, worker_id=worker_id, reason="token_invalid")
        return owned_hijack

    # Optional application-level validation (token still live; a rejected
    # resume leaves it consumable for the legitimate browser).
    if hub._on_resume is not None and not await hub._on_resume(old_token, session):
        logger.warning(EVENT_RESUME_FAILED, worker_id=worker_id, reason="callback_rejected")
        return owned_hijack

    # Prepare all fallible ownership work before consuming the single-use
    # authority. Rejected/failed reclaim must leave the legitimate token live.
    new_role, can_hijack = _select_resumed_role(role, session.role)
    owned_hijack, reclaimed_hijack, competing_owner = await _try_reclaim_hijack(hub, ws, worker_id, session, can_hijack)
    reclaim_required = session.was_hijack_owner and can_hijack
    if reclaim_required and not reclaimed_hijack:
        open_mode = await hub.is_input_open_mode(worker_id)
        role_only_resume_allowed = open_mode or (competing_owner and hub.allow_stale_owner_role_resume)
        if not role_only_resume_allowed:
            reason = "competing_owner" if competing_owner else "reclaim_failed"
            logger.warning(EVENT_RESUME_FAILED, worker_id=worker_id, reason=reason)
            return False

    new_token: str | None = None
    try:
        # Pre-create replacement authority so create failure cannot burn the old
        # token. Concurrent attempts may each prepare one, but only the consume
        # winner keeps its replacement; losers revoke theirs below.
        new_token = await store.create(worker_id, new_role, hub._resume_ttl_s)
        consumed = await store.consume(old_token)
    except BaseException:
        if new_token is not None:
            # Cleanup must not replace the original create/consume failure.
            with suppress(Exception):
                await store.revoke(new_token)
        if reclaimed_hijack:
            await _rollback_reclaimed_hijack(hub, ws, worker_id)
        raise
    if consumed is None:
        await store.revoke(new_token)
        if reclaimed_hijack:
            await _rollback_reclaimed_hijack(hub, ws, worker_id)
        logger.warning(EVENT_RESUME_FAILED, worker_id=worker_id, reason="token_invalid")
        return False

    if new_role != role:
        await hub.set_browser_role(worker_id, ws, new_role)
    # Bind under the lock so the replacement token is armed with its own detach
    # latch (and the superseded connect-time token's latch is released) before
    # any peer can wait on it.
    async with hub._lock:
        hub._bind_resume_token_locked(ws, new_token)

    _resumed_state = await hub.register_browser_state_snapshot(worker_id, ws)
    await ws.send_text(
        encode_control_frame(
            make_hello_frame(
                worker_id=worker_id,
                can_hijack=can_hijack,
                hijacked=_resumed_state.get("is_hijacked", False),
                hijacked_by_me=_resumed_state.get("hijacked_by_me", False),
                worker_online=_resumed_state.get("worker_online", False),
                input_mode=_resumed_state.get("input_mode", "hijack"),
                role=new_role,
                hijack_control="ws",
                hijack_step_supported=True,
                capabilities={
                    "hijack_control": "ws",
                    "hijack_step_supported": True,
                },
                resume_supported=True,
                resume_token=new_token,
                resumed=True,
            )
        )
    )
    await ws.send_text(encode_control_frame(await hub.hijack_state_msg_for(worker_id, ws)))
    if reclaimed_hijack:
        await hub.broadcast_hijack_state(worker_id)
        hub.notify_hijack_changed(worker_id, enabled=True, owner="dashboard")
        await hub.append_event(worker_id, "hijack_acquired", {"owner": "dashboard_resume"})
    logger.info("ws_browser_resumed worker_id=%s role=%s hijack=%s", worker_id, new_role, owned_hijack)
    return owned_hijack
