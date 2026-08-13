#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TermBridge publishes a snapshot when the screen changes, not only on request.

Raw terminal bytes have always streamed on every chunk, but the SNAPSHOT — the
only frame carrying ``screen_hash``, which is how a client detects that the
screen changed — used to be sent solely in reply to a ``snapshot_req``. A screen
change was therefore invisible to every consumer until some external party
happened to ask. Measured gaps between requests were 4.4s, 17.2s, 17.9s and
18.7s; a client waiting 4s for a prompt times out inside any of them and reports
a stall against a worker that is behaving perfectly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock, patch

from provide.uterm.defaults import TerminalDefaults
from provide.uterm.server.bridge.worker_link import TermBridge

# Short enough to keep the suite fast. Presence assertions never depend on this
# value — they await the push task itself (see _await_push) — so a loaded runner
# makes them slower, never flaky.
_DEBOUNCE_S = 0.02
# Only for asserting that NOTHING was published: long enough that a push which
# was wrongly armed would have fired by now.
_QUIET_S = _DEBOUNCE_S * 6


class _Session:
    """Session double whose snapshot carries a distinguishable hash."""

    def __init__(self, screen_hash: str = "hash-a") -> None:
        self._watches: list[Any] = []
        self.emulator = MagicMock()
        self.emulator.get_snapshot.return_value = {
            "screen": "sector 1",
            "cursor": {"x": 1, "y": 2},
            "cols": 80,
            "rows": 25,
            "screen_hash": screen_hash,
        }

    def set_hash(self, screen_hash: str) -> None:
        """Repoint the emulator at a new screen, as a real change would."""
        self.emulator.get_snapshot.return_value = {
            **self.emulator.get_snapshot.return_value,
            "screen_hash": screen_hash,
        }

    def add_watch(self, fn: Any, *, interval_s: float) -> None:
        self._watches.append(fn)

    async def send(self, data: str) -> None:  # pragma: no cover - unused here
        raise AssertionError("these tests never send keystrokes")

    async def set_size(self, cols: int, rows: int) -> None:  # pragma: no cover - unused here
        raise AssertionError("these tests never resize")


class _Worker:
    def __init__(self, session: _Session | None) -> None:
        self.session = session

    async def set_hijacked(self, enabled: bool) -> None:  # pragma: no cover - unused here
        raise AssertionError("these tests never hijack")


def _make_bridge(
    *,
    screen_hash: str = "hash-a",
    debounce_s: float = _DEBOUNCE_S,
) -> tuple[TermBridge, _Session]:
    session = _Session(screen_hash)
    bridge = TermBridge(
        _Worker(session),
        "bot1",
        "http://localhost:8000",
        snapshot_debounce_s=debounce_s,
    )
    bridge.attach_session()
    return bridge, session


def _drain(bridge: TermBridge) -> list[dict[str, Any]]:
    return [bridge._send_q.get_nowait() for _ in range(bridge._send_q.qsize())]


def _snapshots(bridge: TermBridge) -> list[dict[str, Any]]:
    return [m for m in _drain(bridge) if m.get("type") == "snapshot"]


