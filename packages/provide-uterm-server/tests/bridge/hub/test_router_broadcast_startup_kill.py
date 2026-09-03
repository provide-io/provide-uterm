#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the startup-window buffer and its flush.

``_buffer_for_startup_browsers`` walks every browser of a session and holds the
frame for the ones still starting up; ``activate_browser_broadcasts`` hands that
backlog over and lets the socket join the normal broadcast set. The existing
suite proves a buffered frame arrives -- it drives one browser at a time, which
is exactly the shape that cannot see any of the following:

*``continue`` is per-browser, not "stop here".* Three separate loop skips
(a browser that is already active, a coalesced snapshot, a browser at its cap)
each hand control to the NEXT browser. Turned into ``break`` they abandon
every browser after the first, and with a single browser in the session that is
indistinguishable from correct. Each is pinned with two browsers in states that
differ.

*The cap warning.* The only signal that a browser is losing frames outright.
Its worker id arrives from three call frames up, so a test that never fills the
cap cannot see it being passed at all.

*Leaving a failed socket pending.* Pending means the broadcast path skips it,
which is what you want for a socket that just failed a write. Discarding it
from the pending set instead puts a known-broken socket back into the fan-out.

*The flush timeout.* Without it, one socket that accepts no writes holds the
activation loop open forever, and the browser never joins the broadcast set.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.control_channel import ControlFrameDecoder
from provide.uterm.server.bridge.hub import TermHub, router_broadcast, router_impl

_WORKER = "w1"
_CAP = router_broadcast._STARTUP_BUFFER_MAX_FRAMES


def _http(index: int) -> dict[str, Any]:
    """An inspect row — buffered by append, so ordering and counts are visible."""
    return {"type": "http_req", "id": f"r{index}", "method": "GET", "url": f"/api/{index}", "_channel": "http"}


def _snapshot(screen: str) -> dict[str, Any]:
    return {"type": "snapshot", "screen": screen, "ts": 1.0}


def _delivered(ws: AsyncMock) -> list[str]:
    """The url (inspect row) or screen (snapshot) of each frame the socket got."""
    decoder = ControlFrameDecoder()
    seen: list[str] = []
    for call in ws.send_text.call_args_list:
        for event in decoder.feed(call.args[0]):
            payload = getattr(event, "control", None)
            if payload is not None:
                seen.append(str(payload.get("url") or payload.get("screen")))
    return seen


async def _hub() -> TermHub:
    hub = TermHub()
    await hub.register_worker(_WORKER, AsyncMock())
    return hub


async def _pending(hub: TermHub, role: str = "viewer") -> AsyncMock:
    ws = AsyncMock()
    await hub.register_browser(_WORKER, ws, role, defer_broadcast=True)
    return ws


# ---------------------------------------------------------------------------
# The loop skips — each must advance to the next browser, not abandon the rest
# ---------------------------------------------------------------------------


async def test_an_active_browser_does_not_stop_the_walk_at_the_ones_behind_it() -> None:
    """The first browser is already live, the second is still starting up.

    Registered in that order deliberately: ``break`` here would return before
    ever reaching the browser that actually needed the frame held.
    """
    hub = await _hub()
    active = AsyncMock()
    await hub.register_browser(_WORKER, active, "viewer")
    starting = await _pending(hub)

    await hub.broadcast(_WORKER, _http(0))
    await hub.activate_browser_broadcasts(_WORKER, starting)

    assert _delivered(starting) == ["/api/0"]


async def test_a_coalesced_snapshot_is_held_for_every_starting_browser() -> None:
    """Coalescing replaces the queued screen; it does not end the walk."""
    hub = await _hub()
    first = await _pending(hub)
    second = await _pending(hub)

    await hub.broadcast(_WORKER, _snapshot("old"))
    await hub.broadcast(_WORKER, _snapshot("new"))
    await hub.activate_browser_broadcasts(_WORKER, first)
    await hub.activate_browser_broadcasts(_WORKER, second)

    assert _delivered(first) == ["new"]
    assert _delivered(second) == ["new"]


