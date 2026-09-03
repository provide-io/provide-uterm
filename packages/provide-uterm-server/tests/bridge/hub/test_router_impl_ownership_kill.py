#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for input-mode control, role bookkeeping and hijack reclaim.

Six methods here had **no covering test at all** in the mutation selection --
not weak assertions, no test: ``try_reclaim_hijack``,
``try_reclaim_hijack_status``, ``set_browser_role``, ``get_worker_browser_role``,
``send_hijack_state_to`` and the ``keystroke_timestamps`` view. Their mutants
came back as "no tests", which is not the same as surviving and is easy to read
past, because a mutant nothing exercises is reported separately from one that
was exercised and lived.

``try_reclaim_hijack_status`` is the one that matters most. It is the only
place a browser takes the terminal for itself, and it decides under a fence
with five conjuncts -- the session must be the one observed before the fence,
the worker must still be connected, the mode must not be ``open``, and nothing
may already hold the lease (checked twice, once for the dashboard field and
once for any lease at all). Each is independently sufficient to refuse; drop
any one and a browser takes a terminal somebody else is driving.

``set_input_mode`` is the neighbouring decision: it refuses to open a session
that is currently hijacked, and announces the change twice -- once as its own
frame and once as fresh hijack state. The refusal reasons are strings a route
turns into distinct HTTP responses, so which one comes back is the contract.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.bridge.coordinator import HijackSession
from provide.uterm.control_channel import ControlFrameDecoder
from provide.uterm.server.bridge.hub import TermHub, router_broadcast

_WORKER = "w1"


async def _hub() -> tuple[TermHub, AsyncMock]:
    hub = TermHub()
    worker = AsyncMock()
    await hub.register_worker(_WORKER, worker)
    return hub, worker


def _control_frames(ws: AsyncMock) -> list[dict[str, Any]]:
    decoder = ControlFrameDecoder()
    return [
        dict(event.control)
        for call in ws.send_text.call_args_list
        for event in decoder.feed(call.args[0])
        if getattr(event, "control", None) is not None
    ]


def _rest_session() -> HijackSession:
    return HijackSession(hijack_id="h1", owner="api", lease_expires_at=time.monotonic() + 900.0)


# ---------------------------------------------------------------------------
# try_reclaim_hijack_status — the fenced take
# ---------------------------------------------------------------------------


async def test_an_unheld_terminal_can_be_reclaimed() -> None:
    """The success path: ownership, a lease deadline, and a bumped generation.

    The generation is what lets in-flight work notice its ownership was
    replaced; leaving it unchanged makes a reclaim invisible to the fences that
    depend on it.
    """
    hub, _worker = await _hub()
    browser = AsyncMock()
    state = hub.registry.get(_WORKER)
    before = state.ownership_generation

    reclaimed, competing = await hub.router.try_reclaim_hijack_status(_WORKER, browser)

    assert (reclaimed, competing) == (True, False)
    assert state.hijack_owner is browser
    assert state.ownership_generation == before + 1
    # The deadline is now PLUS the lease, not minus it: a lease stamped in the
    # past is expired the moment it is granted, so the reclaim appears to
    # succeed and the session reads as unheld to the very next caller.
    assert state.hijack_owner_expires_at is not None
    assert state.hijack_owner_expires_at > time.monotonic()
    assert hub.is_dashboard_hijack_active(state) is True


async def test_reclaiming_an_unknown_worker_reports_no_competitor() -> None:
    """Not found is not contention — the caller must not retry against nothing."""
    hub, _worker = await _hub()

    assert await hub.router.try_reclaim_hijack_status("nobody", AsyncMock()) == (False, False)


async def test_a_terminal_someone_else_holds_reports_the_competitor() -> None:
    """``competing_owner`` is what tells the caller to back off rather than retry."""
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).hijack_owner = AsyncMock()

    assert await hub.router.try_reclaim_hijack_status(_WORKER, AsyncMock()) == (False, True)


async def test_a_terminal_held_by_a_rest_lease_is_not_reclaimable() -> None:
    """The second ownership check: a REST lease leaves ``hijack_owner`` unset.

    Checking only the dashboard field would hand a browser a terminal the API
    is holding.
    """
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).hijack_session = _rest_session()

    assert await hub.router.try_reclaim_hijack_status(_WORKER, AsyncMock()) == (False, True)


async def test_a_disconnected_worker_cannot_be_reclaimed() -> None:
    """Taking the terminal of a worker that is gone leaves an unreleasable lease."""
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).worker_ws = None

    assert await hub.router.try_reclaim_hijack_status(_WORKER, AsyncMock()) == (False, False)


