#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fail when a C# test matches none of the Makefile's TEST_BATCH filters.

`make test` and `make cover` never run the whole test project: they run one
`dotnet test --filter` per TEST_BATCH_* variable (a fresh host per batch, see
the Makefile). A test whose fully qualified name contains none of the filter
substrings is therefore built, listed, and never executed -- by the local gate
or by CI -- and it says nothing when it starts failing.

That is not hypothetical. SharedCorpusParityTests, which holds every C# corpus
copy to its TypeScript source, matched no filter; six copies drifted under it
unnoticed. Six more classes (19 tests) were in the same state when this check
was written. A new class is the usual way in, so this runs on every gate.

Usage:
    dotnet test -c Release --no-build --list-tests | python3 ci/check_test_batches.py Makefile
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BATCH = re.compile(r"^TEST_BATCH_\w+\s*:=\s*(.+)$", re.MULTILINE)
_LISTED = "The following Tests are available:"


def batch_substrings(makefile: str) -> list[str]:
    """Every `FullyQualifiedName~<substring>` across the TEST_BATCH_* filters."""
    substrings: list[str] = []
    for line in _BATCH.findall(makefile):
        for clause in line.split("|"):
            name, _, value = clause.strip().partition("~")
            if name != "FullyQualifiedName" or not value:
                raise ValueError(f"unsupported filter clause {clause!r}")
            substrings.append(value)
    return substrings


def listed_tests(output: str) -> list[str]:
    """The test names `dotnet test --list-tests` printed, parameters stripped."""
    if _LISTED not in output:
        raise ValueError("no test listing found in the input")
    names = output.split(_LISTED, 1)[1].splitlines()
    return [name.strip().split("(", 1)[0] for name in names if name.strip()]


def unbatched(tests: list[str], substrings: list[str]) -> dict[str, int]:
    """Test classes (with their test counts) that no filter selects."""
    missed: dict[str, int] = {}
    for test in tests:
        if not any(substring in test for substring in substrings):
            cls = test.rsplit(".", 1)[0]
            missed[cls] = missed.get(cls, 0) + 1
    return missed


def main(argv: list[str]) -> int:
    makefile = Path(argv[1] if len(argv) > 1 else "Makefile").read_text(encoding="utf-8")
    substrings = batch_substrings(makefile)
    tests = listed_tests(sys.stdin.read())
    if not tests or not substrings:
        print("test-batch check: nothing to compare -- refusing to pass vacuously", file=sys.stderr)
        return 1
    missed = unbatched(tests, substrings)
    if missed:
        print("test-batch check FAILED: these tests match no TEST_BATCH_* filter and never run:", file=sys.stderr)
        for cls, count in sorted(missed.items()):
            print(f"  {cls} ({count} test(s))", file=sys.stderr)
        print("Add a FullyQualifiedName~<ClassName> clause to a TEST_BATCH_* in the Makefile.", file=sys.stderr)
        return 1
    print(f"test-batch check: all {len(tests)} tests are selected by a TEST_BATCH_* filter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
