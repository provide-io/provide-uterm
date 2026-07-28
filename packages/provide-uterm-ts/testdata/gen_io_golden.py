#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript io port.

``InputSender`` decides what actually reaches the far end for a given prompt
type, and the differences are exactly the ones that break an automation
quietly rather than loudly:

* ``single_key`` sends the keys bare — appending a newline to a menu that
  wanted one keypress submits an extra blank line;
* ``any_key`` sends a *space* and ignores whatever the caller passed, because
  "press any key" prompts want a keypress, not the text;
* anything unrecognised is treated as ``multi_key`` and gets a carriage
  return, so a typo in the prompt type still submits rather than hanging.

The prompt-wait defaults are recorded alongside, since they are the timings
an automation inherits when it asks for nothing in particular.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_io_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.io import (
    DEFAULT_INPUT_TYPE,
    DEFAULT_PROMPT_IDLE_GRACE_RATIO,
    DEFAULT_PROMPT_READ_INTERVAL_MS,
    DEFAULT_PROMPT_REQUIRE_IDLE,
    DEFAULT_PROMPT_TIMEOUT_MS,
    DEFAULT_WAIT_AFTER_SEC,
    InputSender,
)

OUT = Path(__file__).with_name("io_golden.json")

# (name, keys, input type)
SEND_CASES: list[tuple[str, str, str | None]] = [
    ("default type", "hello", None),
    ("multi key", "hello", "multi_key"),
    ("single key", "y", "single_key"),
    ("any key", "ignored", "any_key"),
    ("unknown type falls back to multi key", "hello", "menu"),
    ("empty type falls back to multi key", "hello", ""),
    ("empty keys, multi key", "", "multi_key"),
    ("empty keys, single key", "", "single_key"),
    ("empty keys, any key", "", "any_key"),
    ("keys already ending in a return", "hello\r", "multi_key"),
    ("keys containing a newline", "a\nb", "multi_key"),
    ("single key with several characters", "yes", "single_key"),
]


class _Session:
    """A session that records what it was sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def snapshot(self) -> dict[str, Any]:
        return {}

    async def wait_for_update(self, *, timeout_ms: int, since: int | None = None) -> bool:
        return False


async def _send_record() -> list[dict[str, Any]]:
    """What reaches the far end for each prompt type."""
    records = []
    for name, keys, input_type in SEND_CASES:
        session = _Session()
        sender = InputSender(session)  # type: ignore[arg-type]
        await sender.send_input(keys, input_type, wait_after_sec=0)
        records.append({"name": name, "keys": keys, "input_type": input_type, "sent": session.sent})
    return records


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_io_golden.py",
        "defaults": {
            "prompt_timeout_ms": DEFAULT_PROMPT_TIMEOUT_MS,
            "prompt_read_interval_ms": DEFAULT_PROMPT_READ_INTERVAL_MS,
            "prompt_require_idle": DEFAULT_PROMPT_REQUIRE_IDLE,
            "prompt_idle_grace_ratio": DEFAULT_PROMPT_IDLE_GRACE_RATIO,
            "input_type": DEFAULT_INPUT_TYPE,
            "wait_after_sec": DEFAULT_WAIT_AFTER_SEC,
        },
        "sends": asyncio.run(_send_record()),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['sends'])} send cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
