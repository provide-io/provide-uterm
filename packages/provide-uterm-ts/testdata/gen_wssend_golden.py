#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for how a session frames what it sends.

One socket carries two kinds of thing: terminal bytes and control frames. Which
a payload becomes is decided by its ``type`` alone.

**Terminal data goes out as terminal data, not as a control frame carrying
it.** A browser reads the two differently, and a screen update delivered as a
control frame would be rendered as nothing at all.

**Only two types are terminal.** Everything else — a snapshot, a presence
update, an error — is a control frame, so a type nobody recognised is framed
the way the browser can at least parse.

**A missing or unreadable type is a control frame**, which is the safe
direction: a control frame the browser does not understand is ignored, where
terminal bytes it did not expect are printed to the screen.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_wssend_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.control_channel import encode_control_frame, encode_terminal_data

OUT = Path(__file__).with_name("wssend_golden.json")


def _framed(payload: dict[str, Any]) -> str:
    """What ``send_ws`` puts on the wire, lifted from the reference."""
    frame_type = str(payload.get("type") or "")
    if frame_type in {"input", "term"}:
        return encode_terminal_data(str(payload.get("data", "")))
    return encode_control_frame(payload)


# (name, payload) — what a session sends.
CASES: list[tuple[str, dict[str, Any]]] = [
    ("terminal output", {"type": "term", "data": "hello"}),
    ("terminal input", {"type": "input", "data": "ls\r"}),
    ("terminal output with no data", {"type": "term"}),
    ("terminal output with empty data", {"type": "term", "data": ""}),
    ("terminal output carrying escapes", {"type": "term", "data": "\x1b[31mred\x1b[0m"}),
    ("terminal output that is not a string", {"type": "term", "data": 7}),
    ("terminal output that is null", {"type": "term", "data": None}),
    ("terminal output with other fields", {"type": "term", "data": "x", "seq": 1}),
    # Everything else is a control frame.
    ("a snapshot", {"type": "snapshot", "rows": 24}),
    ("an error", {"type": "error", "reason": "protocol_mismatch"}),
    ("a presence update", {"type": "presence_sync", "users": [{"user_id": "u1"}]}),
    ("a type nobody recognised", {"type": "nonsense", "x": 1}),
    # A type that is absent, empty or not a string.
    ("no type at all", {"data": "hello"}),
    ("an empty type", {"type": "", "data": "hello"}),
    ("a type that is null", {"type": None, "data": "hello"}),
    ("a type that is a number", {"type": 7}),
    # A type that merely resembles a terminal one.
    ("a type that starts the same", {"type": "input_send", "data": "x"}),
    ("a type in capitals", {"type": "TERM", "data": "x"}),
    ("a type with space around it", {"type": " term ", "data": "x"}),
    ("an empty payload", {}),
]


def _build() -> dict[str, Any]:
    """What each payload becomes on the wire."""
    return {
        "terminal_types": ["input", "term"],
        "frames": [
            {
                "name": name,
                "payload": payload,
                "wire": _framed(payload),
                "terminal": _framed(payload) == encode_terminal_data(str(payload.get("data", ""))),
            }
            for name, payload in CASES
        ],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    # Not sorted. The wire strings are built from each payload's own field
    # order, and sorting the corpus would sort the recorded payloads too —
    # leaving them pinned against frames whose order had already been
    # destroyed.
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CASES)} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
