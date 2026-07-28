#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for the hub's float coercion.

Values arriving on the wire are whatever a client sent, and a frame carrying a
bad number must not take the connection down with it. Every one of these
becomes a timeout, a rate or an interval, so the fallback is what the hub was
configured with rather than zero.

**An absent value takes the default; an unreadable one takes it too.** They
are not distinguished, because a caller that omitted a field and one that sent
nonsense both want whatever the server would have used.

**Python's ``float`` is not the host's ``Number``.** It refuses the empty
string where ``Number`` reads zero, accepts underscores and surrounding space,
and takes ``inf`` and ``nan`` by name. Each of those is a value a client can
put on a wire.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_safefloat_golden.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from provide.uterm.server.bridge.models import _safe_float

OUT = Path(__file__).with_name("safefloat_golden.json")

DEFAULT = 2.5

# (name, value) — what a frame can carry where a number was expected.
CASES: list[tuple[str, Any]] = [
    ("a float", 1.5),
    ("an integer", 3),
    ("zero", 0),
    ("a negative", -1.5),
    ("nothing", None),
    ("a numeric string", "1.5"),
    ("an integer string", "3"),
    ("a string with space around it", "  1.5  "),
    ("a string with an underscore", "1_000.5"),
    ("an exponent", "1e3"),
    ("a signed string", "+1.5"),
    ("an empty string", ""),
    ("a string of spaces", "   "),
    ("a string that is not a number", "nonsense"),
    ("a hexadecimal string", "0x10"),
    ("a boolean", True),
    ("a false boolean", False),
    ("a list", [1.5]),
    ("a mapping", {"a": 1}),
    # The three names Python's float() accepts and JSON cannot carry.
    ("infinity", "inf"),
    ("negative infinity", "-inf"),
    ("not a number", "nan"),
    ("infinity in capitals", "INFINITY"),
]


def _describe(value: float) -> Any:
    """A float as JSON can carry it, naming the three it cannot."""
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return value


def _build() -> dict[str, Any]:
    """What each value coerces to."""
    return {
        "default": DEFAULT,
        "values": [
            {"name": name, "value": value, "result": _describe(_safe_float(value, DEFAULT))} for name, value in CASES
        ],
        # A default that is itself unusual still comes back unchanged.
        "zero_default": _describe(_safe_float("nonsense", 0.0)),
        "negative_default": _describe(_safe_float(None, -1.0)),
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CASES)} values)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
