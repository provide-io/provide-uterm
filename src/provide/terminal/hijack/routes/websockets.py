#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocket terminal routes for the hijack hub.

Registers:
- ``/ws/worker/{worker_id}/term``  — worker → hub (terminal output, snapshots)
- ``/ws/browser/{worker_id}/term`` — browser → hub (dashboard viewer + hijack control)
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Annotated, Any

try:
    from fastapi import APIRouter, Path, WebSocket, WebSocketDisconnect
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-terminal[websocket]'") from _e

import logging

from provide.terminal.hijack.models import WorkerTermState, extract_prompt_id

if TYPE_CHECKING:
    from provide.terminal.hijack.hub import TermHub

logger = logging.getLogger(__name__)

_WORKER_IDLE_TIMEOUT_S = 120.0  # workers produce regular term/snapshot output
_BROWSER_IDLE_TIMEOUT_S = 180.0  # browsers may be silent while watching


def _safe_int(val: Any, default: int) -> int:
    try:
        return int(default if val is None else val)
    except (ValueError, TypeError):
        return default


def register_ws_routes(hub: TermHub, router: APIRouter) -> None:
    """Attach WebSocket terminal routes to *router*."""

    @router.websocket("/ws/worker/{worker_id}/term")
    async def ws_worker_term(websocket: WebSocket, worker_id: Annotated[str, Path(pattern=r"^[\w\-]+$")]) -> None:
        await websocket.accept()
        prev_was_hijacked = False
        async with hub._lock:
            st = hub._workers.setdefault(worker_id, WorkerTermState())
            # A crashed worker may reconnect before its old finally block clears
            # state (the identity check `worker_ws is old_ws` skips cleanup when
            # a new connection has already overwritten worker_ws).  Clear any stale
            # hijack state now so the new worker starts unpaused and REST clients
            # cannot send keystrokes under a dead session.
            if st.hijack_session is not None or st.hijack_owner is not None:
                prev_was_hijacked = True
                st.hijack_session = None
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
            st.worker_ws = websocket
        logger.info("term_worker_connected worker_id=%s", worker_id)
        if prev_was_hijacked:
            hub._notify_hijack_changed(worker_id, enabled=False, owner=None)
            await hub._broadcast_hijack_state(worker_id)
        await hub._broadcast(
            worker_id,
            {"type": "worker_connected", "worker_id": worker_id, "ts": time.time()},
        )
        await hub._request_snapshot(worker_id)

        try:
            _last_cleanup = 0.0
            while True:
                _now = time.monotonic()
                if _now - _last_cleanup >= 1.0:
                    await hub._cleanup_expired_hijack(worker_id)
                    _last_cleanup = _now
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception as exc:  # noqa: S112
                    logger.debug("ws_worker_bad_json worker_id=%s: %s", worker_id, exc)
                    continue
                mtype = msg.get("type")
                if mtype == "worker_hello":
                    _hello_mode = msg.get("input_mode")
                    if _hello_mode in ("hijack", "open"):
                        async with hub._lock:
                            _st_hello = hub._workers.get(worker_id)
                            if _st_hello is not None:
                                _st_hello.input_mode = _hello_mode
                        await hub._broadcast_hijack_state(worker_id)
                        logger.info("worker_hello worker_id=%s input_mode=%s", worker_id, _hello_mode)
                    continue
                if mtype == "term":
                    data = msg.get("data", "")
                    if data:
                        await hub._broadcast(
                            worker_id, {"type": "term", "data": data, "ts": msg.get("ts", time.time())}
                        )
                elif mtype == "snapshot":
                    snapshot: dict[str, Any] = {
                        "type": "snapshot",
                        "screen": msg.get("screen", ""),
                        "cursor": msg.get("cursor", {"x": 0, "y": 0}),
                        "cols": _safe_int(msg.get("cols"), 80),
                        "rows": _safe_int(msg.get("rows"), 25),
                        "screen_hash": msg.get("screen_hash", ""),
                        "cursor_at_end": bool(msg.get("cursor_at_end", True)),
                        "has_trailing_space": bool(msg.get("has_trailing_space", False)),
                        "prompt_detected": msg.get("prompt_detected"),
                        "ts": msg.get("ts", time.time()),
                    }
                    async with hub._lock:
                        st2 = hub._workers.get(worker_id)
                        if st2 is not None:
                            st2.last_snapshot = snapshot
                    await hub._broadcast(worker_id, snapshot)
                    await hub._append_event(
                        worker_id,
                        "snapshot",
                        {"prompt_id": extract_prompt_id(snapshot), "screen_hash": snapshot.get("screen_hash")},
                    )
                elif mtype == "analysis":
                    await hub._broadcast(
                        worker_id,
                        {
                            "type": "analysis",
                            "formatted": msg.get("formatted", ""),
                            "raw": msg.get("raw"),
                            "ts": msg.get("ts", time.time()),
                        },
                    )
                elif mtype == "status":
                    await hub._broadcast(worker_id, msg)
                    await hub._append_event(worker_id, "worker_status", {"status": msg})
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover
            logger.warning("term_worker_ws_error worker_id=%s error=%s", worker_id, exc)
        finally:
            was_hijacked = False
            async with hub._lock:
                st3 = hub._workers.get(worker_id)
                if st3 is not None and st3.worker_ws is websocket:
                    st3.worker_ws = None
                    was_hijacked = st3.hijack_session is not None or st3.hijack_owner is not None
                    st3.hijack_session = None
                    st3.hijack_owner = None
                    st3.hijack_owner_expires_at = None
            logger.info("term_worker_disconnected worker_id=%s", worker_id)
            await hub._broadcast(
                worker_id,
                {"type": "worker_disconnected", "worker_id": worker_id, "ts": time.time()},
            )
            if was_hijacked:
                hub._notify_hijack_changed(worker_id, enabled=False, owner=None)
                await hub._broadcast_hijack_state(worker_id)
            await hub._prune_if_idle(worker_id)

    @router.websocket("/ws/browser/{worker_id}/term")
    async def ws_browser_term(websocket: WebSocket, worker_id: Annotated[str, Path(pattern=r"^[\w\-]+$")]) -> None:
        await websocket.accept()
        # Capture all startup state atomically while registering the browser.
        async with hub._lock:
            st = hub._workers.setdefault(worker_id, WorkerTermState())
            st.browsers.add(websocket)
            is_hijacked = hub._is_hijacked(st)
            hijacked_by_me = hub._is_dashboard_hijack_active(st) and st.hijack_owner is websocket
            worker_online = st.worker_ws is not None
            input_mode = st.input_mode
            initial_snapshot = st.last_snapshot  # captured under lock to avoid stale read

        await websocket.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "worker_id": worker_id,
                    "can_hijack": True,
                    "hijacked": is_hijacked,
                    "hijacked_by_me": hijacked_by_me,
                    "worker_online": worker_online,
                    "input_mode": input_mode,
                },
                ensure_ascii=True,
            )
        )
        await websocket.send_text(json.dumps(await hub._hijack_state_msg_for(worker_id, websocket), ensure_ascii=True))

        if initial_snapshot is not None:
            await websocket.send_text(json.dumps(initial_snapshot, ensure_ascii=True))
        else:
            await hub._request_snapshot(worker_id)

        try:
            _last_cleanup = 0.0
            while True:
                _now = time.monotonic()
                if _now - _last_cleanup >= 1.0:
                    await hub._cleanup_expired_hijack(worker_id)
                    _last_cleanup = _now
                raw = await websocket.receive_text()
                try:
                    msg_b: dict[str, Any] = json.loads(raw)
                except Exception as exc:  # noqa: S112
                    logger.debug("ws_browser_bad_json worker_id=%s: %s", worker_id, exc)
                    continue
                mtype = msg_b.get("type")

                if mtype == "snapshot_req":
                    # Touch the lease if this browser is the owner (extends it).
                    is_owner = await hub._touch_if_owner(worker_id, websocket) is not None
                    if is_owner:
                        await hub._request_snapshot(worker_id)
                    else:
                        # Non-owner viewers may request snapshots only when no
                        # hijack is active — forwarding during an active hijack
                        # disrupts the owner's _wait_for_guard prompt detection.
                        async with hub._lock:
                            _st = hub._workers.get(worker_id)
                            _hijack_active = _st is not None and hub._is_hijacked(_st)
                        if not _hijack_active:
                            await hub._request_snapshot(worker_id)

                elif mtype == "analyze_req":
                    if await hub._touch_if_owner(worker_id, websocket) is not None:
                        await hub._request_analysis(worker_id)

                elif mtype == "heartbeat":
                    lease_expires_at = await hub._touch_if_owner(worker_id, websocket)
                    if lease_expires_at is not None:
                        await websocket.send_text(
                            json.dumps(
                                {"type": "heartbeat_ack", "lease_expires_at": lease_expires_at, "ts": time.time()},
                                ensure_ascii=True,
                            )
                        )
                        await hub._broadcast_hijack_state(worker_id)

                elif mtype == "hijack_request":
                    # Reject in open mode — no exclusive ownership.
                    async with hub._lock:
                        _st_mode = hub._workers.get(worker_id)
                        _is_open = _st_mode is not None and _st_mode.input_mode == "open"
                    if _is_open:
                        await websocket.send_text(
                            json.dumps(
                                {"type": "error", "message": "Hijack not available in open input mode."},
                                ensure_ascii=True,
                            )
                        )
                        continue
                    # Send pause to the worker *before* writing ownership — mirrors
                    # REST hijack_acquire so that concurrent acquires see the worker
                    # as free while the network send is in flight.
                    pause_sent = await hub._send_worker(
                        worker_id,
                        {
                            "type": "control",
                            "action": "pause",
                            "owner": "dashboard",
                            "lease_s": 0,
                            "ts": time.time(),
                        },
                    )
                    if not pause_sent:
                        await websocket.send_text(
                            json.dumps(
                                {"type": "error", "message": "No worker connected for this worker."},
                                ensure_ascii=True,
                            )
                        )
                        await websocket.send_text(
                            json.dumps(await hub._hijack_state_msg_for(worker_id, websocket), ensure_ascii=True)
                        )
                        continue
                    # Worker is paused — now atomically check-and-set ownership.
                    acquired, err = await hub._try_acquire_ws_hijack(worker_id, websocket)
                    if not acquired:
                        # Compensating resume. Skip for "already_hijacked": set_hijacked
                        # is a boolean; sending resume would unpause the legitimate
                        # owner's session (same reasoning as REST hijack_acquire).
                        if err != "already_hijacked":
                            await hub._send_worker(
                                worker_id,
                                {
                                    "type": "control",
                                    "action": "resume",
                                    "owner": "dashboard",
                                    "lease_s": 0,
                                    "ts": time.time(),
                                },
                            )
                        msg_text = (
                            "No worker connected for this worker."
                            if err == "no_worker"
                            else "Already hijacked by another client."
                        )
                        await websocket.send_text(json.dumps({"type": "error", "message": msg_text}, ensure_ascii=True))
                        await websocket.send_text(
                            json.dumps(await hub._hijack_state_msg_for(worker_id, websocket), ensure_ascii=True)
                        )
                        continue
                    await hub._broadcast_hijack_state(worker_id)
                    hub._notify_hijack_changed(worker_id, enabled=True, owner="dashboard")
                    await hub._append_event(worker_id, "hijack_acquired", {"owner": "dashboard_ws"})

                elif mtype == "hijack_step":
                    if await hub._touch_if_owner(worker_id, websocket) is not None:
                        ok = await hub._send_worker(
                            worker_id,
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
                                    {"type": "error", "message": "No worker connected for this worker."},
                                    ensure_ascii=True,
                                )
                            )
                        else:
                            await hub._append_event(worker_id, "hijack_step", {"owner": "dashboard_ws"})

                elif mtype == "hijack_release":
                    # Atomically check ownership and clear in one lock block to
                    # prevent a concurrent hijack_request stealing ownership.
                    # rest_active is captured inside the same lock block to
                    # avoid a post-release TOCTOU on _is_rest_session_active.
                    released, rest_active = await hub._try_release_ws_hijack(worker_id, websocket)
                    if released:
                        _do_resume = not rest_active
                        if _do_resume:
                            # Re-check: a concurrent hijack_acquire may have written
                            # a new session between _try_release_ws_hijack and here.
                            async with hub._lock:
                                _st = hub._workers.get(worker_id)
                                if _st is not None and hub._is_hijacked(_st):
                                    _do_resume = False
                        if _do_resume:
                            await hub._send_worker(
                                worker_id,
                                {
                                    "type": "control",
                                    "action": "resume",
                                    "owner": "dashboard",
                                    "lease_s": 0,
                                    "ts": time.time(),
                                },
                            )
                        await hub._broadcast_hijack_state(worker_id)
                        if _do_resume:
                            hub._notify_hijack_changed(worker_id, enabled=False, owner=None)
                        await hub._append_event(worker_id, "hijack_released", {"owner": "dashboard_ws"})

                elif mtype == "ping":
                    pass  # keepalive — TCP ACK is sufficient, no response needed

                elif mtype == "input":
                    # In open mode any browser can send; in hijack mode only the owner.
                    can_send = False
                    async with hub._lock:
                        _st_input = hub._workers.get(worker_id)
                        if _st_input is not None:
                            can_send = hub._can_send_input(_st_input, websocket)
                            # Extend hijack lease if this browser is the owner
                            if (
                                hub._is_dashboard_hijack_active(_st_input)
                                and _st_input.hijack_owner is websocket
                            ):
                                _st_input.hijack_owner_expires_at = (
                                    time.time() + hub._dashboard_hijack_lease_s
                                )
                    if can_send:
                        data = msg_b.get("data", "")
                        if data:
                            ok = await hub._send_worker(worker_id, {"type": "input", "data": data, "ts": time.time()})
                            if not ok:
                                await websocket.send_text(
                                    json.dumps(
                                        {"type": "error", "message": "Worker connection lost."}, ensure_ascii=True
                                    )
                                )
                            else:
                                await hub._append_event(
                                    worker_id, "input_send", {"owner": "dashboard_ws", "keys": data[:120]}
                                )

        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover
            logger.warning("term_browser_ws_error worker_id=%s error=%s", worker_id, exc)
        finally:
            # Atomically detect ownership, capture REST-session liveness, and clear
            # the owner — all in one lock block to avoid the TOCTOU window where
            # _is_owner() returns True but another coroutine steals hijack_owner
            # (or vice-versa), and to avoid a second lock round-trip for
            # _is_rest_session_active after the owner has already been cleared.
            was_owner = False
            rest_still_active = False
            async with hub._lock:
                st3 = hub._workers.get(worker_id)
                was_owner = st3 is not None and hub._is_dashboard_hijack_active(st3) and st3.hijack_owner is websocket
                if st3 is not None:
                    st3.browsers.discard(websocket)
                    if was_owner:
                        st3.hijack_owner = None
                        st3.hijack_owner_expires_at = None
                        rest_still_active = hub._is_rest_session_active(st3)
            if was_owner:
                _do_resume = not rest_still_active
                if _do_resume:
                    # Re-check: a concurrent hijack_acquire may have written a new
                    # session between the lock release above and _send_worker below.
                    async with hub._lock:
                        _st4 = hub._workers.get(worker_id)
                        if _st4 is not None and hub._is_hijacked(_st4):
                            _do_resume = False
                if _do_resume:
                    await hub._send_worker(
                        worker_id,
                        {"type": "control", "action": "resume", "owner": "dashboard", "lease_s": 0, "ts": time.time()},
                    )
                await hub._broadcast_hijack_state(worker_id)
                if _do_resume:
                    hub._notify_hijack_changed(worker_id, enabled=False, owner=None)
                await hub._append_event(worker_id, "hijack_released", {"owner": "dashboard_ws_disconnect"})
            await hub._prune_if_idle(worker_id)
