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

from pydantic import ValidationError

from provide.telemetry import get_logger, get_tracer

try:
    from fastapi import APIRouter, Path, WebSocket, WebSocketDisconnect, WebSocketException
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-uterm[websocket]'") from _e


from provide.uterm.bridge.contracts import (
    CURRENT_PROTOCOL_VERSION,
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    PREFERRED_PROTOCOL_VERSION,
)
from provide.uterm.control_channel import (
    ControlFrameDecoder,
    ControlFrameProtocolError,
    DataChunk,
    encode_control_frame,
)
from provide.uterm.server.bridge.frames import (
    make_hello_frame,
    make_term_frame,
    make_worker_connected_frame,
    make_worker_disconnected_frame,
)
from provide.uterm.server.bridge.models import VALID_ROLES
from provide.uterm.server.bridge.ratelimit import TokenBucket
from provide.uterm.server.bridge.routes.browser_handlers import handle_browser_message
from provide.uterm.server.bridge.routes.websockets_browser import (
    dispatch_browser_event,
    resume_worker_on_disconnect,
)
from provide.uterm.server.bridge.routes.websockets_worker import (
    _build_worker_frame,
    _dispatch_worker_frame,
    _handle_worker_hello,
)

if TYPE_CHECKING:
    from provide.uterm.server.bridge.hub import BrowserRoleResolutionError, TermHub
