#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the role-scoped policy resolution and the fence arming.

``payloads_by_role`` builds one redacted payload per DISTINCT viewer role, once
each, before the concurrent fan-out reads the mapping. Existing tests assert
that redaction happens; none assert what the gate is asked, so the whole
question -- which browser, which session, which action -- could be handed over
as ``None`` and the redaction rules would come back and be applied just the
same. A gate that cannot tell who is asking cannot scope anything to a role,
which is the entire feature.

Two further things live here:

*Deduplication skips a role, it does not stop resolving.* Turned into a
``break``, every role after the first repeat is missing from the mapping. The
per-send lookup then raises ``KeyError``, ``gather`` captures it as an ordinary
send failure, and a room full of live viewers is declared dead at once.

*The fence needs both halves or neither.* ``broadcast`` refuses a snapshot
contract that names a worker without a sequence (or the reverse) rather than
half-checking it. A snapshot that has not been assigned a sequence yet would
otherwise compare equal to "no sequence given" and pass an ownership check it
was never given the information to make.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import OutputPolicyGate, PolicyContext, RedactionRule

_WORKER = "w1"
_TERM: dict[str, Any] = {"type": "term", "data": "hello"}


class _RecordingGate(OutputPolicyGate):
    """An active gate that redacts nothing and remembers what it was asked."""

    def __init__(self) -> None:
        self.asked: list[PolicyContext | None] = []

    async def get_redaction_rules(self, context: Any) -> list[RedactionRule]:
        self.asked.append(context)
        return []


async def _hub(*roles: str) -> tuple[TermHub, _RecordingGate, list[AsyncMock]]:
    gate = _RecordingGate()
    hub = TermHub(output_policy_gate=gate)
    await hub.register_worker(_WORKER, AsyncMock())
    sockets = [AsyncMock() for _ in roles]
    state = hub.registry.get(_WORKER)
    for ws, role in zip(sockets, roles, strict=True):
        state.browsers[ws] = role
    return hub, gate, sockets


# ---------------------------------------------------------------------------
# What the gate is asked
# ---------------------------------------------------------------------------


async def test_the_gate_is_told_which_browser_session_and_action_it_is_ruling_on() -> None:
    """Every field here is what makes the answer role-scoped rather than global.

    Handing the gate a ``None`` context, or a context built from a ``None``
    socket, session or action, still returns rules and still applies them --
    it just applies whatever the gate says with no idea who is asking.
    """
    hub, gate, _ = await _hub("operator")

    await hub.broadcast(_WORKER, dict(_TERM))

    assert len(gate.asked) == 1
    context = gate.asked[0]
    assert context is not None, "the gate was asked with no context at all"
    assert (context.worker_id, context.role, context.action) == (_WORKER, "operator", "output")


async def test_each_distinct_role_is_resolved_exactly_once() -> None:
    """Two viewers sharing a role cost one policy build, not two.

    Each build re-acquires the hub lock, so this is the cap that keeps N
    viewers from triggering N lock acquisitions per frame.
    """
    hub, gate, _ = await _hub("viewer", "viewer")

    await hub.broadcast(_WORKER, dict(_TERM))

    assert [c.role for c in gate.asked if c is not None] == ["viewer"]


async def test_a_repeated_role_does_not_abandon_the_roles_behind_it() -> None:
    """The dedup skip advances to the next browser; ``break`` strands the rest.

    A role that never gets resolved is missing from the mapping the fan-out
    reads, and because ``gather`` captures the resulting lookup failure as an
    ordinary send failure, the viewer is dropped as dead instead.
    """
    hub, gate, (_first, _second, third) = await _hub("viewer", "viewer", "operator")

    await hub.broadcast(_WORKER, dict(_TERM))

    assert sorted(c.role for c in gate.asked if c is not None) == ["operator", "viewer"]
    assert set(hub.registry.get(_WORKER).browsers) == {_first, _second, third}
    third.send_text.assert_awaited_once()


async def test_a_role_the_gate_redacts_nothing_for_still_gets_the_real_frame() -> None:
    """The empty-rules arm must fall back to the shared payload, not to nothing.

    Substituting ``None`` sends ``None`` down the socket: the frame is gone and
    the send still reports success.
    """
    hub, _gate, (browser,) = await _hub("viewer")

    await hub.broadcast(_WORKER, dict(_TERM))

    browser.send_text.assert_awaited_once_with("hello")


# ---------------------------------------------------------------------------
# Arming the snapshot fence
# ---------------------------------------------------------------------------


async def test_a_worker_named_without_a_sequence_is_refused_not_half_checked() -> None:
    """``or``, not ``and``: either half alone is an incomplete contract.

    The session here holds a snapshot with no ``event_seq`` yet, so a
    half-checked contract finds ``None == None``, concludes the state still
    owns the frame, and broadcasts on the strength of an ownership check it was
    never given the sequence to make.
    """
    hub = TermHub()
    worker = AsyncMock()
    await hub.register_worker(_WORKER, worker)
    browser = AsyncMock()
    state = hub.registry.get(_WORKER)
    state.browsers[browser] = "viewer"
    state.last_snapshot = {"screen": "S"}

    await hub.broadcast(_WORKER, dict(_TERM), expected_worker=worker, expected_event_seq=None)

    browser.send_text.assert_not_awaited()
