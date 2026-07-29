#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for `uterm watch`'s tunnel argument.

Somebody watching a tunnel has a link in their clipboard, not an identifier —
so the command takes either. Pulling the wrong thing out of a URL means
watching somebody else's tunnel, or nothing at all, so which part of an
address is the identifier is worth pinning exactly.

A bare identifier is taken as it stands: only something that looks like an
address is searched, and only the part before any query, since a tunnel named
in a query parameter is not the tunnel the path names.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_watch_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.cli.watch import extract_tunnel_id

OUT = Path(__file__).resolve().parent / "watch_golden.json"

VALUES: list[tuple[str, str]] = [
    ("a bare identifier", "t-abc123"),
    ("an identifier with underscores", "t_abc_123"),
    ("nothing at all", ""),
    ("an inspect link", "https://warp.example/app/inspect/t-abc123"),
    ("a session link", "https://warp.example/app/session/t-abc123"),
    ("an operator link", "https://warp.example/app/operator/t-abc123"),
    ("a short share link", "https://warp.example/s/t-abc123"),
    ("a link over cleartext", "http://warp.example/s/t-abc123"),
    ("a websocket link", "wss://warp.example/s/t-abc123"),
    ("a link with a query", "https://warp.example/s/t-abc123?x=1"),
    ("a link whose query names another tunnel", "https://warp.example/s/t-abc123?id=t-other"),
    ("a link with a fragment", "https://warp.example/s/t-abc123#top"),
    ("a link with something after the identifier", "https://warp.example/s/t-abc123/more"),
    ("a link with a port", "https://warp.example:8443/s/t-abc123"),
    ("a link with a path in front", "https://warp.example/tunnels/s/t-abc123"),
    ("a link naming no tunnel", "https://warp.example/"),
    ("a link to a route nobody serves", "https://warp.example/app/other/t-abc123"),
    ("a link with an empty identifier", "https://warp.example/s/"),
    ("a link with an identifier of one character", "https://warp.example/s/a"),
    ("a link with two identifiers", "https://warp.example/s/first/s/second"),
    ("a link with a dotted identifier", "https://warp.example/s/t.abc"),
    ("something that merely contains a scheme marker", "not-a-url://but-has-one"),
    ("a bare path", "/s/t-abc123"),
    ("an identifier that looks like a path", "s/t-abc123"),
]


def main() -> None:
    corpus = {
        "extracted": [{"name": name, "value": value, "id": extract_tunnel_id(value)} for name, value in VALUES],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(VALUES)} values)")


if __name__ == "__main__":
    main()
