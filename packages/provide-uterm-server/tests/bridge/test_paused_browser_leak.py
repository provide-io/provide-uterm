#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression test for CB-2: dead-socket leak in ``_paused_browsers``.

``cleanup_browser_disconnect`` and ``remove_dead_browsers`` must discard the
disconnecting browser from ``_paused_browsers``; otherwise a browser that
disconnects while an approval is pending leaks a dead ``WebSocket`` into the
set forever (only ``resolve_approval`` discarded it previously).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState


def _make_hub(**kwargs: Any) -> TermHub:
    return TermHub(**kwargs)


def _make_ws() -> MagicMock:
    return MagicMock()


async def test_cleanup_browser_disconnect_clears_paused_browser() -> None:
    """A paused browser disconnecting must be removed from ``_paused_browsers``."""
    hub = _make_hub()
    ws = _make_ws()
    async with hub._lock:
        st = hub.registry._workers.setdefault("w1", WorkerTermState())
        st.browsers[ws] = "viewer"
    hub._paused_browsers.add(ws)
    hub._hold_buffers[ws] = "queued"

    await hub.cleanup_browser_disconnect("w1", ws, owned_hijack=False)

    assert ws not in hub._paused_browsers
    assert ws not in hub._hold_buffers


async def test_remove_dead_browsers_clears_paused_browser() -> None:
    """Dead browsers pruned in bulk must also leave ``_paused_browsers``."""
    hub = _make_hub()
    ws = _make_ws()
    async with hub._lock:
        st = hub.registry._workers.setdefault("w1", WorkerTermState())
        st.browsers[ws] = "viewer"
    hub._paused_browsers.add(ws)
    hub._hold_buffers[ws] = "queued"

    await hub.remove_dead_browsers("w1", {ws})

    assert ws not in hub._paused_browsers
    assert ws not in hub._hold_buffers
