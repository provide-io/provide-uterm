#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``emulator`` port.

Covers ``TerminalEmulator`` and the SGR row renderer it shares with
``AnsiBuffer``. Timestamps are excluded: ``captured_at`` is always fresh by
design, so the corpus records everything else and the port asserts freshness
separately.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_emulator_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.emulator import TerminalEmulator
from provide.uterm.render.buffer import render_cell_rows, style_to_sgr

OUT = Path(__file__).with_name("emulator_golden.json")

COLS = 20
ROWS = 5


def _snapshot(emulator: TerminalEmulator) -> dict[str, Any]:
    """Snapshot without the always-fresh timestamp."""
    snap = emulator.get_snapshot()
    snap.pop("captured_at", None)
    return snap


# (name, cols, rows, chunks-as-latin1-safe-strings). Each chunk is encoded
# CP437 before being fed, matching how a transport delivers bytes.
CASES: list[tuple[str, int, int, list[str]]] = [
    ("empty", COLS, ROWS, []),
    ("plain text", COLS, ROWS, ["hello"]),
    ("two rows", COLS, ROWS, ["one\r\ntwo"]),
    ("fed in two chunks", COLS, ROWS, ["hel", "lo"]),
    ("prompt with trailing space", COLS, ROWS, ["Command? "]),
    ("prompt ending in a colon", COLS, ROWS, ["Name:"]),
    ("prompt ending in a colon and space", COLS, ROWS, ["Name: "]),
    ("no trailing space", COLS, ROWS, ["done"]),
    # cursor_at_end has a deliberate two-character slack for BBS carets.
    ("cursor just before the end", COLS, ROWS, ["abcdef\x1b[5G"]),
    ("cursor well before the end", COLS, ROWS, ["abcdef\x1b[1G"]),
    ("cursor on a row below the content", COLS, ROWS, ["abc\r\n\r\n"]),
    ("blank screen", COLS, ROWS, ["\x1b[2J"]),
    # Colour and attributes, which only ansi_screen preserves.
    ("coloured text", COLS, ROWS, ["\x1b[31mred\x1b[0m plain"]),
    ("bold and underline", COLS, ROWS, ["\x1b[1;4mboth\x1b[0m"]),
    ("reverse video", COLS, ROWS, ["\x1b[7mrev\x1b[0m"]),
    ("background colour", COLS, ROWS, ["\x1b[42mbg\x1b[0m"]),
    ("256-colour", COLS, ROWS, ["\x1b[38;5;196mx"]),
    ("brown, which the renderer has no code for", COLS, ROWS, ["\x1b[33mbrown"]),
    ("blink", COLS, ROWS, ["\x1b[5mblink"]),
]

# (fg, bg, bold, underscore, reverse, blink) styles for style_to_sgr.
STYLE_CASES: list[tuple[str, str, bool, bool, bool, bool]] = [
    ("default", "default", False, False, False, False),
    ("red", "default", False, False, False, False),
    ("default", "green", False, False, False, False),
    ("red", "green", False, False, False, False),
    ("brightcyan", "brightblack", False, False, False, False),
    ("default", "default", True, False, False, False),
    ("default", "default", False, True, False, False),
    ("default", "default", False, False, False, True),
    ("default", "default", True, True, False, True),
    # Reverse swaps the two colours before they are encoded.
    ("red", "green", False, False, True, False),
    ("default", "default", False, False, True, False),
    # pyte hex colours become truecolor.
    ("ff8000", "default", False, False, False, False),
    ("default", "0000ff", False, False, False, False),
    ("ff8000", "0000ff", True, False, False, False),
    # A name the renderer's table does not carry emits no colour code.
    ("brown", "default", False, False, False, False),
    ("brightbrown", "default", False, False, False, False),
    # Not a colour name and not hex.
    ("nonsense", "default", False, False, False, False),
    ("ff800", "default", False, False, False, False),
    ("FF8000", "default", False, False, False, False),
]


def main() -> int:
    """Write the golden corpus and report the record count."""
    cases: list[dict[str, Any]] = []
    for name, cols, rows, chunks in CASES:
        emulator = TerminalEmulator(cols=cols, rows=rows)
        for chunk in chunks:
            emulator.process(chunk.encode("cp437", errors="replace"))
        cases.append(
            {
                "name": name,
                "cols": cols,
                "rows": rows,
                "chunks": chunks,
                "snapshot": _snapshot(emulator),
                "ansi_screen": emulator.ansi_screen(),
                "raw_tail": emulator.get_raw_tail(),
            }
        )

    # Reset and resize change the observable state; record both.
    #
    # Resize is order-dependent in the reference: reading the screen first
    # changes what a later resize does, because the underlying buffer is a
    # defaultdict and merely reading it materialises the blank rows that a
    # shrink then shifts over the content. Both orders are recorded so the
    # divergence is visible; the port is deterministic and matches the
    # read-first result, which is both the documented behaviour ("lines will
    # be clipped at the top") and what a server that snapshots continuously
    # actually sees.
    resized = TerminalEmulator(cols=COLS, rows=ROWS)
    resized.process(b"hello")
    resized.get_snapshot()
    resized.resize(10, 3)

    resized_unread = TerminalEmulator(cols=COLS, rows=ROWS)
    resized_unread.process(b"hello")
    resized_unread.resize(10, 3)
    reset = TerminalEmulator(cols=COLS, rows=ROWS)
    reset.process(b"hello")
    reset.reset()

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_emulator_golden.py",
        "defaults": {"cols": 80, "rows": 25, "term": "ANSI", "receive_encoding": "cp437"},
        "cases": cases,
        "after_resize": {"snapshot": _snapshot(resized), "raw_tail": resized.get_raw_tail()},
        "resize_order_divergence": {
            "read_first": _snapshot(resized)["screen"],
            "never_read": _snapshot(resized_unread)["screen"],
        },
        "after_reset": {"snapshot": _snapshot(reset), "raw_tail": reset.get_raw_tail()},
        "styles": [
            {"style": list(style), "sgr": style_to_sgr(*style)}  # type: ignore[arg-type]
            for style in STYLE_CASES
        ],
        # A row renderer fed an empty buffer still emits a reset per row.
        "empty_rows": render_cell_rows({}, 3, 2),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(cases)} cases, {len(payload['styles'])} styles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
