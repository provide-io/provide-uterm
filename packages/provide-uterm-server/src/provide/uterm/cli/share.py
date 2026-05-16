#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``uterm share`` — share a terminal session via a tunnel server.

Spawns a PTY (or attaches to the current TTY) and bridges I/O to a
remote tunnel WebSocket endpoint, providing a shareable URL.

Example::

    uterm share --server https://warp.provide.io
    uterm share --server https://warp.provide.io -- htop
    uterm share --server https://warp.provide.io --attach
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import platform
import sys
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse

from provide.uterm.defaults import TerminalDefaults
from provide.uterm.tunnel.protocol import (
    CHANNEL_DATA,
    FLAG_EOF,
    encode_frame,
)
from provide.uterm.tunnel.pty_capture import SpawnedPty, TtyProxy, spawn_pty

log = logging.getLogger(__name__)


def _default_token_file_hint() -> str:
    """Return the user-facing default token file path with ``~`` when possible."""
    home = str(Path.home())
    actual = str(TerminalDefaults.token_file())
    return actual.replace(home, "~", 1) if actual.startswith(home) else actual


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _read_token(args: argparse.Namespace) -> str | None:
    """Resolve bearer token from --token, --token-file, or default file."""
    if getattr(args, "token", None):
        return args.token  # type: ignore[no-any-return]

    token_path_str: str = getattr(args, "token_file", None) or str(TerminalDefaults.token_file())
    token_path = Path(token_path_str).expanduser()
    if token_path.is_file():
        return token_path.read_text().strip()

    return None


def _create_tunnel(server: str, display_name: str, token: str | None) -> dict[str, Any]:
    """POST /api/tunnels to create a new tunnel session.

    Returns:
        Parsed JSON response with tunnel_id, share_url, etc.

    Raises:
        SystemExit: On HTTP errors or connection failures.
    """
    url = f"{server.rstrip('/')}/api/tunnels"
    body = json.dumps({"tunnel_type": "terminal", "display_name": display_name}).encode()

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "uterm-share/1.0",
    }
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


def _display_name(args: argparse.Namespace) -> str:
    """Build the display name from --display-name or auto-detect."""
    if getattr(args, "display_name", None):
        return args.display_name  # type: ignore[no-any-return]
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    hostname = platform.node() or "localhost"
    return f"{user}@{hostname}"


# ---------------------------------------------------------------------------
# Bridge loop
# ---------------------------------------------------------------------------


async def _bridge_loop(
    pty_source: Any,
    ws_send: Any,
    ws_recv: Any,
    *,
    is_attach: bool = False,
) -> None:
    """Bridge PTY ↔ WebSocket until one side closes.

    Args:
        pty_source: Object with async ``read()`` and ``write()``/``write_local()`` methods.
        ws_send: Coroutine to send bytes to the WebSocket.
        ws_recv: Coroutine to receive bytes from the WebSocket.
        is_attach: If True, also write received data to local stdout via ``write_local``.
    """

    async def pty_to_ws() -> None:
        try:
            while True:
                data = await pty_source.read(4096)
                if not data:
                    break
                frame = encode_frame(CHANNEL_DATA, data)
                await ws_send(frame)
        except (OSError, EOFError):
            pass

    async def ws_to_pty() -> None:
        try:
            while True:
                data = await ws_recv()
                if not data:
                    break
                if is_attach:
                    await pty_source.write_local(data)
                else:
                    await pty_source.write(data)
        except (OSError, EOFError):
            pass

    await asyncio.gather(pty_to_ws(), ws_to_pty())


# ---------------------------------------------------------------------------
# Main command handler
# ---------------------------------------------------------------------------


