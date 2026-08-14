#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A committed snapshot that reaches no browser must say so.

``commit_snapshot_event`` storing a frame does not mean any client saw it. The
broadcast that follows can end with nobody receiving it in two ways that leave
no trace:

* every registered browser is filtered out (none registered yet, or all still
  in ``_startup_pending_browsers``), so the fan-out sends to an empty set and
  returns normally. Emitted at TRACE: the hijack path registers no browsers at
  all, so it fires on essentially every snapshot — 9,881 times in one measured
  run — and ``logger.trace`` is a no-op until TRACE is explicitly enabled;
* a send raises or times out, which is recorded at DEBUG — below the level a
  manager runs at, so it is invisible in production logs.

Live 2026-08-14: the worker read the Shipyards menu (chunks 48, 49, 50 —
2, 957 and 41 bytes), published a snapshot capturing it, and the hub committed
it with zero ``snapshot_commit_dropped``. Two and a half seconds later the
client's newest snapshot still reported the pre-burst ingest count. Commit
succeeded and delivery did not, and nothing between them said which.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.bridge.hub import TermHub, router_broadcast

_WORKER_ID = "bot1"


def _snapshot(*, screen: str = "shipyards") -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 1, "y": 0},
        "cols": 132,
        "rows": 43,
        "screen_hash": f"sha256:{screen}",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "raw_tail": screen,
        "ts": 1234.5,
    }


async def _make_hub() -> tuple[TermHub, AsyncMock]:
    hub = TermHub()
    worker = AsyncMock()
    await hub.register_worker(_WORKER_ID, worker)
    return hub, worker


@pytest.mark.asyncio
async def test_a_snapshot_with_no_eligible_browser_is_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delivered to nobody — emitted, and emitted at TRACE.

    Asserted against the logger rather than ``caplog``: ``trace`` is a no-op
    unless TRACE is enabled, which is the property that makes it free to leave
    in, so capturing it would mean reconfiguring telemetry mid-test and would
    stop testing the level actually used.
    """
    hub, _worker = await _make_hub()
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        """``_TraceWrapper`` uses ``__slots__``, so swap the whole logger."""

        def __init__(self) -> None:
            self._real = router_broadcast.logger

        def trace(self, event: str, **kw: Any) -> None:
            calls.append((event, kw))

        def __getattr__(self, item: str) -> Any:
            return getattr(self._real, item)

    monkeypatch.setattr(router_broadcast, "logger", _Recorder())

    await hub.broadcast(_WORKER_ID, _snapshot())

    assert calls, "a snapshot reaching nobody must still be recorded"
    event, kw = calls[0]
    assert event == "snapshot_broadcast_no_browsers"
    assert kw["worker_id"] == _WORKER_ID
    assert kw["registered"] == 0


@pytest.mark.asyncio
async def test_the_no_browsers_line_never_warns(caplog: pytest.LogCaptureFixture) -> None:
    """It must not drown the two lines beside it that fire only on a real fault."""
    hub, _worker = await _make_hub()

    with caplog.at_level(logging.WARNING):
        await hub.broadcast(_WORKER_ID, _snapshot())

    assert "snapshot_broadcast_no_browsers" not in caplog.text, f"must not warn; got {caplog.text!r}"


@pytest.mark.asyncio
async def test_a_delivered_snapshot_stays_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """One live browser is the happy path and must not log."""
    hub, _worker = await _make_hub()
    browser = AsyncMock()
    hub.registry._workers[_WORKER_ID].browsers[browser] = "viewer"

    with caplog.at_level(logging.WARNING):
        await hub.broadcast(_WORKER_ID, _snapshot())

    assert browser.send_text.await_count == 1
    assert "snapshot_broadcast_no_browsers" not in caplog.text


@pytest.mark.asyncio
async def test_a_non_snapshot_frame_with_no_browsers_stays_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Terminal data flows constantly with nobody watching; only snapshots matter."""
    hub, _worker = await _make_hub()

    with caplog.at_level(logging.WARNING):
        await hub.broadcast(_WORKER_ID, {"type": "term", "data": "x", "ts": 1.0})

    assert "snapshot_broadcast_no_browsers" not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_snapshot_send_is_visible_above_debug(caplog: pytest.LogCaptureFixture) -> None:
    """A send that times out must not be recorded below the level managers run at."""
    hub, _worker = await _make_hub()
    browser = AsyncMock()
    browser.send_text.side_effect = TimeoutError("socket stalled")
    hub.registry._workers[_WORKER_ID].browsers[browser] = "viewer"

    with caplog.at_level(logging.WARNING):
        await hub.broadcast(_WORKER_ID, _snapshot())

    assert "snapshot_broadcast_send_failed" in caplog.text, f"got {caplog.text!r}"


@pytest.mark.asyncio
async def test_a_failed_term_send_stays_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """Only snapshots get the louder line — term frames would drown the log."""
    hub, _worker = await _make_hub()
    browser = AsyncMock()
    browser.send_text.side_effect = TimeoutError("socket stalled")
    hub.registry._workers[_WORKER_ID].browsers[browser] = "viewer"

    with caplog.at_level(logging.WARNING):
        await hub.broadcast(_WORKER_ID, {"type": "term", "data": "x", "ts": 1.0})

    assert "snapshot_broadcast_send_failed" not in caplog.text


@pytest.mark.asyncio
async def test_two_browsers_one_failing_still_names_the_failure(caplog: pytest.LogCaptureFixture) -> None:
    """The concurrent fan-out path must trace a failure the same way."""
    hub, _worker = await _make_hub()
    good, bad = AsyncMock(), AsyncMock()
    bad.send_text.side_effect = TimeoutError("socket stalled")
    hub.registry._workers[_WORKER_ID].browsers[good] = "viewer"
    hub.registry._workers[_WORKER_ID].browsers[bad] = "viewer"

    with caplog.at_level(logging.WARNING):
        await hub.broadcast(_WORKER_ID, _snapshot())

    assert good.send_text.await_count == 1
    assert "snapshot_broadcast_send_failed" in caplog.text


@pytest.mark.asyncio
async def test_a_startup_pending_browser_counts_as_no_browser(caplog: pytest.LogCaptureFixture) -> None:
    """A browser still in startup is filtered out, so the snapshot reaches nobody."""
    hub, _worker = await _make_hub()
    browser = AsyncMock()
    hub.registry._workers[_WORKER_ID].browsers[browser] = "viewer"
    hub._startup_pending_browsers.add(browser)

    with caplog.at_level(logging.WARNING):
        await hub.broadcast(_WORKER_ID, _snapshot())

    # The filtered browser gets nothing — that is the behaviour that matters —
    # and the trace line stays out of the warning stream.
    assert browser.send_text.await_count == 0
    assert "snapshot_broadcast_no_browsers" not in caplog.text
    await asyncio.sleep(0)
