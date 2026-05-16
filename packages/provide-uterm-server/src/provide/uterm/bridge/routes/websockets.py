#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocket terminal routes for the hijack hub.

Registers:
- ``/ws/worker/{worker_id}/term``  — worker → hub (terminal output, snapshots)
- ``/ws/browser/{worker_id}/term`` — browser → hub (dashboard viewer + hijack control)
"""

from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Annotated, Any, cast

from provide.telemetry import get_logger, get_tracer

try:
    from fastapi import APIRouter, Path, WebSocket, WebSocketDisconnect, WebSocketException
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-uterm[websocket]'") from _e


from provide.uterm.bridge.contracts import CURRENT_PROTOCOL_VERSION
from provide.uterm.bridge.frames import (
    coerce_worker_status_frame,
    make_analysis_frame,
    make_error_frame,
    make_hello_frame,
    make_snapshot_frame,
    make_term_frame,
    make_worker_connected_frame,
    make_worker_disconnected_frame,
)
from provide.uterm.bridge.models import VALID_ROLES, _safe_float, _safe_int
from provide.uterm.bridge.ratelimit import TokenBucket
from provide.uterm.bridge.rest_helpers import extract_prompt_id
from provide.uterm.bridge.routes.browser_handlers import handle_browser_message
from provide.uterm.control_channel import (
    ControlChannelDecoder,
    ControlChannelProtocolError,
    DataChunk,
    encode_control,
)

if TYPE_CHECKING:
    from provide.uterm.bridge.hub import BrowserRoleResolutionError, TermHub
else:
    from provide.uterm.bridge.hub import BrowserRoleResolutionError

logger = get_logger(__name__)
_WORKER_HIJACK_CLEANUP_INTERVAL_S = 1.0
_BROWSER_HIJACK_CLEANUP_INTERVAL_S = 1.0


def _set_ws_span_attrs(span: Any, **attrs: str | None) -> None:
    """Set uterm.* attributes on a span if it exposes set_attribute."""
    set_attr = getattr(span, "set_attribute", None)
    if not callable(set_attr):
        return
    for key, val in attrs.items():
        if val is not None:
            set_attr(f"uterm.{key}", val)


async def _periodic_hijack_cleanup(hub: TermHub, worker_id: str, interval_s: float) -> None:
    """Run lease cleanup on a fixed cadence while a WS handler is active."""
    while True:
        await asyncio.sleep(interval_s)
        await hub.cleanup_expired_hijack(worker_id)


def register_ws_routes(hub: TermHub, router: APIRouter) -> None:
    """Attach WebSocket terminal routes to *router*."""
    hub._on_browser_message = handle_browser_message

    @router.websocket("/ws/worker/{worker_id}/term")
    async def ws_worker_term(websocket: WebSocket, worker_id: Annotated[str, Path(pattern=r"^[\w\-]+$")]) -> None:
        worker_token = hub.worker_token()
        if worker_token is not None:
            auth_header = websocket.headers.get("authorization", "")
            provided = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
            if not secrets.compare_digest(provided, worker_token):
                # Accept first so the close code is transmitted to the client.
                # Calling close() before accept() silently drops the connection
                # without sending the 1008 policy-violation code.
                await websocket.accept()
                await websocket.close(code=1008, reason="authentication required")
                return
        await websocket.accept()
        # Register worker, atomically clearing any stale hijack state from a
        # crashed previous connection.  A crashed worker may reconnect before its
        # old finally block clears state; the identity check `worker_ws is old_ws`
        # in deregister_worker skips cleanup when a new connection has already
        # overwritten worker_ws, so stale REST clients cannot send keystrokes under
        # a dead session.
        prev_was_hijacked = await hub.register_worker(worker_id, websocket)
        await hub.touch_activity(worker_id)
        with get_tracer(__name__).start_as_current_span("uterm.ws.worker.connect") as _w_span:
            _set_ws_span_attrs(_w_span, worker_id=worker_id, operation="ws.worker.connect")
        logger.info("term_worker_connected worker_id=%s", worker_id)
        if prev_was_hijacked:
            hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
            await hub.broadcast_hijack_state(worker_id)
        await hub.broadcast(worker_id, cast("dict[str, Any]", make_worker_connected_frame(worker_id)))
        await hub.request_snapshot(worker_id)

        cleanup_task = asyncio.create_task(_periodic_hijack_cleanup(hub, worker_id, _WORKER_HIJACK_CLEANUP_INTERVAL_S))
        decoder = ControlChannelDecoder(max_control_payload_bytes=hub.max_ws_message_bytes)
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=hub.ws_idle_timeout_s)
                except TimeoutError:
                    logger.info("ws_worker_idle_timeout worker_id=%s", worker_id)
                    break
                if len(raw.encode("utf-8")) > hub.max_ws_message_bytes:
                    logger.warning("ws_worker_oversized worker_id=%s size=%d", worker_id, len(raw))
                    continue
                if not await hub.is_active_worker(worker_id, websocket):
                    with suppress(Exception):
                        await websocket.close()
                        logger.debug("ws_worker_closed_inactive worker_id=%s", worker_id)
                    break
                try:
                    events = decoder.feed(raw)
                except ControlChannelProtocolError as exc:
                    logger.warning("ws_worker_bad_stream worker_id=%s: %s", worker_id, exc)
                    with suppress(Exception):
                        await websocket.close(code=1003, reason=str(exc))
                        logger.debug("ws_worker_closed_protocol_error worker_id=%s", worker_id)
                    break
                for event in events:
                    if isinstance(event, DataChunk):
                        if event.data:  # pragma: no branch
                            await hub.touch_activity(worker_id)
                            await hub.broadcast(
                                worker_id,
                                cast("dict[str, Any]", make_term_frame(event.data, ts=time.time())),
                            )
                            # Also publish to EventBus so fanout OutputCollector can
                            # accumulate output_delta from PTY-backed sessions.
                            await hub.append_event(worker_id, "term", {"data": event.data})
                        continue
                    msg = event.control
                    mtype = msg.get("type")
                    if mtype not in {"worker_hello", "snapshot", "analysis", "status"}:
                        logger.debug("ws_worker_ignored worker_id=%s type=%r", worker_id, mtype)
                        continue
                    if mtype == "worker_hello":
                        _hello_mode = msg.get("input_mode")
                        _protocol_v = _safe_int(msg.get("protocol_version"), 0) if "protocol_version" in msg else None
                        if _hello_mode in ("hijack", "open"):
                            if _protocol_v is not None:
                                logger.info("worker_hello_protocol worker_id=%s version=%d", worker_id, _protocol_v)
                                if _protocol_v < 1:
                                    logger.warning(
                                        "worker_hello_legacy_protocol worker_id=%s version=%d", worker_id, _protocol_v
                                    )
                            mode_applied = await hub.set_worker_hello_mode(worker_id, _hello_mode)
                            if mode_applied:
                                await hub.broadcast_hijack_state(worker_id)
                            logger.info(
                                "worker_hello worker_id=%s input_mode=%s protocol=%s applied=%s",
                                worker_id,
                                _hello_mode,
                                _protocol_v,
                                mode_applied,
                            )
                        elif _hello_mode is not None:
                            logger.warning(
                                "worker_hello_invalid_mode worker_id=%s input_mode=%r — expected 'hijack' or 'open', ignoring",
                                worker_id,
                                _hello_mode,
                            )
                        continue
                    if mtype == "snapshot":
                        snapshot = make_snapshot_frame(
                            screen=str(msg.get("screen", "")),
                            cursor=cast("dict[str, int]", msg.get("cursor", {"x": 0, "y": 0})),
                            cols=_safe_int(msg.get("cols"), 80, min_val=1),
                            rows=_safe_int(msg.get("rows"), 25, min_val=1),
                            screen_hash=str(msg.get("screen_hash", "")),
                            cursor_at_end=bool(msg.get("cursor_at_end", True)),
                            has_trailing_space=bool(msg.get("has_trailing_space", False)),
                            prompt_detected=cast("dict[str, Any] | None", msg.get("prompt_detected")),
                            ts=_safe_float(msg.get("ts"), time.time()),
                        )
                        await hub.update_last_snapshot(worker_id, cast("dict[str, Any]", snapshot))
                        await hub.broadcast(worker_id, cast("dict[str, Any]", snapshot))
                        await hub.append_event(
                            worker_id,
                            "snapshot",
                            {
                                "prompt_id": extract_prompt_id(cast("dict[str, Any]", snapshot)),
                                "screen_hash": snapshot.get("screen_hash"),
                                "screen": snapshot.get("screen", ""),
                            },
                        )
                    elif mtype == "analysis":
                        await hub.broadcast(
                            worker_id,
                            cast(
                                "dict[str, Any]",
                                make_analysis_frame(
                                    formatted=str(msg.get("formatted", "")),
                                    raw=msg.get("raw"),
                                    ts=_safe_float(msg.get("ts"), time.time()),
                                ),
                            ),
                        )
                    elif mtype == "status":  # pragma: no branch
                        status_frame = coerce_worker_status_frame(msg)
                        await hub.broadcast(worker_id, cast("dict[str, Any]", status_frame))
                        await hub.append_event(worker_id, "worker_status", {"status": status_frame})
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover
            logger.warning("term_worker_ws_error worker_id=%s error=%s", worker_id, exc)
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            should_broadcast, was_hijacked = await hub.deregister_worker(worker_id, websocket)
            with get_tracer(__name__).start_as_current_span("uterm.ws.worker.disconnect") as _wd_span:
                _set_ws_span_attrs(_wd_span, worker_id=worker_id, operation="ws.worker.disconnect")
            if should_broadcast:
                hub.metric("ws_disconnect_total")
                hub.metric("ws_disconnect_worker_total")
                logger.info("term_worker_disconnected worker_id=%s", worker_id)
                _broadcast_task = asyncio.create_task(
                    hub.broadcast(
                        worker_id,
                        cast("dict[str, Any]", make_worker_disconnected_frame(worker_id)),
                    )
                )
                hub._background_tasks.add(_broadcast_task)
                _broadcast_task.add_done_callback(hub._background_tasks.discard)
                _broadcast_task.add_done_callback(
                    lambda t: (
                        logger.warning(
                            "worker_disconnected_broadcast_failed worker_id=%s error=%s", worker_id, t.exception()
                        )
                        if not t.cancelled() and t.exception() is not None
                        else None
                    )
                )
                if was_hijacked:
                    hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
                    _hijack_state_task = asyncio.create_task(hub.broadcast_hijack_state(worker_id))
                    hub._background_tasks.add(_hijack_state_task)
                    _hijack_state_task.add_done_callback(hub._background_tasks.discard)
                    _hijack_state_task.add_done_callback(
                        lambda t: (
                            logger.warning(
                                "broadcast_hijack_state_failed worker_id=%s error=%s", worker_id, t.exception()
                            )
                            if not t.cancelled() and t.exception() is not None
                            else None
                        )
                    )
            await hub.prune_if_idle(worker_id)

    @router.websocket("/ws/browser/{worker_id}/term")
    async def ws_browser_term(
        websocket: WebSocket,
        worker_id: Annotated[str, Path(pattern=r"^[\w\-]+$")],
    ) -> None:
        await websocket.accept()
        try:
            role = await hub.resolve_role_for_browser(websocket, worker_id)
        except BrowserRoleResolutionError:
            await websocket.close(code=1008, reason="browser role resolution failed")
            return
        except WebSocketException:
            raise  # re-raise so FastAPI closes the already-accepted socket with the exception's code
        if role not in VALID_ROLES:  # pragma: no cover
            role = "viewer"
        can_hijack = role == "admin"
        # True once this browser has owned a dashboard hijack this session.
        # Retained even after the hijack is released so the finally block can
        # send a resume if the worker is still paused.  Does NOT reflect current
        # ownership — check hub state for that.
        owned_hijack = False
        # Capture all startup state atomically while registering the browser.
        browser_state = await hub.register_browser(worker_id, websocket, role)
        await hub.touch_activity(worker_id)
        with get_tracer(__name__).start_as_current_span("uterm.ws.browser.connect") as _b_span:
            _set_ws_span_attrs(_b_span, worker_id=worker_id, operation="ws.browser.connect", role=role)
        is_hijacked = browser_state["is_hijacked"]
        hijacked_by_me = browser_state["hijacked_by_me"]
        worker_online = browser_state["worker_online"]
        input_mode = browser_state["input_mode"]
        initial_snapshot = browser_state["initial_snapshot"]

        _resume_token = browser_state.get("resume_token")
        _hello_kwargs: dict[str, Any] = {
            "worker_id": worker_id,
            "can_hijack": can_hijack,
            "hijacked": is_hijacked,
            "hijacked_by_me": hijacked_by_me,
            "worker_online": worker_online,
            "input_mode": input_mode,
            "role": role,
            "hijack_control": "ws",
            "hijack_step_supported": True,
            "capabilities": {
                "hijack_control": "ws",
                "hijack_step_supported": True,
            },
            "resume_supported": hub.resume_store is not None,
            "resume_token": _resume_token,
            "protocol_version": CURRENT_PROTOCOL_VERSION,
        }
        if hasattr(hub, "deckmux_on_browser_connect"):
            _hello_kwargs["presence_enabled"] = True
        await websocket.send_text(encode_control(make_hello_frame(**_hello_kwargs)))
        await websocket.send_text(encode_control(await hub.hijack_state_msg_for(worker_id, websocket)))

        _dm_connect: Any = getattr(hub, "deckmux_on_browser_connect", None)
        if _dm_connect is not None:
            _dm_principal = getattr(getattr(websocket, "state", None), "uterm_principal", None)
            sync_msg = await _dm_connect(worker_id, websocket, role, principal=_dm_principal)
            if sync_msg:
                await websocket.send_text(encode_control(sync_msg))

        if initial_snapshot is not None:
            await websocket.send_text(encode_control(initial_snapshot))
        else:
            await hub.request_snapshot(worker_id)

        cleanup_task = asyncio.create_task(_periodic_hijack_cleanup(hub, worker_id, _BROWSER_HIJACK_CLEANUP_INTERVAL_S))
        decoder = ControlChannelDecoder(max_control_payload_bytes=hub.max_ws_message_bytes)
        try:
            _browser_bucket = TokenBucket(hub.browser_rate_limit_per_sec)
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=hub.ws_idle_timeout_s)
                except TimeoutError:
                    logger.info("ws_browser_idle_timeout worker_id=%s", worker_id)
                    break
                if len(raw.encode("utf-8")) > hub.max_ws_message_bytes:
                    logger.warning("ws_browser_oversized worker_id=%s size=%d", worker_id, len(raw))
                    continue
                try:
                    events = decoder.feed(raw)
                except ControlChannelProtocolError as exc:
                    logger.warning("ws_browser_bad_stream worker_id=%s: %s", worker_id, exc)
                    with suppress(Exception):
                        await websocket.close(code=1003, reason=str(exc))
                        logger.debug("ws_browser_closed_protocol_error worker_id=%s", worker_id)
                    break
                for event in events:
                    msg_b = {"type": "input", "data": event.data} if isinstance(event, DataChunk) else event.control
                    mtype = msg_b.get("type")
                    if mtype == "input" and not _browser_bucket.allow():
                        logger.warning("ws_browser_rate_limited worker_id=%s", worker_id)
                        with suppress(Exception):
                            await websocket.send_text(encode_control(make_error_frame("rate_limited")))
                            logger.debug("ws_browser_rate_limited_sent worker_id=%s", worker_id)
                        continue

                    # Resume handled here (not in browser_handlers) because it can
                    # update the local `role` / `can_hijack` variables.
                    if mtype == "resume" and hub.resume_store is not None:
                        from provide.uterm.bridge.routes.browser_handlers import _handle_resume

                        owned_hijack = await _handle_resume(hub, websocket, worker_id, role, msg_b, owned_hijack)
                        # _handle_resume may have updated the role in st.browsers;
                        # read it back so subsequent messages use the correct role.
                        role = await hub.get_worker_browser_role(worker_id, websocket) or role
                        can_hijack = role == "admin"
                        continue

                    if mtype in ("presence_update", "queued_input", "control_request"):
                        _dm_handle: Any = getattr(hub, "deckmux_handle_message", None)
                        if _dm_handle is not None:
                            _dm_msg_principal = getattr(getattr(websocket, "state", None), "uterm_principal", None)
                            await _dm_handle(worker_id, websocket, msg_b, principal=_dm_msg_principal)
                        continue

                    if mtype == "fanout_send":
                        _fo_ctrl: Any = getattr(hub, "fan_out_controller", None)
                        if _fo_ctrl is not None:
                            from dataclasses import asdict

                            _fo_group_id = msg_b.get("group_id", "")
                            _fo_data = msg_b.get("data", "")
                            _fo_principal = getattr(getattr(websocket, "state", None), "uterm_principal", None)
                            _fo_subj = _fo_principal.subject_id if _fo_principal else "anonymous"
                            # Verify caller has access to the group
                            _fo_group = await _fo_ctrl.get_group(_fo_group_id, principal=_fo_subj)
                            if _fo_group is None:
                                continue  # caller doesn't own/have access
                            _fo_result = await _fo_ctrl.send(
                                _fo_group_id,
                                _fo_data,
                                principal=_fo_subj,
                            )
                            await websocket.send_text(
                                encode_control(
                                    {
                                        "type": "fanout_result",
                                        "group_id": _fo_result.group_id,
                                        "send_id": _fo_result.send_id,
                                        "results": [asdict(r) for r in _fo_result.results],
                                        "divergent_sessions": _fo_result.divergent_sessions,
                                        "failed_sessions": _fo_result.failed_sessions,
                                    }
                                )
                            )
                        continue

                    if mtype in ("input", "hijack_request", "hijack_release"):
                        await hub.touch_activity(worker_id)
                    owned_hijack = await handle_browser_message(hub, websocket, worker_id, role, msg_b, owned_hijack)

        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover
            logger.warning("term_browser_ws_error worker_id=%s error=%s", worker_id, exc)
        finally:
            hub.metric("ws_disconnect_total")
            hub.metric("ws_disconnect_browser_total")
            await hub.touch_activity(worker_id)
            _dm_disconnect: Any = getattr(hub, "deckmux_on_browser_disconnect", None)
            if _dm_disconnect is not None:
                _dm_disc_principal = getattr(getattr(websocket, "state", None), "uterm_principal", None)
                await _dm_disconnect(worker_id, websocket, principal=_dm_disc_principal)
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            # Atomically detect ownership, capture REST-session liveness, and clear
            # the owner — all in one lock block to avoid the TOCTOU window where
            # _is_owner() returns True but another coroutine steals hijack_owner
            # (or vice-versa), and to avoid a second lock round-trip for
            # has_valid_rest_lease after the owner has already been cleared.
            disconnect_result = await hub.cleanup_browser_disconnect(worker_id, websocket, owned_hijack)
            was_owner = disconnect_result["was_owner"]
            rest_still_active = disconnect_result["rest_still_active"]
            resume_without_owner = disconnect_result["resume_without_owner"]
            if was_owner:
                _do_resume = not rest_still_active
                # Re-check: a concurrent hijack_acquire may have written a new
                # session between the lock release above and _send_worker below.
                if _do_resume and await hub.check_still_hijacked(worker_id):
                    _do_resume = False
                if _do_resume:
                    _resume_task = asyncio.create_task(
                        hub.send_worker(
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
                            logger.warning(
                                "ws_disconnect_resume_failed worker_id=%s error=%s", worker_id, t.exception()
                            )
                            if not t.cancelled() and t.exception() is not None
                            else None
                        )
                    )
                await hub.broadcast_hijack_state(worker_id)
                if _do_resume:
                    hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
                await hub.append_event(worker_id, "hijack_released", {"owner": "dashboard_ws_disconnect"})
            elif resume_without_owner:
                if await hub.check_still_hijacked(worker_id):
                    resume_without_owner = False
                if resume_without_owner:
                    _resume_task = asyncio.create_task(
                        hub.send_worker(
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
                            logger.warning(
                                "ws_disconnect_resume_failed worker_id=%s error=%s", worker_id, t.exception()
                            )
                            if not t.cancelled() and t.exception() is not None
                            else None
                        )
                    )
            await hub.prune_if_idle(worker_id)
