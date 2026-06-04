#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for Sub-fix D: per-principal browser connection quota.

Covers:
- A principal opening up to the cap succeeds; the (cap+1)th is rejected (1008).
- A different principal is independent (its own quota counter).
- Anonymous principal (subject_id="anonymous") is exempt — never capped.
- None principal (missing ws.state.uterm_principal) is exempt — never capped.
- Worker registration is NOT subject to the quota (fleet-safe).
- Disconnect DECREMENTS the count: register to the cap, disconnect one,
  register one more → succeeds.
- Count dict cleaned up: key removed when the last connection for a
  principal closes (no dict growth / leak).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketException

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.bridge.models import WorkerTermState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws(subject_id: str | None = None) -> MagicMock:
    """Create a mock WebSocket with optional ``ws.state.uterm_principal``."""
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.state = SimpleNamespace()
    if subject_id is not None:
        ws.state.uterm_principal = Principal(
            subject_id=subject_id,
            roles=frozenset({"viewer"}),
            scopes=frozenset(),
        )
    # No uterm_principal attribute at all when subject_id is None
    return ws


def _make_hub(cap: int = 3) -> TermHub:
    return TermHub(max_connections_per_principal=cap)


async def _register_ws(hub: TermHub, ws: MagicMock, worker_id: str = "w1") -> dict[str, Any]:
    """Call register_browser, ensuring the worker state entry exists first."""
    async with hub._lock:
        hub.registry._workers.setdefault(worker_id, WorkerTermState())
    return await hub.register_browser(worker_id, ws, "viewer")


# ---------------------------------------------------------------------------
# Basic quota enforcement
# ---------------------------------------------------------------------------


async def test_up_to_cap_succeeds() -> None:
    """Registrations up to the cap (inclusive) must succeed."""
    hub = _make_hub(cap=3)
    for _i in range(3):
        ws = _make_ws("alice")
        await _register_ws(hub, ws)
    assert hub._principal_browser_counts.get("alice") == 3


async def test_cap_plus_one_rejected_with_1008() -> None:
    """The (cap+1)th registration for a principal must raise WebSocketException(1008)."""
    hub = _make_hub(cap=2)
    ws1 = _make_ws("alice")
    ws2 = _make_ws("alice")
    await _register_ws(hub, ws1)
    await _register_ws(hub, ws2)

    ws3 = _make_ws("alice")
    with pytest.raises(WebSocketException) as exc_info:
        await _register_ws(hub, ws3)
    assert exc_info.value.code == 1008
    # Count must NOT have been incremented
    assert hub._principal_browser_counts["alice"] == 2
    # Rejected ws must NOT be in st.browsers
    async with hub._lock:
        st = hub.registry._workers["w1"]
    assert ws3 not in st.browsers


async def test_different_principals_independent() -> None:
    """Two principals each have their own independent quota counter."""
    hub = _make_hub(cap=2)
    ws_a1 = _make_ws("alice")
    ws_a2 = _make_ws("alice")
    ws_b1 = _make_ws("bob")
    ws_b2 = _make_ws("bob")

    await _register_ws(hub, ws_a1)
    await _register_ws(hub, ws_a2)
    await _register_ws(hub, ws_b1)
    await _register_ws(hub, ws_b2)

    assert hub._principal_browser_counts["alice"] == 2
    assert hub._principal_browser_counts["bob"] == 2

    # alice is now at cap — rejecting one more
    with pytest.raises(WebSocketException) as exc_info:
        await _register_ws(hub, _make_ws("alice"))
    assert exc_info.value.code == 1008

    # bob is also at cap — rejecting one more
    with pytest.raises(WebSocketException) as exc_info:
        await _register_ws(hub, _make_ws("bob"))
    assert exc_info.value.code == 1008


# ---------------------------------------------------------------------------
# Exempt principals
# ---------------------------------------------------------------------------


async def test_anonymous_principal_exempt() -> None:
    """anonymous subject_id must never be capped, and must not increment the counter."""
    hub = _make_hub(cap=2)
    for _ in range(10):
        ws = _make_ws("anonymous")
        await _register_ws(hub, ws)
    assert "anonymous" not in hub._principal_browser_counts


async def test_none_principal_exempt() -> None:
    """A WebSocket with no uterm_principal attribute must never be capped."""
    hub = _make_hub(cap=2)
    for _ in range(10):
        ws = _make_ws(subject_id=None)  # no .uterm_principal attr
        await _register_ws(hub, ws)
    # No principal key recorded
    assert len(hub._principal_browser_counts) == 0


