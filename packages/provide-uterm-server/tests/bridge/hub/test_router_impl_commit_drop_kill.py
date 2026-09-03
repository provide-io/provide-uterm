#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the snapshot commit fence and the record of a drop.

``commit_snapshot_event`` refuses a snapshot published by a connection the
worker id no longer owns. Refusing is right -- that screen is stale by
definition -- but the refusal is invisible from outside: a poller reading
``last_snapshot`` sees the same old screen whether the publish was dropped,
never arrived, or was stored. The counter and the warning are the only things
that tell those apart, and neither was asserted, so both could have been
emitting a constant reason for either case.

The **reason** is the part that carries the diagnosis. ``unregistered`` means
the worker is gone entirely; ``superseded_connection`` means it reconnected and
this frame belongs to the previous socket. Those call for opposite
investigations, and the conditional expression that picks between them is
evaluated twice -- once for the counter label, once for the log field -- so
each needs pinning on both sides.

The fence itself needs both operands: no ``expected_worker`` means the caller
made no ownership claim and the frame is stored unconditionally, which is what
the broadcast-source path relies on.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.hub import TermHub, router_impl, snapshot_metrics

_WORKER = "w1"


def _snapshot(**extra: Any) -> dict[str, Any]:
    return {"type": "snapshot", "screen": "$ ls", "screen_hash": "sha256:abc", **extra}


async def _hub() -> tuple[TermHub, AsyncMock]:
    hub = TermHub()
    worker = AsyncMock()
    await hub.register_worker(_WORKER, worker)
    return hub, worker


# ---------------------------------------------------------------------------
# The fence
# ---------------------------------------------------------------------------


async def test_a_publish_that_claims_no_ownership_is_always_stored() -> None:
    """``expected_worker is not None`` guards the whole check.

    The broadcast source path publishes without a claim; fencing it anyway
    would drop every snapshot the hub itself commits.
    """
    hub, _worker = await _hub()

    committed = await hub.router.commit_snapshot_event(_WORKER, _snapshot())

    assert committed is not None
    assert hub.registry.get(_WORKER).last_snapshot is not None


async def test_a_publish_from_the_connection_that_owns_the_worker_is_stored() -> None:
    """The near side of the identity check, so "always drop" cannot pass."""
    hub, worker = await _hub()

    committed = await hub.router.commit_snapshot_event(_WORKER, _snapshot(), expected_worker=worker)

    assert committed is not None


async def test_a_publish_from_a_replaced_connection_is_refused() -> None:
    """A reconnect under the same worker id must not accept the old socket's screen."""
    hub, _worker = await _hub()
    previous = AsyncMock()

    assert await hub.router.commit_snapshot_event(_WORKER, _snapshot(), expected_worker=previous) is None
    assert hub.registry.get(_WORKER).last_snapshot is None


async def test_a_publish_for_a_worker_that_is_gone_is_refused() -> None:
    hub, worker = await _hub()

    assert await hub.router.commit_snapshot_event("nobody", _snapshot(), expected_worker=worker) is None


# ---------------------------------------------------------------------------
# The counter — and which reason it is labelled with
# ---------------------------------------------------------------------------


async def test_a_drop_for_a_vanished_worker_is_counted_as_unregistered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unregistered``: nothing to publish to. A different investigation entirely."""
    hub, worker = await _hub()
    counter = MagicMock()
    monkeypatch.setattr(snapshot_metrics, "snapshot_commit_dropped", counter)

    await hub.router.commit_snapshot_event("nobody", _snapshot(), expected_worker=worker)

    counter.add.assert_called_once_with(1, {"worker_id": "nobody", "reason": "unregistered"})


async def test_a_drop_from_an_older_socket_is_counted_as_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``superseded_connection``: the worker is fine, this frame is not.

    Same counter, same shape, the other arm of the conditional — which is why
    a constant reason on either side passes every single-case test.
    """
    hub, _worker = await _hub()
    counter = MagicMock()
    monkeypatch.setattr(snapshot_metrics, "snapshot_commit_dropped", counter)

    await hub.router.commit_snapshot_event(_WORKER, _snapshot(), expected_worker=AsyncMock())

    counter.add.assert_called_once_with(1, {"worker_id": _WORKER, "reason": "superseded_connection"})


async def test_a_stored_snapshot_is_not_counted_as_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The counter's whole value is that it only moves on a real drop."""
    hub, worker = await _hub()
    counter = MagicMock()
    monkeypatch.setattr(snapshot_metrics, "snapshot_commit_dropped", counter)

    await hub.router.commit_snapshot_event(_WORKER, _snapshot(), expected_worker=worker)

    counter.add.assert_not_called()


# ---------------------------------------------------------------------------
# The warning
# ---------------------------------------------------------------------------


async def test_the_drop_warning_names_the_worker_the_reason_and_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted as one exact call.

    The screen hash is what makes the line answer "which frame was lost"; the
    reason is what makes it answer "why". Both travel as keywords, so a dropped
    or renamed one is silently absent from the record rather than an error.
    """
    hub, _worker = await _hub()
    recorder = MagicMock()
    monkeypatch.setattr(router_impl, "logger", recorder)

    await hub.router.commit_snapshot_event(_WORKER, _snapshot(), expected_worker=AsyncMock())

    recorder.warning.assert_called_once_with(
        "snapshot_commit_dropped",
        worker_id=_WORKER,
        reason="superseded_connection",
        screen_hash="sha256:abc",
    )


async def test_the_drop_warning_reports_the_unregistered_reason_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conditional is evaluated twice — the log field needs its own both-sides pin."""
    hub, worker = await _hub()
    recorder = MagicMock()
    monkeypatch.setattr(router_impl, "logger", recorder)

    await hub.router.commit_snapshot_event("nobody", _snapshot(), expected_worker=worker)

    recorder.warning.assert_called_once_with(
        "snapshot_commit_dropped",
        worker_id="nobody",
        reason="unregistered",
        screen_hash="sha256:abc",
    )


async def test_a_stored_snapshot_warns_about_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    hub, worker = await _hub()
    recorder = MagicMock()
    monkeypatch.setattr(router_impl, "logger", recorder)

    await hub.router.commit_snapshot_event(_WORKER, _snapshot(), expected_worker=worker)

    recorder.warning.assert_not_called()


# ---------------------------------------------------------------------------
# The unclaimed miss — refused differently from a fenced one
# ---------------------------------------------------------------------------


async def test_an_unclaimed_publish_to_a_missing_worker_returns_sequence_zero() -> None:
    """Not a drop: no ownership was claimed, so the caller gets its frame back.

    Sequence zero is the signal that nothing was stored. Returning ``None``
    here instead would be indistinguishable from the fenced refusal above,
    which is a different situation with a different remedy.
    """
    hub, _worker = await _hub()

    committed = await hub.router.commit_snapshot_event("nobody", _snapshot())

    assert committed is not None
    assert committed["event_seq"] == 0
    assert committed["screen"] == "$ ls"
