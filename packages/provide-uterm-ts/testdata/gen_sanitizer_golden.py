#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``sanitizer`` port.

Runs the CPython reference implementation over a deterministic corpus of
inputs and records the outputs. The TypeScript tests replay the same inputs
and must match byte-for-byte.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_sanitizer_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.sanitizer import prepare_keystrokes, sanitize_keystrokes, unescape_keys

OUT = Path(__file__).with_name("sanitizer_golden.json")


def _unescape_inputs() -> list[str]:
    """Deterministic corpus for ``unescape_keys``."""
    return [
        "",
        "plain",
        # Every simple escape in the table.
        "\\n",
        "\\r",
        "\\t",
        "\\e",
        "\\0",
        "\\\\",
        "\\'",
        '\\"',
        # All of them at once, plus surrounding literal text.
        "a\\nb\\rc\\td\\ee\\0f\\\\g",
        # Hex and unicode forms, upper and lower case digits.
        "\\x1b",
        "\\x1B",
        "\\x00",
        "\\xff",
        "\\u001b",
        "\\u0041",
        "\\uFFFF",
        "\\u00e9",
        # Malformed / partial escapes must survive verbatim.
        "\\x",
        "\\x1",
        "\\xzz",
        "\\u",
        "\\u12",
        "\\u123",
        "\\uzzzz",
        # An unknown single-character escape is returned as written.
        "\\q",
        "\\1",
        "\\ ",
        # A trailing lone backslash has nothing to consume.
        "\\",
        "abc\\",
        # DOTALL: a backslash before a newline escapes the newline itself.
        "\\\n",
        # Escaped backslash must not let the next character escape.
        "\\\\n",
        "\\\\x41",
        # Consecutive escapes.
        "\\x41\\x42\\x43",
        "\\n\\n\\n",
        # Escapes embedded in longer text.
        "ls -la\\r",
        'echo \\"quoted\\"\\r',
        # Non-ASCII passes through untouched.
        "café",
    ]


def _sanitize_inputs() -> list[tuple[str, int]]:
    """Deterministic (input, max_bytes) corpus for ``sanitize_keystrokes``."""
    control_sweep = "".join(chr(c) for c in range(0x20))
    high_sweep = "".join(chr(c) for c in range(0x7F, 0xA1))
    return [
        ("", 4096),
        ("hello world", 4096),
        # Every C0 control: only \t \n \r \x0b \x0c \x03 \x1b survive.
        (control_sweep, 4096),
        # DEL and the C1 range are dropped.
        (high_sweep, 4096),
        # Printable ASCII survives in full.
        ("".join(chr(c) for c in range(0x20, 0x7F)), 4096),
        # Non-ASCII is not in string.printable and is dropped.
        ("café", 4096),
        ("你好", 4096),
        ("aéb", 4096),
        # Individually allowed controls.
        ("a\rb\nc\td", 4096),
        ("\x03", 4096),
        ("\x1b[A", 4096),
        ("\x0b\x0c", 4096),
        # Truncation boundaries: at, one under, and one over the limit.
        ("abcdefghij", 10),
        ("abcdefghij", 9),
        ("abcdefghij", 11),
        ("abcdefghij", 0),
        ("abcdefghij", 1),
        # Truncation counts bytes after filtering, not before.
        ("aébcdefghijk", 5),
        ("\x00\x00abcde", 3),
    ]


def _prepare_inputs() -> list[tuple[str, int]]:
    """Deterministic (raw, max_bytes) corpus for ``prepare_keystrokes``."""
    return [
        ("", 4096),
        ("ls -la\\r", 4096),
        # An unescaped control is dropped by the sanitize stage.
        ("\\x07bell", 4096),
        # An escape that produces an allowed control survives.
        ("\\x1b[A", 4096),
        ("\\x03", 4096),
        # An escape that produces a non-ASCII character is dropped.
        ("\\u00e9", 4096),
        ("\\u4f60", 4096),
        # Unescaping happens before the byte budget is applied.
        ("\\x41\\x42\\x43", 2),
        ("abc\\ndef", 4),
        ("\\\\n", 4096),
    ]


def main() -> int:
    """Write the golden corpus and report the record count."""
    unescape_records = [{"raw": raw, "out": unescape_keys(raw)} for raw in _unescape_inputs()]
    sanitize_records = [
        {"keys": keys, "max_bytes": max_bytes, "out": sanitize_keystrokes(keys, max_bytes=max_bytes)}
        for (keys, max_bytes) in _sanitize_inputs()
    ]
    prepare_records = [
        {"raw": raw, "max_bytes": max_bytes, "out": prepare_keystrokes(raw, max_bytes=max_bytes)}
        for (raw, max_bytes) in _prepare_inputs()
    ]
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_sanitizer_golden.py",
        "unescape": unescape_records,
        "sanitize": sanitize_records,
        "prepare": prepare_records,
    }
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    total = len(unescape_records) + len(sanitize_records) + len(prepare_records)
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
