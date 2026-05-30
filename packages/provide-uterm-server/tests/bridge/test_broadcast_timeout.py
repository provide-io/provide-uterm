#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Per-send timeout in broadcast(): stalled browser is pruned from st.browsers."""

from __future__ import annotations

import asyncio

import provide.uterm.server.bridge.hub.router_impl as router_impl
from provide.uterm.server.bridge.hub import TermHub


class _HangingWS:
    """Fake browser WebSocket whose send_text blocks forever (simulates stalled TCP window)."""

    async def send_text(self, _payload: str) -> None:
        await asyncio.Event().wait()  # never completes


async def test_broadcast_prunes_browser_whose_send_stalls(monkeypatch: object) -> None:
    """A browser that stalls on send_text must be pruned from st.browsers after broadcast."""
    monkeypatch.setattr(router_impl, "_BROADCAST_SEND_TIMEOUT_S", 0.05)

    hub = TermHub()
    # Mirror the exact hub-registration harness used in test_hub.py:
    # _get() creates the WorkerTermState; direct dict assignment registers the browser.
    await hub._get("w1")
    ws = _HangingWS()
    hub._workers["w1"].browsers[ws] = "viewer"  # type: ignore[arg-type]

    await hub.broadcast("w1", {"type": "term", "data": "x"})

    st = hub.registry.get("w1")
    # The stalled browser must be gone from the browsers map.
    assert ws not in (st.browsers if st is not None else {}), (
        "Expected stalled browser to be pruned from st.browsers after broadcast timeout"
    )
