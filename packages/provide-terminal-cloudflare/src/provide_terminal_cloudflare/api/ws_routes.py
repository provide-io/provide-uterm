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

try:
    from provide_terminal_cloudflare.contracts import MessageLimits, ProtocolError, RuntimeProtocol, parse_frame
except Exception:  # pragma: no cover
    from contracts import (  # type: ignore[import-not-found]  # pragma: no cover
        MessageLimits,
        ProtocolError,
        RuntimeProtocol,
        parse_frame,
    )


async def handle_socket_message(runtime: RuntimeProtocol, ws: object, raw: str, *, is_worker: bool) -> None:
    try:
        frame = parse_frame(
            raw,
            limits=MessageLimits(
                max_ws_message_bytes=runtime.config.limits.max_ws_message_bytes,
                max_input_chars=runtime.config.limits.max_input_chars,
            ),
        )
    except ProtocolError as exc:
        await runtime.send_ws(ws, {"type": "error", "message": str(exc)})
        return

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
        return

    frame_type = frame.get("type")

    if frame_type == "input":
        # Open mode: operator and admin browsers can send input without an active hijack.
        if runtime.input_mode == "open":
            browser_role = runtime._socket_browser_role(ws)
            if browser_role in {"operator", "admin"}:
                await runtime.push_worker_input(str(frame.get("data", "")))
            else:
                await runtime.send_ws(ws, {"type": "error", "message": "viewer_cannot_send"})
            return
        # Hijack mode: must hold the active hijack lease.
        active = runtime.hijack.session
        if active is None:
            await runtime.send_ws(ws, {"type": "error", "message": "not_hijacked"})
            return
        if runtime.browser_hijack_owner.get(runtime.ws_key(ws)) != active.hijack_id:
            await runtime.send_ws(ws, {"type": "error", "message": "not_owner"})
            return
        await runtime.push_worker_input(str(frame.get("data", "")))
    elif frame_type in {"hijack_request", "hijack_release", "hijack_step"}:
        # CF backend: hijack is REST-only. Inform the client rather than silently dropping.
        await runtime.send_ws(ws, {"type": "error", "message": "use_rest_hijack_api"})
    # heartbeat / ping: keep-alive frames, no response required.
