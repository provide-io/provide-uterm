#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for what ``commit_snapshot_event`` actually stores and sends.

One call produces four bounded copies of the same screen, and they are
deliberately not the same object or the same content:

- ``st.last_snapshot`` — raw, and the polling read path serves it.
- the returned frame — raw, and the broadcast path sends it.
- the ring event — REDACTED, because the events API, watch and MCP read it.
- the private operation bus — raw again, because supervised operations need
  what was really on screen.

Nothing asserted the difference. A single shared copy passes every existing
test while being either a leak (raw content in the ring) or a broken operation
stream (redacted content where the raw was required). The copies must also be
independent: mutating the frame a caller was handed must not reach back into
the stored screen.

Two smaller things ride along. ``type`` is popped off the event payload and
carried as ``frame_type``, so a GUI snapshot keeps its own frame type in the
ring instead of being relabelled ``snapshot``; and ``prompt_id`` is lifted out
of ``prompt_detected`` so the ring row can be correlated with the prompt that
produced it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.hub import TermHub

_WORKER = "w1"

#: Matches the default ruleset's GitHub-token pattern.
_PLANTED = "ghp_" + "B" * 36
_REDACTED = "[GITHUB_TOKEN_REDACTED]"


def _snapshot(**extra: Any) -> dict[str, Any]:
    return {"type": "snapshot", "screen": "$ ls", "screen_hash": "sha256:abc", **extra}


@pytest.fixture()
async def hub() -> TermHub:
    term_hub = TermHub()
    await term_hub.register_worker(_WORKER, AsyncMock())
    term_hub._event_bus = MagicMock()
    term_hub._operation_event_bus = MagicMock()
    return term_hub


def _ring(hub: TermHub) -> dict[str, Any]:
    events = list(hub.registry.get(_WORKER).events)
    assert len(events) == 1, f"expected exactly one event, got {len(events)}"
    return events[0]


# ---------------------------------------------------------------------------
# The raw / redacted split across four copies
# ---------------------------------------------------------------------------


async def test_the_ring_copy_is_redacted(hub: TermHub) -> None:
    """The events API, ``/events/watch`` and the MCP tools all read this row."""
    await hub.router.commit_snapshot_event(_WORKER, _snapshot(screen=f"$ gh auth login {_PLANTED}"))

    assert _ring(hub)["data"]["screen"] == f"$ gh auth login {_REDACTED}"


async def test_the_stored_and_returned_screens_stay_raw(hub: TermHub) -> None:
    """The broadcast path applies role-scoped redaction of its own downstream.

    Redacting here as well would double-scrub the live screen and lose content
    an operator is entitled to see.
    """
    screen = f"$ gh auth login {_PLANTED}"

    committed = await hub.router.commit_snapshot_event(_WORKER, _snapshot(screen=screen))

    assert committed is not None
    assert committed["screen"] == screen
    assert hub.registry.get(_WORKER).last_snapshot["screen"] == screen


async def test_the_private_operation_bus_receives_the_raw_screen(hub: TermHub) -> None:
    """The second bus exists so supervised operations see what was really typed."""
    screen = f"$ gh auth login {_PLANTED}"

    await hub.router.commit_snapshot_event(_WORKER, _snapshot(screen=screen))

    worker_id, raw_evt = hub._operation_event_bus._enqueue.call_args.args
    assert worker_id == _WORKER
    assert raw_evt["data"]["screen"] == screen
    assert raw_evt["data"]["type"] == "snapshot", "the raw row is typed too, not just the redacted one"
    assert raw_evt["data"]["event_seq"] == raw_evt["seq"] == 1


async def test_the_public_bus_receives_the_redacted_row(hub: TermHub) -> None:
    await hub.router.commit_snapshot_event(_WORKER, _snapshot(screen=f"$ gh auth login {_PLANTED}"))

    worker_id, evt = hub._event_bus._enqueue.call_args.args
    assert worker_id == _WORKER
    assert evt["data"]["screen"] == f"$ gh auth login {_REDACTED}"


# ---------------------------------------------------------------------------
# The copies are independent
# ---------------------------------------------------------------------------


async def test_the_returned_frame_is_not_the_stored_one(hub: TermHub) -> None:
    """The caller broadcasts this; scribbling on it must not rewrite the screen
    a later poll will serve."""
    committed = await hub.router.commit_snapshot_event(_WORKER, _snapshot())
    assert committed is not None

    committed["screen"] = "TAMPERED"

    assert hub.registry.get(_WORKER).last_snapshot["screen"] == "$ ls"


