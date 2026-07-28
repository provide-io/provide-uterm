#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript session adapters.

The telnet and websocket sessions are thin over the shared transport session,
and what they actually contribute is *encodings*. Those are not cosmetic: a
BBS expects CP437 on the wire, so sending an accented character as UTF-8
puts two bytes where the server wanted one, and the screen desynchronises
from that point on.

The defaults differ per transport and are recorded as such — telnet encodes
both directions as CP437, while the websocket session decodes terminal bytes
as CP437 but treats a text frame as latin-1, because a text frame already
carries characters rather than bytes.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_session_adapters_golden.py
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from provide.uterm.ws_session import WebSocketSession, connect_ws

from provide.uterm.telnet_session import TelnetSession, connect_telnet

OUT = Path(__file__).with_name("session_adapters_golden.json")

# Text a BBS-facing client actually sends and receives.
ENCODE_CASES: list[tuple[str, str]] = [
    ("ascii", "hello"),
    ("carriage return", "ls\r"),
    ("box drawing", "╔═╗"),
    ("shaded blocks", "░▒▓█"),
    ("accented latin", "café"),
    ("greek in the high range", "αβΓ"),
    ("currency", "£¥₧"),
    ("arrows", "↑↓→←"),
    ("empty", ""),
    # RUF001: the no-break space is deliberate — CP437 maps it to 0xFF.
    ("no-break space", "⌂ "),  # noqa: RUF001
]

# Bytes arriving from a BBS.
DECODE_CASES: list[tuple[str, list[int]]] = [
    ("ascii", [104, 105]),
    ("box drawing", [201, 205, 187]),
    ("shaded blocks", [176, 177, 178, 219]),
    ("accented", [130, 129]),
    ("the whole high half", list(range(128, 160))),
    ("nulls and controls", [0, 7, 27, 91, 51, 49, 109]),
    ("empty", []),
]


def _defaults(fn: Any) -> dict[str, Any]:
    """Every keyword default a factory declares."""
    signature = inspect.signature(fn)
    return {
        name: parameter.default
        for name, parameter in signature.parameters.items()
        if parameter.default is not inspect.Parameter.empty
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_session_adapters_golden.py",
        "telnet_defaults": _defaults(connect_telnet),
        "ws_defaults": _defaults(connect_ws),
        "telnet_session_defaults": _defaults(TelnetSession.__init__),
        "ws_session_defaults": _defaults(WebSocketSession.__init__),
        "encodes": [
            {"name": name, "text": text, "bytes": list(text.encode("cp437", errors="replace"))}
            for name, text in ENCODE_CASES
        ],
        "decodes": [
            {"name": name, "bytes": data, "text": bytes(data).decode("cp437", errors="replace")}
            for name, data in DECODE_CASES
        ],
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['encodes'])} encode cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
