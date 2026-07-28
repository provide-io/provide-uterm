#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the CPython string predicates.

``str.isdigit()`` is not ``/^\\d+$/``. CPython says yes to any character whose
Unicode numeric type is Decimal *or* Digit, which pulls in superscripts,
subscripts, circled digits and several historic scripts that a JavaScript
``\\d`` test rejects outright.

That difference is not cosmetic here. The regex-safety validator decides
whether ``{...}`` is a counted quantifier by asking ``isdigit()`` of its body,
and a quantifier is what triggers the nested-quantifier rejection. So a
pattern like ``(a+){٣}`` is refused by the reference and would sail past an
ASCII-only port — a ReDoS guard bypassed by choosing a different alphabet for
the repeat count.

The corpus records the exact code-point ranges rather than a sample, so the
port's table can be checked against CPython in full rather than spot-checked.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_pystr_golden.py
"""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("pystr_golden.json")

# (name, text) — the shapes a caller actually passes.
ISDIGIT_CASES: list[tuple[str, str]] = [
    ("empty string", ""),
    ("ascii digits", "123"),
    ("single ascii digit", "7"),
    ("leading zero", "007"),
    ("letters", "abc"),
    ("mixed", "1a"),
    ("sign", "-1"),
    ("decimal point", "1.5"),
    ("whitespace", " 1"),
    ("arabic-indic", "٣"),
    ("arabic-indic run", "٣٤٥"),
    ("superscript two", "²"),
    ("superscript run", "²³"),
    ("subscript", "₃"),
    ("circled", "①"),
    # RUF001: a confusable digit is exactly what this case is testing.
    ("fullwidth", "３"),  # noqa: RUF001
    ("devanagari", "३"),
    ("mixed scripts", "1٣"),
    ("roman numeral", "Ⅷ"),
    ("fraction", "½"),
    ("kanji numeral", "三"),
]


def _ranges(code_points: list[int]) -> list[list[int]]:
    """Collapse a sorted code-point list into inclusive ranges."""
    out: list[list[int]] = []
    start = prev = None
    for cp in code_points:
        if start is None:
            start = prev = cp
            continue
        if cp == (prev or 0) + 1:
            prev = cp
            continue
        out.append([start, prev if prev is not None else start])
        start = prev = cp
    if start is not None:
        out.append([start, prev if prev is not None else start])
    return out


def main() -> int:
    """Write the golden corpus and report the range count."""
    decimal_only: list[int] = []
    digit_not_decimal: list[int] = []
    for cp in range(sys.maxunicode + 1):
        char = chr(cp)
        if not char.isdigit():
            continue
        if unicodedata.category(char) == "Nd":
            decimal_only.append(cp)
        else:
            digit_not_decimal.append(cp)

    payload: dict[str, Any] = {
        "generator": "packages/provide-uterm-ts/testdata/gen_pystr_golden.py",
        "unicode_version": unicodedata.unidata_version,
        "isdigit": [{"name": name, "text": text, "result": text.isdigit()} for name, text in ISDIGIT_CASES],
        # Nd is what a JavaScript \p{Nd} class already covers; the second list
        # is everything CPython additionally accepts and is what the port has
        # to carry explicitly.
        "decimal_ranges": _ranges(decimal_only),
        "digit_not_decimal_ranges": _ranges(digit_not_decimal),
        "decimal_count": len(decimal_only),
        "digit_not_decimal_count": len(digit_not_decimal),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"wrote {OUT} ({len(payload['decimal_ranges'])} decimal ranges, "
        f"{len(payload['digit_not_decimal_ranges'])} digit-only ranges)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
