#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the router's lifecycle helpers and fence forwarding.

Three things, unrelated except that none of them was asserted:

*``hijack_state_msg_for``* answers "who is driving, from this browser's point of
view" for a single socket, and is the startup sequence's copy of the same
question ``broadcast_hijack_state`` answers for everyone. It has its own
independent implementation of the owner and lease rules, so pinning the
broadcast version leaves this one entirely unenforced -- and a browser that is
told the wrong thing at startup stays wrong until some later event corrects it.

*``prune_if_idle``* deletes a session. All four operands of its guard must
hold: a live worker socket, a browser, a dashboard owner or a REST lease each
independently mean "still in use", and dropping any one of them from the check
reaps a session somebody is holding.

*``broadcast``* is a thin forwarder, but the snapshot fence it forwards is not
optional decoration: the receiving side refuses a half-specified contract
outright, so a dropped ``expected_event_seq`` does not weaken the fence -- it
silently discards the frame.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.bridge.coordinator import HijackSession
from provide.uterm.server.bridge.hub import TermHub, router_impl

_WORKER = "w1"
_REST_LEASE_S = 900.0
_DASHBOARD_LEASE_S = 30.0


async def _hub() -> tuple[TermHub, AsyncMock]:
    hub = TermHub()
    worker = AsyncMock()
    await hub.register_worker(_WORKER, worker)
    return hub, worker


def _is_close(actual: Any, mono_deadline: float) -> bool:
    """A monotonic deadline converted to wall clock, within scheduling noise."""
    return isinstance(actual, float) and abs(actual - (time.time() + (mono_deadline - time.monotonic()))) < 1.0


def _rest_session(*, expires_in: float) -> HijackSession:
    return HijackSession(hijack_id="h1", owner="api-caller", lease_expires_at=time.monotonic() + expires_in)


# ---------------------------------------------------------------------------
# hijack_state_msg_for — the per-socket answer
# ---------------------------------------------------------------------------


async def test_an_unknown_worker_yields_a_well_formed_unheld_frame() -> None:
    """A browser attaching to a session that has gone still needs a valid frame.

    Every field is defaulted here rather than read, so each default is the only
    thing standing between the browser and a malformed startup frame.
    """
    hub, _worker = await _hub()

    frame = await hub.router.hijack_state_msg_for("nobody", AsyncMock())

    assert frame == {
        "type": "hijack_state",
        "hijacked": False,
        "owner": None,
        "lease_expires_at": None,
        "input_mode": "hijack",
    }


async def test_the_dashboard_owner_is_told_the_lease_is_its_own() -> None:
    """``me`` is what lets that browser show controls instead of a lock."""
    hub, _worker = await _hub()
    owner = AsyncMock()
    hub.registry.get(_WORKER).hijack_owner = owner

    frame = await hub.router.hijack_state_msg_for(_WORKER, owner)

    assert (frame["owner"], frame["hijacked"]) == ("me", True)


async def test_another_browser_is_told_someone_else_is_driving() -> None:
    """The same session, the other socket — the answer is per-recipient."""
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).hijack_owner = AsyncMock()

    frame = await hub.router.hijack_state_msg_for(_WORKER, AsyncMock())

    assert frame["owner"] == "other"


async def test_a_rest_lease_alone_still_reads_as_taken() -> None:
    """``is_rest`` reaches the ``other`` arm; losing it offers control that is held."""
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).hijack_session = _rest_session(expires_in=_REST_LEASE_S)

    frame = await hub.router.hijack_state_msg_for(_WORKER, AsyncMock())

    assert (frame["owner"], frame["hijacked"]) == ("other", True)


async def test_an_unheld_terminal_names_no_owner() -> None:
    """The null arm, so a constant ``other`` cannot pass either."""
    hub, _worker = await _hub()

    frame = await hub.router.hijack_state_msg_for(_WORKER, AsyncMock())

    assert (frame["owner"], frame["hijacked"]) == (None, False)


async def test_a_live_rest_lease_is_the_deadline_reported() -> None:
    """Both operands of the ``and``; the dashboard deadline is set and much sooner."""
    hub, _worker = await _hub()
    state = hub.registry.get(_WORKER)
    state.hijack_session = _rest_session(expires_in=_REST_LEASE_S)
    state.hijack_owner_expires_at = time.monotonic() + _DASHBOARD_LEASE_S

    frame = await hub.router.hijack_state_msg_for(_WORKER, AsyncMock())

    assert _is_close(frame["lease_expires_at"], state.hijack_session.lease_expires_at)


