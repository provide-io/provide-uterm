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
from unittest.mock import AsyncMock, MagicMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import PolicyDecision
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


async def test_resolve_approval_survives_browser_disconnect_deny() -> None:
    """A browser disconnecting mid-resolve must not abort the deny broadcast.

    Both the rejection-message send and the ``approval_resolved`` send must be
    guarded: a dead socket is pruned, the surviving browser still receives its
    frames, and ``_paused_browsers`` does not leak.
    """
    hub = _make_hub()
    good = _make_ws()
    good.send_text = AsyncMock()
    dead = _make_ws()
    dead.send_text = AsyncMock(side_effect=RuntimeError("browser gone"))
    async with hub._lock:
        st = hub.registry._workers.setdefault("w1", WorkerTermState())
        st.browsers[dead] = "viewer"  # dead first → it raises before `good` is reached
        st.browsers[good] = "operator"
    hub._paused_browsers.update({good, dead})
    hub._hold_buffers.update({good: "queued", dead: "queued"})

    # Must not raise even though `dead` errors on send.
    await hub.resolve_approval("w1", "req1", PolicyDecision(action="deny", reason="nope"), "bad\r")

    # Surviving browser received the rejection message AND approval_resolved.
    assert good.send_text.await_count >= 2
    # Dead socket pruned; paused set drained.
    assert dead not in hub._paused_browsers
    assert good not in hub._paused_browsers
    assert dead not in hub.registry._workers["w1"].browsers


async def test_resolve_approval_survives_browser_disconnect_allow() -> None:
    """The approved-resolution send loop is guarded the same way."""
    hub = _make_hub()
    good = _make_ws()
    good.send_text = AsyncMock()
    dead = _make_ws()
    dead.send_text = AsyncMock(side_effect=RuntimeError("browser gone"))
    worker_ws = _make_ws()
    worker_ws.send_text = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("w1", WorkerTermState())
        st.worker_ws = worker_ws
        st.browsers[dead] = "viewer"
        st.browsers[good] = "operator"
    hub._paused_browsers.update({good, dead})

    await hub.resolve_approval("w1", "req1", PolicyDecision(action="allow"), "ls\r")

    assert good.send_text.await_count >= 1  # approval_resolved reached the survivor
    assert dead not in hub.registry._workers["w1"].browsers


async def test_resolve_approval_deny_without_reason() -> None:
    """Deny with no reason still broadcasts the rejection (no dead sockets)."""
    hub = _make_hub()
    good = _make_ws()
    good.send_text = AsyncMock()
    async with hub._lock:
        st = hub.registry._workers.setdefault("w1", WorkerTermState())
        st.browsers[good] = "operator"

    await hub.resolve_approval("w1", "req1", PolicyDecision(action="deny"), "bad\r")

    assert good.send_text.await_count >= 2