# ---------------------------------------------------------------------------
# Worker registration must be unaffected by the quota
# ---------------------------------------------------------------------------


async def test_worker_registration_not_capped() -> None:
    """register_worker with the shared 'worker' subject_id must not be limited."""
    hub = _make_hub(cap=1)

    ws_worker = MagicMock()
    ws_worker.send_text = AsyncMock()
    ws_worker.state = SimpleNamespace()
    ws_worker.state.uterm_principal = Principal(
        subject_id="worker",
        roles=frozenset({"admin"}),
        scopes=frozenset({"*"}),
    )

    # Register many workers — all must succeed
    for i in range(5):
        wid = f"fleet-{i}"
        async with hub._lock:
            hub.registry._workers.setdefault(wid, WorkerTermState())
        ws = MagicMock()
        ws.send_text = AsyncMock()
        await hub.register_worker(wid, ws)

    # Worker count is fleet-level, not limited
    assert len(hub.registry._workers) == 5
    # Principal counter must NOT have grown (workers exempt)
    assert len(hub._principal_browser_counts) == 0


# ---------------------------------------------------------------------------
# Disconnect DECREMENTS the count correctly
# ---------------------------------------------------------------------------


async def test_disconnect_decrements_count() -> None:
    """Disconnecting a browser must decrement the principal's count."""
    hub = _make_hub(cap=2)
    ws1 = _make_ws("alice")
    ws2 = _make_ws("alice")
    await _register_ws(hub, ws1)
    await _register_ws(hub, ws2)
    assert hub._principal_browser_counts["alice"] == 2

    # Disconnect ws1
    await hub.cleanup_browser_disconnect("w1", ws1, owned_hijack=False)
    assert hub._principal_browser_counts["alice"] == 1


async def test_disconnect_allows_new_registration_after_cap() -> None:
    """After filling to the cap and disconnecting one, a new registration must succeed."""
    hub = _make_hub(cap=2)
    ws1 = _make_ws("alice")
    ws2 = _make_ws("alice")
    await _register_ws(hub, ws1)
    await _register_ws(hub, ws2)

    # Now at cap — a 3rd should fail
    with pytest.raises(WebSocketException):
        await _register_ws(hub, _make_ws("alice"))

    # Disconnect ws1
    await hub.cleanup_browser_disconnect("w1", ws1, owned_hijack=False)

    # Now one slot free — a new ws must succeed
    ws3 = _make_ws("alice")
    await _register_ws(hub, ws3)
    assert hub._principal_browser_counts["alice"] == 2


async def test_count_dict_cleaned_up_when_last_connection_closes() -> None:
    """When the last browser for a principal disconnects, the key is removed."""
    hub = _make_hub(cap=5)
    ws1 = _make_ws("alice")
    await _register_ws(hub, ws1)
    assert "alice" in hub._principal_browser_counts

    await hub.cleanup_browser_disconnect("w1", ws1, owned_hijack=False)
    # Key should be removed (not left as 0)
    assert "alice" not in hub._principal_browser_counts


async def test_anonymous_not_decremented_on_disconnect() -> None:
    """Disconnecting an anonymous browser must not touch the count dict."""
    hub = _make_hub(cap=2)
    ws = _make_ws("anonymous")
    await _register_ws(hub, ws)

    # Disconnect — should not error, no keys in count dict
    await hub.cleanup_browser_disconnect("w1", ws, owned_hijack=False)
    assert len(hub._principal_browser_counts) == 0


async def test_none_principal_not_decremented_on_disconnect() -> None:
    """Disconnecting a browser without principal must not touch the count dict."""
    hub = _make_hub(cap=2)
    ws = _make_ws(subject_id=None)
    await _register_ws(hub, ws)

    await hub.cleanup_browser_disconnect("w1", ws, owned_hijack=False)
    assert len(hub._principal_browser_counts) == 0


# ---------------------------------------------------------------------------
# Config field threading
# ---------------------------------------------------------------------------


async def test_hub_default_cap_is_25() -> None:
    """The default max_connections_per_principal must be 25."""
    hub = TermHub()
    assert hub.max_connections_per_principal == 25


async def test_hub_cap_minimum_is_one() -> None:
    """Passing 0 or negative must be clamped to 1."""
    hub = TermHub(max_connections_per_principal=0)
    assert hub.max_connections_per_principal == 1

    hub2 = TermHub(max_connections_per_principal=-5)
    assert hub2.max_connections_per_principal == 1
