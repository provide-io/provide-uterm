#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript reconnect port.

A transport that drops mid-session should come back without the caller
noticing, but the retry budget is what stops "come back" turning into a
client hammering a server that is down.

The backoff is exponential from a one-based attempt number and bounded, so a
long outage settles at a steady rate rather than drifting towards never
retrying. The schedule is recorded across and beyond the point where it
saturates, because both the growth and the ceiling matter — and the one-based
indexing is easy to get wrong by a factor of two.

Which errors are retryable is the other half. A connection fault is worth
retrying; a protocol or programming error is not, and retrying it just delays
the report by the whole budget.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_reconnect_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.transports.reconnect import ReconnectPolicy, _policy_delay

OUT = Path(__file__).with_name("reconnect_golden.json")

# (name, policy overrides)
POLICY_CASES: list[tuple[str, dict[str, Any]]] = [
    ("defaults", {}),
    ("fast", {"base_backoff_s": 0.1, "max_backoff_s": 1.0}),
    ("no backoff", {"base_backoff_s": 0.0}),
    ("immediate ceiling", {"base_backoff_s": 5.0, "max_backoff_s": 5.0}),
    ("single retry", {"max_retries": 1}),
    ("no retries", {"max_retries": 0}),
]


def main() -> int:
    """Write the golden corpus and report the case count."""
    default = ReconnectPolicy()
    schedules = []
    for name, overrides in POLICY_CASES:
        policy = ReconnectPolicy(**overrides)
        schedules.append(
            {
                "name": name,
                "max_retries": policy.max_retries,
                "base_backoff_s": policy.base_backoff_s,
                "max_backoff_s": policy.max_backoff_s,
                # Attempt zero is included because the helper accepts it and
                # clamps the exponent rather than producing a fraction.
                "delays": [_policy_delay(policy, attempt) for attempt in range(12)],
            }
        )

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_reconnect_golden.py",
        "defaults": {
            "max_retries": default.max_retries,
            "base_backoff_s": default.base_backoff_s,
            "max_backoff_s": default.max_backoff_s,
        },
        "schedules": schedules,
        "retryable": ["ConnectionError", "OSError", "websockets.ConnectionClosed"],
        "not_retryable": ["ValueError", "TypeError", "KeyError", "RuntimeError"],
        "exhausted_message": "reconnect retries exhausted",
        "connect_exhausted_message": "connect retries exhausted",
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(schedules)} policy cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
