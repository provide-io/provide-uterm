#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for key-value extraction.

Extraction turns a screen into the numbers an agent acts on, so the ways it
can be quietly wrong all end with something acting on a stale or mistyped
value.

**The last match wins, not the first.** A screen buffer holds scroll history,
so the same label appears many times and only the bottom one is current. A
port reaching for `search` instead of the last of `finditer` reads a credit
balance from several screens ago and spends money that is not there.

**A capture group is preferred over the whole match**, so a pattern written
with parentheses yields the number rather than the label and the number
together. Without a group the whole match is the value, which is what a
pattern with no parentheses means.

**A conversion that fails drops the field rather than guessing.** A field that
could not be read is absent, and a required field that is absent is a
validation error — which is how a caller learns the screen was not what it
expected, instead of receiving a zero.

**Numbers are Python numbers.** Thousands separators are stripped, so
`1,234` is 1234; underscores are legal in Python's own literals; and `int()`
refuses a float-shaped string outright rather than truncating it, so `"1.9"`
is not 1.

**Validation is reported, not enforced.** The result carries `_validation`
alongside the values, so a caller can act on a value it knows is out of range
— or refuse to. Silently dropping it would leave them unable to tell "absent"
from "implausible".

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_extractor_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.detection.extractor import KVExtractor, extract_kv

OUT = Path(__file__).with_name("extractor_golden.json")

# A screen with scroll history: the same labels appear more than once.
SCROLLED = "\n".join(
    [
        "Sector  1  Credits 100",
        "you travel onwards",
        "Sector  42  Credits 1,234",
        "Command [TL=00:00:00]:? ",
    ]
)

