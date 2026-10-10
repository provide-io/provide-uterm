#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript query-parameter validation.

The reference's routes declare their query parameters as
``Annotated[int, Query(ge=..., le=...)]`` and ``Annotated[str,
Query(max_length=...)]``, so what a raw query string turns into — a value, or
which pydantic error with which message and context — is pydantic's decision.
This records that decision for each raw string, through the same constraint
types, so the port's parser is held to it rather than to a description of it.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_query_params_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from annotated_types import Ge, Le, MaxLen
from pydantic import TypeAdapter, ValidationError

OUT = Path(__file__).with_name("query_params_golden.json")

INT_INPUTS = [
    "5",
    "05",
    "00",
    "-0",
    "+5",
    "-5",
    " 5",
    "5 ",
    "\t5\n",
    chr(0xA0) + "5",
    chr(0x2003) + "5",
    chr(0x3000) + "5",
    "\u00855",
    chr(0xFEFF) + "5",
    "5.0",
    "5.00",
    "5.000000000",
    "5.0 ",
    " 5.0",
    "1_0",
    "0_0",
    "1_000.0",
    "",
    "-",
    ".0",
    "5.",
    "5.01",
    "1e2",
    "0x10",
    "1__0",
    "_1",
    "1_",
    "5.0_0",
    "+-5",
    chr(0x665),
    "ten",
    "1.5",
    "0",
    "1",
    "500",
    "501",
    "-1",
    "99999999999999999999",
    "-99999999999999999999",
]

STRING_INPUTS = ["", "e" * 100, "e" * 101, "\U0001f600" * 100, "\U0001f600" * 101, "é" * 101]


def _check(adapter: TypeAdapter[Any], raw: str) -> dict[str, Any]:
    """The value pydantic produces, or its error without the documentation link."""
    try:
        return {"input": raw, "value": adapter.validate_python(raw)}
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        return {"input": raw, "error": {key: error[key] for key in ("type", "msg", "input", "ctx") if key in error}}


def main() -> int:
    """Write the golden corpus and report the record count."""
    bounded = TypeAdapter(Annotated[int, Ge(1), Le(500)])
    unbounded_low = TypeAdapter(Annotated[int, Ge(0)])
    short = TypeAdapter(Annotated[str, MaxLen(100)])
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_query_params_golden.py",
        "int_1_500": [_check(bounded, raw) for raw in INT_INPUTS],
        "int_ge_0": [_check(unbounded_low, raw) for raw in INT_INPUTS],
        "str_max_100": [_check(short, raw) for raw in STRING_INPUTS],
    }
    text = json.dumps(payload, indent=1, ensure_ascii=False, default=str)
    OUT.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(INT_INPUTS)} integer inputs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
