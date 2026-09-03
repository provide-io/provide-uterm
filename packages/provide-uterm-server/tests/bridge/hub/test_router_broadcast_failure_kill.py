#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for what happens after a browser send fails.

A failed send is not an error the caller sees -- ``broadcast`` returns None
either way -- so everything the failure path does is a side effect, and none of
it was asserted. Four separate things live here:

*The record.* A DEBUG line for every failure, and for snapshots additionally a
counter and a WARNING. The split is deliberate: term frames fail routinely on a
closing socket, so raising every failure to WARNING would bury the snapshot
case, which is the one where a committed frame silently never arrives. Both are
asserted against the logger call rather than ``caplog``, because telemetry
filters below INFO and a DEBUG assertion built on the capture fixture passes
whether or not the line was ever emitted.

*The bound on the error text.* ``str(result)[:200]`` keeps one pathological
exception from dominating a log line, so the tests use an error long enough for
the bound to matter.

*The removal.* The socket that failed is the one dropped -- the identity and
the worker id both matter, and getting either wrong leaves a dead socket in the
session failing every later broadcast the same way.

*The republish.* Removing a browser changes the roster, and the surviving
browsers are told. Skipping it leaves them showing a viewer who is gone.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.control_channel import ControlFrameDecoder
from provide.uterm.server.bridge.hub import TermHub, router_broadcast, snapshot_metrics

_WORKER = "w1"
_TERM: dict[str, Any] = {"type": "term", "data": "hello"}
_SNAPSHOT: dict[str, Any] = {"type": "snapshot", "screen": "S", "screen_hash": "sha256:abc"}

#: Long enough that the 200-character bound actually truncates it.
_LONG_ERROR = "x" * 300


async def _hub(*, browsers: int = 1) -> tuple[TermHub, list[AsyncMock]]:
    hub = TermHub()
    await hub.register_worker(_WORKER, AsyncMock())
    sockets = [AsyncMock() for _ in range(browsers)]
    state = hub.registry.get(_WORKER)
    for index, ws in enumerate(sockets):
        state.browsers[ws] = f"role-{index}"
    return hub, sockets


def _control_types(ws: AsyncMock) -> list[str]:
    """Every control-frame type the socket was sent, in order.

    A ``term`` frame goes out as bare terminal bytes rather than a control
    frame, so it does not appear here -- which is what makes a ``hijack_state``
    in this list unambiguously the republish.
    """
    decoder = ControlFrameDecoder()
    seen: list[str] = []
    for call in ws.send_text.call_args_list:
        for event in decoder.feed(call.args[0]):
            payload = getattr(event, "control", None)
            if payload is not None:
                seen.append(str(payload.get("type")))
    return seen


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


async def test_a_failed_send_says_which_worker_failed_and_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both arguments carry the meaning, and the format string is pinned verbatim.

    A reworded, case-changed or sentinel-wrapped literal is a different line to
    anyone grepping for it, so the whole call is asserted rather than a
    substring of the rendered message.
    """
    hub, (browser,) = await _hub()
    failure = RuntimeError("socket gone")
    browser.send_text.side_effect = failure
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    await hub.broadcast(_WORKER, dict(_TERM))

    recorder.debug.assert_called_once_with("broadcast_send_failed worker_id=%s: %s", _WORKER, failure)


async def test_a_failed_snapshot_send_is_counted_once_against_its_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregate the 2026-08-14 investigation needed and did not have."""
    hub, (browser,) = await _hub()
    browser.send_text.side_effect = RuntimeError("socket gone")
    counter = MagicMock()
    monkeypatch.setattr(snapshot_metrics, "snapshot_broadcast_send_failed", counter)

    await hub.broadcast(_WORKER, dict(_SNAPSHOT))

    counter.add.assert_called_once_with(1, {"worker_id": _WORKER})


async def test_a_failed_snapshot_send_warns_with_the_frame_and_a_bounded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The screen hash says which frame was lost; the bound keeps the line readable.

    The error here is 300 characters, so the assertion distinguishes the real
    200-character bound from one that is off by a character -- a difference no
    short error message can show.
    """
    hub, (browser,) = await _hub()
    browser.send_text.side_effect = RuntimeError(_LONG_ERROR)
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    await hub.broadcast(_WORKER, dict(_SNAPSHOT))

    recorder.warning.assert_called_once_with(
        "snapshot_broadcast_send_failed",
        worker_id=_WORKER,
        screen_hash="sha256:abc",
        error="x" * 200,
    )


async def test_a_failed_term_send_is_not_raised_to_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only snapshots are worth saying loudly — term frames fail on every close."""
    hub, (browser,) = await _hub()
    browser.send_text.side_effect = RuntimeError("socket gone")
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    await hub.broadcast(_WORKER, dict(_TERM))

    recorder.warning.assert_not_called()


# ---------------------------------------------------------------------------
# The removal, and the republish it triggers
# ---------------------------------------------------------------------------


async def test_the_socket_that_failed_is_the_one_dropped() -> None:
    """Identity matters in both directions: the failure is removed, the rest stay.

    Leaving it registered makes every later broadcast fail on the same socket;
    removing the wrong one disconnects a healthy viewer.
    """
    hub, (failing, healthy) = await _hub(browsers=2)
    failing.send_text.side_effect = RuntimeError("socket gone")

    await hub.broadcast(_WORKER, dict(_TERM))

    assert set(hub.registry.get(_WORKER).browsers) == {healthy}


async def test_losing_the_hijack_owner_republishes_the_state_to_the_survivors() -> None:
    """``remove_dead_browsers`` reports the ownership drop, and that gates the republish.

    Its return value is not "the roster changed" -- it is "the dashboard hijack
    owner went away", which is the case the survivors cannot infer for
    themselves. Discarding that answer, or republishing under the wrong worker
    id, leaves every other viewer showing a driver who is gone.
    """
    hub, (owner, healthy) = await _hub(browsers=2)
    hub.registry.get(_WORKER).hijack_owner = owner
    owner.send_text.side_effect = RuntimeError("socket gone")

    await hub.broadcast(_WORKER, dict(_TERM))

    assert "hijack_state" in _control_types(healthy)


async def test_a_broadcast_that_loses_nobody_republishes_nothing() -> None:
    """The near side of the same gate, so an unconditional republish cannot pass."""
    hub, (healthy,) = await _hub()

    await hub.broadcast(_WORKER, dict(_TERM))

    assert _control_types(healthy) == []
    healthy.send_text.assert_awaited_once_with("hello")
