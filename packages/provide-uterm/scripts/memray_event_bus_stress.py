# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
EventBus fanout stress script for memray profiling.

Drives the EventBus hot path (``_enqueue`` -> ``_deliver`` -> per-subscriber
``put_nowait``) with a large number of subscribers and events. Each event is
fanned out to every subscription queue, so total enqueue work scales as
``NUM_SUBSCRIBERS * NUM_EVENTS``.

Workload: 100 subscribers x 10_000 events on a single worker.
Run via: python -m memray run -o event_bus_stress.bin scripts/memray_event_bus_stress.py
"""

from __future__ import annotations

import asyncio
import contextlib

from provide.uterm.bridge.hub.event_bus import EventBus

WORKER_ID = "worker-stress"
NUM_SUBSCRIBERS = 100
NUM_EVENTS = 10_000


async def _drain(sub: object) -> None:
    """Drain a subscription queue until cancelled — simulates a live consumer."""
    queue = sub.queue  # type: ignore[attr-defined]
    with contextlib.suppress(asyncio.CancelledError):
        while True:
            item = await queue.get()
            if item is None:  # pragma: no cover — sentinel never sent in this stress
                return


async def main() -> None:
    # Generous queue depth so we measure the fast-path put_nowait, not the
    # drop-oldest ring-buffer fallback.
    bus = EventBus(max_queue_depth=NUM_EVENTS + 16, max_subscribers_per_worker=NUM_SUBSCRIBERS + 16)

    async with contextlib.AsyncExitStack() as stack:
        consumers: list[asyncio.Task[None]] = []
        for _ in range(NUM_SUBSCRIBERS):
            sub = await stack.enter_async_context(bus.watch(WORKER_ID))
            consumers.append(asyncio.create_task(_drain(sub)))

        # Hot path: each call fans out to every subscriber queue.
        for i in range(NUM_EVENTS):
            event = {
                "type": "snapshot",
                "data": {
                    "screen": f"output line {i}\n" * 4,
                    "screen_hash": f"hash-{i}",
                    "prompt_detected": i % 3 == 0,
                },
            }
            bus._enqueue(WORKER_ID, event)
            # Let the loop schedule consumer drains so queues don't grow
            # unboundedly. Yielding every 100 events balances allocation
            # determinism with consumer progress.
            if i % 100 == 0:
                await asyncio.sleep(0)

        # Stop consumers cleanly.
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
