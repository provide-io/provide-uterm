#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""CLI entry point for the standalone hosted terminal server.

Two surfaces share one handler:

- ``uterm server [--config ...]`` — the canonical, unified path mounted as a
  subcommand on the ``uterm`` parser (see :mod:`provide.uterm.cli`).
- ``uterm-server [--config ...]`` — kept as a console_script alias for
  backward compatibility with existing deployments, scripts, and docs.
"""

from __future__ import annotations

import argparse

import uvicorn

from provide.uterm.server import load_server_config
from provide.uterm.server.app import create_server_app


def _add_server_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the server's flags to ``parser`` (works on both top-level and subparser)."""
    parser.add_argument("--config", type=str, default=None, help="Path to a TOML config file")
    parser.add_argument("--host", type=str, default=None, help="Override the bind host")
    parser.add_argument("--port", type=int, default=None, help="Override the bind port")


def _cmd_server(args: argparse.Namespace) -> None:
    """Run the reference hosted terminal server with the parsed args."""
    config = load_server_config(args.config)
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = int(args.port)
    if args.host or args.port:
        scheme = "https" if config.server.public_base_url.startswith("https://") else "http"
        config.server.public_base_url = f"{scheme}://{config.server.host}:{config.server.port}"

    app = create_server_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


def add_server_subcommand(sub: argparse._SubParsersAction) -> None:
    """Register ``server`` as a subcommand on the unified ``uterm`` CLI.

    Called by :mod:`provide.uterm.cli` so that ``uterm server`` is the
    canonical invocation. The standalone ``uterm-server`` binary
    (:func:`main` below) is kept as an alias for backward compatibility.
    """
    server_p = sub.add_parser(
        "server",
        help="run the reference hosted terminal server",
        description="Run the provide-uterm reference server (FastAPI + TermHub).",
    )
    _add_server_arguments(server_p)
    server_p.set_defaults(func=_cmd_server)


def main(argv: list[str] | None = None) -> None:
    """Standalone entry point for the legacy ``uterm-server`` console script."""
    parser = argparse.ArgumentParser(prog="uterm-server", description="Run the provide-uterm reference server")
    _add_server_arguments(parser)
    args = parser.parse_args(argv)
    _cmd_server(args)