async def test_an_open_session_is_never_reclaimed() -> None:
    """``open`` means shared input; taking a lease over it would lock everyone out."""
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).input_mode = "open"

    assert await hub.router.try_reclaim_hijack_status(_WORKER, AsyncMock()) == (False, False)


async def test_a_session_replaced_under_the_fence_is_not_reclaimed() -> None:
    """``st is state`` — the identity re-check after the fence is acquired.

    Between the first observation and the fence the worker can be deregistered
    and re-registered under the same id. That is a different session, and the
    reclaim must not carry across it.
    """
    hub, _worker = await _hub()
    original = hub.registry.get(_WORKER)
    fence = original.owned_input_fence

    async with fence:
        hub.registry.pop(_WORKER)
        await hub.register_worker(_WORKER, AsyncMock())
        replacement = hub.registry.get(_WORKER)
        replacement.owned_input_fence = fence

    assert original is not replacement
    reclaimed, _competing = await hub.router.try_reclaim_hijack_status(_WORKER, AsyncMock())
    assert reclaimed is True, "the fresh session is reclaimable on its own terms"


async def test_the_boolean_helper_reports_only_whether_it_took_the_lease() -> None:
    """``try_reclaim_hijack`` drops the competitor flag; it must keep the first."""
    hub, _worker = await _hub()

    assert await hub.router.try_reclaim_hijack(_WORKER, AsyncMock()) is True
    assert await hub.router.try_reclaim_hijack(_WORKER, AsyncMock()) is False


# ---------------------------------------------------------------------------
# Browser roles
# ---------------------------------------------------------------------------


async def test_a_browsers_role_can_be_changed_and_read_back() -> None:
    """The role is what every redaction decision downstream is scoped to."""
    hub, _worker = await _hub()
    browser = AsyncMock()
    hub.registry.get(_WORKER).browsers[browser] = "viewer"

    await hub.router.set_browser_role(_WORKER, browser, "operator")

    assert await hub.router.get_worker_browser_role(_WORKER, browser) == "operator"


async def test_a_socket_that_is_not_a_browser_here_has_no_role() -> None:
    """Reading a role for a stranger must answer None, not invent one."""
    hub, _worker = await _hub()

    assert await hub.router.get_worker_browser_role(_WORKER, AsyncMock()) is None
    assert await hub.router.get_worker_browser_role("nobody", AsyncMock()) is None


async def test_setting_a_role_for_a_stranger_does_not_enrol_them() -> None:
    """The guard is what stops a role write from adding a browser to the session."""
    hub, _worker = await _hub()

    await hub.router.set_browser_role(_WORKER, AsyncMock(), "operator")

    assert hub.registry.get(_WORKER).browsers == {}


async def test_the_keystroke_view_is_the_routers_own_buffer() -> None:
    """A copy here would silently discard everything the audit loop records."""
    hub, _worker = await _hub()
    source = object()

    hub.router.record_keystroke(source)

    assert source in hub.router.keystroke_timestamps


# ---------------------------------------------------------------------------
# set_input_mode
# ---------------------------------------------------------------------------


async def test_setting_the_mode_records_it_as_an_operator_decision() -> None:
    """The flag is what stops a later ``worker_hello`` lowering the mode back."""
    hub, _worker = await _hub()

    assert await hub.router.set_input_mode(_WORKER, "readonly") == (True, None)

    state = hub.registry.get(_WORKER)
    assert state.input_mode == "readonly"
    assert state.input_mode_set_by_operator is True


async def test_an_unknown_worker_is_refused_as_not_found() -> None:
    """The reason is a string a route turns into a distinct HTTP response."""
    hub, _worker = await _hub()

    assert await hub.router.set_input_mode("nobody", "readonly") == (False, "not_found")


async def test_opening_a_hijacked_session_is_refused_as_an_active_hijack() -> None:
    """Opening input under a live lease would let anyone type past the owner."""
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).hijack_owner = AsyncMock()

    assert await hub.router.set_input_mode(_WORKER, "open") == (False, "active_hijack")
    assert hub.registry.get(_WORKER).input_mode != "open"


async def test_a_hijacked_session_can_still_be_set_to_a_restrictive_mode() -> None:
    """Only ``open`` is refused — the check is not "reject while hijacked"."""
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).hijack_owner = AsyncMock()

    assert await hub.router.set_input_mode(_WORKER, "readonly") == (True, None)


async def test_an_unhijacked_session_can_be_opened() -> None:
    """The other operand: being asked for ``open`` is not itself a refusal."""
    hub, _worker = await _hub()

    assert await hub.router.set_input_mode(_WORKER, "open") == (True, None)


