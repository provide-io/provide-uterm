#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Event-log parity. FastAPI (``TermHub.append_event``) and Cloudflare
(``SqliteStateStore.append_event``) are independent implementations; these
tests pin identical sequence-numbering and ordering behaviour."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .backends import ConformanceBackend

pytestmark = pytest.mark.asyncio


async def test_sequence_numbers_are_monotonic_from_one(backend: ConformanceBackend) -> None:
    seqs = [
        await backend.append_event("w1", "snapshot", {"i": 0}),
        await backend.append_event("w1", "snapshot", {"i": 1}),
        await backend.append_event("w1", "snapshot", {"i": 2}),
    ]
    assert seqs == [1, 2, 3]


async def test_list_events_preserves_order_and_payload(backend: ConformanceBackend) -> None:
    for i in range(3):
        await backend.append_event("w1", "snapshot", {"i": i})
    events = await backend.list_events("w1")
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert [e["type"] for e in events] == ["snapshot", "snapshot", "snapshot"]
    assert [e["data"]["i"] for e in events] == [0, 1, 2]


async def test_events_are_isolated_per_worker(backend: ConformanceBackend) -> None:
    await backend.append_event("w1", "snapshot", {})
    await backend.append_event("w2", "snapshot", {})
    # Each worker's sequence starts independently at 1.
    assert [e["seq"] for e in await backend.list_events("w1")] == [1]
    assert [e["seq"] for e in await backend.list_events("w2")] == [1]
