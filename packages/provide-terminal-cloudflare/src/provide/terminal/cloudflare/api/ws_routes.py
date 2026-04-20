"""Browser and worker WebSocket message dispatch for the Cloudflare backend.

Protocol note — CF vs FastAPI divergence
-----------------------------------------
The main FastAPI package (TermHub) supports the full protocol: open input mode,
viewer/operator/admin roles, browser-WS hijack negotiation, prompt guards, and
per-browser rate limiting.

This CF package is a subset: hijack control is REST-only
(`acquire`/`heartbeat`/`release`/`step`/`send`) and advertised via the WS
`hello.capabilities` handshake (`hijack_control="rest"`). WS-level hijack
frames are rejected with `use_rest_hijack_api`.
"""

from __future__ import annotations

import logging
import secrets
import time

logger = logging.getLogger(__name__)

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

try:
    from provide.terminal.cloudflare.contracts import MessageLimits, ProtocolError, RuntimeProtocol, parse_stream
except Exception:  # pragma: no cover
    from contracts import (  # type: ignore[import-not-found]  # pragma: no cover
        MessageLimits,
        ProtocolError,
        RuntimeProtocol,
        parse_stream,
    )


async def handle_socket_message(runtime: RuntimeProtocol, ws: object, raw: str, *, is_worker: bool) -> None:
    try:
        frames = parse_stream(
            raw,
            data_frame_type="term" if is_worker else "input",
            limits=MessageLimits(
                max_ws_message_bytes=runtime.config.limits.max_ws_message_bytes,
                max_input_chars=runtime.config.limits.max_input_chars,
            ),
        )
    except ProtocolError as exc:
        await runtime.send_ws(ws, {"type": "error", "message": str(exc)})
        return

    for frame in frames:
        if is_worker:
            frame_type = frame.get("type")
            if frame_type == "snapshot":
                runtime.last_snapshot = {"type": "snapshot", "screen": frame.get("screen", ""), "ts": frame.get("ts")}
                runtime.store.save_snapshot(runtime.worker_id, runtime.last_snapshot)
            elif frame_type == "worker_hello":
                mode = frame.get("mode")
                if mode in {"hijack", "open"} and (mode != "open" or runtime.hijack.session is None):
                    # Block open mode while a hijack lease is active (mirrors FastAPI set_worker_hello_mode).
                    runtime.input_mode = mode
                    runtime.store.save_input_mode(runtime.worker_id, mode)
            elif frame_type == "analysis":
                formatted = str(frame.get("formatted", ""))
                if formatted:
                    runtime.last_analysis = formatted
            await runtime.broadcast_worker_frame(frame)
            continue

        frame_type = frame.get("type")

        if frame_type == "resume":
            await _handle_resume(runtime, ws, frame)
            continue

        if frame_type == "input":
            # Open mode: operator and admin browsers can send input without an active hijack.
            if runtime.input_mode == "open":
                browser_role = runtime._socket_browser_role(ws)
                if browser_role in {"operator", "admin"}:
                    await runtime.push_worker_input(str(frame.get("data", "")))
                else:
                    await runtime.send_ws(ws, {"type": "error", "message": "viewer_cannot_send"})
                continue
            # Hijack mode: must hold the active hijack lease.
            active = runtime.hijack.session
            if active is None:
                await runtime.send_ws(ws, {"type": "error", "message": "not_hijacked"})
                continue
            if runtime.browser_hijack_owner.get(runtime.ws_key(ws)) != active.hijack_id:
                await runtime.send_ws(ws, {"type": "error", "message": "not_owner"})
                continue
            await runtime.push_worker_input(str(frame.get("data", "")))
        elif frame_type in {"hijack_request", "hijack_release", "hijack_step"}:
            # CF backend: hijack is REST-only. Inform the client rather than silently dropping.
            await runtime.send_ws(ws, {"type": "error", "message": "use_rest_hijack_api"})
        # heartbeat / ping: keep-alive frames, no response required.


async def _handle_resume(runtime: RuntimeProtocol, ws: object, frame: dict) -> None:  # type: ignore[type-arg]
    """Handle a browser resume request using a previously issued token."""
    old_token = str(frame.get("token", ""))
    if not old_token:
        return
    record = runtime.store.get_resume_token(old_token)
    if record is None or record.get("worker_id") != runtime.worker_id:
        # Invalid / expired / wrong worker — silently ignore (browser gets fresh session)
        return

    # Valid resume — revoke old token
    runtime.store.revoke_resume_token(old_token)

    stored_role = str(record.get("role", "viewer"))
    current_role = runtime._socket_browser_role(ws)
    effective_role = stored_role
    if _ROLE_RANK.get(stored_role, 0) > _ROLE_RANK.get(current_role, 0):
        effective_role = current_role
    was_hijack_owner = bool(record.get("was_hijack_owner"))

    # Update socket attachment with restored role
    try:
        ws.serializeAttachment(f"browser:{effective_role}:{runtime.worker_id}")  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("resume: serializeAttachment failed: %s", exc)

    # Reclaim hijack ownership if the session held it and the current role is admin
    reclaimed_hijack = False
    if was_hijack_owner and effective_role == "admin" and runtime.input_mode != "open":
        _lease_s = int(getattr(runtime.config, "hijack_lease_s", 60))
        result = runtime.hijack.acquire("dashboard_resume", _lease_s)
        if result.ok and result.session is not None:
            ws_key = runtime.ws_key(ws)
            runtime.browser_hijack_owner[ws_key] = result.session.hijack_id
            runtime.persist_lease(result.session)
            if not result.is_renewal:
                await runtime.push_worker_control("pause", owner="dashboard_resume", lease_s=_lease_s)
            await runtime.broadcast_hijack_state()
            reclaimed_hijack = True

    # Issue new token
    new_token = secrets.token_urlsafe(32)
    resume_ttl_s = float(getattr(runtime.config, "resume_ttl_s", 300))
    runtime.store.create_resume_token(new_token, runtime.worker_id, effective_role, resume_ttl_s)

    # Send updated hello with resumed=True
    await runtime.send_ws(
        ws,
        {
            "type": "hello",
            "worker_id": runtime.worker_id,
            "worker_online": runtime.worker_ws is not None,
            "can_hijack": effective_role == "admin",
            "input_mode": runtime.input_mode,
            "role": effective_role,
            "hijack_control": "rest",
            "hijack_step_supported": True,
            "resume_supported": True,
            "resume_token": new_token,
            "resumed": True,
            "ts": time.time(),
        },
    )
    await runtime.send_hijack_state(ws)  # type: ignore[attr-defined]
    if runtime.last_snapshot is not None:
        await runtime.send_ws(ws, runtime.last_snapshot)
    logger.info(
        "ws_browser_resumed worker_id=%s role=%s hijack_owner=%s reclaimed=%s",
        runtime.worker_id,
        effective_role,
        was_hijack_owner,
        reclaimed_hijack,
    )
