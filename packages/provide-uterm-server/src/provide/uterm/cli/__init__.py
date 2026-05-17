#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""uterm — bidirectional WebSocket terminal proxy.

Two complementary subcommands:

``proxy``  (browser WS → telnet/SSH)
    Accepts browser WebSocket connections and proxies to a remote BBS.

        uterm proxy bbs.example.com 23
        uterm proxy bbs.example.com 23 --port 9000 --path /ws/term
        uterm proxy bbs.example.com 22 --transport ssh

``listen``  (telnet/SSH client → WebSocket server)
    Accepts traditional telnet and/or SSH clients and proxies to a
    remote WebSocket terminal endpoint.

        uterm listen wss://warp.provide.io/ws/terminal
        uterm listen wss://warp.provide.io/ws/terminal --port 2112 --ssh-port 2222
        uterm listen wss://warp.provide.io/ws/terminal --server-key /etc/host_key

``share``  (PTY → tunnel WebSocket → shareable URL)
    Shares a terminal session via a remote tunnel server.

        uterm share --server https://warp.provide.io
        uterm share --server https://warp.provide.io -- htop

Requires the ``[cli]`` extra::

    pip install 'provide-uterm[cli]'

SSH support additionally requires the ``[ssh]`` extra::

    pip install 'provide-uterm[cli,ssh]'
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.uterm.transports.base import ConnectionTransport

from provide.uterm.defaults import TerminalDefaults
from provide.uterm.server.models import FITADDON_CDN_DEFAULT, FONTS_CDN_DEFAULT, XTERM_CDN_DEFAULT

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "server" / "frontend"

# ---------------------------------------------------------------------------
# Subcommand: proxy  (WS server → outbound telnet/SSH)
# ---------------------------------------------------------------------------


def _cmd_proxy(args: argparse.Namespace) -> None:
    """Start the WsTerminalProxy server."""
    try:
        import uvicorn
        from fastapi import FastAPI

        from provide.uterm.fastapi_utils import WsTerminalProxy
    except ImportError as exc:
        print(
            f"error: missing dependency — {exc}\ninstall the cli extra: pip install 'provide-uterm[cli]'",
            file=sys.stderr,
        )
        sys.exit(1)

    transport_factory: Callable[[], ConnectionTransport] | None = None
    if args.transport == "ssh":
        try:
            import importlib

            _ssh_mod = importlib.import_module("provide.uterm.transports.ssh")
            ssh_transport_cls = getattr(_ssh_mod, "SSHTransport", None)
            if ssh_transport_cls is None:
                raise AttributeError("SSHTransport")
            transport_factory = cast("Callable[[], ConnectionTransport]", ssh_transport_cls)
        except (ImportError, AttributeError):
            print(
                "error: SSH transport requires asyncssh: pip install 'provide-uterm[ssh]'",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        from provide.uterm.transports.telnet import TelnetTransport

        transport_factory = cast("Callable[[], ConnectionTransport]", TelnetTransport)

    proxy = WsTerminalProxy(
        args.host,
        args.bbs_port,
        transport_factory=transport_factory,
    )

    from fastapi.responses import HTMLResponse
    from starlette.staticfiles import StaticFiles

    app = FastAPI(title="uterm proxy", docs_url=None, redoc_url=None)
    app.include_router(proxy.create_router(args.path))

    title = f"uterm — {args.host}:{args.bbs_port}"

    @app.get("/", response_class=HTMLResponse)
    async def _terminal_page() -> str:
        from html import escape

        safe_title = escape(title)
        ws_path = escape(args.path)
        return (
            '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">'
            f"<title>{safe_title}</title>"
            '<link rel="stylesheet" href="/static/terminal-page.css">'
            f'<link rel="stylesheet" href="{XTERM_CDN_DEFAULT}/css/xterm.css">'
            f'<link href="{FONTS_CDN_DEFAULT}" rel="stylesheet">'
            '<link rel="stylesheet" href="/static/terminal.css">'
            '</head><body><div id="app"></div>'
            f'<script src="{XTERM_CDN_DEFAULT}/lib/xterm.js"></script>'
            f'<script src="{FITADDON_CDN_DEFAULT}/lib/addon-fit.js"></script>'
            '<script src="/static/terminal.js"></script>'
            "<script>"
            "new window.ProvideTerminal(document.getElementById('app'),"
            f"{{wsUrl:'{ws_path}',title:'{safe_title}'}});"
            "</script></body></html>"
        )

    if _FRONTEND_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="frontend")

    print(f"uterm proxy  {args.transport}://{args.host}:{args.bbs_port}  →  ws://{args.bind}:{args.port}{args.path}")
    print(f"  terminal   http://{args.bind}:{args.port}/")

    uvicorn.run(app, host=args.bind, port=args.port, log_level="warning")


