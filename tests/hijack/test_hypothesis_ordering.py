#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hypothesis ordering and broadcast tests for TermHub.

Supplements test_hypothesis.py with:
- Event seq monotonicity under concurrent appends (additional scenarios)
- Many-browser broadcast validation
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import hypothesis.strategies as st_h
import pytest
from hypothesis import HealthCheck, given, settings

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import WorkerTermState


def _mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# C1: Event seq monotonicity under concurrent appends (additional scenarios)
# ---------------------------------------------------------------------------


class TestEventSeqMonotonicityAdditional:
    @given(n=st_h.integers(min_value=5, max_value=50))
    @settings(
        max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.differing_executors]
    )
    @pytest.mark.asyncio
    async def test_event_seqs_never_repeat_or_skip_with_multiple_workers(self, n: int) -> None:
        """Multiple workers' event sequences are independent and monotonic."""
        hub = TermHub()

        # Register two workers
        ws1 = _mock_ws()
        ws2 = _mock_ws()
        async with hub._lock:
            st1 = hub._workers.setdefault("w1", WorkerTermState())
            st1.worker_ws = ws1
            st2 = hub._workers.setdefault("w2", WorkerTermState())
            st2.worker_ws = ws2

        results: dict[str, list[int]] = {"w1": [], "w2": []}
        lock = asyncio.Lock()

        async def _append_w1(i: int) -> None:
            evt = await hub.append_event("w1", "test", {"i": i})
            async with lock:
                results["w1"].append(evt["seq"])

        async def _append_w2(i: int) -> None:
            evt = await hub.append_event("w2", "test", {"i": i})
            async with lock:
                results["w2"].append(evt["seq"])

        await asyncio.gather(*[_append_w1(i) for i in range(n)], *[_append_w2(i) for i in range(n)])

        for worker_id in ("w1", "w2"):
            seqs = sorted(results[worker_id])
            assert len(seqs) == n, f"{worker_id}: expected {n} events, got {len(seqs)}"
            assert len(seqs) == len(set(seqs)), f"{worker_id}: duplicate seqs: {seqs}"
            for i in range(1, len(seqs)):
                assert seqs[i] > seqs[i - 1], f"{worker_id}: non-monotonic seqs: {seqs}"

    @given(n=st_h.integers(min_value=2, max_value=20))
    @settings(
        max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.differing_executors]
    )
    @pytest.mark.asyncio
    async def test_event_seq_resumes_after_maxlen_overflow(self, n: int) -> None:
        """Event seq is monotonically increasing even after deque wraps around."""
        hub = TermHub(event_deque_maxlen=5)
        ws = _mock_ws()
        await hub.register_worker("w1", ws)

        # Append more events than the maxlen
        total = n + 10
        seqs: list[int] = []
        for i in range(total):
            evt = await hub.append_event("w1", "test", {"i": i})
            seqs.append(evt["seq"])

        # All returned seqs should be monotonically increasing
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1], f"Seq should increase: {seqs[i - 1]} -> {seqs[i]}"

        # Deque should only hold the last 5
        async with hub._lock:
            st = hub._workers.get("w1")
            assert st is not None, "Worker state should exist"
            assert len(st.events) <= 5, f"Deque should be bounded to 5, got {len(st.events)}"


# ---------------------------------------------------------------------------
# C1: Many-browser broadcast validation
# ---------------------------------------------------------------------------


class TestManyBrowserBroadcast:
    @given(n=st_h.integers(min_value=1, max_value=20))
    @settings(
        max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.differing_executors]
    )
    @pytest.mark.asyncio
    async def test_broadcast_reaches_all_browsers(self, n: int) -> None:
        """Broadcast sends to all n browsers; each browser's send_text is called once."""
        hub = TermHub()

        browsers = [_mock_ws() for _ in range(n)]
        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            for b in browsers:
                st.browsers[b] = "viewer"

        msg = {"type": "term", "data": "hello", "ts": 0.0}
        await hub.broadcast("w1", msg)

        for i, b in enumerate(browsers):
            b.send_text.assert_called_once(), f"Browser {i} should have received broadcast"

    @given(n=st_h.integers(min_value=2, max_value=10))
    @settings(
        max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.differing_executors]
    )
    @pytest.mark.asyncio
    async def test_broadcast_with_one_dead_browser_removes_it(self, n: int) -> None:
        """Broadcast with one dead browser removes it and still delivers to others."""
        hub = TermHub()

        good_browsers = [_mock_ws() for _ in range(n - 1)]
        dead_browser = _mock_ws()
        dead_browser.send_text = AsyncMock(side_effect=RuntimeError("dead"))

        async with hub._lock:
            st = hub._workers.setdefault("w1", WorkerTermState())
            for b in good_browsers:
                st.browsers[b] = "viewer"
            st.browsers[dead_browser] = "viewer"

        msg = {"type": "term", "data": "hello", "ts": 0.0}
        await hub.broadcast("w1", msg)

        # Dead browser should be removed from the state
        async with hub._lock:
            st2 = hub._workers.get("w1")
            if st2 is not None:
                assert dead_browser not in st2.browsers, "Dead browser should be removed after failed broadcast"

        # Good browsers should have received the message
        for i, b in enumerate(good_browsers):
            b.send_text.assert_called_once(), f"Good browser {i} should have received broadcast"
