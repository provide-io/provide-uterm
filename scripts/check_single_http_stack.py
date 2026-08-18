#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fail if httpx or respx reappear anywhere in the repo.

This repo ran two HTTP stacks side by side -- httpx 0.28 pulled in by our own
extras and by respx, httpx2 2.x pulled in by mcp and starlette. They are
separate distributions with unrelated class hierarchies, so
``isinstance(httpx2_response, httpx.Response)`` is False and
``except httpx.HTTPError`` does not catch an httpx2 error. That drift is not
theoretical: when starlette moved its TestClient to httpx2, a conftest patch
still targeting httpx silently stopped applying, header-mode auth resolved
``anonymous``, and 323 server tests failed with 401 while line coverage stayed
at 100%.

The repo is now single-stack on httpx2. This guard keeps it that way, because
nothing else would notice: adding ``httpx`` back to a pyproject resolves
cleanly, imports cleanly, and only misbehaves at the seams.

respx is banned for the same reason -- it declares ``httpx>=0.25`` and
validates with ``isinstance(value, httpx.Response)``, so depending on it
reinstalls the second stack. Outbound HTTP is mocked through
``tests/helpers/http_mock.py`` instead, which intercepts at
``provide.uterm.server._http.async_client``.

Run: ``python scripts/check_single_http_stack.py``
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Paths this repo does not own or does not ship.
EXCLUDED_PREFIXES: Final[tuple[str, ...]] = (
    "packages/provide-telemetry/",  # git submodule, released independently
    "scripts/check_single_http_stack.py",  # this file names both on purpose
)

# Files that must discuss both libraries to explain why only one remains.
DOCUMENTED_EXCEPTIONS: Final[frozenset[str]] = frozenset(
    {
        "packages/provide-uterm-server/tests/helpers/http_mock.py",
        "packages/provide-uterm-server/tests/conftest_part1.py",
        "packages/provide-uterm-server/src/provide/uterm/server/_http.py",
        "CLAUDE.md",
    }
)

SCANNED_SUFFIXES: Final[tuple[str, ...]] = (".py", ".toml", ".yaml", ".yml", ".cfg", ".txt", ".ini")

# ``\b`` will not match ``httpx2``: a digit is a word character, so there is no
# boundary between the ``x`` and the ``2``.
BANNED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("httpx", re.compile(r"\bhttpx\b")),
    ("respx", re.compile(r"\brespx\b")),
)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    name: str
    text: str


def _tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return result.stdout.split()


def _should_scan(path: str) -> bool:
    if any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return False
    if path in DOCUMENTED_EXCEPTIONS:
        return False
    return path.endswith(SCANNED_SUFFIXES)


def find_violations(paths: list[str]) -> list[Violation]:
    violations: list[Violation] = []
    for path in paths:
        if not _should_scan(path):
            continue
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover — file vanished between listing and read
            continue
        for lineno, text in enumerate(content.splitlines(), start=1):
            for name, pattern in BANNED:
                if pattern.search(text):
                    violations.append(Violation(path, lineno, name, text.strip()))
    return violations


def main() -> int:
    violations = find_violations(_tracked_files())
    if not violations:
        print("single HTTP stack: no httpx/respx references outside documented exceptions")
        return 0

    print(f"error: {len(violations)} reference(s) to a banned HTTP library:\n", file=sys.stderr)
    for violation in violations:
        print(f"  {violation.path}:{violation.line}: {violation.name}: {violation.text}", file=sys.stderr)
    print(
        "\nThis repo is single-stack on httpx2. Two stacks silently break "
        "isinstance/except across the seam (see the 401 regression in this "
        "script's docstring). Use httpx2, and mock outbound HTTP with "
        "tests/helpers/http_mock.py rather than respx.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
