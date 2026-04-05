#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""CLI entry point for the standalone hosted terminal server."""

from __future__ import annotations

import argparse

import uvicorn

from provide.terminal.server import load_server_config
from provide.terminal.server.app import create_server_app


def main(argv: list[str] | None = None) -> None:
    """Run the reference hosted terminal server."""
    parser = argparse.ArgumentParser(prog="uterm-server", description="Run the provide-terminal reference server")
    parser.add_argument("--config", type=str, default=None, help="Path to a TOML config file")
    parser.add_argument("--host", type=str, default=None, help="Override the bind host")
    parser.add_argument("--port", type=int, default=None, help="Override the bind port")
    args = parser.parse_args(argv)

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
