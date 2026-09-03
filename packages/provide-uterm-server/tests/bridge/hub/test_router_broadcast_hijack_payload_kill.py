#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for what a ``hijack_state`` frame actually says.

``broadcast_hijack_state`` reads five things off the session under lock and
``send_hijack_state_to`` turns them into a per-browser frame. Every existing
test asserts that a frame arrives; none assert what is in it, so each of the
five could be read as ``None`` and the frame would still be delivered, still be
well-formed, and still be wrong.

Two pieces of that carry more than they look like:

*Which lease is reported.* A REST lease and a dashboard WS lease both expire,
and the frame carries exactly one of them: the REST session's expiry when a
valid REST lease exists, the dashboard owner's otherwise. Both operands of that
``and`` are required -- ``is_rest`` alone would dereference a missing session,
and an expired session must not be preferred over a live dashboard lease. The
two are given visibly different deadlines here so a swap is not a coincidence.

*Who ``owner`` names.* It is relative to the recipient: ``"me"`` for the
dashboard owner reading its own state, ``"other"`` for anyone else while
someone holds the terminal, and ``None`` when nobody does. A browser told
``"other"`` shows the terminal as taken; told ``None`` it offers to take it.
Getting this wrong offers control that is not available.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

from provide.uterm.bridge.coordinator import HijackSession
from provide.uterm.control_channel import ControlFrameDecoder
from provide.uterm.server.bridge.hub import TermHub

_WORKER = "w1"

#: Far enough apart that the frame says unambiguously which one it reported.
_REST_LEASE_MONO_OFFSET = 900.0
_DASHBOARD_LEASE_MONO_OFFSET = 30.0


async def _hub(*, browsers: int = 1) -> tuple[TermHub, list[AsyncMock]]:
    hub = TermHub()
    await hub.register_worker(_WORKER, AsyncMock())
    sockets = [AsyncMock() for _ in range(browsers)]
    state = hub.registry.get(_WORKER)
    for index, ws in enumerate(sockets):
        state.browsers[ws] = f"role-{index}"
    return hub, sockets


def _hijack_frame(ws: AsyncMock) -> dict[str, Any]:
    """The single hijack_state frame the socket was sent."""
    decoder = ControlFrameDecoder()
    frames = [
        dict(event.control)
        for call in ws.send_text.call_args_list
        for event in decoder.feed(call.args[0])
        if getattr(event, "control", None) is not None and event.control.get("type") == "hijack_state"
    ]
    assert len(frames) == 1, f"expected exactly one hijack_state, got {len(frames)}"
    return frames[0]


def _rest_session(*, expires_in: float) -> HijackSession:
    return HijackSession(hijack_id="h1", owner="api-caller", lease_expires_at=time.monotonic() + expires_in)


def _is_close(actual: Any, mono_deadline: float) -> bool:
    """A monotonic deadline converted to wall clock, within scheduling noise."""
    return isinstance(actual, float) and abs(actual - (time.time() + (mono_deadline - time.monotonic()))) < 1.0


# ---------------------------------------------------------------------------
# Which lease the frame reports
# ---------------------------------------------------------------------------


async def test_a_live_rest_lease_is_the_deadline_reported() -> None:
    """Both operands of the ``and`` matter, and this pins the true arm.

    The dashboard lease is set too, and much sooner, so reporting it instead is
    a visible difference rather than a rounding one.
    """
    hub, (browser,) = await _hub()
    state = hub.registry.get(_WORKER)
    state.hijack_session = _rest_session(expires_in=_REST_LEASE_MONO_OFFSET)
    state.hijack_owner_expires_at = time.monotonic() + _DASHBOARD_LEASE_MONO_OFFSET

    await hub.router.broadcast_hijack_state(_WORKER)

    frame = _hijack_frame(browser)
    assert _is_close(frame["lease_expires_at"], state.hijack_session.lease_expires_at)


async def test_an_expired_rest_lease_does_not_displace_the_dashboard_deadline() -> None:
    """The false arm: a session object exists, but it is not a *valid* lease.

    Preferring it on the strength of the object being present reports a
    deadline that has already passed, and the browser shows the terminal as
    free while a dashboard owner still holds it.
    """
    hub, (browser,) = await _hub()
    state = hub.registry.get(_WORKER)
    state.hijack_session = _rest_session(expires_in=-1.0)
    state.hijack_owner = browser
    state.hijack_owner_expires_at = time.monotonic() + _DASHBOARD_LEASE_MONO_OFFSET

    await hub.router.broadcast_hijack_state(_WORKER)

    frame = _hijack_frame(browser)
    assert _is_close(frame["lease_expires_at"], state.hijack_owner_expires_at)


async def test_a_session_with_no_lease_at_all_reports_no_deadline() -> None:
    """Nothing held, nothing to expire — the frame must say so rather than invent one."""
    hub, (browser,) = await _hub()

    await hub.router.broadcast_hijack_state(_WORKER)

    assert _hijack_frame(browser)["lease_expires_at"] is None


# ---------------------------------------------------------------------------
# Who the frame says is driving
# ---------------------------------------------------------------------------


async def test_the_dashboard_owner_is_told_the_lease_is_its_own() -> None:
    """``me`` is what lets a browser show controls rather than a lock."""
    hub, (owner,) = await _hub()
    hub.registry.get(_WORKER).hijack_owner = owner

    await hub.router.broadcast_hijack_state(_WORKER)

    frame = _hijack_frame(owner)
    assert frame["owner"] == "me"
    assert frame["hijacked"] is True


async def test_everyone_else_is_told_someone_else_is_driving() -> None:
    """The same broadcast, the other recipient — the answer is per-socket."""
    hub, (owner, viewer) = await _hub(browsers=2)
    hub.registry.get(_WORKER).hijack_owner = owner

    await hub.router.broadcast_hijack_state(_WORKER)

    assert _hijack_frame(viewer)["owner"] == "other"


async def test_a_rest_lease_with_no_dashboard_owner_still_reads_as_taken() -> None:
    """``is_rest`` alone reaches the ``other`` arm; losing it offers control that is held."""
    hub, (browser,) = await _hub()
    hub.registry.get(_WORKER).hijack_session = _rest_session(expires_in=_REST_LEASE_MONO_OFFSET)

    await hub.router.broadcast_hijack_state(_WORKER)

    frame = _hijack_frame(browser)
    assert frame["owner"] == "other"
    assert frame["hijacked"] is True


async def test_an_unheld_terminal_names_no_owner() -> None:
    """The null arm, so a constant ``other`` cannot pass either."""
    hub, (browser,) = await _hub()

    await hub.router.broadcast_hijack_state(_WORKER)

    frame = _hijack_frame(browser)
    assert frame["owner"] is None
    assert frame["hijacked"] is False


# ---------------------------------------------------------------------------
# The input mode
# ---------------------------------------------------------------------------


async def test_the_frame_carries_the_sessions_input_mode() -> None:
    """Read off the session under lock and passed through two call frames."""
    hub, (browser,) = await _hub()
    hub.registry.get(_WORKER).input_mode = "readonly"

    await hub.router.broadcast_hijack_state(_WORKER)

    assert _hijack_frame(browser)["input_mode"] == "readonly"
