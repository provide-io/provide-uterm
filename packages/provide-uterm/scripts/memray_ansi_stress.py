# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ANSI color processing stress script for memray profiling.

Exercises all SGR color modes (truecolor, 256, 16) across a range of colors.
Run via: python -m memray run -o ansi_stress.bin scripts/memray_ansi_stress.py
"""

from __future__ import annotations

from provide.uterm.render.sgr import SGR_FUNCTIONS

NUM_ITERATIONS = 50_000

COLOR_SAMPLES = [
    (0, 0, 0),
    (255, 255, 255),
    (128, 64, 192),
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (200, 100, 50),
    (10, 20, 30),
]


def run() -> None:
    fn_names = list(SGR_FUNCTIONS.keys())
    fn_list = [SGR_FUNCTIONS[k] for k in fn_names]

    results: list[str] = []
    for i in range(NUM_ITERATIONS):
        fg = COLOR_SAMPLES[i % len(COLOR_SAMPLES)]
        bg = COLOR_SAMPLES[(i + 3) % len(COLOR_SAMPLES)]
        results.extend(fn(fg, bg) for fn in fn_list)
        if len(results) > 10_000:
            results.clear()


if __name__ == "__main__":
    run()
