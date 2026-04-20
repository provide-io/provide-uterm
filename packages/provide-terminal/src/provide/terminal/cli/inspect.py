#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
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

from provide.terminal.defaults import TerminalDefaults
from provide.terminal.tunnel.http_proxy import encode_body, format_log_line
from provide.terminal.tunnel.protocol import (
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

    tunnel_info = _create_tunnel(server, display_name, token, target_port)
    ws_endpoint = tunnel_info.get("ws_endpoint", "")
    worker_token = tunnel_info.get("worker_token", "")
    share_url = tunnel_info.get("share_url", "")

    if not ws_endpoint:
        print("error: server response missing ws_endpoint", file=sys.stderr)
        sys.exit(1)

    # Resolve relative WS endpoint.
    if ws_endpoint.startswith("/"):
        ws_base = server.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        ws_endpoint = f"{ws_base}{ws_endpoint}"

    print(f"Inspecting HTTP traffic on localhost:{target_port}...")
    if share_url:
        print(f"  Share: {share_url}")
    print("\nConnected. Press Ctrl+C to stop.")

    with suppress(KeyboardInterrupt):
        asyncio.run(_run_inspect(ws_endpoint, worker_token, target_port, listen_port))


async def _run_inspect(
    ws_endpoint: str, worker_token: str, target_port: int, listen_port: int
) -> None:  # pragma: no cover — integration; tested via E2E
    """Connect to tunnel WS and start an HTTP reverse proxy with inspection."""
    import time

    try:
        import httpx
        import uvicorn
        import websockets
    except ImportError as exc:
        print(
            f"error: missing dependency — {exc}\ninstall the cli extra: pip install 'provide-terminal[cli]'",
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

        # Send http_req on channel 0x03
        req_event: dict[str, Any] = {
            "type": "http_req",
            "id": rid,
            "ts": time.time(),
            "method": method,
            "url": f"{path}?{qs}" if qs else path,
            "headers": req_headers,
            **encode_body(req_body, req_ct),
        }
        with suppress(Exception):
            await ws.send(encode_frame(CHANNEL_HTTP, json.dumps(req_event, separators=(",", ":")).encode()))

        print(format_log_line(method, path, None, None, len(req_body)), file=sys.stderr)
        await ws.send(
            encode_frame(CHANNEL_DATA, (format_log_line(method, path, None, None, len(req_body)) + "\n").encode())
        )

        # Forward to target
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient() as client:
                fwd_headers = {k: v for k, v in req_headers.items() if k.lower() not in ("host", "transfer-encoding")}
                upstream = await client.request(method, target_url, headers=fwd_headers, content=req_body)
                resp_body = upstream.content
                duration_ms = (time.monotonic() - t0) * 1000
                resp_ct = upstream.headers.get("content-type", "")

                # Send http_res on channel 0x03
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
    inspect_p.set_defaults(func=_cmd_inspect)
