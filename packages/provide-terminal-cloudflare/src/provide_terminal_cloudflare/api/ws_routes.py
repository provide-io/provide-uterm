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
except Exception:
    from contracts import MessageLimits, ProtocolError, RuntimeProtocol, parse_frame  # type: ignore[import-not-found]


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
        await runtime.broadcast_worker_frame(frame)
        return

    frame_type = frame.get("type")

    if frame_type == "input":
        active = runtime.hijack.session
        if active is None:
            await runtime.send_ws(ws, {"type": "error", "message": "not_hijacked"})
            return
        if runtime.browser_hijack_owner.get(runtime._ws_key(ws)) != active.hijack_id:
            await runtime.send_ws(ws, {"type": "error", "message": "not_owner"})
            return
        await runtime.push_worker_input(str(frame.get("data", "")))
    elif frame_type in {"hijack_request", "hijack_release", "hijack_step"}:
        # CF backend: hijack is REST-only. Inform the client rather than silently dropping.
        await runtime.send_ws(ws, {"type": "error", "message": "use_rest_hijack_api"})
    # heartbeat / ping: keep-alive frames, no response required.
