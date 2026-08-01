#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Browser-side WebSocket dispatch + disconnect helpers for the hijack hub.

Extracted from ``websockets_impl.py`` to keep that module under the
source-size cap. Module-level helpers driving the ``ws_browser_term`` handler:

- ``dispatch_browser_event`` — per-frame rate-limit + resume / presence /
  fanout / generic dispatch. Returns the (possibly updated) ``role`` /
  ``can_hijack`` / ``owned_hijack`` triple so the caller's recv loop stays in
  sync with role changes a ``resume`` frame may apply.
- ``resume_worker_on_disconnect`` — the finally-block resume fan-out used both
  when the disconnecting browser owned the hijack and when a non-owner
  disconnect leaves the worker paused with no owner.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.control_channel import DataChunk, encode_control_frame
from provide.uterm.server.bridge.frames import make_error_frame
from provide.uterm.server.bridge.routes.browser_handlers import handle_browser_message

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.ratelimit import TokenBucket
else:
    WebSocket = Any

logger = get_logger(__name__)


async def dispatch_browser_event(
    hub: TermHub,
    websocket: WebSocket,
    worker_id: str,
    role: str,
    can_hijack: bool,
    owned_hijack: bool,
    event: Any,
    browser_bucket: TokenBucket,
    browser_control_bucket: TokenBucket,
) -> tuple[str, bool, bool]:
    """Dispatch a single parsed browser frame, returning the updated state triple.

    Returns ``(role, can_hijack, owned_hijack)``. A ``resume`` frame can update
    ``role`` / ``can_hijack`` in hub state, so the caller reads them back from
    the return value; every other path leaves them unchanged.
    """
    msg_b = {"type": "input", "data": event.data} if isinstance(event, DataChunk) else event.control
    mtype = msg_b.get("type")
    if mtype == "input" and not browser_bucket.allow():
        hub.metric("ws_browser_rate_limited_total")
        logger.warning("ws_browser_rate_limited worker_id=%s", worker_id)
        with suppress(Exception):
            await websocket.send_text(encode_control_frame(make_error_frame("rate_limited")))
            logger.debug("ws_browser_rate_limited_sent worker_id=%s", worker_id)
        return role, can_hijack, owned_hijack
    if mtype is not None and mtype != "input" and not browser_control_bucket.allow():
        hub.metric("ws_browser_control_rate_limited_total")
        logger.warning("ws_browser_control_rate_limited worker_id=%s mtype=%s", worker_id, mtype)
        with suppress(Exception):
            await websocket.send_text(encode_control_frame(make_error_frame("rate_limited")))
        return role, can_hijack, owned_hijack

    # Resume handled here (not in browser_handlers) because it can
    # update the local `role` / `can_hijack` variables.
    if mtype == "resume" and hub.resume_store is not None:
        from provide.uterm.server.bridge.routes.browser_handlers import _handle_resume

        owned_hijack = await _handle_resume(hub, websocket, worker_id, role, msg_b, owned_hijack)
        # _handle_resume may have updated the role in st.browsers;
        # read it back so subsequent messages use the correct role.
        role = await hub.get_worker_browser_role(worker_id, websocket) or role
        can_hijack = role == "admin"
        return role, can_hijack, owned_hijack

    if mtype in ("presence_update", "queued_input", "control_request"):
        _dm_handle: Any = getattr(hub, "deckmux_handle_message", None)
        if _dm_handle is not None:
            import os as _os

            _dm_msg_principal = None
            if _os.environ.get("UTERM_TEST_MODE") != "1":
                _dm_msg_principal = getattr(getattr(websocket, "state", None), "uterm_principal", None)
            await _dm_handle(worker_id, websocket, msg_b, principal=_dm_msg_principal)
        return role, can_hijack, owned_hijack

    if mtype == "fanout_send":
        _fo_principal = getattr(getattr(websocket, "state", None), "uterm_principal", None)
        _fo_authz = getattr(getattr(websocket, "app", None), "state", None)
        _fo_authz = getattr(_fo_authz, "uterm_authz", None)
        try:
            _fo_is_admin = (
                _fo_principal is not None
                and _fo_principal.subject_id != "anonymous"
                and _fo_authz is not None
                and await _fo_authz.is_admin(_fo_principal)
            )
        except Exception:
            _fo_is_admin = False
        if not _fo_is_admin:
            await websocket.send_text(encode_control_frame(make_error_frame("global admin role required")))
            return role, can_hijack, owned_hijack
        assert _fo_principal is not None  # the admin gate above rejects a missing principal
        _fo_ctrl: Any = getattr(hub, "fan_out_controller", None)
        if _fo_ctrl is not None:
            _fo_group_id = msg_b.get("group_id", "")
            _fo_data = msg_b.get("data", "")
            _fo_subj = _fo_principal.subject_id
            # Verify caller has access to the group
            _fo_group = await _fo_ctrl.get_group(_fo_group_id, principal=_fo_subj)
            if _fo_group is None:
                return role, can_hijack, owned_hijack  # caller doesn't own/have access
            _fo_result = await _fo_ctrl.send(
                _fo_group_id,
                _fo_data,
                principal=_fo_principal,
            )
            await websocket.send_text(
                encode_control_frame(
                    {
                        "type": "fanout_result",
                        "group_id": _fo_result.group_id,
                        "send_id": _fo_result.send_id,
                        "command": _fo_result.command,
                        "sent_at": _fo_result.sent_at,
                        "results": [asdict(r) for r in _fo_result.results],
                        "divergent_sessions": _fo_result.divergent_sessions,
                        "failed_sessions": _fo_result.failed_sessions,
                        "error": _fo_result.error,
                        "approval_required": _fo_result.approval_required,
                        "approval_id": _fo_result.approval_id,
                    }
                )
            )
        return role, can_hijack, owned_hijack

    if mtype in ("input", "hijack_request", "hijack_release"):
        await hub.touch_activity(worker_id)
    owned_hijack = await handle_browser_message(hub, websocket, worker_id, role, msg_b, owned_hijack)
    return role, can_hijack, owned_hijack


def resume_worker_on_disconnect(hub: TermHub, worker_id: str) -> None:
    """Fire-and-forget a worker ``resume`` after a browser disconnect.

    Schedules ``send_worker`` on the hub's background-task set with a failure
    log callback. Used by both the owner-disconnect and the
    resume-without-owner cleanup paths in ``ws_browser_term``.
    """
    _resume_task = asyncio.create_task(
        hub.send_worker_if_unowned(
            worker_id,
            {
                "type": "control",
                "action": "resume",
                "owner": "dashboard",
                "lease_s": 0,
                "ts": time.time(),
            },
        )
    )
    hub._background_tasks.add(_resume_task)
    _resume_task.add_done_callback(hub._background_tasks.discard)
    _resume_task.add_done_callback(
        lambda t: (
            logger.warning("ws_disconnect_resume_failed worker_id=%s error=%s", worker_id, t.exception())
            if not t.cancelled() and t.exception() is not None
            else None
        )
    )
