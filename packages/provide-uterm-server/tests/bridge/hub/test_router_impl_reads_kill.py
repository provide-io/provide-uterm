#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the router's read accessors.

Reads look harmless and are not: three of these are egress points that must
apply the same policy the live broadcast does, and the rest hand out interior
state that callers then mutate.

*Ownership of what is returned.* ``get_recent_events`` and ``get_last_snapshot``
deep-copy before returning. Handing out the ring's own dicts lets any caller
rewrite history in place, and the stored ``last_snapshot`` is served to every
later poller.

*The read-path redaction parity (M5).* The REST ``/snapshot`` and the WS
initial-snapshot read the stored screen directly, bypassing the broadcast
path's redaction unless these apply it themselves. The policy context must
identify the recipient, or the rules cannot be role-scoped; ``type`` must be
forced onto the copy or the frame redactor's field map never fires and the
screen comes back raw.

*The idle sweep's condition.* ``not st.browsers and (now - last) > timeout``
decides whether a live session is torn down. Each operand, and the strictness
of the comparison, is the difference between reaping an idle worker and
reaping one somebody is watching.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.bridge.hub import TermHub, router_impl
from provide.uterm.server.bridge.hub.ext import OutputPolicyGate, PolicyContext, RedactionRule

_WORKER = "w1"
_PLANTED = "ghp_" + "C" * 36
_REDACTED = "[REDACTED]"


class _RecordingGate(OutputPolicyGate):
    """Redacts the planted token and remembers what it was asked."""

    def __init__(self, *, rules: list[RedactionRule] | None = None) -> None:
        self.asked: list[PolicyContext | None] = []
        self._rules = [RedactionRule(pattern=r"ghp_\w+", replacement=_REDACTED)] if rules is None else rules

    async def get_redaction_rules(self, context: Any) -> list[RedactionRule]:
        self.asked.append(context)
        return self._rules


async def _hub(*, gate: OutputPolicyGate | None = None) -> TermHub:
    hub = TermHub(output_policy_gate=gate) if gate is not None else TermHub()
    await hub.register_worker(_WORKER, AsyncMock())
    return hub


# ---------------------------------------------------------------------------
# get_recent_events — the clamp and the copy
# ---------------------------------------------------------------------------


async def _fill(hub: TermHub, count: int) -> None:
    for index in range(count):
        await hub.router.append_event(_WORKER, "term", {"data": f"line-{index}"})


async def test_the_most_recent_events_are_the_ones_returned() -> None:
    """The slice takes the tail, not the head — a reader wants what just happened."""
    hub = await _hub()
    await _fill(hub, 5)

    events = await hub.router.get_recent_events(_WORKER, 2)

    assert [e["data"]["data"] for e in events] == ["line-3", "line-4"]


async def test_a_limit_of_zero_still_returns_one_event() -> None:
    """``max(1, ...)`` — a zero-length tail slice would return the WHOLE ring.

    ``list(events)[-0:]`` is every event, so clamping up to one here is not
    politeness, it is what stops a zero limit from dumping the buffer.
    """
    hub = await _hub()
    await _fill(hub, 5)

    assert len(await hub.router.get_recent_events(_WORKER, 0)) == 1


async def test_a_limit_past_the_ceiling_is_capped_at_five_hundred() -> None:
    """The upper clamp bounds one response; the ring holds up to 2000.

    Filled past the ceiling on purpose: with fewer events than the cap, a
    ceiling of 500 and one of 501 return the same list, and the clamp is
    unenforced.
    """
    hub = await _hub()
    await _fill(hub, 600)

    events = await hub.router.get_recent_events(_WORKER, 10_000)

    assert len(events) == 500


async def test_an_unknown_worker_has_no_recent_events() -> None:
    hub = await _hub()

    assert await hub.router.get_recent_events("nobody", 10) == []


async def test_returned_events_are_the_callers_own_copies() -> None:
    """Handing out the ring's dicts lets any reader rewrite stored history."""
    hub = await _hub()
    await _fill(hub, 1)

    events = await hub.router.get_recent_events(_WORKER, 1)
    events[0]["data"]["data"] = "TAMPERED"

    assert list(hub.registry.get(_WORKER).events)[0]["data"]["data"] == "line-0"


# ---------------------------------------------------------------------------
# get_last_snapshot — the copy and the read-path redaction
# ---------------------------------------------------------------------------


async def test_an_unknown_worker_has_no_last_snapshot() -> None:
    hub = await _hub()

    assert await hub.router.get_last_snapshot("nobody") is None


async def test_the_returned_snapshot_is_the_callers_own_copy() -> None:
    """Every later poller is served the stored screen; it must not be editable."""
    hub = await _hub()
    await hub.router.commit_snapshot_event(_WORKER, {"type": "snapshot", "screen": "$ ls"})

    snapshot = await hub.router.get_last_snapshot(_WORKER)
    assert snapshot is not None
    snapshot["screen"] = "TAMPERED"

    assert hub.registry.get(_WORKER).last_snapshot["screen"] == "$ ls"


async def test_a_read_with_no_recipient_is_not_redacted() -> None:
    """The broadcast source path reads with no recipient and needs the raw screen."""
    hub = await _hub(gate=_RecordingGate())
    await hub.router.commit_snapshot_event(_WORKER, {"type": "snapshot", "screen": f"$ {_PLANTED}"})

    snapshot = await hub.router.get_last_snapshot(_WORKER)

    assert snapshot is not None and snapshot["screen"] == f"$ {_PLANTED}"


