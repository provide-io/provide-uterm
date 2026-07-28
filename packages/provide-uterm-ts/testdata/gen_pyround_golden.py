#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for CPython's two-argument round().

`round(x, n)` looks like `round(x * 10**n) / 10**n` and is not. Two things
separate them, and both change answers that travel over the wire.

**Ties go to even.** `round(0.03125, 4)` is `0.0312`, not `0.0313`. JavaScript
reaches for `Math.round` (half away from zero) or `toFixed` (also half away
from zero, per the spec's "pick the larger n"), and both disagree.

**The tie test is on the exact binary value, not on the scaled one.** A double
is a dyadic rational, so it either sits exactly on a decimal half or it does
not — and multiplying by a power of ten can move a value that was just off a
half onto one, or off one. CPython asks the question of the original value.
The corpus records neighbours of exact ties (`nextafter` either side) so a
port that scales first is caught: the scaled product is the same double for
all three, but the answers differ.

Ties at four places happen exactly at odd thirty-seconds, because a double is
exactly `n / 10**5` only when `n` is a multiple of `5**5`.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_pyround_golden.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("pyround_golden.json")

# (name, value, ndigits) — what CPython's round() does with each.
CASES: list[tuple[str, float, int]] = [
    ("a tie at four places", 0.03125, 4),
    ("a tie that rounds down to even", 0.40625, 4),
    ("a tie one ulp low", math.nextafter(0.03125, 0.0), 4),
    ("a tie one ulp high", math.nextafter(0.03125, 1.0), 4),
    ("a negative tie", -0.03125, 4),
    ("a negative tie one ulp low", math.nextafter(-0.03125, -1.0), 4),
    ("a tie at two places", 0.125, 2),
    ("a tie at two places rounding up to even", 0.375, 2),
    ("a tie at one place", 0.25, 1),
    ("a tie at zero places", 0.5, 0),
    ("another tie at zero places", 1.5, 0),
    ("a third", 1 / 3, 4),
    ("two thirds", 2 / 3, 4),
    ("a seventh", 1 / 7, 4),
    ("a tenth", 0.1, 4),
    ("something already short", 0.5, 4),
    ("zero", 0.0, 4),
    ("negative zero", -0.0, 4),
    ("one", 1.0, 4),
    ("negative one", -1.0, 4),
    ("a whole number", 42.0, 4),
    ("a negative repeating fraction", -1 / 3, 4),
    ("more digits than the value has", 0.5, 10),
    ("no digits at all", 2.5, 0),
    ("a large value", 12345.678901234, 4),
    # Past 2**52 a double has no fractional part left, so the rounding takes
    # the other branch entirely — and that branch still has to scale.
    ("larger than any fraction", float(2**60), 4),
    ("very large indeed", 1e300, 4),
    ("negative and very large", -(2.0**60), 4),
    # The smallest subnormal, where the mantissa carries no implicit leading
    # one and the exponent is pinned rather than derived.
    ("the smallest subnormal", 5e-324, 4),
    ("a larger subnormal", 1e-320, 4),
    ("a negative subnormal", -5e-324, 4),
    ("a very small value", 1e-9, 4),
    ("a negative very small value", -1e-9, 4),
    # 2.675 is the classic float-surprise: it is stored just below the tie, so
    # a correct implementation rounds it *down* even though the decimal
    # literal looks like a tie that should go up.
    ("the classic float surprise", 2.675, 2),
    ("its negative", -2.675, 2),
    ("a value whose scaling would drift", 1.005, 2),
    ("another scaling drift", 1.015, 2),
    ("a third scaling drift", 8.835, 2),
]


def _record(name: str, value: float, ndigits: int) -> dict[str, Any]:
    """One rounding, with the exact input recorded as a hex float."""
    return {
        "name": name,
        # The decimal form of a double is lossy to read back at some precision
        # settings; the hex form is exact, so the port rounds the same bits.
        "value_hex": value.hex(),
        "value": value,
        "ndigits": ndigits,
        "rounded": round(value, ndigits),
        "rounded_hex": float(round(value, ndigits)).hex(),
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {"cases": [_record(*case) for case in CASES]}
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CASES)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