async def _await_push(bridge: TermBridge, *, timeout_s: float = 5.0) -> None:
    """Block until the currently-armed push task has run to completion.

    Waiting on the task rather than on wall-clock time keeps these assertions
    deterministic: a loaded runner makes the test slower, never flaky.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = bridge._snapshot_push_task
        if task is not None and task.done():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the debounced snapshot push never fired")


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


class TestSnapshotPublishedOnChange:
    async def test_screen_change_publishes_a_snapshot_unprompted(self) -> None:
        """Bytes arriving must produce a snapshot with no ``snapshot_req``."""
        bridge, session = _make_bridge()

        session._watches[0]({"screen": "sector 1"}, b"catalog bytes")
        await _await_push(bridge)

        pushed = _snapshots(bridge)
        assert len(pushed) == 1, f"expected exactly one unprompted snapshot, got {len(pushed)}"
        assert pushed[0]["screen_hash"] == "hash-a", (
            "the snapshot must carry the screen hash — it is the only thing a client can diff"
        )
        await bridge.stop()

    async def test_the_push_is_debounced_not_immediate(self) -> None:
        """Nothing is on the wire before the window elapses.

        Without this, a per-chunk snapshot would put a fully rendered screen on
        the wire for every byte burst.
        """
        bridge, session = _make_bridge()

        session._watches[0]({"screen": "sector 1"}, b"x")
        await asyncio.sleep(0)  # let the task start, but not finish its sleep

        assert _snapshots(bridge) == []
        await bridge.stop()

    async def test_a_burst_coalesces_into_one_snapshot(self) -> None:
        """Many chunks in quick succession publish once, not once per chunk.

        Bursts of 8 frames inside 0.4ms have been measured upstream.
        """
        bridge, session = _make_bridge()

        for _ in range(8):
            session._watches[0]({"screen": "sector 1"}, b"x")
        await _await_push(bridge)
        # Give a wrongly-armed second push time to fire before counting.
        await asyncio.sleep(_QUIET_S)

        assert len(_snapshots(bridge)) == 1
        await bridge.stop()

    async def test_a_later_change_publishes_again(self) -> None:
        """The debounce coalesces a burst; it must not suppress the next one."""
        bridge, session = _make_bridge()

        session._watches[0]({"screen": "sector 1"}, b"first")
        await _await_push(bridge)
        session._watches[0]({"screen": "sector 2"}, b"second")
        await _await_push(bridge)

        assert len(_snapshots(bridge)) == 2
        await bridge.stop()

    async def test_empty_raw_publishes_nothing(self) -> None:
        """A watch tick with no bytes is not a screen change."""
        bridge, session = _make_bridge()

        session._watches[0]({"screen": "sector 1"}, b"")
        await asyncio.sleep(_QUIET_S)

        assert _drain(bridge) == []
        assert bridge._snapshot_push_task is None
        await bridge.stop()

    async def test_terminal_data_still_streams_alongside(self) -> None:
        """The snapshot is additive — raw bytes must keep flowing as before."""
        bridge, session = _make_bridge()

        session._watches[0]({"screen": "sector 1"}, b"Hello")
        await _await_push(bridge)

        queued = _drain(bridge)
        assert [m["type"] for m in queued] == ["term", "snapshot"], (
            "terminal data must precede the snapshot it describes"
        )
        assert "Hello" in queued[0]["data"]
        await bridge.stop()


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class TestSnapshotPushScheduling:
    def test_watcher_outside_a_running_loop_is_harmless(self) -> None:
        """No running loop → no task, no exception; request-driven push still works."""
        bridge, session = _make_bridge()

        session._watches[0]({"screen": "sector 1"}, b"x")

        assert bridge._snapshot_push_task is None
        assert [m["type"] for m in _drain(bridge)] == ["term"]

    async def test_a_pending_push_is_not_rearmed(self) -> None:
        """A push already pending covers changes that arrive before it fires."""
        bridge, session = _make_bridge()

        session._watches[0]({"screen": "sector 1"}, b"x")
        first = bridge._snapshot_push_task
        session._watches[0]({"screen": "sector 2"}, b"y")

        assert bridge._snapshot_push_task is first, "re-arming would add wire traffic for the same screen"
        await bridge.stop()

    async def test_a_change_during_the_send_rearms_the_push(self) -> None:
        """A change that lands mid-send is not in the frame being sent.

        ``_send_snapshot`` reads the emulator once and then does a BLOCKING put
        onto a bounded queue, and ``_schedule_snapshot_push`` refuses to arm
        while this task is still running — so without the re-arm the late change
        stays unpublished until someone asks, which is the exact failure the
        push exists to remove.

        This bridge was never ``start()``ed, which is the normal state for a
        watcher-driven publisher; gating the re-arm on ``_running`` would
        silently disable it here.
        """
        bridge, session = _make_bridge()
        real_send = bridge._send_snapshot
        fired: list[int] = []

        async def _send_then_change() -> None:
            await real_send()
            if not fired:  # once, or the re-arm would recur forever
                fired.append(1)
                session._watches[0]({"screen": "sector 2"}, b"late")

        with patch.object(bridge, "_send_snapshot", _send_then_change):
            session._watches[0]({"screen": "sector 1"}, b"first")
            await _await_push(bridge)

        queued = _drain(bridge)
        assert len([m for m in queued if m["type"] == "snapshot"]) == 2, (
            "the change that landed mid-send must get its own snapshot"
        )
        assert any(m["type"] == "term" and "late" in m["data"] for m in queued)
        await bridge.stop()

    async def test_no_rearm_once_stopped(self) -> None:
        """Teardown wins over a change that lands during the final send."""
        bridge, session = _make_bridge()
        real_send = bridge._send_snapshot

        async def _send_then_stop_and_change() -> None:
            await real_send()
            bridge._snapshot_push_stopped = True  # as stop() sets it
            session._watches[0]({"screen": "sector 2"}, b"late")

        with patch.object(bridge, "_send_snapshot", _send_then_stop_and_change):
            session._watches[0]({"screen": "sector 1"}, b"first")
            await _await_push(bridge)
            await asyncio.sleep(_QUIET_S)

        assert len(_snapshots(bridge)) == 1, "a stopped bridge must not publish again"
        await bridge.stop()

    async def test_stop_marks_the_push_stopped(self) -> None:
        """``stop()`` must set the flag the re-arm checks, not only cancel."""
        bridge, _session = _make_bridge()
        assert bridge._snapshot_push_stopped is False

        await bridge.stop()

        assert bridge._snapshot_push_stopped is True

    async def test_the_default_debounce_comes_from_terminal_defaults(self) -> None:
        """The interval is a shared default, not a literal buried in the bridge."""
        bridge = TermBridge(_Worker(None), "bot1", "http://localhost:8000")

        assert bridge._snapshot_debounce_s == TerminalDefaults.SNAPSHOT_PUSH_DEBOUNCE_S


# ---------------------------------------------------------------------------
# Failure and shutdown
# ---------------------------------------------------------------------------


class TestSnapshotPushLifecycle:
    async def test_stop_cancels_a_pending_push(self) -> None:
        """A debounced push must not outlive the bridge."""
        bridge, session = _make_bridge(debounce_s=30.0)

        session._watches[0]({"screen": "sector 1"}, b"x")
        pending = bridge._snapshot_push_task
        assert pending is not None and not pending.done()

        await bridge.stop()

        assert bridge._snapshot_push_task is None
        assert pending.cancelled()
        assert _snapshots(bridge) == []

    async def test_cancelling_a_push_is_not_logged_as_a_failure(self) -> None:
        """Shutdown is not an error — a cancelled push must stay quiet."""
        bridge, session = _make_bridge(debounce_s=30.0)
        session._watches[0]({"screen": "sector 1"}, b"x")

        mock_logger = MagicMock()
        with patch("provide.uterm.server.bridge.worker_link.logger", mock_logger):
            await bridge.stop()

        warnings = [str(c) for c in mock_logger.warning.call_args_list]
        assert not any("snapshot_push_failed" in c for c in warnings), warnings

    async def test_stop_after_the_push_fired_is_a_noop(self) -> None:
        """Stopping once the push has completed must not raise."""
        bridge, session = _make_bridge()

        session._watches[0]({"screen": "sector 1"}, b"x")
        await _await_push(bridge)
        completed = bridge._snapshot_push_task
        assert completed is not None and completed.done()

        await bridge.stop()

        assert bridge._snapshot_push_task is None

    async def test_stop_without_any_push_is_a_noop(self) -> None:
        """A bridge that never saw a screen change stops cleanly."""
        bridge, _session = _make_bridge()

        await bridge.stop()

        assert bridge._snapshot_push_task is None

    async def test_a_failing_push_is_logged_and_does_not_kill_the_bridge(self) -> None:
        """Publishing is best-effort — a failure must not take the bridge down."""
        bridge, session = _make_bridge()

        async def _boom() -> None:
            raise RuntimeError("emulator exploded")

        mock_logger = MagicMock()
        with (
            patch.object(bridge, "_send_snapshot", _boom),
            patch("provide.uterm.server.bridge.worker_link.logger", mock_logger),
        ):
            session._watches[0]({"screen": "sector 1"}, b"x")
            await _await_push(bridge)

        task = bridge._snapshot_push_task
        assert task is not None and task.done() and task.exception() is None, (
            "the push task must absorb the failure rather than surface an unretrieved exception"
        )
        warnings = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("snapshot_push_failed" in c for c in warnings), warnings
        assert any("emulator exploded" in c for c in warnings), warnings

        # And the next change still publishes — the bridge is not wedged.
        session._watches[0]({"screen": "sector 2"}, b"y")
        await _await_push(bridge)
        assert len(_snapshots(bridge)) == 1
        await bridge.stop()

    async def test_a_push_with_no_session_publishes_nothing(self) -> None:
        """A worker that lost its session has no screen to describe."""
        bridge, session = _make_bridge()
        bridge._worker.session = None

        session._watches[0]({"screen": "sector 1"}, b"x")
        await _await_push(bridge)

        assert _snapshots(bridge) == []
        await bridge.stop()


# ---------------------------------------------------------------------------
# Changes that land while a snapshot is in flight
# ---------------------------------------------------------------------------


class TestChangeDuringSend:
    """A change arriving mid-send must still reach the wire.

    _send_snapshot reads the emulator once and then does a BLOCKING put onto a
    bounded queue, while _schedule_snapshot_push refuses to re-arm for as long
    as the push task is running. Together those leave a window in which a screen
    change is neither in the frame being sent nor scheduled to follow it — the
    exact "invisible until someone asks" failure the push exists to remove, just
    narrower. Anything that makes the put slow (a full queue, a stalled reader)
    widens it.
    """

    async def test_a_change_during_the_send_is_published_after_it(self) -> None:
        bridge, session = _make_bridge()
        original_put = bridge._send_q.put
        landed = False

        async def put_and_change(item: dict[str, Any]) -> None:
            # Fire a change from INSIDE the put, i.e. after the snapshot was
            # captured and before it is queued.
            nonlocal landed
            if not landed and item.get("type") == "snapshot":
                landed = True
                session.set_hash("hash-b")
                session._watches[0]({"screen": "sector 2"}, b"second")
            await original_put(item)

        bridge._send_q.put = put_and_change  # type: ignore[method-assign]

        session._watches[0]({"screen": "sector 1"}, b"first")

        deadline = time.monotonic() + 5.0
        seen: list[dict[str, Any]] = []
        while time.monotonic() < deadline and len(seen) < 2:
            seen.extend(_snapshots(bridge))
            await asyncio.sleep(0.001)

        assert landed, "the test never injected a change during the send"
        assert len(seen) == 2, f"the mid-send change was never published (got {len(seen)} snapshot(s))"
        # The second frame must carry the NEW screen, not a repeat of the first.
        assert [s["screen_hash"] for s in seen] == ["hash-a", "hash-b"]
        await bridge.stop()

    async def test_no_rearm_when_nothing_changed_during_the_send(self) -> None:
        """The re-arm is conditional — a quiet send must not loop."""
        bridge, session = _make_bridge()
        session._watches[0]({"screen": "sector 1"}, b"first")
        await _await_push(bridge)
        await asyncio.sleep(_QUIET_S)

        assert len(_snapshots(bridge)) == 1
        await bridge.stop()