# ---------------------------------------------------------------------------
# Subcommand: listen  (TCP/SSH server → outbound WebSocket)
# ---------------------------------------------------------------------------


def _cmd_listen(args: argparse.Namespace) -> None:
    """Start the TelnetWsGateway and/or SshWsGateway."""
    try:
        from provide.uterm.gateway import SshWsGateway, TelnetWsGateway
    except ImportError as exc:  # pragma: no cover
        print(
            f"error: missing dependency — {exc}\ninstall the cli extra: pip install 'provide-uterm[cli]'",
            file=sys.stderr,
        )
        sys.exit(1)

    telnet_port: int = args.port
    ssh_port: int = args.ssh_port

    if telnet_port == 0 and ssh_port == 0:
        print("error: at least one of --port or --ssh-port must be non-zero", file=sys.stderr)
        sys.exit(1)

    if args.require_resolver and not args.authorized_keys:
        print(
            "error: --require-authorized-keys requires --authorized-keys to be set",
            file=sys.stderr,
        )
        sys.exit(1)

    asyncio.run(  # pragma: no cover
        _run_listen(
            args.ws_url,
            args.bind,
            telnet_port,
            ssh_port,
            args.server_key,
            args.color_mode,
            TelnetWsGateway,
            SshWsGateway,
            iac_negotiate=args.iac_negotiate,
            authorized_keys=args.authorized_keys,
            require_resolver=args.require_resolver,
        )
    )


