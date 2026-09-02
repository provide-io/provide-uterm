#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Frames broadcast while a browser is still starting up must not be lost.

A browser registers with ``defer_broadcast=True`` so its hello, hijack_state
and presence_sync arrive before anything else. Until it is activated it is not
in the broadcast set at all, and what is broadcast meanwhile used to be
dropped on the floor.

That is right for frames the startup sequence already carries — a ``term``
chunk is superseded by the hello's ``initial_snapshot``, and replaying it would
print the screen twice. It is wrong for the inspect channel, which has no
replay: the browser builds that list from nothing and the store appends without
dedupe, so a dropped ``http_req`` is a row missing for the rest of the session.

Measured as a Playwright flake before it was understood: multi-backend
csharp failing "element(s) not found" on a row the worker demonstrably sent
(docs/ard-startup-broadcast-window.md).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from provide.uterm.control_channel import ControlFrameDecoder
from provide.uterm.server.bridge.hub import TermHub

_HTTP_REQ: dict[str, Any] = {
    "type": "http_req",
    "id": "r1",
    "method": "GET",
    "url": "/api/users",
    "_channel": "http",
}


def _decoded(ws: AsyncMock) -> list[dict[str, Any]]:
    """Every control frame the socket was actually sent, in order."""
    decoder = ControlFrameDecoder()
    seen: list[dict[str, Any]] = []
    for call in ws.send_text.call_args_list:
        for event in decoder.feed(call.args[0]):
            payload = getattr(event, "control", None)
            if payload is not None:
                seen.append(payload)
    return seen


async def _hub_with_worker(worker_id: str) -> TermHub:
    hub = TermHub()
    await hub.register_worker(worker_id, AsyncMock())
    return hub


@pytest.mark.asyncio
async def test_an_inspect_frame_sent_during_startup_is_delivered_on_activation() -> None:
    """The regression: this row used to be dropped and never arrive at all."""
    hub = await _hub_with_worker("w-buffer")
    browser = AsyncMock()
    await hub.register_browser("w-buffer", browser, "viewer", defer_broadcast=True)

    await hub.broadcast("w-buffer", dict(_HTTP_REQ))
    assert _decoded(browser) == [], "a browser mid-startup must not be written to yet"

    await hub.activate_browser_broadcasts("w-buffer", browser)

    assert [frame["url"] for frame in _decoded(browser)] == ["/api/users"]


@pytest.mark.asyncio
async def test_buffered_inspect_frames_keep_their_order() -> None:
    hub = await _hub_with_worker("w-order")
    browser = AsyncMock()
    await hub.register_browser("w-order", browser, "viewer", defer_broadcast=True)

    for index in range(3):
        await hub.broadcast("w-order", {**_HTTP_REQ, "id": f"r{index}", "url": f"/api/{index}"})
    await hub.activate_browser_broadcasts("w-order", browser)

    assert [frame["url"] for frame in _decoded(browser)] == ["/api/0", "/api/1", "/api/2"]


@pytest.mark.asyncio
async def test_terminal_output_from_the_window_is_not_replayed() -> None:
    """The hello's initial_snapshot already covers it; replaying prints twice."""
    hub = await _hub_with_worker("w-term")
    browser = AsyncMock()
    await hub.register_browser("w-term", browser, "viewer", defer_broadcast=True)

    await hub.broadcast("w-term", {"type": "term", "data": "ls -la\r\n", "ts": 1.0})
    await hub.activate_browser_broadcasts("w-term", browser)

    assert browser.send_text.call_args_list == []


@pytest.mark.asyncio
async def test_a_presence_sync_from_the_window_is_delivered_on_activation() -> None:
    """A browser that joins while another is mid-handshake must still be seen.

    The startup sequence sends each browser its own presence_sync, which is why
    presence used to be dropped here -- but that sync is computed at the
    browser's OWN join, so it cannot carry a user who arrives afterwards. The
    roster stayed one user short until some later presence event corrected it.
    """
    hub = await _hub_with_worker("w-presence")
    browser = AsyncMock()
    await hub.register_browser("w-presence", browser, "viewer", defer_broadcast=True)

    await hub.broadcast(
        "w-presence", {"type": "presence_sync", "users": [{"user_id": "a"}, {"user_id": "b"}], "config": {}}
    )
    assert _decoded(browser) == [], "a browser mid-startup must not be written to yet"

    await hub.activate_browser_broadcasts("w-presence", browser)

    assert [len(frame["users"]) for frame in _decoded(browser)] == [2]


