# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
TermHub stress script for memray profiling.

Simulates 200 workers each with 50 browsers: register, broadcast events, deregister.
Run via: python -m memray run -o hub_stress.bin scripts/memray_hub_stress.py
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from provide.terminal.hijack.hub import TermHub

NUM_WORKERS = 200
BROWSERS_PER_WORKER = 50
EVENTS_PER_WORKER = 10


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


async def run() -> None:
    hub = TermHub()

    for i in range(NUM_WORKERS):
        worker_id = f"worker-{i}"
        worker_ws = _make_ws()

        await hub.register_worker(worker_id, worker_ws)

        browsers = [_make_ws() for _ in range(BROWSERS_PER_WORKER)]
        roles = ["viewer", "operator", "admin"]
        for j, bws in enumerate(browsers):
            await hub.register_browser(worker_id, bws, roles[j % 3])

        for k in range(EVENTS_PER_WORKER):
            await hub.append_event(
                worker_id,
                "snapshot",
                {
                    "screen": f"output line {k}\n" * 24,
                    "screen_hash": f"hash-{k}",
                    "prompt_detected": k % 3 == 0,
                },
            )

        await hub.broadcast(worker_id, {"type": "term", "data": "x" * 80})

        await hub.deregister_worker(worker_id, worker_ws)


if __name__ == "__main__":
    asyncio.run(run())