# (name, screen, config) — one field at a time.
SINGLE_CASES: list[tuple[str, str, Any]] = [
    ("a plain string", "Name: Alice", {"field": "name", "regex": r"Name:\s*(\w+)"}),
    ("an int", "Sector 42", {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"}),
    ("a float", "Ratio 1.5", {"field": "ratio", "regex": r"Ratio\s+([\d.]+)", "type": "float"}),
    ("a bool", "Docked yes", {"field": "docked", "regex": r"Docked\s+(\w+)", "type": "bool"}),
    ("the last of several", SCROLLED, {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"}),
    ("thousands separators", SCROLLED, {"field": "credits", "regex": r"Credits\s+([\d,]+)", "type": "int"}),
    ("no capture group takes the whole match", "Sector 42", {"field": "sector", "regex": r"\d+", "type": "int"}),
    ("two groups take the first", "a=1 b=2", {"field": "pair", "regex": r"a=(\d+) b=(\d+)"}),
    ("no match at all", "nothing here", {"field": "sector", "regex": r"Sector\s+(\d+)"}),
    ("an int that is not one", "Sector abc", {"field": "sector", "regex": r"Sector\s+(\w+)", "type": "int"}),
    (
        "a float-shaped string as an int",
        "Sector 1.9",
        {"field": "sector", "regex": r"Sector\s+([\d.]+)", "type": "int"},
    ),
    ("a bool that is not one", "Docked maybe", {"field": "docked", "regex": r"Docked\s+(\w+)", "type": "bool"}),
    ("an unknown type falls back to text", "Sector 42", {"field": "sector", "regex": r"(\d+)", "type": "nonsense"}),
    ("whitespace is stripped", "Name:   Alice   ", {"field": "name", "regex": r"Name:(.*)"}),
    ("no field name", "Sector 42", {"regex": r"(\d+)"}),
    ("no regex", "Sector 42", {"field": "sector"}),
    ("an empty field name", "Sector 42", {"field": "", "regex": r"(\d+)"}),
    ("an empty regex", "Sector 42", {"field": "sector", "regex": ""}),
    ("case-insensitive by default", "SECTOR 42", {"field": "sector", "regex": r"sector\s+(\d+)", "type": "int"}),
    ("multiline by default", "one\nSector 42", {"field": "sector", "regex": r"^Sector\s+(\d+)", "type": "int"}),
    ("a negative int", "Balance -50", {"field": "balance", "regex": r"Balance\s+(-?\d+)", "type": "int"}),
    ("a negative float", "Drift -1.5", {"field": "drift", "regex": r"Drift\s+(-?[\d.]+)", "type": "float"}),
    ("an exponent", "Distance 1e5", {"field": "distance", "regex": r"Distance\s+(\S+)", "type": "float"}),
    ("an infinity", "Distance inf", {"field": "distance", "regex": r"Distance\s+(\S+)", "type": "float"}),
    ("underscores in a number", "Sector 1_000", {"field": "sector", "regex": r"Sector\s+(\S+)", "type": "int"}),
]

# (name, value, type) — the conversions on their own.
CONVERT_CASES: list[tuple[str, str, str]] = [
    ("plain text", "hello", "string"),
    ("text with padding", "  hello  ", "string"),
    ("an integer", "42", "int"),
    ("an integer with separators", "1,234,567", "int"),
    ("an integer with padding", "  42  ", "int"),
    ("a negative integer", "-42", "int"),
    ("a positive integer", "+42", "int"),
    ("an integer with underscores", "1_000", "int"),
    ("a float as an int", "1.9", "int"),
    ("text as an int", "abc", "int"),
    ("nothing as an int", "", "int"),
    ("a float", "1.5", "float"),
    ("a float with separators", "1,234.5", "float"),
    ("an integer as a float", "42", "float"),
    ("an exponent", "1e5", "float"),
    ("an infinity", "inf", "float"),
    ("a negative infinity", "-inf", "float"),
    ("a not-a-number", "nan", "float"),
    ("text as a float", "abc", "float"),
    ("true", "true", "bool"),
    ("yes", "yes", "bool"),
    ("y", "y", "bool"),
    ("one", "1", "bool"),
    ("on", "on", "bool"),
    ("upper case true", "TRUE", "bool"),
    ("mixed case yes", "Yes", "bool"),
    ("false", "false", "bool"),
    ("no", "no", "bool"),
    ("n", "n", "bool"),
    ("zero", "0", "bool"),
    ("off", "off", "bool"),
    ("something else as a bool", "maybe", "bool"),
    ("nothing as a bool", "", "bool"),
    ("an unknown type", "42", "nonsense"),
]

# (name, screen, config) — whole extractions with validation.
EXTRACT_CASES: list[tuple[str, str, Any]] = [
    (
        "several fields",
        SCROLLED,
        [
            {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"},
            {"field": "credits", "regex": r"Credits\s+([\d,]+)", "type": "int"},
        ],
    ),
    ("a single config rather than a list", "Sector 42", {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"}),
    ("nothing configured", "Sector 42", None),
    ("an empty list", "Sector 42", []),
    ("an empty dict", "Sector 42", {}),
    ("a config that is neither", "Sector 42", "sector"),
    ("a dict with no field key", "Sector 42", {"regex": r"(\d+)"}),
    ("nothing matched", "nothing", [{"field": "sector", "regex": r"Sector\s+(\d+)"}]),
    (
        "one field matched and one not",
        "Sector 42",
        [
            {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"},
            {"field": "credits", "regex": r"Credits\s+(\d+)", "type": "int"},
        ],
    ),
    (
        "a required field that is missing",
        "Sector 42",
        [
            {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"},
            {"field": "credits", "regex": r"Credits\s+(\d+)", "type": "int", "required": True},
        ],
    ),
    (
        "a value below its minimum",
        "Sector 3",
        [{"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int", "validate": {"min": 10}}],
    ),
    (
        "a value above its maximum",
        "Sector 300",
        [{"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int", "validate": {"max": 100}}],
    ),
    (
        "a value inside its range",
        "Sector 42",
        [{"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int", "validate": {"min": 1, "max": 100}}],
    ),
    (
        "a value exactly on its bounds",
        "Sector 42",
        [{"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int", "validate": {"min": 42, "max": 42}}],
    ),
    (
        "a string against a pattern",
        "Name: Alice",
        [{"field": "name", "regex": r"Name:\s*(\w+)", "validate": {"pattern": r"^[A-Z]"}}],
    ),
    (
        "a string failing its pattern",
        "Name: alice",
        [{"field": "name", "regex": r"Name:\s*(\w+)", "validate": {"pattern": r"^[A-Z]"}}],
    ),
    (
        "a pattern anchored only at the start",
        "Name: Alice",
        [{"field": "name", "regex": r"Name:\s*(\w+)", "validate": {"pattern": r"^A"}}],
    ),
    (
        "a string in its allowed values",
        "Mode: safe",
        [{"field": "mode", "regex": r"Mode:\s*(\w+)", "validate": {"allowed_values": ["safe", "fast"]}}],
    ),
    (
        "a string outside its allowed values",
        "Mode: reckless",
        [{"field": "mode", "regex": r"Mode:\s*(\w+)", "validate": {"allowed_values": ["safe", "fast"]}}],
    ),
    (
        "a float validated as one",
        "Ratio 1.5",
        [{"field": "ratio", "regex": r"Ratio\s+([\d.]+)", "type": "float", "validate": {"min": 1.0, "max": 2.0}}],
    ),
    (
        "a bool needs no validation rules",
        "Docked yes",
        [{"field": "docked", "regex": r"Docked\s+(\w+)", "type": "bool"}],
    ),
    (
        "a float that is a whole number",
        "Ratio 2",
        [{"field": "ratio", "regex": r"Ratio\s+(\d+)", "type": "float", "validate": {"min": 1}}],
    ),
    (
        "a required flag that is not a boolean",
        "Sector 42",
        [
            {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"},
            {"field": "credits", "regex": r"Credits\s+(\d+)", "required": 1},
        ],
    ),
    (
        "a field name that is zero",
        "Sector 42",
        [{"field": 0, "regex": r"(\d+)"}],
    ),
    (
        "a pattern with no anchor",
        "Name: Alice",
        [{"field": "name", "regex": r"Name:\s*(\w+)", "validate": {"pattern": "lice"}}],
    ),
    (
        "a screen that says undefined",
        "Sector undefined",
        [{"field": "sector"}],
    ),
    # Two configs naming the same field. The last one to extract wins the
    # value, and every config still validates against it — so a value can be
    # checked against a type it was never converted to.
    (
        "a value checked against a type it is not",
        "Sector abc",
        [
            {"field": "sector", "regex": r"Sector\s+(\w+)"},
            {"field": "sector", "regex": r"Sector\s+(\w+)", "type": "int"},
        ],
    ),
    (
        "a fraction checked as a whole number",
        "Ratio 1.5",
        [
            {"field": "ratio", "regex": r"Ratio\s+([\d.]+)", "type": "float"},
            {"field": "ratio", "regex": r"Ratio\s+(zzz)", "type": "int"},
        ],
    ),
    # A string field whose allowed values are written as numbers. The message
    # renders them the way the reference does, which is not the way JSON does.
    (
        "allowed values that are numbers",
        "Sector 42",
        [{"field": "sector", "regex": r"Sector\s+(\d+)", "validate": {"allowed_values": [1, 2, "3"]}}],
    ),
    # The second config does not match, so the integer from the first survives
    # and is checked as text — which is how a whole number reaches the string
    # validator and has to be named as one.
    (
        "a whole number checked as text",
        "Sector 42",
        [
            {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"},
            {"field": "sector", "regex": r"Sector\s+(zzz)", "type": "string"},
        ],
    ),
    (
        "a boolean checked as text",
        "Docked yes",
        [
            {"field": "docked", "regex": r"Docked\s+(\w+)", "type": "bool"},
            {"field": "docked", "regex": r"Docked\s+(zzz)", "type": "string"},
        ],
    ),
    # A boolean *is* an integer in the reference's type system, so this passes.
    # Refusing it would report an error the reference does not.
    (
        "a boolean checked as a whole number",
        "Docked yes",
        [
            {"field": "docked", "regex": r"Docked\s+(\w+)", "type": "bool"},
            {"field": "docked", "regex": r"Docked\s+(zzz)", "type": "int"},
        ],
    ),
    (
        "a boolean checked as a fraction",
        "Docked yes",
        [
            {"field": "docked", "regex": r"Docked\s+(\w+)", "type": "bool"},
            {"field": "docked", "regex": r"Docked\s+(zzz)", "type": "float"},
        ],
    ),
    (
        "a config whose field is not a string",
        "Sector 42",
        [{"field": 7, "regex": r"(\d+)"}, {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"}],
    ),
    (
        "several problems at once",
        "Sector 3 Mode: reckless",
        [
            {"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int", "validate": {"min": 10}},
            {"field": "mode", "regex": r"Mode:\s*(\w+)", "validate": {"allowed_values": ["safe"]}},
            {"field": "credits", "regex": r"Credits\s+(\d+)", "type": "int", "required": True},
        ],
    ),
]


def _json_safe(value: Any) -> Any:
    """Describe what JSON cannot hold.

    A float field can legitimately extract to an infinity or a NaN, because
    Python's ``float()`` reads those words. Neither has a JSON form, so they
    are recorded as markers the test rebuilds.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value:
            return {"__float__": "nan"}
        if value == float("inf"):
            return {"__float__": "inf"}
        if value == float("-inf"):
            return {"__float__": "-inf"}
    return value


def _stringify_keys(result: Any) -> Any:
    """Record extracted keys as strings, which is what JSON makes of them.

    A config whose ``field`` is not a string still becomes a key — the
    extractor does not check — while validation skips it, because that pass
    does check. So the value is extracted and never validated, which is worth
    pinning: it is the one way a field can reach a caller unchecked.
    """
    if not isinstance(result, dict):
        return result
    return {str(key): _json_safe(value) for key, value in result.items()}


def _convert(value: str, target: str) -> dict[str, Any]:
    """One conversion, or the refusal."""
    try:
        converted = KVExtractor._convert_type(value, target)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    # NaN has no JSON form; it is described rather than written.
    if isinstance(converted, float) and converted != converted:
        return {"ok": True, "value_out": None, "is_nan": True}
    if isinstance(converted, float) and converted in (float("inf"), float("-inf")):
        return {"ok": True, "value_out": None, "is_infinite": converted > 0}
    return {"ok": True, "value_out": converted, "is_bool": isinstance(converted, bool)}


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "screen": SCROLLED,
        "single": [
            {
                "name": name,
                "screen": screen,
                "config": config,
                "result": _stringify_keys(KVExtractor.extract(screen, config, run_validation=False)),
            }
            for name, screen, config in SINGLE_CASES
        ],
        "convert": [
            {"name": name, "value": value, "type": target, **_convert(value, target)}
            for name, value, target in CONVERT_CASES
        ],
        "extract": [
            {
                "name": name,
                "screen": screen,
                "config": config,
                "result": _stringify_keys(KVExtractor.extract(screen, config)),
            }
            for name, screen, config in EXTRACT_CASES
        ],
        "unvalidated": [
            {"name": name, "result": _stringify_keys(KVExtractor.extract(screen, config, run_validation=False))}
            for name, screen, config in EXTRACT_CASES
        ],
        "convenience_matches_extract": extract_kv(
            SCROLLED, [{"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"}]
        )
        == KVExtractor.extract(SCROLLED, [{"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"}]),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(SINGLE_CASES)} single, {len(CONVERT_CASES)} convert, {len(EXTRACT_CASES)} extract)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
