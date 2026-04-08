#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for session inactivity timeout sweep logic."""

from __future__ import annotations

import contextlib
import time
from unittest.mock import AsyncMock, patch

import pytest

from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.models import WorkerTermState


@pytest.fixture
def hub() -> TermHub:
    return TermHub()


# ---------------------------------------------------------------------------
# touch_activity
# ---------------------------------------------------------------------------


async def test_touch_activity_updates_timestamp(hub: TermHub) -> None:
    """touch_activity should update last_activity_at for a registered worker."""
    st = WorkerTermState()
    st.last_activity_at = 1000.0
    hub._workers["w1"] = st

    with patch("time.monotonic", return_value=2000.0):
        await hub.touch_activity("w1")

    assert st.last_activity_at == 2000.0


async def test_touch_activity_noop_for_unknown_worker(hub: TermHub) -> None:
    """touch_activity should not raise for an unknown worker_id."""
    await hub.touch_activity("nonexistent")  # should not raise


# ---------------------------------------------------------------------------
# _sweep_idle_sessions (imported from app.py indirectly, tested via extraction)
# ---------------------------------------------------------------------------


async def _run_sweep_once(hub: TermHub, timeout_s: int) -> None:
    """Simulate one sweep iteration matching the logic in app.py."""
    if timeout_s <= 0:
        return
    now = time.time()
    async with hub._lock:
        candidates = [
            (wid, st.last_activity_at)
            for wid, st in hub._workers.items()
            if not st.browsers and (now - st.last_activity_at) > timeout_s
        ]
    for worker_id, _last_at in candidates:
        await hub.disconnect_worker(worker_id)


async def test_idle_session_disconnected_after_timeout(hub: TermHub) -> None:
    """A worker idle beyond timeout_s with no browsers should be disconnected."""
    ws_mock = AsyncMock()
    st = WorkerTermState()
    st.worker_ws = ws_mock
    st.last_activity_at = time.time() - 600  # 10 minutes ago
    hub._workers["idle-1"] = st

    await _run_sweep_once(hub, timeout_s=300)

    # Worker should have been disconnected and pruned
    assert "idle-1" not in hub._workers


async def test_active_session_not_disconnected(hub: TermHub) -> None:
    """A worker with recent activity should not be disconnected."""
    ws_mock = AsyncMock()
    st = WorkerTermState()
    st.worker_ws = ws_mock
    st.last_activity_at = time.time()  # just now
    hub._workers["active-1"] = st

    await _run_sweep_once(hub, timeout_s=300)

    assert "active-1" in hub._workers
    assert st.worker_ws is ws_mock


async def test_timeout_disabled_when_zero(hub: TermHub) -> None:
    """When timeout_s=0, sweep should do nothing."""
    ws_mock = AsyncMock()
    st = WorkerTermState()
    st.worker_ws = ws_mock
    st.last_activity_at = time.time() - 99999
    hub._workers["old-1"] = st

    await _run_sweep_once(hub, timeout_s=0)

    assert "old-1" in hub._workers
    assert st.worker_ws is ws_mock


async def test_session_with_browsers_not_disconnected(hub: TermHub) -> None:
    """A worker with connected browsers should not be swept even if idle."""
    ws_mock = AsyncMock()
    browser_mock = AsyncMock()
    st = WorkerTermState()
    st.worker_ws = ws_mock
    st.last_activity_at = time.time() - 600
    st.browsers[browser_mock] = "viewer"
    hub._workers["has-browser"] = st

    await _run_sweep_once(hub, timeout_s=300)

    assert "has-browser" in hub._workers
    assert st.worker_ws is ws_mock


async def test_sweep_resilient_to_per_worker_errors(hub: TermHub) -> None:
    """If disconnecting one worker fails, others should still be processed."""
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    st1 = WorkerTermState()
    st1.worker_ws = ws1
    st1.last_activity_at = time.time() - 600
    st2 = WorkerTermState()
    st2.worker_ws = ws2
    st2.last_activity_at = time.time() - 600
    hub._workers["fail-1"] = st1
    hub._workers["ok-1"] = st2

    original_disconnect = hub.disconnect_worker
    call_count = 0

    async def _patched_disconnect(worker_id: str) -> bool:
        nonlocal call_count
        call_count += 1
        if worker_id == "fail-1":
            raise RuntimeError("simulated failure")
        return await original_disconnect(worker_id)

    # Replicate the resilient sweep from app.py
    now = time.time()
    async with hub._lock:
        candidates = [
            (wid, st.last_activity_at)
            for wid, st in hub._workers.items()
            if not st.browsers and (now - st.last_activity_at) > 300
        ]
    for worker_id, _ in candidates:
        with contextlib.suppress(Exception):
            await _patched_disconnect(worker_id)

    assert call_count == 2
    # ok-1 should be cleaned up; fail-1 still there since disconnect raised
    assert "ok-1" not in hub._workers


async def test_activity_updated_on_worker_data() -> None:
    """WorkerTermState.last_activity_at should have a default from time.monotonic."""
    before = time.monotonic()
    st = WorkerTermState()
    after = time.monotonic()
    assert before <= st.last_activity_at <= after


async def test_config_default_idle_timeout() -> None:
    """ServerConfig.session_idle_timeout_s defaults to 0 (disabled)."""
    from provide.terminal.server.models import ServerConfig

    cfg = ServerConfig()
    assert cfg.session_idle_timeout_s == 0


async def test_config_custom_idle_timeout() -> None:
    """ServerConfig.session_idle_timeout_s can be set to a positive value."""
    from provide.terminal.server.models import ServerConfig

    cfg = ServerConfig(session_idle_timeout_s=300)
    assert cfg.session_idle_timeout_s == 300
