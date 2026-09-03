#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the event ring's write path — redaction, cap, fan-out.

``append_event`` is the only writer of the ring buffer that the events API,
``/events/watch``, the webhooks and the MCP event tools all read. It does four
things that no existing test distinguishes, because all four produce an event
that looks correct from the outside:

*Redaction happens at write time, and before truncation.* The ring must never
be an unredacted egress for what the live broadcast scrubs. Doing it after the
cap would leave a secret that straddles the truncation boundary half-stored;
doing it not at all leaves the whole thing. Nothing asserted either, so the
redactor could be built from an empty ruleset and every test still passed.

*The two buses carry deliberately different payloads.* ``_event_bus`` gets the
redacted event; ``_operation_event_bus`` -- the private stream no route exposes
-- gets the raw one. Feeding the raw payload to both is the leak this split
exists to prevent, and feeding the redacted one to both silently breaks
supervised operations. Only the exact call distinguishes them.

*A term event whose ``data`` is not a string is stored verbatim.* The redactor's
term branch ``str()``-coerces ``data``, which would change the stored type, so
the guard preserves the legacy ring contract. Both halves of that ``and`` are
required.

*The cap is exclusive.* ``len(raw) > cap`` truncates; a payload of exactly the
cap is stored whole.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.hub import TermHub

_WORKER = "w1"

#: Matches the default ruleset's GitHub-token pattern (gh[opusr]_ + 36+ chars).
_PLANTED = "ghp_" + "A" * 36
_REDACTED = "[GITHUB_TOKEN_REDACTED]"

#: The floor TermHub clamps ``max_event_data_chars`` to, so the boundary is exact.
_CAP = 256


@pytest.fixture()
async def hub() -> TermHub:
    """A hub with both buses replaced, so the exact enqueue calls are assertable."""
    term_hub = TermHub(max_event_data_chars=_CAP)
    await term_hub.register_worker(_WORKER, AsyncMock())
    term_hub._event_bus = MagicMock()
    term_hub._operation_event_bus = MagicMock()
    return term_hub


def _stored(hub: TermHub) -> dict[str, Any]:
    """The single event actually written to the ring."""
    events = list(hub.registry.get(_WORKER).events)
    assert len(events) == 1, f"expected exactly one event, got {len(events)}"
    return events[0]


# ---------------------------------------------------------------------------
# Redaction at write time
# ---------------------------------------------------------------------------


async def test_a_secret_in_a_term_event_is_redacted_before_it_is_stored(hub: TermHub) -> None:
    """The ring is read by the events API, watch and MCP — it is a real egress."""
    await hub.router.append_event(_WORKER, "term", {"data": f"$ gh auth login {_PLANTED}"})

    assert _stored(hub)["data"]["data"] == f"$ gh auth login {_REDACTED}"


async def test_a_secret_past_the_cap_is_redacted_rather_than_half_truncated(hub: TermHub) -> None:
    """Redaction runs BEFORE truncation, so where the cap falls cannot matter.

    The secret is placed so the cap would cut through the middle of it. Capping
    first stores a truncated but still-recognisable credential; redacting first
    removes it and truncates the placeholder instead.
    """
    await hub.router.append_event(_WORKER, "term", {"data": "x" * (_CAP - 10) + _PLANTED})

    stored = _stored(hub)["data"]["data"]
    assert _PLANTED[:20] not in stored, "a truncated secret is still a secret"
    assert len(stored) == _CAP


async def test_the_synthetic_type_key_is_stripped_back_off(hub: TermHub) -> None:
    """``type`` is added only to reuse the frame redactor's field map.

    Leaving it on writes a payload the API did not send; keying the strip on a
    different literal leaves it, and inverting the comparison discards
    everything except it.
    """
    await hub.router.append_event(_WORKER, "term", {"data": "plain", "cols": 80})

    payload = _stored(hub)["data"]
    assert "type" not in payload
    assert payload == {"data": "plain", "cols": 80}


async def test_a_snapshot_events_screen_is_redacted_too(hub: TermHub) -> None:
    """Content events are term/snapshot/analysis — not term alone.

    This also pins the first half of the non-string guard: a snapshot has no
    ``data`` field at all, so an ``or`` there would return it unredacted.
    """
    await hub.router.append_event(_WORKER, "snapshot", {"screen": f"$ echo {_PLANTED}"})

    assert _stored(hub)["data"]["screen"] == f"$ echo {_REDACTED}"


async def test_a_control_event_is_stored_exactly_as_given(hub: TermHub) -> None:
    """Non-content events carry control metadata, not scraped output."""
    await hub.router.append_event(_WORKER, "hijack_acquire", {"owner": _PLANTED})

    assert _stored(hub)["data"] == {"owner": _PLANTED}


