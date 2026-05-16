#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``uterm inspect`` — HTTP reverse proxy with traffic inspection.

Forwards HTTP traffic to a local port through a tunnel server, sending
structured JSON inspection data on CHANNEL_HTTP (0x03) for each request.

Example::

    uterm inspect 3000 --server https://warp.provide.io
    uterm inspect 8080 --server https://warp.provide.io --display-name "my-api"
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse

from provide.uterm.defaults import TerminalDefaults
from provide.uterm.tunnel.http_proxy import encode_body, format_log_line
from provide.uterm.tunnel.protocol import (
    CHANNEL_DATA,
    CHANNEL_HTTP,
    encode_control,
    encode_frame,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _read_token(args: argparse.Namespace) -> str | None:
    """Resolve bearer token from --token or --token-file."""
    if getattr(args, "token", None):
        return args.token  # type: ignore[no-any-return]
    token_path = Path(getattr(args, "token_file", "") or str(TerminalDefaults.token_file())).expanduser()
    if token_path.is_file():
        return token_path.read_text().strip()
    return None


def _create_tunnel(server: str, display_name: str, token: str | None, target_port: int) -> dict[str, Any]:
    """POST /api/tunnels to create an HTTP inspection tunnel session."""
    url = f"{server.rstrip('/')}/api/tunnels"
    body = json.dumps(
        {
            "tunnel_type": "http",
            "display_name": display_name,
            "local_port": target_port,
        }
    ).encode()
    headers: dict[str, str] = {"Content-Type": "application/json", "User-Agent": "uterm-inspect/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
            return json.loads(resp.read())  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        detail = ""
        with suppress(Exception):
            detail = exc.read().decode(errors="replace")
        print(f"error: tunnel creation failed (HTTP {exc.code}): {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"error: cannot reach server: {exc.reason}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


def _cmd_inspect(args: argparse.Namespace) -> None:
    """Execute the ``uterm inspect`` subcommand."""
    server: str = args.server
    target_port: int = args.port
    listen_port: int = getattr(args, "listen_port", 0)
    display_name: str = getattr(args, "display_name", None) or f"http:{target_port}"
    token = _read_token(args)

    intercept = getattr(args, "intercept", False)
    intercept_timeout = getattr(args, "intercept_timeout", 30.0)
    intercept_timeout_action = getattr(args, "intercept_timeout_action", "forward")

    print("Creating tunnel...", end=" ", flush=True)
    tunnel_info = _create_tunnel(server, display_name, token, target_port)
    ws_endpoint = tunnel_info.get("ws_endpoint", "")
    worker_token = tunnel_info.get("worker_token", "")
    share_url = tunnel_info.get("share_url", "")
    tunnel_id = tunnel_info.get("tunnel_id", tunnel_info.get("session_id", ""))
    print(f"done ({tunnel_id})" if tunnel_id else "done")

    if not ws_endpoint:
        print("error: server response missing ws_endpoint", file=sys.stderr)
        sys.exit(1)

    # Resolve relative WS endpoint.
    if ws_endpoint.startswith("/"):
        ws_base = server.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        ws_endpoint = f"{ws_base}{ws_endpoint}"

    print(f"Inspecting HTTP traffic on localhost:{target_port}")
    if share_url:
        print(f"  Share: {share_url}")
    if intercept:
        print(f"  Intercept: ON (timeout: {intercept_timeout}s, action: {intercept_timeout_action})")
    print("Press Ctrl+C to stop.\n")

    with suppress(KeyboardInterrupt):
        asyncio.run(
            _run_inspect(
                ws_endpoint,
                worker_token,
                target_port,
                listen_port,
                intercept=intercept,
                intercept_timeout=intercept_timeout,
                intercept_timeout_action=intercept_timeout_action,
            )
        )


async def _run_inspect(
    ws_endpoint: str,
    worker_token: str,
    target_port: int,
    listen_port: int,
    *,
    intercept: bool = False,
    intercept_timeout: float = 30.0,
    intercept_timeout_action: str = "forward",
) -> None:
    """Connect to tunnel WS and start an HTTP reverse proxy with inspection."""
    import time

    try:
        import httpx
        import uvicorn
        import websockets
    except ImportError as exc:  # pragma: no cover — can't test without uninstalling deps
        print(
            f"error: missing dependency — {exc}\ninstall the cli extra: pip install 'provide-uterm[cli]'",
            file=sys.stderr,
        )
        sys.exit(1)

    headers: dict[str, str] = {}
    if worker_token:
        headers["Authorization"] = f"Bearer {worker_token}"

    ws = await websockets.connect(ws_endpoint, additional_headers=headers)
    await ws.send(
        encode_control(
            {
                "type": "open",
                "channel": CHANNEL_HTTP,
                "tunnel_type": "http",
                "local_port": target_port,
            }
        )
    )

    from provide.uterm.tunnel.intercept import InterceptGate, parse_action_message

    gate = InterceptGate(timeout_s=intercept_timeout, timeout_action=intercept_timeout_action)
    gate.enabled = intercept

    # Send initial intercept state
    _state_msg = {
        "type": "http_intercept_state",
        "enabled": gate.enabled,
        "inspect_enabled": gate.inspect_enabled,
        "timeout_s": gate.timeout_s,
        "timeout_action": gate.timeout_action,
    }
    with suppress(Exception):
        await ws.send(encode_frame(CHANNEL_HTTP, json.dumps(_state_msg).encode()))

    req_counter = 0

    async def _proxy_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Minimal ASGI app that proxies HTTP requests with inspection."""
        nonlocal req_counter
        if scope["type"] != "http":
            return

        # Read request body
        body_parts: list[bytes] = []
        while True:
            msg = await receive()
            body_parts.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        req_body = b"".join(body_parts)

        method = scope["method"]
        path = scope["path"]
        qs = scope.get("query_string", b"").decode()
        target_url = f"http://127.0.0.1:{target_port}{path}"
        if qs:
            target_url += f"?{qs}"

        req_headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        req_ct = req_headers.get("content-type", "")

        req_counter += 1
        rid = f"r{req_counter}"

        # Send http_req on channel 0x03 (only when inspect is enabled)
        if gate.inspect_enabled:
            req_event: dict[str, Any] = {
                "type": "http_req",
                "id": rid,
                "ts": time.time(),
                "method": method,
                "url": f"{path}?{qs}" if qs else path,
                "headers": req_headers,
                "intercepted": gate.enabled,
                **encode_body(req_body, req_ct),
            }
            with suppress(Exception):
                await ws.send(encode_frame(CHANNEL_HTTP, json.dumps(req_event, separators=(",", ":")).encode()))

            print(format_log_line(method, path, None, None, len(req_body)), file=sys.stderr)
            await ws.send(
                encode_frame(CHANNEL_DATA, (format_log_line(method, path, None, None, len(req_body)) + "\n").encode())
            )

        # Intercept gate: pause for browser decision if enabled
        fwd_headers = {k: v for k, v in req_headers.items() if k.lower() not in ("host", "transfer-encoding")}
        fwd_body = req_body

        if gate.enabled and gate.inspect_enabled:
            decision = await gate.await_decision(rid)
            if decision["action"] == "drop":
                await send(
                    {"type": "http.response.start", "status": 502, "headers": [(b"content-type", b"text/plain")]}
                )
                await send({"type": "http.response.body", "body": b"Request dropped by interceptor"})
                # Send synthetic http_res so browser shows the drop
                drop_event: dict[str, Any] = {
                    "type": "http_res",
                    "id": rid,
                    "ts": time.time(),
                    "status": 502,
                    "status_text": "Dropped",
                    "headers": {},
                    "body_size": 0,
                    "duration_ms": 0,
                }
                with suppress(Exception):
                    await ws.send(encode_frame(CHANNEL_HTTP, json.dumps(drop_event, separators=(",", ":")).encode()))
                return
            if decision["action"] == "modify":
                if decision["headers"] is not None:
                    fwd_headers = decision["headers"]
                if decision["body"] is not None:
                    fwd_body = decision["body"]

        # Forward to target
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient() as client:
                upstream = await client.request(method, target_url, headers=fwd_headers, content=fwd_body)
                resp_body = upstream.content
                duration_ms = (time.monotonic() - t0) * 1000
                resp_ct = upstream.headers.get("content-type", "")

                # Send http_res on channel 0x03 (only when inspect is enabled)
                if gate.inspect_enabled:
                    res_event: dict[str, Any] = {
                        "type": "http_res",
                        "id": rid,
                        "ts": time.time(),
                        "status": upstream.status_code,
                        "status_text": upstream.reason_phrase,
                        "headers": dict(upstream.headers),
                        "duration_ms": round(duration_ms, 1),
                        **encode_body(resp_body, resp_ct),
                    }
                    with suppress(Exception):
                        await ws.send(encode_frame(CHANNEL_HTTP, json.dumps(res_event, separators=(",", ":")).encode()))

                    log_line = format_log_line(method, path, upstream.status_code, duration_ms, len(resp_body))
                    print(log_line, file=sys.stderr)
                    await ws.send(encode_frame(CHANNEL_DATA, (log_line + "\n").encode()))

                # Send response back to local client
                resp_headers = [
                    (k.encode(), v.encode())
                    for k, v in upstream.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-encoding")
                ]
                await send({"type": "http.response.start", "status": upstream.status_code, "headers": resp_headers})
                await send({"type": "http.response.body", "body": resp_body})

        except Exception as exc:
            log.warning("inspect_proxy_error url=%s error=%s", target_url, exc)
            await send({"type": "http.response.start", "status": 502, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": f"Bad Gateway: {exc}".encode()})

    async def _ws_action_receiver() -> None:
        """Read http_action/toggle messages from the tunnel WS."""
        from provide.uterm.tunnel.protocol import decode_frame

        try:
            async for raw in ws:
                # Binary frames: tunnel protocol (CHANNEL_HTTP)
                if isinstance(raw, bytes) and len(raw) > 2:
                    frame = decode_frame(raw)
                    if frame.channel != CHANNEL_HTTP:
                        continue
                    try:
                        msg = json.loads(frame.payload)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                elif isinstance(raw, str):
                    # Text frames: direct JSON (FastAPI relay path)
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") not in ("http_action", "http_intercept_toggle", "http_inspect_toggle"):
                        continue
                else:
                    continue
                msg_type = msg.get("type")
                if msg_type == "http_action":
                    decision = parse_action_message(msg)
                    rid = str(msg.get("id", ""))
                    if not gate.resolve(rid, decision):
                        log.warning("intercept_unknown_id id=%s", rid)
                elif msg_type == "http_intercept_toggle":
                    gate.enabled = bool(msg.get("enabled", False))
                    if not gate.enabled:
                        gate.cancel_all("forward")
                    _broadcast_state()
                elif msg_type == "http_inspect_toggle":
                    gate.inspect_enabled = bool(msg.get("enabled", True))
                    if not gate.inspect_enabled:
                        gate.cancel_all("forward")
                        gate.enabled = False
                    _broadcast_state()
        except Exception:
            gate.cancel_all("forward")

    def _broadcast_state() -> None:
        state = {
            "type": "http_intercept_state",
            "enabled": gate.enabled,
            "inspect_enabled": gate.inspect_enabled,
            "timeout_s": gate.timeout_s,
            "timeout_action": gate.timeout_action,
        }
        with suppress(Exception):
            _t = asyncio.create_task(ws.send(encode_frame(CHANNEL_HTTP, json.dumps(state).encode())))
            _t.add_done_callback(lambda _: None)

    _receiver = asyncio.create_task(_ws_action_receiver())  # noqa: RUF006

    bind_port = listen_port or 0
    config = uvicorn.Config(_proxy_app, host="127.0.0.1", port=bind_port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


# ---------------------------------------------------------------------------
# Subcommand registration
# ---------------------------------------------------------------------------


def add_inspect_subcommand(subparsers: Any) -> None:
    """Register the ``inspect`` subcommand."""
    inspect_p = subparsers.add_parser(
        "inspect",
        help="HTTP reverse proxy with traffic inspection via tunnel server",
        description="Forward HTTP traffic to a local port through a remote tunnel server with structured inspection.",
    )
    inspect_p.add_argument(
        "port",
        type=int,
        metavar="PORT",
        help="local HTTP target port to inspect",
    )
    inspect_p.add_argument(
        "--server",
        "-s",
        required=True,
        metavar="URL",
        help="CF worker / tunnel server URL",
    )
    inspect_p.add_argument(
        "--listen-port",
        type=int,
        metavar="PORT",
        default=0,
        help="local proxy listen port (0 = auto-assign, default: 0)",
    )
    inspect_p.add_argument(
        "--token",
        metavar="TOKEN",
        default=None,
        help="bearer token for API auth",
    )
    inspect_p.add_argument(
        "--token-file",
        metavar="FILE",
        default=str(TerminalDefaults.token_file()),
        help="path to token file",
    )
    inspect_p.add_argument(
        "--display-name",
        metavar="NAME",
        default=None,
        help="override display name (default: http:<port>)",
    )
    inspect_p.add_argument(
        "--intercept",
        action="store_true",
        default=False,
        help="enable HTTP request interception (pause before forwarding)",
    )
    inspect_p.add_argument(
        "--intercept-timeout",
        type=float,
        metavar="SECONDS",
        default=30.0,
        help="seconds to wait for browser action (default: 30)",
    )
    inspect_p.add_argument(
        "--intercept-timeout-action",
        choices=["forward", "drop"],
        default="forward",
        help="action on timeout: forward (default) or drop",
    )
    inspect_p.set_defaults(func=_cmd_inspect)
