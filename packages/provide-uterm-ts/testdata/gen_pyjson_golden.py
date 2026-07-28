#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for CPython-exact canonical JSON.

The identity-signature payload is
``json.dumps(claims, sort_keys=True, separators=(",", ":"))``, and the HMAC is
taken over those exact bytes. Any difference in key order, separator, string
escaping or number formatting changes the signature, so this corpus pins
``json.dumps`` itself rather than any uterm-specific behaviour.

Note ``ensure_ascii`` defaults to **True** here, unlike the control-frame
encoder which passes ``ensure_ascii=False``. The two are different wire
surfaces and the corpus keeps both honest.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_pyjson_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("pyjson_golden.json")

# Values whose JSON rendering a JavaScript port can reproduce exactly.
# Every number here is an integer, a non-integral float, or a float whose
# JavaScript rendering agrees with CPython's.
PORTABLE_VALUES: list[Any] = [
    {},
    [],
    "",
    "plain",
    0,
    1,
    -1,
    True,
    False,
    None,
    1.5,
    -2.25,
    0.1,
    # Shortest round-trip repr, where the naive rendering would differ.
    0.1 + 0.2,
    1 / 3,
    2**53 - 1,
    -(2**53) + 1,
    # Nesting and ordering.
    {"b": 1, "a": 2},
    {"z": {"y": {"x": 1}}},
    {"list": [1, "two", None, True]},
    [[1, [2, [3]]]],
    {"": "empty key"},
    # String escaping under ensure_ascii=True.
    {"quote": '"'},
    {"backslash": "\\"},
    {"newline": "\n", "tab": "\t", "cr": "\r"},
    {"formfeed": "\f", "backspace": "\b"},
    {"control": "\x00\x01\x1f"},
    {"del": "\x7f"},
    {"latin": "é"},
    {"cjk": "你好"},
    {"astral": "𝄞"},
    {"mixed": "a\xe9b你c"},
    {"solidus": "/"},
    # Sort order is over the raw key strings.
    {"B": 1, "a": 2, "A": 3, "b": 4},
    {"10": 1, "9": 2, "1": 3},
    {"é": 1, "e": 2},
    # Realistic claim shapes.
    {"role": "operator", "exp": 1735689600, "scopes": ["read", "write"]},
    {"sub": "user:alice", "admin": False},
]

# Values whose rendering genuinely differs, because JavaScript has one number
# type and cannot tell a Python float from a Python int.
FLOAT_DIVERGENT_VALUES: list[Any] = [
    0.0,
    1.0,
    -0.0,
    -1.0,
    1e21,
    1e-7,
    1e16,
    {"whole": 2.0},
    [1.0, 1.5],
]


def main() -> int:
    """Write the golden corpus and report the record count."""
    portable = [
        {
            "value": value,
            "canonical": json.dumps(value, sort_keys=True, separators=(",", ":")),
            "unsorted": json.dumps(value, separators=(",", ":")),
            "unicode": json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        }
        for value in PORTABLE_VALUES
    ]
    divergent = [
        {"repr": repr(value), "canonical": json.dumps(value, sort_keys=True, separators=(",", ":"))}
        for value in FLOAT_DIVERGENT_VALUES
    ]
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_pyjson_golden.py",
        "portable": portable,
        "float_divergences": divergent,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(portable) + len(divergent)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
