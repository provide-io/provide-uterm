#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for ``router_broadcast.send_worker`` — the hub → worker path.

``send_worker`` decides, per message, which of two incompatible wire formats a
worker receives. A tunnel worker's bridge loop has no JSON envelope handling at
all, so sending it the JSON form is not a formatting difference -- it is a
message the worker cannot parse. The existing suites drive the JSON path and
leave the tunnel branch, the ownership fence, and the failure handling
unasserted; those account for the largest single cluster of surviving mutants
in the file.

Four things here are load-bearing and none of them were pinned:

*The ownership fence.* ``expected_worker`` exists so a send aimed at a
particular socket cannot land on its replacement after a reconnect. Dropping
the check sends the old session's input to the new one.

*The tunnel format split.* Input becomes raw UTF-8 PTY bytes; the three HTTP
inspect controls go framed on ``CHANNEL_HTTP``; everything else is dropped on
purpose and must still report success, because the caller treats False as "the
worker is gone" and tears the session down.

*Non-string input data.* Guarded because ``.encode()`` on a dict raises inside
the send path.

*The BaseException split.* An ordinary failure clears the socket and returns
False; a CancelledError or KeyboardInterrupt must clear the socket and then
propagate, or task cancellation is silently swallowed.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.hub import TermHub, router_broadcast

_INPUT = {"type": "input", "data": "ls -la\n"}


async def _hub_with_worker(*, tunnel: bool = False) -> tuple[TermHub, AsyncMock]:
    hub = TermHub()
    worker = AsyncMock()
    await hub.register_worker("w1", worker)
    if tunnel:
        hub.registry.get("w1").is_tunnel_worker = True
    return hub, worker


# ---------------------------------------------------------------------------
# Presence and the ownership fence
# ---------------------------------------------------------------------------


async def test_sending_to_an_unknown_worker_reports_failure() -> None:
    hub = TermHub()

    assert await hub.router.send_worker("nobody", dict(_INPUT)) is False


async def test_sending_to_a_worker_whose_socket_is_gone_reports_failure() -> None:
    """The registry entry outlives the socket; the caller needs the difference."""
    hub, _worker = await _hub_with_worker()
    hub.registry.get("w1").worker_ws = None

    assert await hub.router.send_worker("w1", dict(_INPUT)) is False


async def test_a_send_aimed_at_a_replaced_socket_is_refused() -> None:
    """``expected_worker`` is a fence against a reconnect landing the old send.

    Without it, input meant for the previous session is delivered to whoever
    reconnected under the same worker id.
    """
    hub, current = await _hub_with_worker()
    stale = AsyncMock()

    sent = await hub.router.send_worker("w1", dict(_INPUT), expected_worker=stale)

    assert sent is False
    current.send_text.assert_not_awaited()


async def test_a_send_aimed_at_the_current_socket_is_delivered() -> None:
    """The near side of the fence, so "always refuse" cannot pass."""
    hub, current = await _hub_with_worker()

    assert await hub.router.send_worker("w1", dict(_INPUT), expected_worker=current) is True
    current.send_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# Keystroke recording
# ---------------------------------------------------------------------------


async def test_input_from_a_browser_is_recorded_as_a_keystroke() -> None:
    """Feeds the behavioral heuristics; only ``input`` counts as typing."""
    hub, _worker = await _hub_with_worker()
    source = object()

    await hub.router.send_worker("w1", dict(_INPUT), source=source)

    assert source in hub.router.keystroke_timestamps


async def test_a_non_input_message_is_not_recorded_as_a_keystroke() -> None:
    """A resize or a control frame is not the user typing."""
    hub, _worker = await _hub_with_worker()
    source = object()

    await hub.router.send_worker("w1", {"type": "resize", "cols": 80}, source=source)

    assert source not in hub.router.keystroke_timestamps


async def test_input_with_no_source_records_nothing() -> None:
    """Hub-originated input has no browser to attribute."""
    hub, _worker = await _hub_with_worker()

    await hub.router.send_worker("w1", dict(_INPUT))

    assert hub.router.keystroke_timestamps == {}


# ---------------------------------------------------------------------------
# The tunnel wire format
# ---------------------------------------------------------------------------