def _cmd_share(args: argparse.Namespace) -> None:
    """Execute the ``uterm share`` subcommand."""
    server: str = args.server
    cmd: list[str] | None = args.cmd or None
    attach: bool = getattr(args, "attach", False)

    display = _display_name(args)
    token = _read_token(args)

    # 1. Create tunnel on the server
    tunnel_info = _create_tunnel(server, display, token)

    share_url = tunnel_info.get("share_url", "")
    control_url = tunnel_info.get("control_url", "")
    ws_endpoint = tunnel_info.get("ws_endpoint", "")
    worker_token = tunnel_info.get("worker_token", "")

    if not ws_endpoint:
        print("error: server response missing ws_endpoint", file=sys.stderr)
        sys.exit(1)

    # ws_endpoint may be a full URL (ws://...) or a relative path (/tunnel/...).
    if ws_endpoint.startswith("/"):
        ws_base = server.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        ws_endpoint = f"{ws_base}{ws_endpoint}"

    # 2. Print URLs
    print("Sharing terminal session...")
    print(f"  View:    {share_url}")
    print(f"  Control: {control_url}")
    print()
    print("Connected. Press Ctrl+C to stop sharing.")

    # 3. Spawn PTY or attach to TTY
    if attach:
        pty_source: SpawnedPty | TtyProxy = TtyProxy()
        pty_source.start()  # type: ignore[union-attr]
    else:
        pty_source = spawn_pty(cmd)

    # 4. Run the bridge
    try:
        asyncio.run(
            _run_share(pty_source, ws_endpoint, worker_token, attach=attach),
        )
    except KeyboardInterrupt:
        pass
    finally:
        pty_source.close()


async def _run_share(  # pragma: no cover — integration; tested via E2E
    pty_source: Any,
    ws_endpoint: str,
    worker_token: str,
    *,
    attach: bool = False,
) -> None:
    """Connect to the tunnel WebSocket and run the bridge loop.

    Args:
        pty_source: SpawnedPty or TtyProxy instance.
        ws_endpoint: WebSocket URL to connect to.
        worker_token: Bearer token for the worker connection.
        attach: Whether in attach mode (mirror to local stdout).
    """
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

        async def ws_send(data: bytes) -> None:
            await ws.send(data)

        async def ws_recv() -> bytes:
            msg = await ws.recv()
            if isinstance(msg, str):
                return msg.encode()
            return msg

        try:
            await _bridge_loop(pty_source, ws_send, ws_recv, is_attach=attach)
        except KeyboardInterrupt:
            # Send EOF frame and close gracefully
            eof = encode_frame(CHANNEL_DATA, b"", flags=FLAG_EOF)
            with suppress(Exception):
                await ws.send(eof)


# ---------------------------------------------------------------------------
# Subcommand registration
# ---------------------------------------------------------------------------


def add_share_subcommand(subparsers: Any) -> None:
    """Register the ``share`` subcommand on the given subparsers action."""
    share_p = subparsers.add_parser(
        "share",
        help="share a terminal session via tunnel server",
        description="Spawn a PTY (or attach to current TTY) and share via a remote tunnel.",
    )
    share_p.add_argument(
        "cmd",
        nargs="*",
        default=None,
        metavar="CMD",
        help="command to run (default: $SHELL)",
    )
    share_p.add_argument(
        "--server",
        "-s",
        required=True,
        metavar="URL",
        help="CF worker / tunnel server URL",
    )
    share_p.add_argument(
        "--token",
        metavar="TOKEN",
        default=None,
        help="bearer token for API auth",
    )
    share_p.add_argument(
        "--token-file",
        metavar="FILE",
        default=str(TerminalDefaults.token_file()),
        help=f"path to token file (default: {_default_token_file_hint()})",
    )
    share_p.add_argument(
        "--attach",
        action="store_true",
        default=False,
        help="attach to current TTY instead of spawning a new PTY",
    )
    share_p.add_argument(
        "--display-name",
        metavar="NAME",
        default=None,
        help="override display name (default: user@hostname)",
    )
    share_p.set_defaults(func=_cmd_share)