else:
    from provide.uterm.server.bridge.hub import BrowserRoleResolutionError

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
        decoder = ControlFrameDecoder(max_control_payload_bytes=hub.max_ws_message_bytes)
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
                except ControlFrameProtocolError as exc:
                    preview = raw[:256]
                    logger.warning(
                        "ws_worker_bad_stream worker_id=%s: %s raw_len=%d preview=%r",
                        worker_id,
                        exc,
                        len(raw),
                        preview,
                    )
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
                        # ``worker_hello`` carries no validated frame builder —
                        # it negotiates protocol + applies the input mode, doing
                        # its own I/O. It can ``break`` (protocol mismatch) or
                        # ``continue``; handle it entirely here, OUTSIDE the
                        # builder guard below.
                        if await _handle_worker_hello(hub, websocket, worker_id, msg):
                            break
                        continue

                    # Finding #5d (narrowed): validate the control-frame BUILDER
                    # at the trust boundary. The builders below
                    # (make_snapshot_frame / make_analysis_frame /
                    # coerce_worker_status_frame) enforce field types and raise
                    # on a malformed worker frame (e.g. snapshot
                    # ``cursor.x="abc"``). Without this guard that exception
                    # would reach the OUTER except and tear down the worker
                    # session AND every browser viewing it — a DoS from one bad
                    # frame. The guard isolates the bad frame: ``drop`` (default)
                    # drops it and keeps the session alive; ``reject`` sends an
                    # error frame and closes 1003.
                    #
                    # CRITICAL: the ``try`` wraps ONLY the builder call. The
                    # downstream I/O (update_last_snapshot / broadcast /
                    # append_event + redaction) runs OUTSIDE it, so a genuine
                    # server-side failure there is NOT mis-isolated as an
                    # "invalid worker frame" (mis-counted + swallowed, masking a
                    # real bug) — it propagates to the outer handler. The
                    # builder runs BEFORE any state mutation, so a build failure
                    # also cannot leave partial state (snapshot stored but never
                    # broadcast/recorded).
                    #
                    # The DataChunk (raw terminal data) hot path stays OUTSIDE
                    # this wrapper (above) — it builds no validated frame and
                    # must not pay the try/except cost.
                    #
                    # Only frame-MALFORMATION errors are caught.
                    # ``WebSocketDisconnect`` / ``CancelledError`` are NOT
                    # subclasses of the caught types, so they still propagate.
                    try:
                        built_frame = _build_worker_frame(mtype, msg)
                    except (ValidationError, ValueError, KeyError, TypeError) as exc:
                        # A malformed worker control frame failed type validation
                        # in a builder above. Isolate it per the configured
                        # policy so one bad frame cannot DoS the session +
                        # viewers.
                        hub.metric("ws_worker_frame_invalid_total")
                        if hub.worker_frame_on_invalid == "reject":
                            logger.warning("ws_worker_frame_invalid_reject worker_id=%s error=%s", worker_id, exc)
                            with suppress(Exception):
                                await websocket.send_text(
                                    encode_control_frame({"type": "error", "reason": "invalid_frame"})
                                )
                                await websocket.close(code=1003, reason="invalid_frame")
                            break
                        logger.debug("ws_worker_frame_invalid_drop worker_id=%s error=%s", worker_id, exc)
                        continue

                    # Builder succeeded → dispatch the I/O OUTSIDE the guard so a
                    # downstream failure propagates instead of being miscounted.
                    await _dispatch_worker_frame(hub, worker_id, mtype, built_frame)
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
        # Multi-backend Playwright e2e: UTERM_TEST_MODE=1 forces admin (never default-on).
        import os as _os

        if _os.environ.get("UTERM_TEST_MODE") == "1":
            role = "admin"
        else:
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
        # Declared before the try so the finally can safely guard it: the
        # cleanup task is created INSIDE the try (after the handshake), so on
        # an early disconnect/raise it may never be assigned.
        cleanup_task: asyncio.Task[None] | None = None
        # Capture all startup state atomically while registering the browser.
        # register_browser increments the per-principal browser quota; the
        # paired decrement lives in cleanup_browser_disconnect, invoked from
        # the finally below. The try MUST start immediately after this call so
        # that any setup line raising (or the browser disconnecting) between
        # the increment and the receive loop still runs the finally — otherwise
        # the quota counter leaks and the principal is eventually locked out.
        browser_state = await hub.register_browser(worker_id, websocket, role, defer_broadcast=True)
        try:
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
                # Range advertisement for client-side negotiation. Browsers
                # currently don't echo back, so this is informational; the
                # field exists so future browser versions can read it.
                "protocol": {
                    "selected": PREFERRED_PROTOCOL_VERSION,
                    "server_min": MIN_PROTOCOL_VERSION,
                    "server_max": MAX_PROTOCOL_VERSION,
                },
            }
            if hasattr(hub, "deckmux_on_browser_connect"):
                _hello_kwargs["presence_enabled"] = True
            await websocket.send_text(encode_control_frame(make_hello_frame(**_hello_kwargs)))
            await websocket.send_text(encode_control_frame(await hub.hijack_state_msg_for(worker_id, websocket)))

            _dm_connect: Any = getattr(hub, "deckmux_on_browser_connect", None)
            if _dm_connect is not None:
                # UTERM_TEST_MODE=1: use connection-scoped DeckMux identity so
                # multi-tab Playwright sees distinct users (shared test admin).
                import os as _os

                _dm_principal = None
                if _os.environ.get("UTERM_TEST_MODE") != "1":
                    _dm_principal = getattr(getattr(websocket, "state", None), "uterm_principal", None)
                sync_msg = await _dm_connect(worker_id, websocket, role, principal=_dm_principal)
                if sync_msg:
                    await websocket.send_text(encode_control_frame(sync_msg))

            if initial_snapshot is not None:
                # The connect-time output-policy redaction already happened in
                # register_browser (role-scoped to this websocket). initial_snapshot
                # here is already a redacted copy when a policy is active (M5).
                await websocket.send_text(encode_control_frame(initial_snapshot))
            else:
                await hub.request_snapshot(worker_id)
            await hub.activate_browser_broadcasts(worker_id, websocket)

            cleanup_task = asyncio.create_task(
                _periodic_hijack_cleanup(hub, worker_id, _BROWSER_HIJACK_CLEANUP_INTERVAL_S)
            )
            decoder = ControlFrameDecoder(max_control_payload_bytes=hub.max_ws_message_bytes)
            _browser_bucket = TokenBucket(hub.browser_rate_limit_per_sec)
            # Separate budget for non-input control frames. See
            # ``browser_control_rate_limit_per_sec`` in TermHub for the
            # threat-model rationale.
            _browser_control_bucket = TokenBucket(hub.browser_control_rate_limit_per_sec)
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
                except ControlFrameProtocolError as exc:
                    logger.warning("ws_browser_bad_stream worker_id=%s: %s", worker_id, exc)
                    with suppress(Exception):
                        await websocket.close(code=1003, reason=str(exc))
                        logger.debug("ws_browser_closed_protocol_error worker_id=%s", worker_id)
                    break
                for event in events:
                    # Per-frame rate-limit + resume/presence/fanout/generic
                    # dispatch lives in ``dispatch_browser_event``. A ``resume``
                    # frame can update the local ``role`` / ``can_hijack``, so
                    # those (and ``owned_hijack``) are read back from the return.
                    role, can_hijack, owned_hijack = await dispatch_browser_event(
                        hub,
                        websocket,
                        worker_id,
                        role,
                        can_hijack,
                        owned_hijack,
                        event,
                        _browser_bucket,
                        _browser_control_bucket,
                    )

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
                import os as _os

                _dm_disc_principal = None
                if _os.environ.get("UTERM_TEST_MODE") != "1":
                    _dm_disc_principal = getattr(getattr(websocket, "state", None), "uterm_principal", None)
                await _dm_disconnect(worker_id, websocket, principal=_dm_disc_principal)
            # cleanup_task may be None if the handshake raised before it was
            # created (e.g. a mid-handshake disconnect); guard both references.
            if cleanup_task is not None:
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
                    resume_worker_on_disconnect(hub, worker_id)
                await hub.broadcast_hijack_state(worker_id)
                if _do_resume:
                    hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
                await hub.append_event(worker_id, "hijack_released", {"owner": "dashboard_ws_disconnect"})
            elif resume_without_owner:
                if await hub.check_still_hijacked(worker_id):
                    resume_without_owner = False
                if resume_without_owner:
                    resume_worker_on_disconnect(hub, worker_id)
            await hub.prune_if_idle(worker_id)
