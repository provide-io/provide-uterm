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

The third part is the *sequence*, driven rather than described: every close,
sleep, connect and hook call the real `ReconnectingSession` makes, in order.
A retry that reconnects before closing the dead socket returns the same value
and leaves a file descriptor behind, so the log is the only thing that can
tell the two apart.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_reconnect_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.transports import reconnect as rc
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


class RecordingSession:
    """A transport session recording what was asked of it, failing on cue."""

    def __init__(self, log: list[Any], name: str, failures: list[str]) -> None:
        self._log = log
        self._name = name
        # One entry per send: the fault to raise, or "ok" to succeed.
        self._failures = list(failures)
        self._sends = 0

    def is_connected(self) -> bool:
        return True

    async def close(self) -> None:
        self._log.append(["close", self._name])

    async def send(self, data: str) -> None:
        index = self._sends
        self._sends += 1
        failure = self._failures[index] if index < len(self._failures) else "ok"
        self._log.append(["send", self._name, failure])
        if failure == "connection":
            raise ConnectionError("socket went away")
        if failure == "os":
            raise OSError("host unreachable")
        if failure == "value":
            raise ValueError("the caller's own bug")


class UncloseableSession(RecordingSession):
    """A session whose close fails, as one whose socket is already gone will."""

    async def close(self) -> None:
        self._log.append(["close", self._name, "raised"])
        raise OSError("already gone")


async def _drive(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case against the real reconnecting session, recording each step."""
    log: list[Any] = []
    connect_failures: list[str] = list(case.get("connect_failures", []))
    attempts = {"n": 0}
    session_type = UncloseableSession if case.get("close_fails") else RecordingSession

    async def connect() -> Any:
        index = attempts["n"]
        attempts["n"] += 1
        failure = connect_failures[index] if index < len(connect_failures) else "ok"
        log.append(["connect", failure])
        if failure != "ok":
            raise ConnectionError(failure)
        return session_type(log, f"session-{index}", case.get("send_failures", {}).get(str(index), []))

    async def on_reconnect(session: Any) -> None:
        log.append(["hook", session._name])

    async def recording_sleep(delay: float) -> None:
        # Recorded rather than slept, which is also what keeps this corpus
        # reproducible.
        log.append(["sleep", delay])

    policy = ReconnectPolicy(
        max_retries=case["max_retries"],
        base_backoff_s=case["base_backoff_s"],
        max_backoff_s=case["max_backoff_s"],
    )

    real_sleep = asyncio.sleep
    asyncio.sleep = recording_sleep  # type: ignore[assignment]
    try:
        try:
            session = await rc.connect_with_reconnect(
                connect,
                policy=policy,
                on_reconnect=on_reconnect if case.get("hook", True) else None,
            )
            await session.send("hello")
            outcome: dict[str, Any] = {"error": None, "message": None}
        except Exception as exc:
            outcome = {"error": type(exc).__name__, "message": str(exc)}
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    return {"name": case["name"], "log": log, **outcome}


# Each case connects, then sends once; the failures decide what happens next.
SEQUENCE_CASES: list[dict[str, Any]] = [
    {"name": "a send that works", "max_retries": 5, "base_backoff_s": 0.5, "max_backoff_s": 30.0},
    {
        "name": "a socket that drops once",
        "max_retries": 5,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "send_failures": {"0": ["connection"]},
    },
    {
        "name": "a socket that drops on an OSError",
        "max_retries": 5,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "send_failures": {"0": ["os"]},
    },
    {
        "name": "the caller's own bug",
        "max_retries": 5,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "send_failures": {"0": ["value"]},
    },
    {
        "name": "a socket that drops twice",
        "max_retries": 5,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "send_failures": {"0": ["connection"], "1": ["connection"]},
    },
    {
        "name": "a session that never stays up",
        "max_retries": 2,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "send_failures": {str(index): ["connection"] for index in range(6)},
    },
    {
        "name": "a server that is hard down",
        "max_retries": 2,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "connect_failures": ["refused"] * 6,
    },
    {
        "name": "a server down for the first attempt only",
        "max_retries": 2,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "connect_failures": ["refused"],
    },
    {
        "name": "a drop, then a server that will not come back",
        "max_retries": 1,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "connect_failures": ["ok", "refused", "refused", "refused"],
        "send_failures": {"0": ["connection"]},
    },
    {
        "name": "no retries allowed at all",
        "max_retries": 0,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "send_failures": {"0": ["connection"]},
    },
    {
        "name": "no backoff configured",
        "max_retries": 5,
        "base_backoff_s": 0.0,
        "max_backoff_s": 30.0,
        "send_failures": {"0": ["connection"]},
    },
    {
        "name": "a ceiling below the base",
        "max_retries": 3,
        "base_backoff_s": 4.0,
        "max_backoff_s": 1.0,
        "send_failures": {"0": ["connection"], "1": ["connection"], "2": ["connection"]},
    },
    {
        "name": "a session whose close fails on the way out",
        "max_retries": 5,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "send_failures": {"0": ["connection"]},
        "close_fails": True,
    },
    {
        "name": "a drop with nobody watching for the reconnect",
        "max_retries": 5,
        "base_backoff_s": 0.5,
        "max_backoff_s": 30.0,
        "send_failures": {"0": ["connection"]},
        "hook": False,
    },
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
        "sequences": asyncio.run(_sequences()),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(schedules)} policy cases)")
    return 0


async def _sequences() -> list[dict[str, Any]]:
    """Drive every sequence case in order."""
    return [await _drive(case) for case in SEQUENCE_CASES]


if __name__ == "__main__":
    raise SystemExit(main())
