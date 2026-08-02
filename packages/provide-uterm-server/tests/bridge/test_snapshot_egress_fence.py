#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Snapshot egress fencing — a stale snapshot must never reach a browser.

``WorkerTermState.snapshot_egress_fence`` serializes snapshot broadcasts so a
newer snapshot cannot overtake an older one on the wire. Serialization alone is
not enough: while an older broadcast waits for the fence (or awaits the output
policy gate), the worker may be replaced, deregistered, or a newer snapshot may
commit. Every one of those makes the waiting frame stale, and
:func:`~provide.uterm.server.bridge.hub.router_broadcast.broadcast` must drop it
rather than emit it late.

These tests drive each abort point deterministically by holding the fence (or
pausing inside the policy gate) and mutating hub state underneath the waiter.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder
from provide.uterm.server.bridge.hub import TermHub

if TYPE_CHECKING:
    from provide.uterm.server.bridge.hub.ext import PolicyContext, RedactionRule

_WORKER_ID = "bot1"


def _snapshot(*, screen: str) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 1, "y": 0},
        "cols": 132,
        "rows": 43,
        "screen_hash": "sha256:raw-screen",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "command", "matched": "P"},
        "raw_tail": screen,
        "ts": 1234.5,
    }


def _snapshot_screens(browser: AsyncMock) -> list[str]:
    decoder = ControlFrameDecoder()
    screens: list[str] = []
    for call in browser.send_text.call_args_list:
        for event in decoder.feed(call.args[0]):
            if isinstance(event, ControlChunk) and event.control.get("type") == "snapshot":
                screens.append(str(event.control["screen"]))
    return screens


async def _make_hub(**kwargs: Any) -> tuple[TermHub, AsyncMock, AsyncMock]:
    hub = TermHub(**kwargs)
    worker = AsyncMock()
    browser = AsyncMock()
    await hub.register_worker(_WORKER_ID, worker)
    hub.registry._workers[_WORKER_ID].browsers[browser] = "viewer"
    return hub, worker, browser


async def _await_fence_contention(fence: asyncio.Lock) -> None:
    """Yield until a second task is parked on *fence*."""
    for _ in range(200):
        if getattr(fence, "_waiters", None):
            return
        await asyncio.sleep(0)
    raise AssertionError("broadcast never reached the snapshot egress fence")


@pytest.mark.asyncio
async def test_broadcast_fails_closed_on_a_half_specified_snapshot_fence() -> None:
    """A partial fence contract must suppress the frame, not silently unfence it.

    ``expected_worker`` and ``expected_event_seq`` together identify one
    snapshot generation. Supplying only one of them cannot be validated, so a
    caller that passes half the contract (a refactor dropping an argument, say)
    must get *no* egress rather than an unchecked broadcast of raw screen data.
    """
    hub, worker, browser = await _make_hub()
    committed = await hub.commit_snapshot_event(_WORKER_ID, _snapshot(screen="current"), expected_worker=worker)
    assert committed is not None

    await hub.broadcast(_WORKER_ID, committed, expected_worker=worker)
    await hub.broadcast(_WORKER_ID, committed, expected_event_seq=committed["event_seq"])

    assert _snapshot_screens(browser) == []

    await hub.broadcast(
        _WORKER_ID,
        committed,
        expected_worker=worker,
        expected_event_seq=committed["event_seq"],
    )

    assert _snapshot_screens(browser) == ["current"]