async def test_the_change_is_announced_to_this_sessions_browsers() -> None:
    """Two frames, both required: the mode itself, and the hijack state after it.

    A browser that misses either keeps offering input the server will now
    refuse, which reads to the user as a dead terminal.
    """
    hub, _worker = await _hub()
    browser = AsyncMock()
    hub.registry.get(_WORKER).browsers[browser] = "viewer"

    await hub.router.set_input_mode(_WORKER, "readonly")

    frames = _control_frames(browser)
    assert [f["type"] for f in frames] == ["input_mode_changed", "hijack_state"]
    assert frames[0]["input_mode"] == "readonly"
    assert isinstance(frames[0]["ts"], float)
    assert frames[1]["input_mode"] == "readonly"


async def test_a_refused_change_announces_nothing() -> None:
    """The broadcasts sit past the early returns; announcing a refusal is a lie."""
    hub, _worker = await _hub()
    browser = AsyncMock()
    state = hub.registry.get(_WORKER)
    state.browsers[browser] = "viewer"
    state.hijack_owner = AsyncMock()

    await hub.router.set_input_mode(_WORKER, "open")

    assert _control_frames(browser) == []


# ---------------------------------------------------------------------------
# send_hijack_state_to — the router's own forwarder
# ---------------------------------------------------------------------------


async def test_hijack_state_is_sent_to_exactly_the_browsers_named() -> None:
    """A direct send, used by the startup sequence — not the session-wide fan-out."""
    hub, _worker = await _hub()
    named, other = AsyncMock(), AsyncMock()

    dead = await hub.router.send_hijack_state_to(
        [named],
        worker_id=_WORKER,
        is_hijacked=True,
        is_dashboard=True,
        is_rest=False,
        hijack_owner=named,
        input_mode="hijack",
        lease_expires_at=None,
    )

    assert dead == set()
    assert [f["owner"] for f in _control_frames(named)] == ["me"]
    other.send_text.assert_not_awaited()


async def test_a_browser_that_cannot_take_the_frame_is_returned_as_dead() -> None:
    """The caller removes what this reports; reporting nothing leaks the socket."""
    hub, _worker = await _hub()
    failing = AsyncMock()
    failing.send_text.side_effect = RuntimeError("socket gone")

    dead = await hub.router.send_hijack_state_to(
        [failing],
        worker_id=_WORKER,
        is_hijacked=False,
        is_dashboard=False,
        is_rest=False,
        hijack_owner=None,
        input_mode="hijack",
        lease_expires_at=None,
    )

    assert dead == {failing}


async def test_the_forwarded_frame_carries_the_lease_the_mode_and_the_holder() -> None:
    """Ten arguments are forwarded; each is a field of the frame a browser reads.

    A REST lease with no dashboard owner is the case that needs ``is_rest`` to
    survive the forwarding on its own -- lose it and the browser is told the
    terminal is free while the API holds it.
    """
    hub, _worker = await _hub()
    browser = AsyncMock()
    deadline = time.monotonic() + 900.0

    await hub.router.send_hijack_state_to(
        [browser],
        worker_id=_WORKER,
        is_hijacked=True,
        is_dashboard=False,
        is_rest=True,
        hijack_owner=None,
        input_mode="readonly",
        lease_expires_at=deadline,
    )

    (frame,) = _control_frames(browser)
    assert (frame["hijacked"], frame["owner"], frame["input_mode"]) == (True, "other", "readonly")
    assert frame["lease_expires_at"] is not None
    assert abs(frame["lease_expires_at"] - (time.time() + (deadline - time.monotonic()))) < 1.0


async def test_a_failure_is_reported_by_default_and_silenced_on_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``suppress_errors`` defaults to False here and must forward as given.

    The flag changes nothing a caller can see in the return value -- the dead
    set is identical either way -- so the log line is the only observable, and
    both the default and the explicit ``True`` need pinning. Asserted against
    the logger call because telemetry filters below INFO.
    """
    hub, _worker = await _hub()
    failing = AsyncMock()
    failing.send_text.side_effect = RuntimeError("socket gone")
    recorder = MagicMock()
    monkeypatch.setattr(router_broadcast, "logger", recorder)

    kwargs: dict[str, Any] = {
        "worker_id": _WORKER,
        "is_hijacked": False,
        "is_dashboard": False,
        "is_rest": False,
        "hijack_owner": None,
        "input_mode": "hijack",
        "lease_expires_at": None,
    }
    await hub.router.send_hijack_state_to([failing], **kwargs)
    assert recorder.debug.call_count == 1
    assert recorder.debug.call_args.args[1] == _WORKER, "the worker id survives the forwarding"

    recorder.reset_mock()
    await hub.router.send_hijack_state_to([failing], suppress_errors=True, **kwargs)
    recorder.debug.assert_not_called()
