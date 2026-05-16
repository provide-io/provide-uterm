#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from provide.uterm.bridge.hub.core import TermHub
from provide.uterm.bridge.models import WorkerTermState


@pytest.mark.asyncio
async def test_hub_drops_input_immediately_after_lease_expiry():
    # Set a very short lease for testing
    hub = TermHub()
    worker_id = "w1"
    hijack_id = "h1"

    # Mock a worker state
    hub._workers[worker_id] = WorkerTermState(
        input_mode="hijack",
        worker_ws=AsyncMock()
    )

    # 1. Acquire REST lease
    now = time.monotonic()
    success, err = await hub.try_acquire_rest_hijack(
        worker_id, owner="admin", lease_s=1, hijack_id=hijack_id, now=now
    )
    assert success is True

    # 2. Wait for expiration
    await asyncio.sleep(1.1)

    # 3. Verify owner is cleared after cleanup
    await hub.cleanup_expired_hijack(worker_id)

    async with hub._lock:
        st = hub._workers.get(worker_id)
        assert st.hijack_session is None

@pytest.mark.asyncio
async def test_hub_rejects_heartbeat_from_wrong_principal():
    hub = TermHub()
    worker_id = "w2"
    hijack_id = "h2"

    # Mock a worker state
    hub._workers[worker_id] = WorkerTermState(
        input_mode="hijack",
        worker_ws=AsyncMock()
    )

    # 1. Admin acquires lease
    now = time.monotonic()
    await hub.try_acquire_rest_hijack(
        worker_id, owner="admin", lease_s=60, hijack_id=hijack_id, now=now
    )

    # 2. Operator attempts heartbeat for the same hijack_id
    success = await hub.extend_hijack_lease(
        worker_id, hijack_id, owner="operator", lease_s=60, now=now + 10
    )
    # This should now FAIL because owner doesn't match
    assert success is None

@pytest.mark.asyncio
async def test_hub_atomic_hijack_acquisition():
    hub = TermHub()
    worker_id = "w3"

    # Mock a worker state
    hub._workers[worker_id] = WorkerTermState(
        input_mode="hijack",
        worker_ws=AsyncMock()
    )

    async def attempt_hijack(user_id):
        success, err = await hub.try_acquire_rest_hijack(
            worker_id, owner=user_id, lease_s=60, hijack_id=f"h-{user_id}", now=time.monotonic()
        )
        return success

    # 10 simultaneous attempts
    results = await asyncio.gather(*(attempt_hijack(f"u{i}") for i in range(10)))

    # Only one should have succeeded
    assert results.count(True) == 1