async def _run_listen(
    ws_url: str,
    bind: str,
    telnet_port: int,
    ssh_port: int,
    server_key: str | None,
    color_mode: str,
    TelnetWsGateway: type,  # noqa: N803
    SshWsGateway: type,  # noqa: N803
    *,
    iac_negotiate: bool = True,
    authorized_keys: str | None = None,
    require_resolver: bool = False,
) -> None:
    servers = []

    if telnet_port:
        gw = TelnetWsGateway(ws_url, color_mode=color_mode, iac_negotiate=iac_negotiate)
        srv = await gw.start(bind, telnet_port)
        servers.append(srv)
        print(f"uterm listen  telnet://{bind}:{telnet_port}  →  {ws_url}")

    if ssh_port:
        try:
            key_resolver = None
            if authorized_keys:
                from provide.uterm.auth import AuthorizedKeysFileResolver

                key_resolver = AuthorizedKeysFileResolver(authorized_keys)
            gw_ssh = SshWsGateway(
                ws_url,
                server_key=server_key,
                key_resolver=key_resolver,
                require_resolver=require_resolver,
            )
            srv_ssh = await gw_ssh.start(bind, ssh_port)
            servers.append(srv_ssh)
            suffix = ""
            if authorized_keys:
                mode = "required" if require_resolver else "optional"
                suffix = f"   [pubkey: {authorized_keys} ({mode})]"
            print(f"uterm listen  ssh://{bind}:{ssh_port}     →  {ws_url}{suffix}")
        except ImportError as exc:
            print(f"warning: SSH gateway disabled — {exc}", file=sys.stderr)

    if not servers:
        print("error: no servers started", file=sys.stderr)
        return

    try:
        await asyncio.gather(*(srv.serve_forever() for srv in servers))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        for srv in servers:
            srv.close()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uterm",
        description="Bidirectional WebSocket terminal proxy for BBS/telnet servers.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # ---- proxy subcommand ----
    proxy_p = sub.add_parser(
        "proxy",
        help="browser WS → remote telnet/SSH (start a WS server)",
        description=("Accept browser WebSocket connections and proxy them to a remote telnet/SSH host."),
    )
    proxy_p.add_argument("host", metavar="HOST", help="remote BBS hostname or IP")
    proxy_p.add_argument("bbs_port", metavar="PORT", type=int, help="remote BBS port")
    proxy_p.add_argument(
        "--port",
        "-p",
        metavar="PORT",
        type=int,
        default=TerminalDefaults.PROXY_PORT,
        help=f"local HTTP listen port (default: {TerminalDefaults.PROXY_PORT})",
    )
    proxy_p.add_argument(
        "--bind",
        metavar="ADDR",
        default=TerminalDefaults.BIND_ALL,  # nosec B104
        help=f"bind address (default: {TerminalDefaults.BIND_ALL})",
    )
    proxy_p.add_argument(
        "--path",
        metavar="PATH",
        default=TerminalDefaults.PROXY_WS_PATH,
        help=f"WebSocket endpoint path (default: {TerminalDefaults.PROXY_WS_PATH})",
    )
    proxy_p.add_argument(
        "--transport",
        choices=["telnet", "ssh"],
        default="telnet",
        help="outbound transport protocol (default: telnet)",
    )
    proxy_p.set_defaults(func=_cmd_proxy)

    # ---- listen subcommand ----
    listen_p = sub.add_parser(
        "listen",
        help="telnet/SSH client → remote WS server (start a TCP/SSH listener)",
        description=(
            "Accept traditional telnet and/or SSH clients and proxy them to a remote WebSocket terminal server."
        ),
    )
    listen_p.add_argument("ws_url", metavar="WS_URL", help="upstream WebSocket terminal URL")
    listen_p.add_argument(
        "--port",
        "-p",
        metavar="PORT",
        type=int,
        default=TerminalDefaults.GATEWAY_TELNET_PORT,
        help=f"telnet TCP listen port (0 to disable, default: {TerminalDefaults.GATEWAY_TELNET_PORT})",
    )
    listen_p.add_argument(
        "--ssh-port",
        metavar="PORT",
        type=int,
        default=0,
        help="SSH listen port (0 to disable, default: 0)",
    )
    listen_p.add_argument(
        "--bind",
        metavar="ADDR",
        default=TerminalDefaults.BIND_ALL,  # nosec B104
        help=f"bind address (default: {TerminalDefaults.BIND_ALL})",
    )
    listen_p.add_argument(
        "--server-key",
        metavar="FILE",
        default=None,
        help="SSH host private key file (ephemeral key used if omitted)",
    )
    listen_p.add_argument(
        "--color-mode",
        choices=["passthrough", "256", "16"],
        default="passthrough",
        help="ANSI color downgrade mode (default: passthrough)",
    )
    listen_p.add_argument(
        "--no-iac-negotiate",
        dest="iac_negotiate",
        action="store_false",
        default=True,
        help=(
            "Disable RFC 1091 TTYPE / RFC 1572 NEW-ENVIRON negotiation on the "
            "telnet listener. The gateway otherwise reads the client's TERM "
            "and COLORTERM, derives a colour palette, and forwards it to the "
            "upstream WS as ?colormode=... — rarely needs turning off, but "
            "useful for clients that mishandle IAC DO options."
        ),
    )
    listen_p.add_argument(
        "--authorized-keys",
        metavar="FILE",
        default=None,
        help=(
            "Path to an OpenSSH authorized_keys file. When set, the SSH "
            "listener calls the resolver during pubkey auth; matching keys "
            "inject a ResolvedIdentity forwarded to the upstream as an "
            "``identity`` control frame (first WS message). Lines support "
            '``subject="..."`` + ``claim-<name>="..."`` options to populate '
            "the identity's subject and claims. Unknown keys fall through "
            "to password auth unless --require-authorized-keys is set."
        ),
    )
    listen_p.add_argument(
        "--require-authorized-keys",
        dest="require_resolver",
        action="store_true",
        default=False,
        help=(
            "Reject SSH connections whose pubkey is not in --authorized-keys. "
            "Disables password and keyboard-interactive fallback so unknown "
            "keys can't sneak through. Requires --authorized-keys."
        ),
    )
    listen_p.set_defaults(func=_cmd_listen)

    # ---- share subcommand ----
    from provide.uterm.cli.share import add_share_subcommand

    add_share_subcommand(sub)

    # ---- tunnel subcommand ----
    from provide.uterm.cli.tunnel import add_tunnel_subcommand

    add_tunnel_subcommand(sub)

    # ---- inspect subcommand ----
    from provide.uterm.cli.inspect import add_inspect_subcommand

    add_inspect_subcommand(sub)

    # ---- watch subcommand ----
    from provide.uterm.cli.watch import add_watch_subcommand

    add_watch_subcommand(sub)

    # ---- server subcommand ----
    # The reference hosted server is also reachable via the legacy
    # ``uterm-server`` console script, but ``uterm server`` is canonical.
    from provide.uterm.server.cli import add_server_subcommand

    add_server_subcommand(sub)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """CLI entry point — called by the ``uterm`` script."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.func(args)
