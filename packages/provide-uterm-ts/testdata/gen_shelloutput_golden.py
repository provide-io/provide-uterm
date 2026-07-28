#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the shell's output formatting.

What the interactive shell writes to a terminal: coloured messages, key-value
lines, and fixed-width tables. Every line ends `\\r\\n`, because a terminal in
raw mode does not translate one into the other and a bare newline would leave
the cursor where the last line ended.

**A column is as wide as its widest cell, then at least as wide as its
header.** Computed from the rows first, so a header longer than any value
still fits and a header shorter than one does not truncate it.

**A short row truncates the table.** The widths come from zipping the rows
together, which stops at the shortest — so a row with fewer cells than the
others silently drops the columns past its end. Recorded rather than fixed:
it is the reference's behaviour, and a port that padded instead would render
a table the reference never would.

**An empty table says so.** A caller printing nothing at all leaves a user
unsure whether the command ran.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_shelloutput_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.shell._output import (
    BANNER,
    CLEAR_SCREEN,
    PROMPT,
    error_msg,
    fmt_kv,
    fmt_table,
    heading,
    info_msg,
    success_msg,
)

OUT = Path(__file__).with_name("shelloutput_golden.json")

# (name, text) — each message helper over the same inputs.
MESSAGE_CASES: list[tuple[str, str]] = [
    ("ordinary text", "something happened"),
    ("nothing", ""),
    ("text with a newline in it", "two\nlines"),
    ("text that is not ascii", "héllo → ✓"),
]

# (name, key, value, width) — a key-value line.
KV_CASES: list[tuple[str, str, str, int]] = [
    ("a short key", "name", "value", 20),
    ("a key exactly the width", "12345678901234567890", "value", 20),
    ("a key longer than the width", "123456789012345678901234", "value", 20),
    ("an empty key", "", "value", 20),
    ("a narrow width", "name", "value", 4),
    ("no width at all", "name", "value", 0),
]

# (name, rows, headers) — a table.
TABLE_CASES: list[tuple[str, list[tuple[str, ...]], tuple[str, ...] | None]] = [
    ("nothing at all", [], None),
    ("nothing at all, with headers", [], ("a", "b")),
    ("one row", [("a", "b")], None),
    ("two rows", [("a", "b"), ("cc", "dd")], None),
    ("rows and headers", [("a", "b"), ("cc", "dd")], ("one", "two")),
    ("a header longer than its column", [("a",)], ("header",)),
    ("a column longer than its header", [("aaaaaaaa",)], ("h",)),
    ("one column", [("a",), ("bb",)], None),
    # A short row truncates the table, which is the reference's own behaviour.
    ("a short row", [("a", "b"), ("c",)], None),
    ("a short row first", [("c",), ("a", "b")], None),
    ("a short row with headers", [("a", "b"), ("c",)], ("one", "two")),
    ("more headers than columns", [("a",)], ("one", "two")),
    ("fewer headers than columns", [("a", "b")], ("one",)),
    ("an empty cell", [("", "b")], None),
    ("a row of empty cells", [("", "")], None),
]


def _build() -> dict[str, Any]:
    """Everything the formatter decides."""
    return {
        "prompt": PROMPT,
        "banner": BANNER,
        "clear_screen": CLEAR_SCREEN,
        "messages": [
            {
                "name": name,
                "text": text,
                "error": error_msg(text),
                "info": info_msg(text),
                "success": success_msg(text),
                "heading": heading(text),
            }
            for name, text in MESSAGE_CASES
        ],
        "kv": [
            {"name": name, "key": key, "value": value, "width": width, "line": fmt_kv(key, value, width=width)}
            for name, key, value, width in KV_CASES
        ],
        "tables": [
            {
                "name": name,
                "rows": [list(row) for row in rows],
                "headers": list(headers) if headers else None,
                "table": fmt_table(rows, headers),
            }
            for name, rows, headers in TABLE_CASES
        ],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(MESSAGE_CASES)} messages, {len(TABLE_CASES)} tables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