async def test_a_term_event_whose_data_is_not_a_string_keeps_its_type(hub: TermHub) -> None:
    """Both halves of the guard, and the legacy ring contract it preserves.

    The redactor's term branch would ``str()``-coerce this into ``"42"``; the
    cap below would then take ``len()`` of an int and raise.
    """
    await hub.router.append_event(_WORKER, "term", {"data": 42})

    assert _stored(hub)["data"] == {"data": 42}


# ---------------------------------------------------------------------------
# The truncation cap
# ---------------------------------------------------------------------------


async def test_a_payload_of_exactly_the_cap_is_stored_whole(hub: TermHub) -> None:
    """``>``, not ``>=`` — the boundary value is not oversized."""
    await hub.router.append_event(_WORKER, "term", {"data": "y" * _CAP})

    assert _stored(hub)["data"]["data"] == "y" * _CAP


async def test_a_payload_one_character_past_the_cap_is_truncated_to_it(hub: TermHub) -> None:
    hub_data = "y" * (_CAP + 1)
    await hub.router.append_event(_WORKER, "term", {"data": hub_data})

    assert _stored(hub)["data"]["data"] == "y" * _CAP


async def test_the_cap_applies_to_term_events_only(hub: TermHub) -> None:
    """A non-term event is not truncated; capping the wrong type loses content."""
    await hub.router.append_event(_WORKER, "worker_status", {"data": "z" * (_CAP + 50)})

    assert len(_stored(hub)["data"]["data"]) == _CAP + 50


async def test_truncation_does_not_disturb_the_rest_of_the_payload(hub: TermHub) -> None:
    await hub.router.append_event(_WORKER, "term", {"data": "y" * (_CAP + 1), "cols": 80})

    assert _stored(hub)["data"]["cols"] == 80


# ---------------------------------------------------------------------------
# The event's own shape and sequence
# ---------------------------------------------------------------------------


async def test_events_are_numbered_from_one_and_step_by_one(hub: TermHub) -> None:
    """The sequence is what every reader pages and de-duplicates on."""
    first = await hub.router.append_event(_WORKER, "term", {"data": "a"})
    second = await hub.router.append_event(_WORKER, "term", {"data": "b"})

    assert (first["seq"], second["seq"]) == (1, 2)


async def test_the_stored_event_carries_its_sequence_type_and_payload(hub: TermHub) -> None:
    """Asserted as whole keys: a renamed one is a field every reader loses."""
    evt = await hub.router.append_event(_WORKER, "term", {"data": "a"})

    assert set(evt) == {"seq", "ts", "type", "data"}
    assert (evt["type"], evt["data"]) == ("term", {"data": "a"})
    assert isinstance(evt["ts"], float)


async def test_the_ring_floor_tracks_the_oldest_event_held(hub: TermHub) -> None:
    """``min_event_seq`` is what tells a reader its cursor fell off the ring."""
    await hub.router.append_event(_WORKER, "term", {"data": "a"})

    assert hub.registry.get(_WORKER).min_event_seq == 1


async def test_an_event_for_an_unknown_worker_is_returned_but_not_stored(hub: TermHub) -> None:
    """Sequence zero says "never stored" — the caller still gets a well-formed event."""
    evt = await hub.router.append_event("nobody", "term", {"data": "a"})

    assert evt["seq"] == 0
    assert (evt["type"], evt["data"]) == ("term", {"data": "a"})
    assert set(evt) == {"seq", "ts", "type", "data"}


# ---------------------------------------------------------------------------
# The two buses
# ---------------------------------------------------------------------------


async def test_the_public_bus_receives_the_redacted_event(hub: TermHub) -> None:
    """This bus backs SSE, webhooks and the MCP tools — it must never carry the secret."""
    evt = await hub.router.append_event(_WORKER, "term", {"data": _PLANTED})

    hub._event_bus._enqueue.assert_called_once_with(_WORKER, evt)
    assert evt["data"]["data"] == _REDACTED


async def test_the_private_operation_bus_receives_the_raw_payload(hub: TermHub) -> None:
    """The whole point of the second bus: supervised operations need what was typed.

    Sending it the redacted copy breaks them silently; sending the raw copy to
    the public bus is the leak the split exists to prevent. Only asserting both
    calls separates the two.
    """
    await hub.router.append_event(_WORKER, "term", {"data": _PLANTED})

    worker_id, raw_evt = hub._operation_event_bus._enqueue.call_args.args
    assert worker_id == _WORKER
    assert raw_evt["data"] == {"data": _PLANTED}
    assert raw_evt["seq"] == 1