async def test_a_read_for_a_recipient_is_redacted_for_that_recipient() -> None:
    """M5: the REST /snapshot and WS initial-snapshot must not bypass the policy.

    The recipient is a registered browser, so the context it produces carries a
    role. That is what makes the rules role-scoped rather than global, and it
    only survives if both the reader and the session reach the gate intact.
    """
    gate = _RecordingGate()
    hub = await _hub(gate=gate)
    recipient = AsyncMock()
    hub.registry.get(_WORKER).browsers[recipient] = "operator"
    await hub.router.commit_snapshot_event(_WORKER, {"type": "snapshot", "screen": f"$ {_PLANTED}"})

    snapshot = await hub.router.get_last_snapshot(_WORKER, recipient=recipient)

    assert snapshot is not None and snapshot["screen"] == f"$ {_REDACTED}"
    context = gate.asked[0]
    assert context is not None
    assert (context.worker_id, context.role) == (_WORKER, "operator")


async def test_a_read_with_no_gate_configured_is_not_redacted() -> None:
    """No policy means no redaction — not a crash on the missing gate."""
    hub = await _hub()
    await hub.router.commit_snapshot_event(_WORKER, {"type": "snapshot", "screen": f"$ {_PLANTED}"})

    snapshot = await hub.router.get_last_snapshot(_WORKER, recipient=AsyncMock())

    assert snapshot is not None and snapshot["screen"] == f"$ {_PLANTED}"


# ---------------------------------------------------------------------------
# redact_snapshot_for_recipient
# ---------------------------------------------------------------------------


async def test_the_gate_is_told_which_recipient_session_and_action_it_is_ruling_on() -> None:
    """Without these the rules cannot be scoped to the reader's role at all."""
    gate = _RecordingGate()
    hub = await _hub(gate=gate)
    recipient = AsyncMock()

    hub.registry.get(_WORKER).browsers[recipient] = "operator"

    await hub.router.redact_snapshot_for_recipient(_WORKER, {"type": "snapshot", "screen": "$ ls"}, recipient)

    context = gate.asked[0]
    assert context is not None
    assert (context.worker_id, context.role, context.action) == (_WORKER, "operator", "output")


async def test_a_gate_with_no_rules_returns_the_snapshot_untouched() -> None:
    """The common case, and it must not pay for a pointless copy."""
    gate = _RecordingGate(rules=[])
    hub = await _hub(gate=gate)
    snapshot = {"type": "snapshot", "screen": f"$ {_PLANTED}"}

    assert await hub.router.redact_snapshot_for_recipient(_WORKER, snapshot, AsyncMock()) is snapshot


async def test_a_stored_snapshot_missing_its_type_is_still_redacted() -> None:
    """The frame redactor only fires on ``type == "snapshot"``.

    A stored screen that somehow lacks the key would otherwise come back raw
    from the read path while the broadcast path scrubbed it.
    """
    hub = await _hub(gate=_RecordingGate())

    result = await hub.router.redact_snapshot_for_recipient(_WORKER, {"screen": f"$ {_PLANTED}"}, AsyncMock())

    assert result["screen"] == f"$ {_REDACTED}"


async def test_redacting_for_one_recipient_does_not_disturb_the_stored_screen() -> None:
    """The same screen is redacted repeatedly for different roles."""
    hub = await _hub(gate=_RecordingGate())
    snapshot = {"type": "snapshot", "screen": f"$ {_PLANTED}"}

    await hub.router.redact_snapshot_for_recipient(_WORKER, snapshot, AsyncMock())

    assert snapshot["screen"] == f"$ {_PLANTED}"


# ---------------------------------------------------------------------------
# browser_count and the idle sweep
# ---------------------------------------------------------------------------


async def test_the_browser_count_is_this_workers_own() -> None:
    hub = await _hub()
    hub.registry.get(_WORKER).browsers[AsyncMock()] = "viewer"

    assert await hub.router.browser_count(_WORKER) == 1
    assert await hub.router.browser_count("nobody") == 0


async def test_a_worker_idle_past_the_timeout_is_a_candidate() -> None:
    hub = await _hub()
    hub.registry.get(_WORKER).last_activity_at = time.monotonic() - 60.0

    assert [wid for wid, _ in await hub.router.get_idle_candidates(30.0)] == [_WORKER]


async def test_a_worker_idle_for_exactly_the_timeout_is_not_yet_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``>``, not ``>=``, and the clock is pinned so the boundary lands exactly.

    Reaping at equality takes a worker the instant its budget runs out rather
    than after it; only an exact landing on the boundary can tell the two
    apart.
    """
    hub = await _hub()
    now = 1_000_000.0
    monkeypatch.setattr(router_impl.time, "monotonic", lambda: now)
    hub.registry.get(_WORKER).last_activity_at = now - 30.0

    assert await hub.router.get_idle_candidates(30.0) == [], "idle for exactly the budget is not past it"
    assert [wid for wid, _ in await hub.router.get_idle_candidates(29.999)] == [_WORKER]


async def test_a_worker_someone_is_watching_is_never_a_candidate() -> None:
    """Both operands: idle time alone must not reap a session with a viewer."""
    hub = await _hub()
    state = hub.registry.get(_WORKER)
    state.last_activity_at = time.monotonic() - 3600.0
    state.browsers[AsyncMock()] = "viewer"

    assert await hub.router.get_idle_candidates(30.0) == []


async def test_a_busy_worker_with_no_browsers_is_not_a_candidate() -> None:
    """The other operand — recent activity keeps an unwatched worker alive."""
    hub = await _hub()
    hub.registry.get(_WORKER).last_activity_at = time.monotonic()

    assert await hub.router.get_idle_candidates(30.0) == []


async def test_the_candidate_carries_the_activity_time_it_was_judged_on() -> None:
    hub = await _hub()
    idle_since = time.monotonic() - 60.0
    hub.registry.get(_WORKER).last_activity_at = idle_since

    assert await hub.router.get_idle_candidates(30.0) == [(_WORKER, idle_since)]
