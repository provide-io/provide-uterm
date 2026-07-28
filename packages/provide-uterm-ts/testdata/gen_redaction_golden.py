#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``redaction`` port.

Every pattern in the corpus is written in the subset of regex syntax that
CPython's ``re`` and ECMAScript agree on, because the redaction patterns are
operator-supplied configuration and both ports must resolve them the same
way.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_redaction_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.redaction import make_redactor, redact_text

OUT = Path(__file__).with_name("redaction_golden.json")

# (patterns, text) pairs. An empty pattern list must yield the identity
# redactor, which is a distinct code path from "patterns that match nothing".
CASES: list[tuple[list[str], str]] = [
    ([], "nothing is configured"),
    ([], ""),
    ([r"secret"], "the secret value"),
    ([r"secret"], "no match here"),
    ([r"secret"], "secret secret secret"),
    ([r"secret"], "secretsecret"),
    # Anchors and word boundaries.
    ([r"^secret"], "secret at the start, secret in the middle"),
    ([r"secret$"], "ends with secret"),
    ([r"\bsecret\b"], "secret secretive"),
    # Character classes and quantifiers.
    ([r"\d+"], "port 8780 and port 2102"),
    ([r"[A-Z]{3,}"], "an ERROR and a WARNING and Ok"),
    ([r"a*"], "aaa b"),
    # Alternation and grouping.
    ([r"(password|token)=\S+"], "password=hunter2 token=abc123 user=tim"),
    ([r"(?:AKIA)[0-9A-Z]{16}"], "key AKIA0123456789ABCDEF here"),
    # Leading inline flags. CPython accepts these only at the start of the
    # pattern; ECMAScript has no inline-flag syntax at all, so the port
    # translates a leading group into RegExp flags.
    ([r"(?i)secret"], "Secret SECRET secret"),
    ([r"(?m)^line"], "line one\nline two"),
    ([r"(?s)a.b"], "a\nb"),
    ([r"(?im)^secret$"], "SECRET\nsecret"),
    # Multiple patterns apply in order, and a later pattern sees the output
    # of the earlier one — including the literal replacement text.
    ([r"foo", r"bar"], "foo bar baz"),
    ([r"\[REDACTED\]", r"foo"], "foo bar"),
    ([r"foo", r"\[REDACTED\]"], "foo bar"),
    # Overlapping patterns.
    ([r"ab", r"bc"], "abc"),
    # A pattern that can match the empty string.
    ([r"x?"], "axb"),
    # Escapes and metacharacters.
    ([r"\."], "a.b.c"),
    ([r"\$\d+\.\d{2}"], "total $42.00 due"),
    ([r"\s+"], "a  b\tc"),
    # Non-ASCII input passes through the scan.
    ([r"x[eé]"], "xé and xe"),
    # Realistic credential shapes.
    ([r"Bearer [A-Za-z0-9._-]+"], "Authorization: Bearer eyJhbGc.iOi-JIUz_I1"),
    ([r"[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{4}"], "card 4111-1111-1111-1111 ok"),
    # Empty subject.
    ([r"secret"], ""),
]

# Patterns whose meaning genuinely differs between CPython's ``re`` and the
# host regex engine, recorded so the divergence is visible rather than
# discovered in production. The other ports have the same boundary: Go's RE2
# and ECMAScript both read ``\d`` / ``\w`` as ASCII-only, while CPython reads
# them as Unicode-aware for ``str`` subjects. Patterns are handed to the host
# engine unchanged, exactly as the Go and C# ports do.
DIALECT_CASES: list[tuple[list[str], str]] = [
    ([r"x\w"], "xé and xe"),
    ([r"\d+"], "42 and ٤٢"),
    ([r"\w+"], "aïb"),
]


def main() -> int:
    """Write the golden corpus and report the record count."""
    records = []
    for patterns, text in CASES:
        redactor = make_redactor(patterns)
        records.append(
            {
                "patterns": patterns,
                "text": text,
                "out": redactor(text),
                # redact_text(None) must be identity, independent of patterns.
                "identity": redact_text(text, None),
            }
        )
    dialect_records = [
        {"patterns": patterns, "text": text, "out": make_redactor(patterns)(text)} for (patterns, text) in DIALECT_CASES
    ]
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_redaction_golden.py",
        "cases": records,
        "dialect_divergences": dialect_records,
    }
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(records)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
