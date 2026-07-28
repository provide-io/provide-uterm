#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``colors`` port.

Runs the CPython reference implementation over a deterministic corpus of
inputs and records the outputs. The TypeScript tests replay the same inputs
and must match byte-for-byte.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_colors_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.colors.downgrade import downgrade_to_16, downgrade_to_256
from provide.uterm.colors.mode import apply_color_mode
from provide.uterm.colors.rgb import rgb_to_16_index, rgb_to_256
from provide.uterm.colors.sgr import rewrite_params

OUT = Path(__file__).with_name("colors_golden.json")


def _rgb_inputs() -> list[tuple[int, int, int]]:
    """Deterministic RGB corpus: greys, cube strides, edges, out-of-range."""
    triples: list[tuple[int, int, int]] = []
    # Every grey value exercises both the ramp and its two guard branches.
    triples.extend((v, v, v) for v in range(256))
    # A stride over the full cube exercises banker's rounding at the .5 seams.
    for r in range(0, 256, 17):
        for g in range(0, 256, 51):
            for b in range(0, 256, 51):
                triples.append((r, g, b))
    # The exact half-way quantiser seams (n / 255 * 5 == k + 0.5).
    for v in (25, 26, 76, 77, 127, 128, 178, 179, 229, 230):
        triples.append((v, 0, 255))
        triples.append((0, v, 128))
    # Clamping on both sides, including asymmetric triples.
    triples.extend(
        [
            (-1, 0, 0),
            (-1000, -1000, -1000),
            (256, 256, 256),
            (999, 0, 0),
            (0, 999, 0),
            (0, 0, 999),
            (300, -5, 128),
        ]
    )
    return triples


def _sgr_param_inputs() -> list[str]:
    """Deterministic SGR parameter-list corpus."""
    return [
        "",
        "0",
        "1;31",
        "38;2;255;0;0",
        "48;2;0;128;255",
        "38;2;10;20;30;48;2;40;50;60",
        "1;38;2;12;34;56;4",
        # Truncated / malformed truecolor runs must pass through untouched.
        "38;2;1;2",
        "38;2;1",
        "38;2",
        "38",
        "48;2;;1;2",
        "38;3;1;2;3",
        "39;2;1;2;3",
        # 256-color runs are not truecolor and must survive verbatim.
        "38;5;196",
        "48;5;21",
        # Empty parameters and repeated separators.
        ";",
        ";;",
        "0;;1",
        # Out-of-range components clamp.
        "38;2;999;0;0",
        "48;2;0;0;99999999999999999999",
        # Leading zeros.
        "38;2;007;000;255",
    ]


def _text_inputs() -> list[str]:
    """Deterministic text corpus for the text-level downgrade functions."""
    return [
        "",
        "plain text without escapes",
        "\x1b[38;2;255;0;0mred\x1b[0m",
        "\x1b[48;2;0;0;255mblue bg\x1b[0m",
        "\x1b[1;38;2;12;34;56;4mmixed\x1b[0m tail",
        "\x1b[38;5;196malready 256\x1b[0m",
        "\x1b[Kerase line is not SGR\x1b[38;2;1;2;3mx",
        "prefix\x1b[38;2;10;20;30mmid\x1b[48;2;40;50;60msuffix",
        "\x1b[m",
        "\x1b[;m",
        "\x1b[38;2;1;2mtruncated\x1b[0m",
        # Non-ASCII passes through the latin-1 round trip unchanged.
        "caf\xe9 \x1b[38;2;200;100;50m\xff\x1b[0m",
        # Repeated identical sequences prove statelessness of the scanner.
        "\x1b[38;2;1;1;1ma\x1b[38;2;1;1;1mb\x1b[38;2;1;1;1mc",
    ]


def main() -> int:
    """Write the golden corpus and report the record count."""
    rgb_records = [
        {
            "r": r,
            "g": g,
            "b": b,
            "to256": rgb_to_256(r, g, b),
            "to16": rgb_to_16_index(r, g, b),
        }
        for (r, g, b) in _rgb_inputs()
    ]
    sgr_records = [
        {
            "params": params,
            "mode256": rewrite_params(params, "256"),
            "mode16": rewrite_params(params, "16"),
        }
        for params in _sgr_param_inputs()
    ]
    text_records = [
        {
            "text": text,
            "to256": downgrade_to_256(text),
            "to16": downgrade_to_16(text),
            "passthrough": apply_color_mode(text, "passthrough"),
            # latin-1 is a byte-exact 1:1 mapping, so the byte path is
            # recorded as a hex string to keep the corpus JSON-safe.
            "bytes256": apply_color_mode(text.encode("latin-1", errors="replace"), "256").hex(),
            "bytes16": apply_color_mode(text.encode("latin-1", errors="replace"), "16").hex(),
        }
        for text in _text_inputs()
    ]
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_colors_golden.py",
        "rgb": rgb_records,
        "sgr": sgr_records,
        "text": text_records,
    }
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    total = len(rgb_records) + len(sgr_records) + len(text_records)
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