async def test_an_expired_rest_lease_does_not_displace_the_dashboard_deadline() -> None:
    """A session object exists but is not a valid lease — the false arm."""
    hub, _worker = await _hub()
    state = hub.registry.get(_WORKER)
    state.hijack_session = _rest_session(expires_in=-1.0)
    state.hijack_owner = AsyncMock()
    state.hijack_owner_expires_at = time.monotonic() + _DASHBOARD_LEASE_S

    frame = await hub.router.hijack_state_msg_for(_WORKER, AsyncMock())

    assert _is_close(frame["lease_expires_at"], state.hijack_owner_expires_at)


async def test_the_frame_carries_the_sessions_input_mode() -> None:
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).input_mode = "readonly"

    frame = await hub.router.hijack_state_msg_for(_WORKER, AsyncMock())

    assert frame["input_mode"] == "readonly"


# ---------------------------------------------------------------------------
# prune_if_idle — all four operands
# ---------------------------------------------------------------------------


async def test_a_session_with_nothing_left_is_removed() -> None:
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).worker_ws = None

    await hub.router.prune_if_idle(_WORKER)

    assert hub.registry.get(_WORKER) is None


@pytest.mark.parametrize("holder", ["worker", "browser", "dashboard", "rest"])
async def test_a_session_anything_still_holds_is_kept(holder: str) -> None:
    """Four independent reasons to keep a session, each sufficient on its own.

    Dropping any one operand from the guard reaps a session that is still in
    use -- a connected worker, a watching browser, or either kind of lease.
    """
    hub, worker = await _hub()
    state = hub.registry.get(_WORKER)
    state.worker_ws = None
    if holder == "worker":
        state.worker_ws = worker
    elif holder == "browser":
        state.browsers[AsyncMock()] = "viewer"
    elif holder == "dashboard":
        state.hijack_owner = AsyncMock()
    else:
        state.hijack_session = _rest_session(expires_in=_REST_LEASE_S)

    await hub.router.prune_if_idle(_WORKER)

    assert hub.registry.get(_WORKER) is not None


async def test_pruning_an_unknown_worker_is_a_no_op() -> None:
    hub, _worker = await _hub()

    await hub.router.prune_if_idle("nobody")

    assert hub.registry.get(_WORKER) is not None


async def test_the_prune_says_which_session_it_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only record that a session was torn down rather than disconnected."""
    hub, _worker = await _hub()
    hub.registry.get(_WORKER).worker_ws = None
    recorder = MagicMock()
    monkeypatch.setattr(router_impl, "logger", recorder)

    await hub.router.prune_if_idle(_WORKER)

    recorder.debug.assert_called_once_with("pruned idle worker_id=%s", _WORKER)


# ---------------------------------------------------------------------------
# broadcast — the forwarded fence
# ---------------------------------------------------------------------------


async def test_the_snapshot_sequence_reaches_the_fence_it_is_checked_against() -> None:
    """Dropping it does not loosen the fence — it discards the frame.

    The receiving side refuses a contract that names a worker without a
    sequence, so a forwarder that loses the sequence turns every fenced
    snapshot broadcast into a silent no-op.
    """
    hub, worker = await _hub()
    browser = AsyncMock()
    hub.registry.get(_WORKER).browsers[browser] = "viewer"
    committed = await hub.router.commit_snapshot_event(_WORKER, {"type": "snapshot", "screen": "$ ls"})
    assert committed is not None

    await hub.router.broadcast(
        _WORKER,
        dict(committed),
        expected_worker=worker,
        expected_event_seq=committed["event_seq"],
    )

    browser.send_text.assert_awaited_once()


async def test_a_stale_snapshot_sequence_is_refused() -> None:
    """The other side of the same forwarding, so "always send" cannot pass."""
    hub, worker = await _hub()
    browser = AsyncMock()
    hub.registry.get(_WORKER).browsers[browser] = "viewer"
    await hub.router.commit_snapshot_event(_WORKER, {"type": "snapshot", "screen": "$ ls"})

    await hub.router.broadcast(
        _WORKER,
        {"type": "snapshot", "screen": "$ stale"},
        expected_worker=worker,
        expected_event_seq=999,
    )

    browser.send_text.assert_not_awaited()