async def test_a_browser_at_its_cap_does_not_starve_one_with_room_left() -> None:
    """The second browser joins late, so it still has its whole budget.

    Dropping the frame for it because an unrelated browser is full is a row it
    never gets back -- the inspect store appends without dedupe, so nothing
    later reconciles it.
    """
    hub = await _hub()
    full = await _pending(hub)
    for index in range(_CAP):
        await hub.broadcast(_WORKER, _http(index))

    fresh = await _pending(hub)
    await hub.broadcast(_WORKER, _http(9000))
    await hub.activate_browser_broadcasts(_WORKER, fresh)

    assert _delivered(fresh) == ["/api/9000"]
    assert len(hub._startup_pending_frames[full]) == _CAP, "the full browser stayed at its cap"


async def test_the_full_buffer_warning_names_the_session_and_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker id travels three call frames to get here; the cap is the budget.

    Asserted as one exact call so a reworded or sentinel-wrapped event name --
    which no substring check can distinguish -- does not pass.
    """
    hub = await _hub()
    await _pending(hub)
    for index in range(_CAP):
        await hub.broadcast(_WORKER, _http(index))
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    await hub.broadcast(_WORKER, _http(_CAP))

    recorder.warning.assert_called_once_with("startup_frame_buffer_full", worker_id=_WORKER, cap=_CAP)


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


async def test_activation_forgets_the_queue_it_just_drained() -> None:
    """The socket's entry is removed, not merely emptied — it must not leak."""
    hub = await _hub()
    starting = await _pending(hub)
    await hub.broadcast(_WORKER, _http(0))

    await hub.activate_browser_broadcasts(_WORKER, starting)

    assert starting not in hub._startup_pending_frames


async def test_a_browser_that_left_mid_startup_stays_pending() -> None:
    """Both halves of the guard are required, so ``or`` cannot stand in for ``and``.

    A socket that is no longer a browser of this session must not be released
    into the broadcast set; pending is precisely what keeps the fan-out off it.
    """
    hub = await _hub()
    gone = await _pending(hub)
    hub.registry.get(_WORKER).browsers.pop(gone)

    await hub.activate_browser_broadcasts(_WORKER, gone)

    assert gone in hub._startup_pending_browsers


async def test_a_socket_that_never_accepts_its_backlog_is_given_up_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a per-send timeout the activation loop never returns at all.

    The outer ``wait_for`` is the assertion: a stalled flush would otherwise
    hold this browser mid-startup for the life of the session.
    """
    hub = await _hub()
    starting = await _pending(hub)
    await hub.broadcast(_WORKER, _http(0))
    monkeypatch.setattr(router_impl, "_BROADCAST_SEND_TIMEOUT_S", 0.05)
    stalled = asyncio.Event()

    async def _never_completes(_payload: str) -> None:
        await stalled.wait()

    starting.send_text.side_effect = _never_completes

    await asyncio.wait_for(hub.activate_browser_broadcasts(_WORKER, starting), timeout=2.0)

    assert starting in hub._startup_pending_browsers, "a socket that failed its flush stays pending"


async def test_a_failed_flush_says_which_session_lost_its_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole backlog is dropped here, so the worker id is the only record of it."""
    hub = await _hub()
    starting = await _pending(hub)
    await hub.broadcast(_WORKER, _http(0))
    starting.send_text.side_effect = RuntimeError("socket gone")
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    await hub.activate_browser_broadcasts(_WORKER, starting)

    recorder.warning.assert_called_once_with("startup_frame_flush_failed", worker_id=_WORKER)


async def test_a_backlog_already_cleaned_up_by_a_disconnect_is_not_an_error() -> None:
    """The default on the pop is load-bearing, not defensive noise.

    The lock is released for the duration of the flush, and the route's own
    disconnect handler removes the same entry. A socket that drops mid-flush
    therefore reaches this line with its queue already gone, and popping
    without a default turns an ordinary disconnect into a ``KeyError`` raised
    out of the activation loop.
    """
    hub = await _hub()
    starting = await _pending(hub)
    await hub.broadcast(_WORKER, _http(0))

    async def _disconnect_then_fail(_payload: str) -> None:
        hub._startup_pending_frames.pop(starting, None)
        raise RuntimeError("socket gone")

    starting.send_text.side_effect = _disconnect_then_fail

    await hub.activate_browser_broadcasts(_WORKER, starting)

    assert starting in hub._startup_pending_browsers
