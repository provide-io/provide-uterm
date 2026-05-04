#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``uterm tunnel`` — forward a local TCP port through a tunnel server.

Connects to a remote tunnel server, then relays raw TCP bytes between
a local port and remote viewers over a multiplexed binary WebSocket channel.

Example::

    uterm tunnel 8080 --server https://warp.provide.io
    uterm tunnel 3000 --server https://warp.provide.io --display-name "dev server"
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
from provide.terminal.tunnel.protocol import (
    CHANNEL_TCP,
    FLAG_EOF,
    decode_frame,
    encode_control,
    encode_frame,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helpers (reuse pattern from share.py)
# ---------------------------------------------------------------------------


def _read_token(args: argparse.Namespace) -> str | None:
    """Resolve bearer token from --token or --token-file."""
    if getattr(args, "token", None):
        return args.token  # type: ignore[no-any-return]
    token_path = Path(getattr(args, "token_file", "") or str(TerminalDefaults.token_file())).expanduser()
    if token_path.is_file():
        return token_path.read_text().strip()
    return None


def _create_tunnel(server: str, display_name: str, token: str | None, local_port: int) -> dict[str, Any]:
    """POST /api/tunnels to create a TCP tunnel session."""
    url = f"{server.rstrip('/')}/api/tunnels"
    body = json.dumps(
        {
            "tunnel_type": "tcp",
            "display_name": display_name,
            "local_port": local_port,
        }
    ).encode()
    headers: dict[str, str] = {"Content-Type": "application/json", "User-Agent": "uterm-tunnel/1.0"}
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
# TCP relay
# ---------------------------------------------------------------------------


async def _relay_tcp_to_ws(reader: asyncio.StreamReader, ws_send: Any) -> None:
    """Read from TCP socket, send as tunnel frames on channel 2."""
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                await ws_send(encode_frame(CHANNEL_TCP, b"", flags=FLAG_EOF))
                break
            await ws_send(encode_frame(CHANNEL_TCP, data))
    except (ConnectionError, OSError):
        pass


async def _relay_ws_to_tcp(ws_recv: Any, writer: asyncio.StreamWriter) -> None:
    """Read tunnel frames from WS, write TCP data to local connection."""
    try:
        while True:
            frame = await ws_recv()
            if frame.channel != CHANNEL_TCP:
                continue
            if frame.is_eof:
                break
            writer.write(frame.payload)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with suppress(Exception):
            writer.close()


async def _handle_tcp_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ws_send: Any,
    ws_recv: Any,
) -> None:
    """Bridge a single TCP connection through the tunnel."""
    try:
        await asyncio.gather(
            _relay_tcp_to_ws(reader, ws_send),
            _relay_ws_to_tcp(ws_recv, writer),
        )
    finally:
        with suppress(Exception):
            writer.close()


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


def _cmd_tunnel(args: argparse.Namespace) -> None:
    """Execute the ``uterm tunnel`` subcommand."""
    server: str = args.server
    local_port: int = args.port
    display_name: str = getattr(args, "display_name", None) or f"tcp:{local_port}"
    token = _read_token(args)

    tunnel_info = _create_tunnel(server, display_name, token, local_port)
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

    print(f"Tunneling localhost:{local_port}...")
    if share_url:
        print(f"  Share: {share_url}")
    print("\nConnected. Press Ctrl+C to stop.")

    with suppress(KeyboardInterrupt):
        asyncio.run(_run_tunnel(ws_endpoint, worker_token, local_port))


async def _run_tunnel(
    ws_endpoint: str, worker_token: str, local_port: int
) -> None:  # pragma: no cover — integration; tested via E2E
    """Connect to tunnel WS and start accepting local TCP connections."""
    try:
        import websockets
    except ImportError:
        print(
            "error: missing dependency — websockets\ninstall the cli extra: pip install 'provide-uterm[cli]'",
            file=sys.stderr,
        )
        sys.exit(1)

    headers = {}
    if worker_token:
        headers["Authorization"] = f"Bearer {worker_token}"

    async with websockets.connect(ws_endpoint, additional_headers=headers) as ws:
        # Open TCP tunnel channel.
        await ws.send(
            encode_control(
                {
                    "type": "open",
                    "channel": CHANNEL_TCP,
                    "tunnel_type": "tcp",
                    "local_port": local_port,
                }
            )
        )

        async def ws_send(data: bytes) -> None:
            await ws.send(data)

        async def ws_recv() -> Any:
            raw = await ws.recv()
            if isinstance(raw, str):
                raw = raw.encode("latin-1")
            return decode_frame(raw)

        # Start local TCP server that relays through the tunnel.
        async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            log.info("tcp_client_connected local_port=%d", local_port)
            await _handle_tcp_client(reader, writer, ws_send, ws_recv)

        srv = await asyncio.start_server(on_client, "127.0.0.1", local_port)
        log.info("tcp_listener_started port=%d", local_port)
        async with srv:
            await srv.serve_forever()


# ---------------------------------------------------------------------------
# Subcommand registration
# ---------------------------------------------------------------------------


def add_tunnel_subcommand(subparsers: Any) -> None:
    """Register the ``tunnel`` subcommand."""
    tunnel_p = subparsers.add_parser(
        "tunnel",
        help="forward a local TCP port via tunnel server",
        description="Forward a local TCP port through a remote tunnel server.",
    )
    tunnel_p.add_argument(
        "port",
        type=int,
        metavar="PORT",
        help="local TCP port to tunnel",
    )
    tunnel_p.add_argument(
        "--server",
        "-s",
        required=True,
        metavar="URL",
        help="CF worker / tunnel server URL",
    )
    tunnel_p.add_argument(
        "--token",
        metavar="TOKEN",
        default=None,
        help="bearer token for API auth",
    )
    tunnel_p.add_argument(
        "--token-file",
        metavar="FILE",
        default=str(TerminalDefaults.token_file()),
        help="path to token file",
    )
    tunnel_p.add_argument(
        "--display-name",
        metavar="NAME",
        default=None,
        help="override display name (default: tcp:<port>)",
    )
    tunnel_p.set_defaults(func=_cmd_tunnel)
