#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript capture port.

``TerminalCapture`` records what a session printed during one caller-owned
scope, bounded so a command that produces megabytes cannot exhaust memory
just because someone was watching.

The bound keeps the *tail*, not the head. That is the right end: a caller
capturing output wants what the command finished saying, not the banner it
started with. It also means the capture is not a prefix of the real output,
which is worth stating because it looks like truncation and behaves like a
sliding window.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_capture_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.transport_session import TerminalCapture

OUT = Path(__file__).with_name("capture_golden.json")

# (name, max chars, chunks appended in order)
CAPTURE_CASES: list[tuple[str, int, list[str]]] = [
    ("nothing captured", 16, []),
    ("one chunk", 16, ["hello"]),
    ("several chunks", 16, ["one ", "two ", "three"]),
    ("empty chunks are ignored", 16, ["a", "", "b"]),
    ("exactly at the bound", 8, ["abcdefgh"]),
    ("one past the bound", 8, ["abcdefghi"]),
    ("accumulating past the bound", 8, ["abcde", "fghij"]),
    ("a single chunk far over the bound", 8, ["a" * 100]),
    ("bound of one", 1, ["abc"]),
    ("bound of zero is raised to one", 0, ["abc"]),
    ("negative bound is raised to one", -5, ["abc"]),
    ("unicode is counted by character", 4, ["héllo"]),
    ("astral characters", 4, ["a😀b😀c"]),
    ("newlines are content", 6, ["a\nb\nc\nd"]),
]


def main() -> int:
    """Write the golden corpus and report the case count."""
    records: list[dict[str, Any]] = []
    for name, max_chars, chunks in CAPTURE_CASES:
        capture = TerminalCapture(max_chars=max_chars)
        for chunk in chunks:
            capture._append(chunk)
        records.append({"name": name, "max_chars": max_chars, "chunks": chunks, "text": capture.text})

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_capture_golden.py",
        "default_max_chars": 65_536,
        "captures": records,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(records)} capture cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
