#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import time

import pytest

from provide.uterm.server.bridge.hub.core import TermHub


@pytest.mark.asyncio
async def test_heuristics_cps_calculation():
    hub = TermHub()
    ws = "fake_ws"

    # Simulate typing 10 characters over 1 second (10 CPS)
    now = time.monotonic()
    for i in range(11):
        # Manually inject timestamps to avoid sleeping in test
        if ws not in hub.router.keystroke_timestamps:
            from collections import deque

            hub.router.keystroke_timestamps[ws] = deque(maxlen=50)
        hub.router.keystroke_timestamps[ws].append(now + (i * 0.1))

    h = hub._get_heuristics(ws)
    assert 9.9 <= h["cps"] <= 10.1
    # Jitter should be 0 because timing is perfectly uniform
    assert h["jitter"] < 1e-9


@pytest.mark.asyncio
async def test_heuristics_jitter_high_variance():
    hub = TermHub()
    ws = "fake_ws"

    now = time.monotonic()
    # Intervals: 0.1, 0.5, 0.1, 0.5 ... (High jitter)
    ts = now
    for i in range(10):
        if ws not in hub.router.keystroke_timestamps:
            from collections import deque

            hub.router.keystroke_timestamps[ws] = deque(maxlen=50)
        hub.router.keystroke_timestamps[ws].append(ts)
        ts += 0.1 if i % 2 == 0 else 0.5

    h = hub._get_heuristics(ws)
    assert h["jitter"] > 0.04  # Expected variance is roughly 0.04 for (0.1, 0.5)


@pytest.mark.asyncio
async def test_heuristics_cleanup_on_disconnect():
    hub = TermHub()
    ws = "fake_ws"

    hub._record_keystroke(ws)
    assert ws in hub.router.keystroke_timestamps

    # cleanup_browser_disconnect is async in some mixins but we can call it
    # We need a worker_id but it does not matter for our mock
    await hub.cleanup_browser_disconnect("any", ws, False)
    assert ws not in hub.router.keystroke_timestamps
