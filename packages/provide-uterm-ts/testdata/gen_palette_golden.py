#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the ANSI colour quantisers.

Mapping an arbitrary colour onto a terminal's palette: the sixteen standard
ANSI colours, or xterm's 256.

**The first nearest colour wins.** The comparison is strict, so a colour
exactly between two palette entries takes the earlier one. Every renderer that
quantises has to agree on that or the same screen renders differently in two
places.

**Distance is squared Euclidean in RGB.** Not perceptual — a perceptual metric
would be better and would also disagree with every other implementation of
this palette, which is the one thing a quantiser cannot do.

The corpus records every one of the 256 palette entries mapped back, which is
what catches an off-by-one in the cube arithmetic: the 216-colour cube's first
step is 55 wide and the rest are 40, so a port using a uniform step is right
for two of the six levels and wrong for four.

That mapping is *not* the identity, and the two exceptions are the point. The
cube's corners repeat colours the standard sixteen already have — index 16 is
black, which index 0 already was, and 231 is white, which 15 already was — so
"the first nearest wins" sends both back to the earlier entry. A port that
broke ties the other way passes every other case in this corpus and fails
those two.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_palette_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.render.palette import (
    _XTERM256,
    ANSI16_PALETTE,
    _build_xterm256,
    nearest_16,
    nearest_256,
)

OUT = Path(__file__).with_name("palette_golden.json")

# (name, rgb) — colours worth pinning by hand, alongside the exhaustive sweep.
CASES: list[tuple[str, tuple[int, int, int]]] = [
    ("black", (0, 0, 0)),
    ("white", (255, 255, 255)),
    ("pure red", (255, 0, 0)),
    ("pure green", (0, 255, 0)),
    ("pure blue", (0, 0, 255)),
    ("mid grey", (128, 128, 128)),
    ("a dark red", (100, 0, 0)),
    ("a colour between two entries", (85, 85, 85)),
    ("a colour just below a boundary", (127, 127, 127)),
    ("a colour just above it", (129, 129, 129)),
    ("the first cube step", (95, 0, 0)),
    ("just below the first cube step", (94, 0, 0)),
    ("the darkest grey", (8, 8, 8)),
    ("the lightest grey", (238, 238, 238)),
    ("a colour outside the range", (300, -20, 999)),
]


def _build() -> dict[str, Any]:
    """Everything the quantisers decide."""
    _build_xterm256()
    return {
        "ansi16": [list(entry) for entry in ANSI16_PALETTE],
        "xterm256": [list(entry) for entry in _XTERM256],
        "cases": [
            {"name": name, "rgb": list(rgb), "ansi16": list(nearest_16(*rgb)), "xterm256": nearest_256(*rgb)}
            for name, rgb in CASES
        ],
        # Every palette entry maps back to itself, which is what catches an
        # off-by-one in the cube arithmetic.
        "round_trip_256": [nearest_256(*rgb) for rgb in _XTERM256],
        "round_trip_16": [list(nearest_16(r, g, b)) for r, g, b, _fg, _bg in ANSI16_PALETTE],
        # A coarse sweep of the whole cube, to catch a wrong distance metric.
        "sweep": [
            {"rgb": [r, g, b], "ansi16": list(nearest_16(r, g, b)), "xterm256": nearest_256(r, g, b)}
            for r in range(0, 256, 51)
            for g in range(0, 256, 51)
            for b in range(0, 256, 51)
        ],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CASES)} cases, {len(corpus['sweep'])} swept)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
