#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the second pass of ``broadcast_hijack_state``.

When a hijack_state send fails, the socket is dropped and the whole session
state is re-read and re-sent to whoever is left. That second pass is a complete
duplicate of the first -- five state reads, eight arguments -- and no test
reached it, so every one of those could be ``None`` and nothing failed.

It exists because the removal can change the answer. The socket that failed may
have been the one holding the terminal, and dropping it releases the lease. The
survivors were told "someone else is driving" microseconds earlier; without the
re-send they keep showing a locked terminal owned by a browser that is gone,
until something else happens to correct them.

Two details are easy to get backwards:

*Errors are suppressed the second time and not the first.* A first-pass failure
is news -- it is why the socket is being dropped. A second-pass failure is the
same socket failing again on the way out, and logging it twice makes one dead
browser look like two.

*The survivors are the ACTIVE browsers.* A browser still inside its startup
window is excluded from both passes; it is sent its own hijack_state directly
by the startup sequence, and writing to it here races that.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.bridge.coordinator import HijackSession
from provide.uterm.control_channel import ControlFrameDecoder
from provide.uterm.server.bridge.hub import TermHub, router_broadcast, router_impl

_WORKER = "w1"

#: Far enough apart that the frame says unambiguously which lease it reported.
_REST_LEASE_S = 900.0
_DASHBOARD_LEASE_S = 30.0


def _is_close(actual: Any, mono_deadline: float) -> bool:
    """A monotonic deadline converted to wall clock, within scheduling noise."""
    return isinstance(actual, float) and abs(actual - (time.time() + (mono_deadline - time.monotonic()))) < 1.0


async def _hub(*, browsers: int = 2) -> tuple[TermHub, list[AsyncMock]]:
    hub = TermHub()
    await hub.register_worker(_WORKER, AsyncMock())
    sockets = [AsyncMock() for _ in range(browsers)]
    state = hub.registry.get(_WORKER)
    for index, ws in enumerate(sockets):
        state.browsers[ws] = f"role-{index}"
    return hub, sockets


def _hijack_frames(ws: AsyncMock) -> list[dict[str, Any]]:
    """Every hijack_state frame the socket was sent, in order."""
    decoder = ControlFrameDecoder()
    return [
        dict(event.control)
        for call in ws.send_text.call_args_list
        for event in decoder.feed(call.args[0])
        if getattr(event, "control", None) is not None and event.control.get("type") == "hijack_state"
    ]


# ---------------------------------------------------------------------------
# The removal
# ---------------------------------------------------------------------------


async def test_a_browser_that_cannot_take_the_frame_is_dropped_from_the_session() -> None:
    """The dead set has to hold the socket that failed, under the right worker.

    Collecting the wrong object, or removing under a worker id that is not this
    session, leaves the socket registered and every later broadcast fails on it
    exactly the same way.
    """
    hub, (failing, healthy) = await _hub()
    failing.send_text.side_effect = RuntimeError("socket gone")

    await hub.router.broadcast_hijack_state(_WORKER)

    assert set(hub.registry.get(_WORKER).browsers) == {healthy}


async def test_the_failure_says_which_session_lost_a_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """First-pass failures are reported — that is what ``suppress_errors=False`` means.

    Asserted as one exact call: the format string is what somebody greps for,
    and a case-changed or sentinel-wrapped literal is a different line to them.
    """
    hub, (failing, _healthy) = await _hub()
    failure = RuntimeError("socket gone")
    failing.send_text.side_effect = failure
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    await hub.router.broadcast_hijack_state(_WORKER)

    recorder.debug.assert_called_once_with("broadcast_hijack_state_send_failed worker_id=%s: %s", _WORKER, failure)


