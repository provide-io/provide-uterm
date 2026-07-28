#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript hijackable port.

``HijackableMixin`` is what makes an automated worker pausable by a human. The
operator's dashboard can hold the loop at a checkpoint, let it through one
iteration at a time, and hand it back — so the arithmetic of *step tokens* is
the interesting part, and it is bounded on purpose: an unbounded accumulation
from a client hammering the step button would let the loop run away the moment
the hijack is released.

Two token boundaries are recorded. Steps only accumulate while hijacked, and
they cap at 100 no matter how many are requested. A negative request adds
nothing rather than removing credit a previous request granted.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_hijackable_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.bridge.base import HijackableMixin

OUT = Path(__file__).with_name("hijackable_golden.json")

# (name, hijacked before the request, requests to make in order)
STEP_CASES: list[tuple[str, bool, list[int]]] = [
    ("not hijacked", False, [2]),
    ("default request", True, []),
    ("single step", True, [1]),
    ("one iteration", True, [2]),
    ("several requests accumulate", True, [2, 2, 2]),
    ("zero adds nothing", True, [0]),
    ("negative adds nothing", True, [-5]),
    ("negative after a grant keeps the grant", True, [2, -5]),
    ("exactly at the cap", True, [100]),
    ("one past the cap", True, [101]),
    ("accumulating past the cap", True, [60, 60]),
    ("far past the cap", True, [10_000]),
    ("fractional request", True, [2]),
]


class _Worker(HijackableMixin):
    """A bare worker; the mixin supplies everything under test."""


async def _step_record() -> list[dict[str, Any]]:
    """How many checkpoints each sequence of requests buys."""
    records = []
    for name, hijacked, requests in STEP_CASES:
        worker = _Worker()
        await worker.set_hijacked(hijacked)
        if not requests:
            await worker.request_step()
        for amount in requests:
            await worker.request_step(amount)
        records.append(
            {
                "name": name,
                "hijacked": hijacked,
                "requests": requests,
                "tokens": worker._hijack_step_tokens,
            }
        )
    return records


async def _gate_record() -> dict[str, Any]:
    """What the checkpoint does in each state, and what a step consumes."""
    idle = _Worker()
    # Not hijacked: the checkpoint is a no-op and returns at once.
    await asyncio.wait_for(idle.await_if_hijacked(), timeout=1)

    stepping = _Worker()
    await stepping.set_hijacked(True)
    await stepping.request_step(2)
    await asyncio.wait_for(stepping.await_if_hijacked(), timeout=1)
    after_one = stepping._hijack_step_tokens
    await asyncio.wait_for(stepping.await_if_hijacked(), timeout=1)
    after_two = stepping._hijack_step_tokens

    # A third pass has no token left and must block.
    blocked = False
    try:
        await asyncio.wait_for(stepping.await_if_hijacked(), timeout=0.05)
    except TimeoutError:
        blocked = True

    # Enabling a hijack discards tokens granted for a previous one.
    discarding = _Worker()
    await discarding.set_hijacked(True)
    await discarding.request_step(4)
    await discarding.set_hijacked(False)
    tokens_after_resume = discarding._hijack_step_tokens
    await discarding.set_hijacked(True)
    tokens_after_rehijack = discarding._hijack_step_tokens

    return {
        "after_one_step": after_one,
        "after_two_steps": after_two,
        "blocks_without_tokens": blocked,
        "tokens_after_resume": tokens_after_resume,
        "tokens_after_rehijack": tokens_after_rehijack,
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_hijackable_golden.py",
        "step_token_cap": 100,
        "default_checkpoints": 2,
        "steps": asyncio.run(_step_record()),
        "gate": asyncio.run(_gate_record()),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['steps'])} step cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
