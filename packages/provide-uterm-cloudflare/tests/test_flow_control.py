#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for the DO terminal-relay flow controller (Tier A backpressure)."""

from __future__ import annotations

from provide.uterm.cloudflare.do.session_runtime.flow_control import (
    PAUSE,
    RESUME,
    FlowController,
)


def _fc(high: int = 1000, low: int = 400, grace: float = 5.0) -> FlowController:
    return FlowController(high_water=high, low_water=low, ack_grace_s=grace)


def test_on_sent_accumulates_per_browser() -> None:
    fc = _fc()
    fc.on_sent("a", 100)
    fc.on_sent("a", 50)  # existing key accumulates
    fc.on_sent("b", 10)  # new key
    fc.on_ack("a", 0, now=0.0)
    fc.on_ack("b", 0, now=0.0)
    assert fc.max_inflight(now=0.0) == 150


def test_on_ack_is_monotonic() -> None:
    fc = _fc()
    fc.on_sent("a", 500)
    fc.on_ack("a", 300, now=0.0)
    fc.on_ack("a", 100, now=1.0)  # stale/lower ack must be ignored
    assert fc.max_inflight(now=1.0) == 200  # 500 - 300, not 500 - 100


def test_forget_clears_state() -> None:
    fc = _fc()
    fc.on_sent("a", 999)
    fc.on_ack("a", 0, now=0.0)
    fc.forget("a")
    assert fc.max_inflight(now=0.0) == 0


def test_max_inflight_excludes_silent_browser() -> None:
    """A browser whose last ACK is older than the grace window is not counted."""
    fc = _fc(grace=5.0)
    fc.on_sent("silent", 5000)
    fc.on_ack("silent", 0, now=0.0)  # last ack at t=0
    fc.on_sent("live", 600)
    fc.on_ack("live", 0, now=9.0)  # last ack at t=9
    # At t=10: silent is 10s stale (>5 grace) → excluded; live is 1s → counted.
    assert fc.max_inflight(now=10.0) == 600


def test_max_inflight_takes_the_largest_live_browser() -> None:
    fc = _fc()
    fc.on_sent("a", 200)
    fc.on_ack("a", 0, now=1.0)
    fc.on_sent("b", 800)
    fc.on_ack("b", 0, now=1.0)
    fc.on_sent("c", 100)
    fc.on_ack("c", 0, now=1.0)
    assert fc.max_inflight(now=1.0) == 800


def test_decide_pauses_when_over_high_water() -> None:
    fc = _fc(high=1000, low=400)
    fc.on_sent("a", 1500)
    fc.on_ack("a", 0, now=0.0)
    assert fc.paused is False
    assert fc.decide(now=0.0) == PAUSE
    assert fc.paused is True


def test_decide_holds_between_watermarks_hysteresis() -> None:
    fc = _fc(high=1000, low=400)
    fc.on_sent("a", 1500)
    fc.on_ack("a", 0, now=0.0)
    assert fc.decide(now=0.0) == PAUSE  # now paused
    # Drain to 600 — below high, above low → stay paused, no signal.
    fc.on_ack("a", 900, now=1.0)  # inflight 1500 - 900 = 600
    assert fc.decide(now=1.0) is None
    assert fc.paused is True


def test_decide_resumes_when_below_low_water() -> None:
    fc = _fc(high=1000, low=400)
    fc.on_sent("a", 1500)
    fc.on_ack("a", 0, now=0.0)
    fc.decide(now=0.0)  # → PAUSE
    fc.on_ack("a", 1200, now=1.0)  # inflight 300 < low
    assert fc.decide(now=1.0) == RESUME
    assert fc.paused is False


def test_decide_no_signal_when_idle_and_unpaused() -> None:
    fc = _fc(high=1000, low=400)
    fc.on_sent("a", 100)
    fc.on_ack("a", 0, now=0.0)
    assert fc.decide(now=0.0) is None
    assert fc.paused is False
