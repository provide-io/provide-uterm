#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the MCP tools' own checks.

Two pieces of the tool surface, both driven.

``session_create`` is how a model starts a process, so its arguments are
checked before anything is spawned:

* the connector must be on the allowlist;
* a port must be a real TCP port;
* a URL must use a scheme somebody vetted — ``file://`` and ``javascript:``
  are refused so a model cannot ask a worker to open whatever it likes;
* and neither the URL's host nor an explicit one may be internal, which is
  what stops a model pivoting into the network the worker sits in. The host
  inside a URL is checked as well as the one beside it, because a model that
  cannot pass ``host="127.0.0.1"`` can otherwise pass
  ``url="ws://127.0.0.1/"``.

The other piece is what a model is *shown*. A snapshot comes back in one of
three shapes, and two of them strip ANSI — a model reading escape sequences
is a model reading noise, and one being *fed* them is a prompt-injection
surface.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_mcptools_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.ai import server_validators

OUT = Path(__file__).resolve().parent / "mcptools_golden.json"

# connector_type, url, port, host
CONFIGS: list[tuple[str, str, Any, Any, Any]] = [
    ("a shell", "shell", None, None, None),
    ("a connector nobody allows", "carrier-pigeon", None, None, None),
    ("a connector in capitals", "SHELL", None, None, None),
    ("no connector at all", "", None, None, None),
    ("a port in range", "ssh", None, 22, None),
    ("the lowest port", "ssh", None, 1, None),
    ("the highest port", "ssh", None, 65535, None),
    ("port zero", "ssh", None, 0, None),
    ("a port past the end", "ssh", None, 65536, None),
    ("a negative port", "ssh", None, -1, None),
    ("no port at all", "ssh", None, None, None),
    ("a websocket url", "websocket", "wss://feed.example/s", None, None),
    ("a scheme in capitals", "websocket", "WSS://feed.example/s", None, None),
    ("a scheme in mixed case", "websocket", "WsS://feed.example/s", None, None),
    ("a cleartext websocket url", "websocket", "ws://feed.example/s", None, None),
    ("an http url", "websocket", "http://feed.example/s", None, None),
    ("a telnet url", "telnet", "telnet://bbs.example", None, None),
    ("an ssh url", "ssh", "ssh://shell.example", None, None),
    ("a file url", "websocket", "file:///etc/passwd", None, None),
    ("a javascript url", "websocket", "javascript:alert(1)", None, None),
    ("a url with no scheme", "websocket", "feed.example/s", None, None),
    ("a url that is only a scheme", "websocket", "gopher://", None, None),
    ("a url pointing at loopback", "websocket", "ws://127.0.0.1/s", None, None),
    ("a url pointing at loopback by name", "websocket", "ws://localhost/s", None, None),
    ("a url pointing at cloud metadata", "websocket", "ws://169.254.169.254/", None, None),
    ("a url pointing at loopback in decimal", "websocket", "ws://2130706433/", None, None),
    ("a url pointing at a private address", "websocket", "ws://10.0.0.5/s", None, None),
    ("a url pointing somewhere public", "websocket", "wss://feed.example/s", None, None),
    ("a host beside the url", "ssh", None, None, "shell.example"),
    ("a loopback host", "ssh", None, None, "127.0.0.1"),
    ("a metadata host", "ssh", None, None, "169.254.169.254"),
    ("a private host", "ssh", None, None, "10.0.0.5"),
    ("a host in decimal", "ssh", None, None, "2130706433"),
    ("everything at once, all fine", "ssh", "ssh://shell.example", 22, "shell.example"),
    ("a bad connector beats a bad port", "carrier-pigeon", None, 0, None),
    ("a bad port beats a bad url", "ssh", "file:///etc/passwd", 0, None),
    ("a bad url beats a bad host", "ssh", "file:///x", None, "127.0.0.1"),
]

SCREEN = "\x1b[1;35mhello\x1b[0m\nsecond\nthird\nfourth"

SNAPSHOTS: list[tuple[str, dict[str, Any], str, Any]] = [
    ("raw, untouched", {"screen": SCREEN, "cursor": {"x": 1, "y": 2}, "cols": 80, "rows": 25}, "raw", None),
    ("raw, tail trimmed", {"screen": SCREEN, "cursor": {"x": 1, "y": 2}, "cols": 80, "rows": 25}, "raw", 2),
    ("text only", {"screen": SCREEN, "cursor": {"x": 1, "y": 2}, "cols": 80, "rows": 25}, "text", None),
    ("text, tail trimmed", {"screen": SCREEN, "cursor": {"x": 1, "y": 2}, "cols": 80, "rows": 25}, "text", 2),
    ("rendered", {"screen": SCREEN, "cursor": {"x": 1, "y": 2}, "cols": 80, "rows": 25}, "rendered", None),
    ("rendered with no layout to report", {"screen": SCREEN}, "rendered", None),
    ("rendered, tail trimmed", {"screen": SCREEN, "cols": 80}, "rendered", 1),
    ("a mode nobody defined", {"screen": SCREEN, "cols": 80}, "elsewhere", None),
    ("no screen at all", {"cols": 80}, "text", None),
    ("a tail longer than the screen", {"screen": SCREEN}, "text", 99),
    ("a tail of zero", {"screen": SCREEN}, "text", 0),
    ("a negative tail", {"screen": SCREEN}, "text", -1),
    ("no tail at all", {"screen": SCREEN}, "text", None),
    ("a screen that is only escapes", {"screen": "\x1b[2J\x1b[H"}, "text", None),
    ("a screen with a trailing newline", {"screen": "one\ntwo\n"}, "text", 1),
]


def main() -> None:
    corpus = {
        "screen": SCREEN,
        "configs": [
            {
                "name": name,
                "connector_type": connector,
                "url": url,
                "port": port,
                "host": host,
                "rejection": server_validators._validate_session_create_config(
                    connector_type=connector, url=url, port=port, host=host
                ),
            }
            for name, connector, url, port, host in CONFIGS
        ],
        "snapshots": [
            {
                "name": name,
                "snapshot": snapshot,
                "output": output,
                "tail_lines": tail,
                "cleaned": server_validators._clean_snapshot(snapshot, output, tail_lines=tail),
            }
            for name, snapshot, output, tail in SNAPSHOTS
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['configs'])} configs)")


if __name__ == "__main__":
    main()
