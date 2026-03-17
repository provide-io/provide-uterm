#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for new source code paths:
- event_deque_maxlen parameter on TermHub
- on_metric called on role resolver timeout
- SessionDefinition.created_at is datetime
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.terminal.hijack.hub import BrowserRoleResolutionError, TermHub
from provide.terminal.server.models import SessionDefinition

# ---------------------------------------------------------------------------
# C4: event_deque_maxlen — deque bounded to configured maxlen
# ---------------------------------------------------------------------------


class TestEventDequeMaxlen:
    async def test_custom_maxlen_limits_events_deque(self) -> None:
        """TermHub with event_deque_maxlen=10 keeps only 10 events."""
        hub = TermHub(event_deque_maxlen=10)

        # register_worker initialises the deque with the configured maxlen
        ws = AsyncMock()
        await hub.register_worker("w1", ws)

        # Append 15 events
        for i in range(15):
            await hub.append_event("w1", "test_event", {"i": i})

        async with hub._lock:
            st = hub._workers.get("w1")
            assert st is not None, "worker state should exist"
            assert isinstance(st.events, deque), "events should be a deque"
            assert st.events.maxlen == 10, f"maxlen should be 10, got {st.events.maxlen}"
            assert len(st.events) == 10, f"should have 10 events, got {len(st.events)}"
            # Oldest events should have been dropped; last event should be seq=15
            last_seq = st.events[-1]["seq"]
            assert last_seq == 15, f"last seq should be 15, got {last_seq}"

    async def test_default_maxlen_is_2000(self) -> None:
        """Default event_deque_maxlen is 2000."""
        hub = TermHub()
        assert hub._event_deque_maxlen == 2000

    async def test_custom_maxlen_stored(self) -> None:
        """event_deque_maxlen is stored on the hub."""
        hub = TermHub(event_deque_maxlen=500)
        assert hub._event_deque_maxlen == 500

    async def test_maxlen_clamped_to_minimum_1(self) -> None:
        """event_deque_maxlen is clamped to at least 1."""
        hub = TermHub(event_deque_maxlen=0)
        assert hub._event_deque_maxlen == 1


# ---------------------------------------------------------------------------
# C4: on_metric called on role resolver timeout
# ---------------------------------------------------------------------------


class TestMetricOnRoleResolverTimeout:
    async def test_metric_called_with_timeout_name_on_resolver_timeout(self) -> None:
        """on_metric is called with 'browser_role_resolution_timeout' when resolver times out."""
        metric_calls: list[tuple[str, Any]] = []

        def _collect_metric(name: str, value: Any) -> None:
            metric_calls.append((name, value))

        def _slow_resolver(ws: Any, worker_id: str) -> Any:
            return asyncio.get_running_loop().create_future()

        async def _mock_wait_for(coro: Any, **_kwargs: Any) -> None:
            raise TimeoutError("mocked timeout")

        hub = TermHub(resolve_browser_role=_slow_resolver, on_metric=_collect_metric)
        browser_ws = MagicMock()

        with patch("asyncio.wait_for", side_effect=_mock_wait_for), pytest.raises(BrowserRoleResolutionError):
            await hub._resolve_role_for_browser(browser_ws, "w1")

        assert any(name == "browser_role_resolution_timeout" for name, _ in metric_calls), (
            f"Expected 'browser_role_resolution_timeout' metric, got: {metric_calls}"
        )

    async def test_metric_not_called_when_no_on_metric_callback(self) -> None:
        """When on_metric is None, no error occurs on resolver timeout."""

        def _slow_resolver(ws: Any, worker_id: str) -> Any:
            return asyncio.get_running_loop().create_future()

        async def _mock_wait_for(coro: Any, **_kwargs: Any) -> None:
            raise TimeoutError("mocked timeout")

        hub = TermHub(resolve_browser_role=_slow_resolver)  # no on_metric
        browser_ws = MagicMock()

        with patch("asyncio.wait_for", side_effect=_mock_wait_for), pytest.raises(BrowserRoleResolutionError):
            await hub._resolve_role_for_browser(browser_ws, "w1")
        # No assertion needed — just verifying it doesn't raise


# ---------------------------------------------------------------------------
# C4: SessionDefinition.created_at is datetime
# ---------------------------------------------------------------------------


class TestSessionDefinitionDatetime:
    def test_created_at_is_datetime_instance(self) -> None:
        """SessionDefinition.created_at is a datetime, not a float."""
        s = SessionDefinition(session_id="test-sess")
        assert isinstance(s.created_at, datetime), f"created_at should be datetime, got {type(s.created_at)}"

    def test_created_at_has_timezone(self) -> None:
        """SessionDefinition.created_at has UTC timezone info."""
        s = SessionDefinition(session_id="tz-sess")
        assert s.created_at.tzinfo is not None, "created_at should have tzinfo"
        assert s.created_at.tzinfo == UTC or s.created_at.utcoffset() is not None

    def test_created_at_is_recent(self) -> None:
        """SessionDefinition.created_at is close to now."""
        now = datetime.now(UTC)
        s = SessionDefinition(session_id="recent-sess")
        delta = abs((s.created_at - now).total_seconds())
        assert delta < 2.0, f"created_at should be close to now, delta={delta}"