async def test_a_stalled_browser_does_not_hold_the_notification_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-send timeout, in parity with ``broadcast``.

    The outer ``wait_for`` is the assertion: without a budget the first stalled
    socket blocks the hijack-state notification to every browser behind it, for
    as long as it likes.
    """
    hub, (stalling, healthy) = await _hub()
    monkeypatch.setattr(router_impl, "_BROADCAST_SEND_TIMEOUT_S", 0.05)
    stalled = asyncio.Event()

    async def _never_completes(_payload: str) -> None:
        await stalled.wait()

    stalling.send_text.side_effect = _never_completes

    await asyncio.wait_for(hub.router.broadcast_hijack_state(_WORKER), timeout=2.0)

    assert set(hub.registry.get(_WORKER).browsers) == {healthy}


# ---------------------------------------------------------------------------
# The second pass
# ---------------------------------------------------------------------------


async def test_the_survivors_are_re_sent_the_state_after_the_removal() -> None:
    """Reaching the second pass at all: two frames, not one."""
    hub, (failing, healthy) = await _hub()
    failing.send_text.side_effect = RuntimeError("socket gone")

    await hub.router.broadcast_hijack_state(_WORKER)

    assert len(_hijack_frames(healthy)) == 2


async def test_the_second_frame_reports_the_lease_the_removal_released() -> None:
    """The whole reason the second pass exists.

    The socket that failed was holding the terminal. The survivor was told
    ``other`` before the removal; afterwards nobody is driving, and if the
    second pass re-reads anything as ``None`` -- or re-sends the state it
    already sent -- the survivor keeps offering a lock that no longer exists.
    """
    hub, (owner, healthy) = await _hub()
    state = hub.registry.get(_WORKER)
    state.hijack_owner = owner
    state.input_mode = "readonly"
    owner.send_text.side_effect = RuntimeError("socket gone")

    await hub.router.broadcast_hijack_state(_WORKER)

    before, after = _hijack_frames(healthy)
    assert (before["hijacked"], before["owner"]) == (True, "other")
    assert (after["hijacked"], after["owner"], after["lease_expires_at"]) == (False, None, None)
    assert after["input_mode"] == "readonly", "the second pass re-reads the mode, it does not blank it"


async def test_a_browser_still_starting_up_is_not_written_to_by_either_pass() -> None:
    """Pending browsers are excluded from the survivor list, not selected by it."""
    hub, (failing, healthy) = await _hub()
    starting = AsyncMock()
    await hub.register_browser(_WORKER, starting, "viewer", defer_broadcast=True)
    failing.send_text.side_effect = RuntimeError("socket gone")

    await hub.router.broadcast_hijack_state(_WORKER)

    assert _hijack_frames(starting) == []
    assert len(_hijack_frames(healthy)) == 2


async def test_a_second_failure_on_the_way_out_is_not_reported_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``suppress_errors=True`` on the second call, and it has to be ``True``.

    The survivor here fails only on the re-send. That is the same socket dying
    that the first pass already reported, and counting it again turns one dead
    browser into two in the log.
    """
    hub, (failing, flaky) = await _hub()
    failing.send_text.side_effect = RuntimeError("socket gone")
    flaky.send_text.side_effect = [None, RuntimeError("socket gone too")]
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    await hub.router.broadcast_hijack_state(_WORKER)

    assert recorder.debug.call_count == 1


async def test_the_second_pass_re_reads_a_lease_that_outlived_the_removal() -> None:
    """The removal does not always release the terminal, and the re-read must show that.

    The socket that failed was an ordinary viewer; the dashboard owner is still
    here. Asserting the released case alone cannot see any of this -- reading
    the owner, the dashboard flag or the deadline as ``None`` produces exactly
    the "nobody is driving" frame that a genuine release produces, so the two
    are indistinguishable unless a lease survives.

    The session also carries an EXPIRED REST lease, which must not displace the
    live dashboard deadline: both operands of that ``and`` are load-bearing, and
    the two deadlines are far apart so a swap is unmistakable.
    """
    hub, (failing, owner, healthy) = await _hub(browsers=3)
    state = hub.registry.get(_WORKER)
    state.hijack_owner = owner
    state.hijack_owner_expires_at = time.monotonic() + _DASHBOARD_LEASE_S
    state.hijack_session = HijackSession(hijack_id="h-old", owner="api-caller", lease_expires_at=time.monotonic() - 1.0)
    failing.send_text.side_effect = RuntimeError("socket gone")

    await hub.router.broadcast_hijack_state(_WORKER)

    after = _hijack_frames(healthy)[1]
    assert (after["hijacked"], after["owner"]) == (True, "other")
    assert _is_close(after["lease_expires_at"], state.hijack_owner_expires_at)
    assert _hijack_frames(owner)[1]["owner"] == "me", "the owner is still the owner after the re-read"


async def test_the_second_pass_re_reads_a_rest_lease_with_no_dashboard_owner() -> None:
    """The other arm: a REST lease alone still reads as taken, with its own deadline.

    With no dashboard owner, ``is_rest`` is the only thing keeping the frame
    from offering control of a terminal the API is holding, and the REST
    session is the only place its deadline comes from.
    """
    hub, (failing, healthy) = await _hub()
    state = hub.registry.get(_WORKER)
    state.hijack_session = HijackSession(
        hijack_id="h1", owner="api-caller", lease_expires_at=time.monotonic() + _REST_LEASE_S
    )
    failing.send_text.side_effect = RuntimeError("socket gone")

    await hub.router.broadcast_hijack_state(_WORKER)

    after = _hijack_frames(healthy)[1]
    assert (after["hijacked"], after["owner"]) == (True, "other")
    assert _is_close(after["lease_expires_at"], state.hijack_session.lease_expires_at)
