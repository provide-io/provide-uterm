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
from typing import TYPE_CHECKING, Any, cast

from provide.uterm.bridge.contracts import (
    CURRENT_PROTOCOL_VERSION,
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    PREFERRED_PROTOCOL_VERSION,
    negotiate_protocol_version,
)

if TYPE_CHECKING:
    from provide.uterm.cloudflare.cf_types import CFWebSocket

logger = logging.getLogger(__name__)

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

try:
    from provide.uterm.cloudflare.contracts import MessageLimits, ProtocolError, RuntimeProtocol, parse_stream
except Exception:  # pragma: no cover
    from contracts import (  # type: ignore[import-not-found,no-redef]  # pragma: no cover
        MessageLimits,
        ProtocolError,
        RuntimeProtocol,
        parse_stream,
    )


async def handle_socket_message(runtime: RuntimeProtocol, ws: CFWebSocket, raw: str, *, is_worker: bool) -> None:
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
                # Protocol range negotiation. Accept the new {min,max,preferred}
                # block or the legacy single int; missing fields default to v1.
                _proto_block = frame.get("protocol")
                if isinstance(_proto_block, dict):
                    _client_min = int(_proto_block.get("min", MIN_PROTOCOL_VERSION))
                    _client_max = int(_proto_block.get("max", MAX_PROTOCOL_VERSION))
                elif "protocol_version" in frame:
                    _legacy_v = int(frame.get("protocol_version") or 0)
                    _client_min = _legacy_v if _legacy_v >= 1 else 1
                    _client_max = _client_min
                else:
                    _client_min = 1
                    _client_max = 1
                _selected = negotiate_protocol_version(_client_min, _client_max)
                if _selected is None:
                    logger.warning(
                        "worker_hello_protocol_mismatch worker_id=%s client=[%d,%d] server=[%d,%d]",
                        runtime.worker_id,
                        _client_min,
                        _client_max,
                        MIN_PROTOCOL_VERSION,
                        MAX_PROTOCOL_VERSION,
                    )
                    # Close the worker WS with the same error contract as the
                    # FastAPI server. CF Workers WebSocket close API only takes
                    # code + reason — the structured frame goes through
                    # broadcast first, then we close.
                    try:
                        await runtime.broadcast_worker_frame(
                            {
                                "type": "error",
                                "reason": "protocol_mismatch",
                                "client_min": _client_min,
                                "client_max": _client_max,
                                "server_min": MIN_PROTOCOL_VERSION,
                                "server_max": MAX_PROTOCOL_VERSION,
                            }
                        )
                    except Exception:
                        pass
                    try:
                        ws.close(1002, "protocol_mismatch")
                    except Exception:
                        pass
                    return
                if mode in {"hijack", "open"} and (mode != "open" or runtime.hijack.session is None):
                    # Block open mode while a hijack lease is active (mirrors FastAPI set_worker_hello_mode).
                    runtime.input_mode = mode
                    runtime.store.save_input_mode(runtime.worker_id, mode)
                    logger.info(
                        "worker_hello worker_id=%s mode=%s protocol_selected=%d",
                        runtime.worker_id,
                        mode,
                        _selected,
                    )
            elif frame_type == "analysis":
                formatted = str(frame.get("formatted", ""))
                if formatted:
                    runtime.last_analysis = formatted
            await runtime.broadcast_worker_frame(frame)
            continue

        frame_type = frame.get("type")

        if frame_type == "resume":
            await _handle_resume(runtime, ws, cast("dict[str, Any]", frame))
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
        elif frame_type in {"presence_update", "queued_input", "control_request"}:
            await _handle_presence_message(runtime, ws, cast("dict[str, Any]", frame))
        elif frame_type in {"http_action", "http_intercept_toggle", "http_inspect_toggle"}:
            # Relay intercept/inspect commands from browser back to the worker
            if runtime.worker_ws is not None:
                await runtime.send_ws(runtime.worker_ws, cast("dict[str, object]", frame))
        elif frame_type == "ack":
            # Browser reports cumulative bytes consumed → drives Tier-A backpressure.
            # _normalize_frame already coerced "bytes" to a non-negative int.
            acked = cast("dict[str, Any]", frame).get("bytes", 0)
            await runtime.note_browser_ack(runtime.ws_key(ws), acked)
        # heartbeat / ping: keep-alive frames, no response required.


async def _handle_presence_message(runtime: RuntimeProtocol, ws: CFWebSocket, frame: dict[str, Any]) -> None:
    """Relay a DeckMux presence message to all other connected browsers.

    The DO acts as a message router only — browser-side coordinators own state.
    Presence messages are silently dropped when the session has not been
    configured with ``presence: true`` in its KV metadata.
    """
    if not runtime.meta.get("presence"):
        return
    frame_type = frame.get("type")
    sender_key = runtime.ws_key(ws)

    # control_request: relay only to the current hijack owner (if any).
    if frame_type == "control_request":
        owner_key = None
        active = runtime.hijack.session
        if active is not None:
            for ws_id, candidate in list(runtime.browser_sockets.items()):  # type: ignore[attr-defined]
                if runtime.browser_hijack_owner.get(ws_id) == active.hijack_id:
                    owner_key = ws_id
                    target_ws = candidate
                    break
        if owner_key is not None and owner_key != sender_key:
            try:
                await runtime.send_ws(target_ws, frame)
            except Exception:
                runtime.browser_sockets.pop(owner_key, None)  # type: ignore[attr-defined]
        return

    # presence_update / queued_input: relay to all other browsers.
    try:
        all_ws = list(runtime.ctx.getWebSockets())  # type: ignore[attr-defined]
    except Exception:
        all_ws = []
    if not all_ws:
        all_ws = list(runtime.browser_sockets.values())  # type: ignore[attr-defined]
    for other_ws in all_ws:
        if runtime._socket_role(other_ws) != "browser":  # type: ignore[attr-defined]
            continue
        if runtime.ws_key(other_ws) == sender_key:
            continue
        ws_id = runtime.ws_key(other_ws)
        try:
            await runtime.send_ws(other_ws, frame)
        except Exception:
            runtime.browser_sockets.pop(ws_id, None)  # type: ignore[attr-defined]


async def _handle_resume(runtime: RuntimeProtocol, ws: CFWebSocket, frame: dict[str, Any]) -> None:
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
        ws.serializeAttachment(f"browser:{effective_role}:{runtime.worker_id}")
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
            "protocol_version": CURRENT_PROTOCOL_VERSION,
            "protocol": {
                "selected": PREFERRED_PROTOCOL_VERSION,
                "server_min": MIN_PROTOCOL_VERSION,
                "server_max": MAX_PROTOCOL_VERSION,
            },
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