@pytest.mark.asyncio
async def test_a_presence_leave_from_the_window_is_delivered_on_activation() -> None:
    """Worse than a missed sync: a delta, so a dropped leave leaves a ghost."""
    hub = await _hub_with_worker("w-leave")
    browser = AsyncMock()
    await hub.register_browser("w-leave", browser, "viewer", defer_broadcast=True)

    await hub.broadcast("w-leave", {"type": "presence_leave", "user_id": "departed"})
    await hub.activate_browser_broadcasts("w-leave", browser)

    assert [frame["user_id"] for frame in _decoded(browser)] == ["departed"]


@pytest.mark.asyncio
async def test_a_control_transfer_from_the_window_is_delivered_on_activation() -> None:
    """Who is driving is a delta too, and nothing restates it.

    The startup presence_sync stamps ``is_owner`` per user, so it carries the
    driver as of this browser's join. A handover during the window is lost, and
    the next control_transfer only comes when someone next takes or drops
    control -- until then the browser shows the wrong driver.
    """
    hub = await _hub_with_worker("w-control")
    browser = AsyncMock()
    await hub.register_browser("w-control", browser, "viewer", defer_broadcast=True)

    await hub.broadcast(
        "w-control",
        {"type": "control_transfer", "from_user_id": "a", "to_user_id": "b", "reason": "handover"},
    )
    await hub.activate_browser_broadcasts("w-control", browser)

    assert [frame["to_user_id"] for frame in _decoded(browser)] == ["b"]


@pytest.mark.asyncio
async def test_a_presence_update_from_the_window_is_not_replayed() -> None:
    """Transient per-user state: the next one supersedes it, so it stays dropped.

    Deliberate, not an oversight -- these are frequent enough to crowd out the
    buffer's cap, and nothing is lost that the next update does not restate.
    """
    hub = await _hub_with_worker("w-update")
    browser = AsyncMock()
    await hub.register_browser("w-update", browser, "viewer", defer_broadcast=True)

    await hub.broadcast(
        "w-update", {"type": "presence_update", "user_id": "a", "name": "A", "color": "#fff", "role": "viewer"}
    )
    await hub.activate_browser_broadcasts("w-update", browser)

    assert browser.send_text.call_args_list == []


@pytest.mark.asyncio
async def test_an_activated_browser_receives_inspect_frames_directly() -> None:
    """After activation the buffer is out of the path entirely."""
    hub = await _hub_with_worker("w-live")
    browser = AsyncMock()
    await hub.register_browser("w-live", browser, "viewer", defer_broadcast=True)
    await hub.activate_browser_broadcasts("w-live", browser)

    await hub.broadcast("w-live", dict(_HTTP_REQ))

    assert [frame["url"] for frame in _decoded(browser)] == ["/api/users"]
    assert browser not in hub._startup_pending_frames


@pytest.mark.asyncio
async def test_the_buffer_is_capped_rather_than_unbounded() -> None:
    """A browser that never activates must not be able to grow this forever."""
    from provide.uterm.server.bridge.hub.router_broadcast import _STARTUP_BUFFER_MAX_FRAMES

    hub = await _hub_with_worker("w-cap")
    browser = AsyncMock()
    await hub.register_browser("w-cap", browser, "viewer", defer_broadcast=True)

    for index in range(_STARTUP_BUFFER_MAX_FRAMES + 25):
        await hub.broadcast("w-cap", {**_HTTP_REQ, "id": f"r{index}"})

    assert len(hub._startup_pending_frames[browser]) == _STARTUP_BUFFER_MAX_FRAMES


@pytest.mark.asyncio
async def test_a_disconnecting_browser_drops_its_backlog() -> None:
    """Nothing will ever flush it, so holding it is a leak."""
    hub = await _hub_with_worker("w-gone")
    browser = AsyncMock()
    await hub.register_browser("w-gone", browser, "viewer", defer_broadcast=True)
    await hub.broadcast("w-gone", dict(_HTTP_REQ))
    assert browser in hub._startup_pending_frames

    await hub.cleanup_browser_disconnect("w-gone", browser, False)

    assert browser not in hub._startup_pending_frames


@pytest.mark.asyncio
async def test_a_socket_that_cannot_take_its_backlog_drops_it() -> None:
    """A failed flush drops the backlog and leaves the socket skipped.

    Pending is the right resting state for a socket that just failed a write:
    the broadcast path skips it rather than retrying into a dead connection,
    and the route's disconnect handler clears both.
    """
    hub = await _hub_with_worker("w-dead")
    browser = AsyncMock()
    browser.send_text.side_effect = RuntimeError("socket gone")
    await hub.register_browser("w-dead", browser, "viewer", defer_broadcast=True)
    await hub.broadcast("w-dead", dict(_HTTP_REQ))

    await hub.activate_browser_broadcasts("w-dead", browser)

    assert browser not in hub._startup_pending_frames
    assert browser in hub._startup_pending_browsers
