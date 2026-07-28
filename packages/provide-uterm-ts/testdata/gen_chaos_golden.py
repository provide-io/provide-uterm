#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript chaos transport.

The chaos wrapper exists so resilience can be tested against a *schedule*
rather than against luck. What has to match across the ports is therefore the
schedule — which receive is the one that drops, which returns empty, and what
the injected error says — not the jitter, which the Go port already draws from
its own generator.

The corpus is recorded by driving the real ``ChaosTransport`` over a fake
inner transport, so the counting (one-based, and counted before any fault is
decided) is pinned as the reference does it rather than as it reads.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_chaos_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.transports.base import ConnectionTransport
from provide.uterm.transports.chaos import ChaosTransport

OUT = Path(__file__).with_name("chaos_golden.json")

# (name, kwargs) — every combination that changes the schedule.
CONFIG_CASES: list[tuple[str, dict[str, Any]]] = [
    ("no faults", {}),
    ("disconnect every third", {"disconnect_every_n_receives": 3}),
    ("timeout every second", {"timeout_every_n_receives": 2}),
    ("disconnect every one", {"disconnect_every_n_receives": 1}),
    ("both, disconnect wins the tie", {"disconnect_every_n_receives": 2, "timeout_every_n_receives": 2}),
    ("both, out of phase", {"disconnect_every_n_receives": 3, "timeout_every_n_receives": 2}),
    ("a custom label", {"disconnect_every_n_receives": 2, "label": "flaky-bbs"}),
    ("an empty label falls back", {"disconnect_every_n_receives": 2, "label": ""}),
]

RECEIVES = 7


class FakeInner(ConnectionTransport):
    """Records what the wrapper delegated, and returns predictable reads."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.connected = False
        self.reads = 0

    async def connect(self, host: str, port: int, **kwargs: Any) -> None:
        """Record the connect and come up."""
        self.calls.append(["connect", host, port, sorted(kwargs)])
        self.connected = True

    async def disconnect(self) -> None:
        """Record the disconnect and go down."""
        self.calls.append(["disconnect"])
        self.connected = False

    async def send(self, data: bytes) -> None:
        """Record the send."""
        self.calls.append(["send", list(data)])

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        """Return a read that identifies itself, so a swap would show."""
        self.reads += 1
        self.calls.append(["receive", max_bytes, timeout_ms])
        return f"read-{self.reads}".encode()

    def is_connected(self) -> bool:
        """Report the inner state, which is what the wrapper must surface."""
        return self.connected


async def _record_schedule(name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drive `RECEIVES` reads and record what each one did."""
    inner = FakeInner()
    chaos = ChaosTransport(inner, **kwargs)
    await chaos.connect("h", 1)
    outcomes = []
    for _ in range(RECEIVES):
        try:
            data = await chaos.receive(4096, 250)
        except ConnectionError as exc:
            outcomes.append({"kind": "disconnect", "message": str(exc), "connected_after": chaos.is_connected()})
        else:
            outcomes.append({"kind": "data" if data else "timeout", "data": data.decode()})
    return {"name": name, "outcomes": outcomes}


async def _record_delegation() -> dict[str, Any]:
    """Record that everything but `receive` is a straight pass-through."""
    inner = FakeInner()
    chaos = ChaosTransport(inner)
    before = chaos.is_connected()
    await chaos.connect("bbs.example.org", 2323, origin="https://app.example.org")
    await chaos.send(b"ls\r")
    after_connect = chaos.is_connected()
    await chaos.disconnect()
    return {
        "calls": inner.calls,
        "connected_before": before,
        "connected_after_connect": after_connect,
        "connected_after_disconnect": chaos.is_connected(),
    }


async def _record_jitter() -> dict[str, Any]:
    """Record the delays the wrapper asks for, with the generator stood aside.

    Only the *shape* is pinned — a jitter within the configured bound before
    every read, and none at all when the bound is zero. The value itself comes
    from the language's own generator, as it already does in the Go port.
    """
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float, *args: Any, **kwargs: Any) -> Any:
        slept.append(delay)
        return await real_sleep(0, *args, **kwargs)

    asyncio.sleep = recording_sleep  # type: ignore[assignment]
    try:
        jittered = ChaosTransport(FakeInner(), max_jitter_ms=50)
        await jittered.connect("h", 1)
        for _ in range(4):
            await jittered.receive(4096, 250)
        with_jitter = list(slept)

        slept.clear()
        plain = ChaosTransport(FakeInner())
        await plain.connect("h", 1)
        for _ in range(4):
            await plain.receive(4096, 250)
        without_jitter = list(slept)

        # An injected timeout waits out the caller's own budget before
        # returning empty, so a test sees the delay a real dead link would
        # have cost it.
        slept.clear()
        timing_out = ChaosTransport(FakeInner(), timeout_every_n_receives=1)
        await timing_out.connect("h", 1)
        await timing_out.receive(4096, 250)
        timeout_delays = list(slept)
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    return {
        "jittered_count": len(with_jitter),
        "jitter_bound_s": 50 / 1000.0,
        "jitter_within_bound": all(0.0 <= value <= 50 / 1000.0 for value in with_jitter),
        "unjittered_count": len(without_jitter),
        "timeout_delays_s": timeout_delays,
    }


async def _main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "receives": RECEIVES,
        "schedules": [await _record_schedule(name, kwargs) for name, kwargs in CONFIG_CASES],
        "delegation": await _record_delegation(),
        "jitter": await _record_jitter(),
        "defaults": {
            "seed": 1,
            "label": "chaos",
            "disconnect_every_n_receives": 0,
            "timeout_every_n_receives": 0,
            "max_jitter_ms": 0,
        },
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['schedules'])} schedules)")
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
