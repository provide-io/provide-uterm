#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``uterm watch`` — TUI HTTP traffic viewer for tunnel sessions."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse

from provide.terminal.defaults import TerminalDefaults

log = logging.getLogger(__name__)

_URL_TUNNEL_RE = re.compile(r"/(?:app/(?:inspect|session|operator)/|s/)([a-zA-Z0-9_-]+)")


def extract_tunnel_id(value: str) -> str:
    """Extract tunnel ID from a bare ID or URL."""
    if "://" in value:
        m = _URL_TUNNEL_RE.search(value.split("?")[0])
        if m:
            return m.group(1)
    return value


def _read_token(args: argparse.Namespace) -> str | None:
    if getattr(args, "token", None):
        return str(args.token)
    token_path = Path(getattr(args, "token_file", "") or str(TerminalDefaults.token_file())).expanduser()
    if token_path.is_file():
        return token_path.read_text().strip()
    return None


def _cmd_watch(args: argparse.Namespace) -> None:  # pragma: no cover — TUI entry point
    """Launch the Textual TUI traffic viewer."""
    try:
        from textual.app import App  # noqa: F401
    except ImportError:
        import sys

        print("error: missing dependency — textual\npip install 'provide-terminal[cli]'", file=sys.stderr)
        sys.exit(1)

    tunnel_id = extract_tunnel_id(args.tunnel)
    server = getattr(args, "server", None) or ""
    layout = getattr(args, "layout", "horizontal")
    token = _read_token(args)

    if not server and "://" in args.tunnel:
        from urllib.parse import urlparse

        parsed = urlparse(args.tunnel)
        server = f"{parsed.scheme}://{parsed.netloc}"

    if not server:
        import sys

        print("error: --server is required when passing a bare tunnel ID", file=sys.stderr)
        sys.exit(1)

    ws_base = server.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_base}/ws/browser/{tunnel_id}/term"
    if token:
        ws_url += f"?token={token}"

    from provide.terminal.cli._watch_app import WatchApp

    app = WatchApp(ws_url=ws_url, tunnel_id=tunnel_id, initial_layout=layout)
    app.run()


def add_watch_subcommand(subparsers: Any) -> None:
    """Register the ``watch`` subcommand."""
    watch_p = subparsers.add_parser(
        "watch",
        help="TUI HTTP traffic viewer for tunnel sessions",
        description="Connect to an existing tunnel and watch HTTP traffic in a terminal UI.",
    )
    watch_p.add_argument("tunnel", metavar="TUNNEL", help="tunnel ID or URL")
    watch_p.add_argument("--server", "-s", metavar="URL", default=None, help="tunnel server URL")
    watch_p.add_argument(
        "--layout", choices=["horizontal", "vertical", "modal"], default="horizontal", help="initial layout mode"
    )
    watch_p.add_argument("--token", metavar="TOKEN", default=None, help="bearer token for auth")
    watch_p.add_argument(
        "--token-file", metavar="FILE", default=str(TerminalDefaults.token_file()), help="path to token file"
    )
    watch_p.set_defaults(func=_cmd_watch)
