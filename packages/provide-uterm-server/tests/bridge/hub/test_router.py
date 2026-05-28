#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for :class:`MessageRouter`.

These cover the service-class surface directly. The hub's mixin facade
(``HubMessagingMixin``) delegates to the router for every non-cooperative
public method, so the existing integration tests in
``tests/bridge/test_hub*`` etc. exercise the shim path; this file is
the canonical place for router-level unit tests.

The router is constructed with a real :class:`TermHub` because it
holds a back-reference for cross-mixin queries (``is_hijacked``,
``prepare_policy_context`` and friends). Building a stub hub would
duplicate too much of the hub's wiring.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.router import MessageRouter
from provide.uterm.server.bridge.models import WorkerTermState


@pytest.fixture()
def hub() -> TermHub:
    """Plain TermHub with default settings — gives the router live deps."""
    return TermHub()


def test_router_attached_to_hub(hub: TermHub) -> None:
    """TermHub.__init__ attaches a MessageRouter as ``hub.router``."""
    assert isinstance(hub.router, MessageRouter)


def test_keystroke_timestamps_initially_empty(hub: TermHub) -> None:
    """Fresh router has an empty heuristics ring-buffer map."""
    assert hub.router.keystroke_timestamps == {}


def test_record_and_get_heuristics(hub: TermHub) -> None:
    """``record_keystroke`` appends and ``get_heuristics`` reports cps / jitter."""
    source = object()
    for _ in range(5):
        hub.router.record_keystroke(source)
    h = hub.router.get_heuristics(source)
    assert h["cps"] >= 0.0
    assert h["jitter"] >= 0.0


def test_get_heuristics_unknown_source_returns_zeros(hub: TermHub) -> None:
    """Unknown source — heuristics return zero-valued metrics."""
    assert hub.router.get_heuristics(object()) == {"cps": 0.0, "jitter": 0.0}


def test_forget_browser_removes_state(hub: TermHub) -> None:
    """``forget_browser`` drops the per-browser heuristic ring buffer."""
    source = object()
    hub.router.record_keystroke(source)
    assert source in hub.router.keystroke_timestamps
    hub.router.forget_browser(source)
    assert source not in hub.router.keystroke_timestamps


def test_keystroke_timestamps_property_aliases_router(hub: TermHub) -> None:
    """The legacy ``hub._keystroke_timestamps`` shim mirrors the router's map.

    Mutations made through either entry point are visible on the other —
    the property returns the underlying dict, not a copy.
    """
    source = object()
    hub._keystroke_timestamps[source] = deque([1.0, 2.0], maxlen=50)
    assert hub.router.keystroke_timestamps[source][-1] == 2.0


async def test_broadcast_with_no_worker_is_noop(hub: TermHub) -> None:
    """Broadcasting to an unknown worker is a no-op (early return)."""
    # Must not raise.
    await hub.router.broadcast("no-such-worker", {"type": "term", "data": "x"})


async def test_send_worker_returns_false_when_no_worker(hub: TermHub) -> None:
    """send_worker returns False when no worker WS is connected."""
    assert await hub.router.send_worker("nobody", {"type": "input", "data": "x"}) is False


async def test_send_worker_reraises_base_exception_after_clearing_stale_worker(hub: TermHub) -> None:
    """BaseException subclasses must not be swallowed, but stale worker state is cleared first."""
    ws = AsyncMock()
    ws.send_text.side_effect = KeyboardInterrupt
    async with hub._lock:
        hub.registry.put("w1", WorkerTermState(worker_ws=ws))

    with pytest.raises(KeyboardInterrupt):
        await hub.router.send_worker("w1", {"type": "input", "data": "x"})

    assert hub.registry.get("w1").worker_ws is None


async def test_hijack_state_msg_for_unknown_worker(hub: TermHub) -> None:
    """hijack_state_msg_for builds a fresh frame even when the worker is unknown."""
    frame = await hub.router.hijack_state_msg_for("no-such-worker", AsyncMock())
    assert frame["hijacked"] is False
    assert frame["owner"] is None


async def test_browser_count_for_unknown_worker_is_zero(hub: TermHub) -> None:
    """browser_count returns 0 for a worker that's not registered."""
    assert await hub.router.browser_count("missing") == 0


async def test_browser_count_total_with_registered_worker(hub: TermHub) -> None:
    """browser_count_total sums browser maps across the registry."""
    async with hub._lock:
        hub.registry.put("w1", WorkerTermState())
    assert await hub.router.browser_count_total() == 0


async def test_get_recent_events_for_unknown_worker(hub: TermHub) -> None:
    """Unknown worker yields an empty events list, not an error."""
    assert await hub.router.get_recent_events("ghost", limit=10) == []


async def test_get_idle_candidates_empty_registry(hub: TermHub) -> None:
    """No workers → no idle candidates."""
    assert await hub.router.get_idle_candidates(0.0) == []
