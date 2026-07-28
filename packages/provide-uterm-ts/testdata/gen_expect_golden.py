#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript expect port.

``send_and_expect`` is how an automation says "type this, then wait until the
screen says that". The matching rule is small but asymmetric in ways that
matter to a caller reading the result:

* literal text is checked *before* the regex, and the returned ``matched_text``
  is the literal itself rather than whatever the regex would have captured;
* a regex returns its whole match, not a group;
* the two guards are independent — either can satisfy the wait.

The corpus records the match rule directly. The waiting behaviour around it
is driven in the port's own tests, because it is about ordering and deadlines
rather than about values.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_expect_golden.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from provide.uterm.expect import _find_match

OUT = Path(__file__).with_name("expect_golden.json")

# (name, screen, expect_text, expect_regex)
MATCH_CASES: list[tuple[str, str, str | None, str | None]] = [
    ("no guards", "anything", None, None),
    ("text present", "login: ready", "ready", None),
    ("text absent", "login: busy", "ready", None),
    ("text is the whole screen", "ready", "ready", None),
    ("empty text matches anything", "anything", "", None),
    ("empty text on an empty screen", "", "", None),
    ("text is case sensitive", "READY", "ready", None),
    ("regex matches", "exit code 0", None, r"exit code \d"),
    ("regex misses", "still running", None, r"exit code \d"),
    ("regex returns the whole match", "user@host:~$ ", None, r"(\w+)@(\w+)"),
    ("regex anchored", "ready", None, "^ready$"),
    ("regex with alternation", "FAIL", None, "OK|FAIL"),
    ("both, text wins", "ready and exit code 0", "ready", r"exit code \d"),
    ("both, only the regex holds", "exit code 0", "ready", r"exit code \d"),
    ("both, only the text holds", "ready", "ready", r"exit code \d"),
    ("both miss", "busy", "ready", r"exit code \d"),
    ("regex matching empty", "anything", None, ""),
    ("multiline screen", "line one\nready\nline three", "ready", None),
    ("regex across lines needs the flag", "one\nready", None, "^ready"),
]


def main() -> int:
    """Write the golden corpus and report the case count."""
    matches = []
    for name, screen, expect_text, expect_regex in MATCH_CASES:
        compiled = re.compile(expect_regex) if expect_regex is not None else None
        matches.append(
            {
                "name": name,
                "screen": screen,
                "expect_text": expect_text,
                "expect_regex": expect_regex,
                "matched": _find_match(screen, expect_text, compiled),
            }
        )

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_expect_golden.py",
        "default_timeout_ms": 5000,
        "matches": matches,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(matches)} match cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