async def test_a_tunnel_worker_receives_input_as_raw_pty_bytes() -> None:
    """Not JSON: the tunnel bridge loop has no envelope handling to parse it."""
    hub, worker = await _hub_with_worker(tunnel=True)

    assert await hub.router.send_worker("w1", dict(_INPUT)) is True

    worker.send_bytes.assert_awaited_once_with(b"ls -la\n")
    worker.send_text.assert_not_awaited()


async def test_a_plain_worker_receives_the_json_envelope() -> None:
    """The other half of the split — the same message, the other format."""
    hub, worker = await _hub_with_worker()

    assert await hub.router.send_worker("w1", dict(_INPUT)) is True

    worker.send_text.assert_awaited_once()
    worker.send_bytes.assert_not_awaited()


@pytest.mark.parametrize("control", ["http_action", "http_intercept_toggle", "http_inspect_toggle"])
async def test_http_inspect_controls_go_framed_down_the_http_channel(control: str) -> None:
    """These three are the exception: JSON, but framed on the HTTP side-channel.

    The payload is compact-separated JSON inside the frame, so the assertion
    reads the bytes back rather than trusting that something was sent.
    """
    hub, worker = await _hub_with_worker(tunnel=True)
    msg: dict[str, Any] = {"type": control, "id": "x1"}

    assert await hub.router.send_worker("w1", dict(msg)) is True

    worker.send_bytes.assert_awaited_once()
    frame = worker.send_bytes.await_args.args[0]
    assert json.dumps(msg, separators=(",", ":")).encode() in frame


async def test_a_tunnel_worker_silently_drops_other_message_types_but_reports_success() -> None:
    """Reporting False here would read as "the worker is gone" and end the session."""
    hub, worker = await _hub_with_worker(tunnel=True)

    assert await hub.router.send_worker("w1", {"type": "resize", "cols": 80}) is True

    worker.send_bytes.assert_not_awaited()
    worker.send_text.assert_not_awaited()


async def test_tunnel_input_that_is_not_a_string_is_dropped_rather_than_encoded() -> None:
    """``.encode()`` on a dict raises inside the send path; the guard is real."""
    hub, worker = await _hub_with_worker(tunnel=True)

    assert await hub.router.send_worker("w1", {"type": "input", "data": {"not": "a string"}}) is True

    worker.send_bytes.assert_not_awaited()


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


async def test_a_failed_send_reports_failure_and_forgets_the_socket() -> None:
    """Leaving a dead socket registered makes every later send fail the same way."""
    hub, worker = await _hub_with_worker()
    worker.send_text.side_effect = RuntimeError("socket gone")

    assert await hub.router.send_worker("w1", dict(_INPUT)) is False
    assert hub.registry.get("w1").worker_ws is None


async def test_a_failed_send_does_not_clear_a_socket_that_has_been_replaced() -> None:
    """Only the socket that failed is dropped, never its successor.

    Clearing unconditionally would disconnect a healthy reconnect that arrived
    while the failing send was in flight.
    """
    hub, worker = await _hub_with_worker()
    replacement = AsyncMock()

    async def _fail_then_swap(_payload: str) -> None:
        hub.registry.get("w1").worker_ws = replacement
        raise RuntimeError("socket gone")

    worker.send_text.side_effect = _fail_then_swap

    assert await hub.router.send_worker("w1", dict(_INPUT)) is False
    assert hub.registry.get("w1").worker_ws is replacement


async def test_cancellation_clears_the_socket_and_still_propagates() -> None:
    """A BaseException is not a send failure — swallowing it breaks task cancellation."""
    hub, worker = await _hub_with_worker()
    worker.send_text.side_effect = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        await hub.router.send_worker("w1", dict(_INPUT))

    assert hub.registry.get("w1").worker_ws is None


async def test_a_failed_send_says_which_worker_failed_and_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only record of a dropped worker send is this one debug line.

    Asserted against the logger call rather than caplog: telemetry filters
    below INFO, so a DEBUG record never reaches the capture fixture and an
    assertion built on it would pass vacuously.

    Both arguments carry the meaning -- the worker id says whose session died,
    the exception says why -- and the format string is asserted verbatim, so a
    reworded, case-changed or sentinel-wrapped literal does not slip through.
    """
    hub, worker = await _hub_with_worker()
    failure = RuntimeError("socket gone")
    worker.send_text.side_effect = failure
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    assert await hub.router.send_worker("w1", dict(_INPUT)) is False

    recorder.debug.assert_called_once_with("send_worker_failed worker_id=%s: %s", "w1", failure)
