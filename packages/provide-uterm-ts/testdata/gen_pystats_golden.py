#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for CPython's ``statistics.variance``.

The obvious two-pass float formula is *not* what CPython computes. Since 3.8
``statistics`` converts every input to an exact rational, accumulates the sum
and the sum of squares exactly, and rounds once at the end::

    ssd = (count * sxx - sx * sx) / count
    variance = ssd / (count - 1)

The comment in the source is explicit that this formula has poor numeric
properties in floating point and is only used because fractions make it
exact. A two-pass float implementation agrees on most inputs and differs by
one ULP on others — the cases below include both, which is the point: a port
checked only against well-conditioned data would look correct.

This is the sample variance, with an ``n - 1`` denominator. Using the
population formula would report a systematically smaller number and shift
every threshold configured against it.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_pystats_golden.py
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("pystats_golden.json")

# (name, data) — chosen so some agree with the naive formula and some do not.
VARIANCE_CASES: list[tuple[str, list[float]]] = [
    ("two values", [1.0, 2.0]),
    ("three integers", [1.0, 2.0, 3.0]),
    ("identical values", [2.5, 2.5, 2.5]),
    ("zeros", [0.0, 0.0]),
    ("negatives", [-1.0, -2.0, -3.0]),
    ("mixed signs", [-1.0, 0.0, 1.0]),
    ("tenths", [0.1, 0.1, 0.1]),
    ("tenths, uneven", [0.1, 0.2, 0.3]),
    ("hundredths", [0.05, 0.05, 0.05, 0.05]),
    ("keystroke intervals, even", [0.09999999999990905, 0.10000000000013642, 0.09999999999990905]),
    ("keystroke intervals, human", [0.12, 0.19, 0.07, 0.34]),
    ("one long pause", [0.05, 0.05, 4.9]),
    ("sub-millisecond", [0.0001, 0.0001]),
    ("large magnitudes", [1e10, 1e10 + 1, 1e10 + 2]),
    ("tiny magnitudes", [1e-10, 2e-10, 3e-10]),
    ("wide spread", [1e-8, 1e8]),
    ("many values", [i / 7.0 for i in range(20)]),
    ("repeating decimal", [1 / 3, 2 / 3, 1.0]),
]


# Values whose exact integer ratio the port must reproduce, including the
# subnormal arm — no implicit leading mantissa bit, and an exponent one step
# above where the normal formula would put it.
RATIO_CASES: list[tuple[str, float]] = [
    ("zero", 0.0),
    ("one", 1.0),
    ("minus one", -1.0),
    ("a half", 0.5),
    ("a tenth", 0.1),
    ("two and a half", 2.5),
    ("ten billion", 1e10),
    ("ten to the minus ten", 1e-10),
    ("largest safe integer", float(2**53 - 1)),
    ("smallest normal", 2.2250738585072014e-308),
    ("smallest subnormal", 5e-324),
    ("a larger subnormal", 5e-324 * 12345),
]


def _naive(data: list[float]) -> float:
    """The two-pass float formula, for comparison only."""
    n = len(data)
    mean = sum(data) / n
    return sum((x - mean) ** 2 for x in data) / (n - 1)


def main() -> int:
    """Write the golden corpus and report how many cases the naive formula misses."""
    records: list[dict[str, Any]] = []
    for name, data in VARIANCE_CASES:
        exact = statistics.variance(data)
        naive = _naive(data)
        records.append(
            {
                "name": name,
                "data": data,
                "variance": exact,
                # Recorded so the port's tests can point at the cases where an
                # easier implementation would have been wrong.
                "naive_agrees": exact == naive,
            }
        )

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_pystats_golden.py",
        "variances": records,
        "ratios": [
            {
                "name": name,
                "value": value,
                "numerator": str(value.as_integer_ratio()[0]),
                "denominator": str(value.as_integer_ratio()[1]),
            }
            for name, value in RATIO_CASES
        ],
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    differing = sum(1 for record in records if not record["naive_agrees"])
    print(f"wrote {OUT} ({len(records)} cases, {differing} where the naive formula differs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
