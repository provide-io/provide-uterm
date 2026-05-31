#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fix 2b — global worker-registration cap.

``register_worker`` does ``setdefault(worker_id, WorkerTermState())`` with no
bound, so one token holder opening thousands of unique ``worker_id`` WS
connections exhausts memory. The per-principal quota is BROWSER-only (workers
share a static principal), so this must be a generous GLOBAL cap.

Critical semantics:
- A brand-new ``worker_id`` over the cap is rejected with
  ``WebSocketException(1008)``.
- A reconnect of an ALREADY-registered ``worker_id`` is ALWAYS allowed, even at
  capacity — worker WS reconnects are routine (CF DO rotation, manager restart,
  network blips). The ``worker_id not in _workers`` guard preserves this.
- Under the cap, any new worker registers normally.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketException

from provide.uterm.server.bridge.hub import TermHub


def _make_worker_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# New-over-cap reject
# ---------------------------------------------------------------------------


async def test_new_worker_over_cap_rejected_with_1008() -> None:
    """Registering distinct worker_ids up to the cap succeeds; the next NEW id is rejected."""
    hub = TermHub(max_workers=2)
    await hub.register_worker("w1", _make_worker_ws())
    await hub.register_worker("w2", _make_worker_ws())
    assert len(hub.registry._workers) == 2

    with pytest.raises(WebSocketException) as exc_info:
        await hub.register_worker("w3", _make_worker_ws())
    assert exc_info.value.code == 1008
    # The rejected worker must NOT have been added.
    assert "w3" not in hub.registry._workers
    assert len(hub.registry._workers) == 2


# ---------------------------------------------------------------------------
# Reconnect-at-capacity ALWAYS allowed (the key reconnect-preservation test)
# ---------------------------------------------------------------------------


async def test_existing_worker_reconnect_allowed_at_capacity() -> None:
    """A reconnect of an already-registered worker_id succeeds even when the map is full."""
    hub = TermHub(max_workers=2)
    await hub.register_worker("w1", _make_worker_ws())
    await hub.register_worker("w2", _make_worker_ws())
    assert len(hub.registry._workers) == 2  # at capacity

    # Reconnect w1 with a fresh WS — must succeed, no raise.
    new_ws = _make_worker_ws()
    await hub.register_worker("w1", new_ws)

    assert len(hub.registry._workers) == 2  # still 2, no growth
    assert hub.registry._workers["w1"].worker_ws is new_ws


# ---------------------------------------------------------------------------
# Under-cap allow
# ---------------------------------------------------------------------------


async def test_new_worker_under_cap_allowed() -> None:
    """Registering a new worker when below the cap succeeds."""
    hub = TermHub(max_workers=3)
    await hub.register_worker("w1", _make_worker_ws())
    await hub.register_worker("w2", _make_worker_ws())
    assert len(hub.registry._workers) == 2

    await hub.register_worker("w3", _make_worker_ws())
    assert "w3" in hub.registry._workers
    assert len(hub.registry._workers) == 3


# ---------------------------------------------------------------------------
# Constructor threading + default
# ---------------------------------------------------------------------------


async def test_hub_default_max_workers_is_10000() -> None:
    hub = TermHub()
    assert hub.max_workers == 10000


async def test_hub_max_workers_minimum_is_one() -> None:
    """A non-positive cap is clamped to 1 (mirrors max_connections_per_principal)."""
    hub = TermHub(max_workers=0)
    assert hub.max_workers == 1

    hub2 = TermHub(max_workers=-5)
    assert hub2.max_workers == 1
