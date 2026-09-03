#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the browser fan-out in ``router_broadcast``.

``_broadcast_to_current_browsers`` is the last thing between a committed frame
and the socket. It carries four decisions that no existing test asserts, all of
them behind 100% line coverage because the happy path walks straight through
them:

*The ownership fence, checked twice.* Once before the output-policy await and
once after it. The second check is the load-bearing one -- policy evaluation
awaits, and a worker can be replaced while it does -- but the first is what
stops a stale frame from being buffered for a starting-up browser and from
spending a policy build. Both checks require BOTH ``expected_worker`` and
``expected_event_seq``; either one alone must not arm the fence.

*The 0/1-vs-many split.* Below two browsers the sends run sequentially, above
it they are fanned out through ``gather(return_exceptions=True)``. Those two
paths do not treat a ``BaseException`` the same way: the sequential arm catches
only ``Exception``, so a cancellation propagates, while ``gather`` captures it
and returns it as a value. That difference is the only observable one, and it
is what pins the boundary at exactly ``<= 1``.

*The per-send timeout.* Without it one stalled socket holds the fan-out open
for as long as it likes. Asserted by stalling a socket forever and requiring
the broadcast to finish anyway.

*The no-browsers counter.* A snapshot that reaches nobody is a real delivery
failure and the counter is the only aggregate record of it, so it must fire
exactly when there are no eligible browsers -- and must not fire when there
are.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.hub import TermHub, router_broadcast, router_impl, snapshot_metrics
from provide.uterm.server.bridge.hub.ext import OutputPolicyGate, RedactionRule

_WORKER = "w1"
_TERM: dict[str, Any] = {"type": "term", "data": "hello"}
_SNAPSHOT: dict[str, Any] = {"type": "snapshot", "screen": "S", "screen_hash": "sha256:abc", "event_seq": 7}


class _Aborted(BaseException):
    """A non-``Exception`` failure, the way a cancellation is.

    Not ``KeyboardInterrupt``: pytest treats that as a request to abandon the
    whole session, which ends the run instead of asserting on it.
    """


class _NoRules(OutputPolicyGate):
    """An active gate that redacts nothing — enough to arm ``gate_active``."""

    async def get_redaction_rules(self, _context: Any) -> list[RedactionRule]:
        return []


async def _hub(*, browsers: int = 0, gate: OutputPolicyGate | None = None) -> tuple[TermHub, list[AsyncMock]]:
    hub = TermHub(output_policy_gate=gate) if gate is not None else TermHub()
    await hub.register_worker(_WORKER, AsyncMock())
    sockets = [AsyncMock() for _ in range(browsers)]
    state = hub.registry.get(_WORKER)
    for index, ws in enumerate(sockets):
        state.browsers[ws] = f"role-{index}"
    return hub, sockets


# ---------------------------------------------------------------------------
# The ownership fence — before the policy await
# ---------------------------------------------------------------------------


async def test_a_frame_from_a_replaced_worker_is_dropped_before_it_is_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both halves of the fence are required; neither alone may arm it.

    Called directly rather than through ``broadcast``, which has its own copy of
    the same check and would return first. The counter is the observable: an
    unfenced frame reaches the no-browsers branch and records a delivery failure
    for a snapshot that was never current.
    """
    hub, _ = await _hub()
    counter = MagicMock()
    monkeypatch.setattr(snapshot_metrics, "snapshot_broadcast_no_browsers", counter)

    await router_broadcast._broadcast_to_current_browsers(
        hub.router,
        _WORKER,
        dict(_SNAPSHOT),
        expected_worker=AsyncMock(),
        expected_event_seq=7,
    )

    counter.add.assert_not_called()


async def test_an_event_seq_with_no_expected_worker_does_not_arm_the_fence() -> None:
    """``and``, not ``or``: a half-specified contract must broadcast normally.

    With ``or``, the post-policy revalidation runs against ``expected_worker=None``,
    finds the state does not own it, and silently drops a frame that had no
    ownership claim to check in the first place.
    """
    hub, (browser,) = await _hub(browsers=1)

    await router_broadcast._broadcast_to_current_browsers(
        hub.router,
        _WORKER,
        dict(_TERM),
        expected_worker=None,
        expected_event_seq=7,
    )

    browser.send_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# The no-browsers counter and its trace line
# ---------------------------------------------------------------------------


async def test_a_snapshot_reaching_nobody_is_counted_once_against_its_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the counter is the worker attribution and the tally."""
    hub, _ = await _hub()
    counter = MagicMock()
    monkeypatch.setattr(snapshot_metrics, "snapshot_broadcast_no_browsers", counter)

    await hub.broadcast(_WORKER, dict(_SNAPSHOT))

    counter.add.assert_called_once_with(1, {"worker_id": _WORKER})


