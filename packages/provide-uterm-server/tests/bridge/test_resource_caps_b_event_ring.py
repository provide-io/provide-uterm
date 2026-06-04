#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for Sub-fix B: per-term-event stored data cap in the event ring.

Covers:
- append_event("term", ...) truncates the stored ring entry's "data" field to
  hub.max_event_data_chars.
- Non-term events (e.g. "input_send") are NOT truncated.
- Live broadcast is separate and unaffected (full data goes to all browsers).
- Data at or below the cap is stored verbatim (no truncation).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState

_CAP = 512  # well above the 256-char floor


def _make_hub(max_event_data_chars: int = _CAP) -> TermHub:
    return TermHub(max_event_data_chars=max_event_data_chars)


async def _register_worker(hub: TermHub, worker_id: str) -> None:
    async with hub._lock:
        hub.registry._workers[worker_id] = WorkerTermState()


async def test_term_event_data_truncated_in_ring() -> None:
    """term events with data longer than max_event_data_chars must be stored truncated."""
    hub = _make_hub(max_event_data_chars=_CAP)
    await _register_worker(hub, "w1")

    long_data = "A" * (_CAP * 3)  # well above cap
    evt = await hub.append_event("w1", "term", {"data": long_data})

    # The returned event (which IS the ring entry) must be truncated
    assert len(evt["data"]["data"]) == _CAP, "ring-stored term data must be truncated to cap"
    assert evt["data"]["data"] == "A" * _CAP

    # Also verify it landed in the ring correctly
    async with hub._lock:
        st = hub.registry._workers["w1"]
        ring_evt = st.events[-1]
    assert len(ring_evt["data"]["data"]) == _CAP


async def test_term_event_data_not_truncated_when_within_cap() -> None:
    """term events with data at or below the cap must be stored verbatim."""
    hub = _make_hub(max_event_data_chars=_CAP)
    await _register_worker(hub, "w1")

    short_data = "B" * (_CAP - 100)  # below cap
    evt = await hub.append_event("w1", "term", {"data": short_data})

    assert evt["data"]["data"] == short_data, "data within cap must not be truncated"


async def test_term_event_data_at_exact_cap_not_truncated() -> None:
    """term events with data exactly at the cap must be stored verbatim."""
    hub = _make_hub(max_event_data_chars=_CAP)
    await _register_worker(hub, "w1")

    exact_data = "C" * _CAP
    evt = await hub.append_event("w1", "term", {"data": exact_data})
    assert evt["data"]["data"] == exact_data


async def test_non_term_event_data_not_truncated() -> None:
    """Non-term events (e.g. 'input_send') must NOT have their data truncated."""
    hub = _make_hub(max_event_data_chars=_CAP)
    await _register_worker(hub, "w1")

    long_keys = "x" * (_CAP * 2)  # well above the cap
    evt = await hub.append_event("w1", "input_send", {"owner": "dashboard_ws", "keys": long_keys})

    # 'input_send' is not a 'term' event — data must be stored intact
    assert evt["data"]["keys"] == long_keys, "non-term event data must not be truncated"


async def test_term_event_without_data_field_unaffected() -> None:
    """term events without a string 'data' field must not raise or be altered."""
    hub = _make_hub(max_event_data_chars=_CAP)
    await _register_worker(hub, "w1")

    # term event with no "data" key in payload
    evt = await hub.append_event("w1", "term", {"extra": "info"})
    assert "extra" in evt["data"]

    # term event with non-string "data" value
    evt2 = await hub.append_event("w1", "term", {"data": 42})
    assert evt2["data"]["data"] == 42


async def test_live_broadcast_receives_full_data_while_ring_is_truncated() -> None:
    """The live broadcast must carry the full event.data; only the ring entry is truncated."""
    hub = _make_hub(max_event_data_chars=_CAP)
    sent_msgs: list[str] = []

    browser_ws = MagicMock()
    browser_ws.send_text = AsyncMock(side_effect=lambda text: sent_msgs.append(text))

    async with hub._lock:
        st = WorkerTermState()
        hub.registry._workers["w1"] = st
        st.browsers[browser_ws] = "viewer"

    long_data = "Z" * (_CAP * 3)  # well above cap
    # broadcast independently (simulating websockets_impl which calls hub.broadcast
    # with the full data and then calls hub.append_event for the ring)
    from provide.uterm.server.bridge.frames import make_term_frame

    await hub.broadcast("w1", make_term_frame(long_data))
    evt = await hub.append_event("w1", "term", {"data": long_data})

    # Ring entry is truncated to cap
    assert len(evt["data"]["data"]) == _CAP, "ring copy must be truncated to cap"

    # Browser received the full broadcast (not the truncated ring copy)
    assert browser_ws.send_text.called
    full_payload = sent_msgs[-1] if sent_msgs else ""
    # The broadcast frame contains the full long_data (not the truncated ring copy)
    assert "Z" * _CAP in full_payload, "broadcast must contain at least CAP Z-chars"
