#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""CLI entry point for the uterm-mcp server.

Usage::

    uterm-mcp --url http://localhost:8780
    uterm-mcp --url http://localhost:8780 --entity-prefix /agent
    uterm-mcp --url http://localhost:8780 --header Authorization:"Bearer tok"
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uterm-mcp",
        description="MCP server for provide-uterm session and hijack control.",
    )
    parser.add_argument("--url", required=True, help="Base URL of the provide-uterm server.")
    parser.add_argument(
        "--entity-prefix",
        default="/worker",
        help="Path prefix for worker endpoints (default: /worker).",
    )
    parser.add_argument(
        "--header",
        dest="headers",
        action="append",
        default=[],
        help="Extra header as key:value (repeatable).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse args and run the MCP server on stdio."""
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    headers: dict[str, str] = {}
    for h in args.headers:
        key, _, value = h.partition(":")
        headers[key.strip()] = value.strip()

    from provide.terminal.ai.server import create_mcp_app

    app = create_mcp_app(
        args.url,
        entity_prefix=args.entity_prefix,
        headers=headers if headers else None,
    )
    app.run(transport="stdio")