async def test_a_snapshot_that_did_reach_a_browser_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """``and``, not ``or`` — counting a delivered snapshot makes the metric useless."""
    hub, _ = await _hub(browsers=1)
    counter = MagicMock()
    monkeypatch.setattr(snapshot_metrics, "snapshot_broadcast_no_browsers", counter)

    await hub.broadcast(_WORKER, dict(_SNAPSHOT))

    counter.add.assert_not_called()


async def test_the_undelivered_snapshot_trace_names_the_worker_the_screen_and_the_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted as one exact call.

    ``trace`` is a no-op unless TRACE is enabled, so no capture fixture ever
    sees this record and a caplog-based assertion would pass vacuously. The
    screen hash is what makes the line answer "which frame", so it is pinned
    alongside the event name.
    """
    hub, _ = await _hub()
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    await hub.broadcast(_WORKER, dict(_SNAPSHOT))

    recorder.trace.assert_called_once_with(
        "snapshot_broadcast_no_browsers",
        worker_id=_WORKER,
        screen_hash="sha256:abc",
        registered=0,
    )


# ---------------------------------------------------------------------------
# The per-send timeout
# ---------------------------------------------------------------------------


async def test_a_socket_that_never_completes_its_send_is_dropped_not_waited_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the timeout the fan-out never returns at all.

    The outer ``wait_for`` is the assertion: it is what fails if the per-send
    budget stops being applied, since a stalled socket would otherwise hold the
    broadcast open forever.
    """
    hub, (browser,) = await _hub(browsers=1)
    monkeypatch.setattr(router_impl, "_BROADCAST_SEND_TIMEOUT_S", 0.05)
    stalled = asyncio.Event()

    async def _never_completes(_payload: str) -> None:
        await stalled.wait()

    browser.send_text.side_effect = _never_completes

    await asyncio.wait_for(hub.broadcast(_WORKER, dict(_TERM)), timeout=2.0)

    assert browser not in hub.registry.get(_WORKER).browsers, "a stalled socket is a dead socket"


# ---------------------------------------------------------------------------
# The 0/1-vs-many boundary
# ---------------------------------------------------------------------------


async def test_a_lone_browser_is_sent_to_directly_so_cancellation_still_propagates() -> None:
    """At one browser the send is awaited, not gathered.

    ``gather(return_exceptions=True)`` turns a ``BaseException`` into a return
    value, so routing a single browser through it would swallow a cancellation
    and report the broadcast as having succeeded.
    """
    hub, (browser,) = await _hub(browsers=1)
    browser.send_text.side_effect = _Aborted()

    with pytest.raises(_Aborted):
        await hub.broadcast(_WORKER, dict(_TERM))


async def test_two_browsers_fan_out_so_one_cancellation_does_not_strand_the_other() -> None:
    """At two browsers the sends are gathered, which is the other side of ``<= 1``.

    Sequentially, the first socket's ``BaseException`` escapes before the second
    is ever written to -- one viewer's failure silently blanks another's screen.
    """
    hub, (failing, healthy) = await _hub(browsers=2)
    failing.send_text.side_effect = _Aborted()

    await hub.broadcast(_WORKER, dict(_TERM))

    healthy.send_text.assert_awaited_once()


async def test_every_fanned_out_browser_is_sent_its_own_roles_payload() -> None:
    """The role travels with the socket into the concurrent send.

    Dropping it looks up a role that was never resolved, and because ``gather``
    captures the resulting ``KeyError`` as an ordinary send failure, every
    browser in the fan-out is quietly declared dead instead.
    """
    hub, (first, second) = await _hub(browsers=2, gate=_NoRules())

    await hub.broadcast(_WORKER, dict(_TERM))

    state = hub.registry.get(_WORKER)
    assert set(state.browsers) == {first, second}, "a live viewer was dropped as dead"
    first.send_text.assert_awaited_once()
    second.send_text.assert_awaited_once()
