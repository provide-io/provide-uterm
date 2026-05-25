#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""CLI entry point for the hosted terminal server.

The canonical invocation is the unified ``uterm server`` subcommand,
registered on the main ``uterm`` parser by :func:`add_server_subcommand`.
A direct ``python -m provide.uterm.server.cli`` invocation routes through
:func:`main`, which builds the same argparse surface and calls the same
handler.
"""

from __future__ import annotations

import argparse

from provide.uterm.server import load_server_config
from provide.uterm.server.app import create_server_app


def _add_server_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the server's flags to ``parser`` (works on both top-level and subparser)."""
    parser.add_argument("--config", type=str, default=None, help="Path to a TOML config file")
    parser.add_argument("--host", type=str, default=None, help="Override the bind host")
    parser.add_argument("--port", type=int, default=None, help="Override the bind port")


def _cmd_server(args: argparse.Namespace) -> None:
    """Run the reference hosted terminal server with the parsed args."""
    # Defer the uvicorn import to the moment it is actually needed so the
    # whole CLI module can be imported even when uvicorn isn't installed
    # (e.g. the ``uterm proxy`` flow has its own "missing dependency"
    # error handler which expects ``import uvicorn`` to fail there, not
    # at module-load time of this sibling module).
    import uvicorn

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


def add_server_subcommand(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``server`` as a subcommand on the unified ``uterm`` CLI."""
    server_p = sub.add_parser(
        "server",
        help="run the reference hosted terminal server",
        description="Run the provide-uterm reference server (FastAPI + TermHub).",
    )
    _add_server_arguments(server_p)
    server_p.set_defaults(func=_cmd_server)


def main(argv: list[str] | None = None) -> None:
    """Direct module entry point (``python -m provide.uterm.server.cli``)."""
    parser = argparse.ArgumentParser(
        prog="python -m provide.uterm.server.cli",
        description="Run the provide-uterm reference server. Prefer `uterm server`.",
    )
    _add_server_arguments(parser)
    args = parser.parse_args(argv)
    _cmd_server(args)


if __name__ == "__main__":  # pragma: no cover — exercised by `uterm-server` console-script
    main()
