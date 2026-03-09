#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
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

Requires the ``[cli]`` extra::

    pip install 'provide-terminal[cli]'

SSH support additionally requires the ``[ssh]`` extra::

    pip install 'provide-terminal[cli,ssh]'
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.terminal.transports.base import ConnectionTransport

# ---------------------------------------------------------------------------
# Subcommand: proxy  (WS server → outbound telnet/SSH)
# ---------------------------------------------------------------------------


def _cmd_proxy(args: argparse.Namespace) -> None:
    """Start the WsTerminalProxy server."""
    try:
        import uvicorn
        from fastapi import FastAPI

        from provide.terminal.fastapi import WsTerminalProxy
    except ImportError as exc:
        print(
            f"error: missing dependency — {exc}\ninstall the cli extra: pip install 'provide-terminal[cli]'",
            file=sys.stderr,
        )
        sys.exit(1)

    transport_factory: Callable[[], ConnectionTransport] | None = None
    if args.transport == "ssh":
        try:
            import importlib

            _ssh_mod = importlib.import_module("provide.terminal.transports.ssh")
            ssh_transport_cls = getattr(_ssh_mod, "SSHTransport", None)
            if ssh_transport_cls is None:
                raise AttributeError("SSHTransport")
            transport_factory = cast("Callable[[], ConnectionTransport]", ssh_transport_cls)
        except (ImportError, AttributeError):
            print(
                "error: SSH transport requires asyncssh: pip install 'provide-terminal[ssh]'",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        from provide.terminal.transports.telnet import TelnetTransport

        transport_factory = cast("Callable[[], ConnectionTransport]", TelnetTransport)

    proxy = WsTerminalProxy(
        args.host,
        args.bbs_port,
        transport_factory=transport_factory,
    )

    app = FastAPI(title="uterm proxy", docs_url=None, redoc_url=None)
    app.include_router(proxy.create_router(args.path))

    print(f"uterm proxy  {args.transport}://{args.host}:{args.bbs_port}  →  ws://{args.bind}:{args.port}{args.path}")

    uvicorn.run(app, host=args.bind, port=args.port, log_level="warning")


# ---------------------------------------------------------------------------
# Subcommand: listen  (TCP/SSH server → outbound WebSocket)
# ---------------------------------------------------------------------------


def _cmd_listen(args: argparse.Namespace) -> None:
    """Start the TelnetWsGateway and/or SshWsGateway."""
    try:
        from provide.terminal.gateway import SshWsGateway, TelnetWsGateway
    except ImportError as exc:  # pragma: no cover
        print(
            f"error: missing dependency — {exc}\ninstall the cli extra: pip install 'provide-terminal[cli]'",
            file=sys.stderr,
        )
        sys.exit(1)

    telnet_port: int = args.port
    ssh_port: int = args.ssh_port

    if telnet_port == 0 and ssh_port == 0:
        print("error: at least one of --port or --ssh-port must be non-zero", file=sys.stderr)
        sys.exit(1)

    asyncio.run(  # pragma: no cover
        _run_listen(args.ws_url, args.bind, telnet_port, ssh_port, args.server_key, TelnetWsGateway, SshWsGateway)
    )


async def _run_listen(
    ws_url: str,
    bind: str,
    telnet_port: int,
    ssh_port: int,
    server_key: str | None,
    TelnetWsGateway: type,  # noqa: N803
    SshWsGateway: type,  # noqa: N803
) -> None:
    servers = []

    if telnet_port:
        gw = TelnetWsGateway(ws_url)
        srv = await gw.start(bind, telnet_port)
        servers.append(srv)
        print(f"uterm listen  telnet://{bind}:{telnet_port}  →  {ws_url}")

    if ssh_port:
        try:
            gw_ssh = SshWsGateway(ws_url, server_key=server_key)
            srv_ssh = await gw_ssh.start(bind, ssh_port)
            servers.append(srv_ssh)
            print(f"uterm listen  ssh://{bind}:{ssh_port}     →  {ws_url}")
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
        default=8765,
        help="local HTTP listen port (default: 8765)",
    )
    proxy_p.add_argument(
        "--bind",
        metavar="ADDR",
        default="0.0.0.0",  # nosec B104
        help="bind address (default: 0.0.0.0)",
    )
    proxy_p.add_argument(
        "--path",
        metavar="PATH",
        default="/ws/terminal",
        help="WebSocket endpoint path (default: /ws/terminal)",
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
        default=2112,
        help="telnet TCP listen port (0 to disable, default: 2112)",
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
        default="0.0.0.0",  # nosec B104
        help="bind address (default: 0.0.0.0)",
    )
    listen_p.add_argument(
        "--server-key",
        metavar="FILE",
        default=None,
        help="SSH host private key file (ephemeral key used if omitted)",
    )
    listen_p.set_defaults(func=_cmd_listen)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """CLI entry point — called by the ``uterm`` script."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.func(args)