async def test_the_bus_copy_is_not_the_ring_copy(hub: TermHub) -> None:
    """Subscribers hold their event past the call; the ring must not follow it."""
    await hub.router.commit_snapshot_event(_WORKER, _snapshot())
    _worker_id, evt = hub._event_bus._enqueue.call_args.args

    evt["data"]["screen"] = "TAMPERED"

    assert _ring(hub)["data"]["screen"] == "$ ls"


async def test_the_caller_s_snapshot_is_never_mutated(hub: TermHub) -> None:
    """``type`` is popped off a copy, not off the dict the caller handed in."""
    original = _snapshot()

    await hub.router.commit_snapshot_event(_WORKER, original)

    assert original == _snapshot()


# ---------------------------------------------------------------------------
# The frame type carried into the ring
# ---------------------------------------------------------------------------


async def test_a_non_screen_snapshot_keeps_its_own_frame_type(hub: TermHub) -> None:
    """A GUI snapshot relabelled ``snapshot`` is indistinguishable in the ring."""
    await hub.router.commit_snapshot_event(_WORKER, _snapshot(type="gui_snapshot"))

    assert _ring(hub)["data"]["type"] == "gui_snapshot"


async def test_a_snapshot_with_no_type_defaults_to_snapshot(hub: TermHub) -> None:
    """The pop's default, and the other side of the key lookup."""
    await hub.router.commit_snapshot_event(_WORKER, {"screen": "$ ls", "screen_hash": "sha256:abc"})

    assert _ring(hub)["data"]["type"] == "snapshot"


async def test_the_ring_row_is_always_typed_as_a_snapshot_event(hub: TermHub) -> None:
    """The event type is the ring's own; the frame type lives inside ``data``."""
    await hub.router.commit_snapshot_event(_WORKER, _snapshot(type="gui_snapshot"))

    assert _ring(hub)["type"] == "snapshot"


# ---------------------------------------------------------------------------
# Prompt correlation
# ---------------------------------------------------------------------------


async def test_the_ring_row_carries_the_prompt_that_produced_it(hub: TermHub) -> None:
    """Lifted out of the nested ``prompt_detected`` so a row can be correlated."""
    await hub.router.commit_snapshot_event(
        _WORKER, _snapshot(prompt_detected={"prompt_id": "command", "matched": "$ "})
    )

    assert _ring(hub)["data"]["prompt_id"] == "command"


async def test_a_snapshot_with_no_prompt_records_none(hub: TermHub) -> None:
    """The key is always present — absent and unknown must not read the same."""
    await hub.router.commit_snapshot_event(_WORKER, _snapshot())

    assert _ring(hub)["data"]["prompt_id"] is None


# ---------------------------------------------------------------------------
# Sequencing
# ---------------------------------------------------------------------------


async def test_snapshots_are_numbered_from_one_and_step_by_one(hub: TermHub) -> None:
    """``event_seq`` is the fence the broadcast path revalidates against."""
    first = await hub.router.commit_snapshot_event(_WORKER, _snapshot())
    second = await hub.router.commit_snapshot_event(_WORKER, _snapshot())

    assert first is not None and second is not None
    assert (first["event_seq"], second["event_seq"]) == (1, 2)


async def test_the_ring_row_and_the_committed_frame_share_one_sequence(hub: TermHub) -> None:
    """Correlating an event with the screen it describes depends on this."""
    committed = await hub.router.commit_snapshot_event(_WORKER, _snapshot())
    assert committed is not None
    row = _ring(hub)

    assert row["seq"] == row["data"]["event_seq"] == committed["event_seq"] == 1


async def test_the_ring_row_carries_its_sequence_type_and_payload(hub: TermHub) -> None:
    """Whole keys: a renamed one is a field every reader silently loses."""
    await hub.router.commit_snapshot_event(_WORKER, _snapshot())
    row = _ring(hub)

    assert set(row) == {"seq", "ts", "type", "data"}
    assert isinstance(row["ts"], float)


async def test_the_ring_floor_tracks_the_oldest_snapshot_held(hub: TermHub) -> None:
    """``min_event_seq`` is what tells a reader its cursor fell off the ring."""
    await hub.router.commit_snapshot_event(_WORKER, _snapshot())

    assert hub.registry.get(_WORKER).min_event_seq == 1
