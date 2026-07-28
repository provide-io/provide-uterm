#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for rendering an image as terminal art.

Two pixel rows per terminal row, using the lower-half block: the bottom pixel
is the foreground and the top one the background, so a cell carries two.

**A repeated colour emits no escape.** The comparison is against the last
sequence written on that row, so a run of identical pixels costs one escape
and then nothing — which is the difference between a frame that fits in a
terminal's buffer and one that does not.

**A transparent pixel is black, not skipped.** There is no way to punch a hole
in a terminal cell, so anything under half opacity becomes black and the cell
is still drawn.

**An odd number of pixel rows pairs the last one with black**, rather than
dropping it or reading past the end of the image.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_imagerender_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.render.image import render_frame
from provide.uterm.render.sgr import SGR_FUNCTIONS, sgr_16, sgr_256, sgr_truecolor

OUT = Path(__file__).with_name("imagerender_golden.json")

# (name, fg, bg) — what each colour mode emits.
SGR_CASES: list[tuple[str, tuple[int, int, int], tuple[int, int, int]]] = [
    ("black on black", (0, 0, 0), (0, 0, 0)),
    ("white on black", (255, 255, 255), (0, 0, 0)),
    ("red on blue", (255, 0, 0), (0, 0, 255)),
    ("two greys", (128, 128, 128), (64, 64, 64)),
    ("a colour off the palette", (200, 100, 50), (17, 34, 51)),
]


class _Pixels:
    """A pixel grid, as PIL's accessor presents one."""

    def __init__(self, rows: list[list[tuple[int, int, int, int]]]) -> None:
        self._rows = rows

    def __getitem__(self, xy: tuple[int, int]) -> tuple[int, int, int, int]:
        x, y = xy
        return self._rows[y][x]


# (name, rows) — a small image, row by row, each pixel RGBA.
OPAQUE = 255
FRAMES: list[tuple[str, list[list[tuple[int, int, int, int]]]]] = [
    ("one cell", [[(255, 0, 0, OPAQUE)], [(0, 0, 255, OPAQUE)]]),
    ("a single row", [[(255, 0, 0, OPAQUE), (0, 255, 0, OPAQUE)]]),
    (
        "a run of one colour",
        [[(255, 0, 0, OPAQUE)] * 4, [(0, 0, 255, OPAQUE)] * 4],
    ),
    (
        "a run broken in the middle",
        [
            [(255, 0, 0, OPAQUE), (255, 0, 0, OPAQUE), (0, 255, 0, OPAQUE), (255, 0, 0, OPAQUE)],
            [(0, 0, 255, OPAQUE)] * 4,
        ],
    ),
    (
        "two terminal rows",
        [
            [(255, 0, 0, OPAQUE)],
            [(0, 255, 0, OPAQUE)],
            [(0, 0, 255, OPAQUE)],
            [(255, 255, 0, OPAQUE)],
        ],
    ),
    (
        "an odd number of pixel rows",
        [[(255, 0, 0, OPAQUE)], [(0, 255, 0, OPAQUE)], [(0, 0, 255, OPAQUE)]],
    ),
    # Transparency: anything under half opacity becomes black.
    ("a transparent top pixel", [[(255, 0, 0, 127)], [(0, 0, 255, OPAQUE)]]),
    ("a transparent bottom pixel", [[(255, 0, 0, OPAQUE)], [(0, 0, 255, 0)]]),
    ("half opacity exactly", [[(255, 0, 0, 128)], [(0, 0, 255, 128)]]),
    ("one under half opacity", [[(255, 0, 0, 127)], [(0, 0, 255, 127)]]),
    ("nothing at all", []),
]


def _build() -> dict[str, Any]:
    """Everything the renderer decides."""
    return {
        "modes": sorted(SGR_FUNCTIONS),
        "sgr": [
            {
                "name": name,
                "fg": list(fg),
                "bg": list(bg),
                "truecolor": sgr_truecolor(fg, bg),
                "256": sgr_256(fg, bg),
                "16": sgr_16(fg, bg),
            }
            for name, fg, bg in SGR_CASES
        ],
        "frames": [
            {
                "name": name,
                "rows": [[list(pixel) for pixel in row] for row in rows],
                "width": len(rows[0]) if rows else 0,
                "height": len(rows),
                "truecolor": render_frame(_Pixels(rows), len(rows[0]) if rows else 0, len(rows), sgr_truecolor),
                "16": render_frame(_Pixels(rows), len(rows[0]) if rows else 0, len(rows), sgr_16),
            }
            for name, rows in FRAMES
        ],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(SGR_CASES)} sgr, {len(FRAMES)} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