@pytest.mark.asyncio
async def test_fenced_snapshot_is_dropped_when_the_worker_deregisters_while_waiting() -> None:
    """A worker that vanishes behind the fence cancels its pending snapshot.

    Deregistration removes the whole ``WorkerTermState``; the frame waiting on
    that state's fence belongs to a session that no longer exists, so it must
    not be delivered to browsers that are being torn down alongside it.
    """
    hub, worker, browser = await _make_hub()
    committed = await hub.commit_snapshot_event(_WORKER_ID, _snapshot(screen="orphan"), expected_worker=worker)
    assert committed is not None
    state = hub.registry._workers[_WORKER_ID]

    await state.snapshot_egress_fence.acquire()
    pending = asyncio.create_task(
        hub.broadcast(
            _WORKER_ID,
            committed,
            expected_worker=worker,
            expected_event_seq=committed["event_seq"],
        )
    )
    await _await_fence_contention(state.snapshot_egress_fence)

    hub.registry._workers.pop(_WORKER_ID)
    state.snapshot_egress_fence.release()
    await asyncio.wait_for(pending, timeout=1.0)

    assert _snapshot_screens(browser) == []


@pytest.mark.asyncio
async def test_fenced_snapshot_is_dropped_when_a_newer_snapshot_commits_while_waiting() -> None:
    """The newest committed sequence wins; the queued older frame is discarded.

    Without the re-check behind the fence the older screen would still be
    written to the socket after the newer one, leaving every viewer showing a
    superseded terminal state until the next update.
    """
    hub, worker, browser = await _make_hub()
    old = await hub.commit_snapshot_event(_WORKER_ID, _snapshot(screen="old"), expected_worker=worker)
    assert old is not None
    state = hub.registry._workers[_WORKER_ID]

    await state.snapshot_egress_fence.acquire()
    pending = asyncio.create_task(
        hub.broadcast(_WORKER_ID, old, expected_worker=worker, expected_event_seq=old["event_seq"])
    )
    await _await_fence_contention(state.snapshot_egress_fence)

    newer = await hub.commit_snapshot_event(_WORKER_ID, _snapshot(screen="newer"), expected_worker=worker)
    assert newer is not None
    state.snapshot_egress_fence.release()
    await asyncio.wait_for(pending, timeout=1.0)

    assert _snapshot_screens(browser) == []
    assert newer["event_seq"] == old["event_seq"] + 1

    await hub.broadcast(_WORKER_ID, newer, expected_worker=worker, expected_event_seq=newer["event_seq"])
    assert _snapshot_screens(browser) == ["newer"]


class _CommitDuringPolicyGate:
    """Output policy gate that commits a newer snapshot mid-broadcast.

    ``payloads_by_role`` awaits this gate once per distinct viewer role, and
    that await is the last suspension point before the socket write. Committing
    here reproduces the real race: a worker frame landing while the hub is busy
    resolving redaction rules for an earlier one.
    """

    def __init__(self, hub: TermHub, worker: AsyncMock) -> None:
        self._hub = hub
        self._worker = worker
        self.calls = 0
        self.superseding_event_seq: int | None = None

    async def get_redaction_rules(self, context: PolicyContext) -> list[RedactionRule]:
        del context
        self.calls += 1
        if self.superseding_event_seq is None:
            committed = await self._hub.commit_snapshot_event(
                _WORKER_ID,
                _snapshot(screen="superseding"),
                expected_worker=self._worker,
            )
            assert committed is not None
            self.superseding_event_seq = int(committed["event_seq"])
        return []


@pytest.mark.asyncio
async def test_fenced_snapshot_is_revalidated_after_the_policy_gate_await() -> None:
    """Ownership is re-checked after redaction resolution, not only before it.

    The policy gate is awaited outside the hub lock, so an ownership check made
    before it is already stale by the time the payload is written. This drives a
    newer commit from inside the gate itself and asserts the older frame never
    reaches the socket.
    """
    hub, worker, browser = await _make_hub()
    gate = _CommitDuringPolicyGate(hub, worker)
    hub._output_policy_gate = gate  # type: ignore[assignment]
    old = await hub.commit_snapshot_event(_WORKER_ID, _snapshot(screen="stale"), expected_worker=worker)
    assert old is not None

    await hub.broadcast(_WORKER_ID, old, expected_worker=worker, expected_event_seq=old["event_seq"])

    assert gate.calls == 1
    assert gate.superseding_event_seq == old["event_seq"] + 1
    assert _snapshot_screens(browser) == []
