#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for the Durable Object flow controller.

A Worker cannot see its own outbound buffer — workerd exposes no
``bufferedAmount`` — so backpressure is driven by what the browsers say they
have consumed. Each reports a cumulative byte count, and what is in flight is
what was sent minus what was acknowledged.

**A silent browser is ignored.** This is the trap the whole design is built
around: a client that stops acknowledging looks maximally congested for ever,
and counting it would pause the producer permanently for one stuck tab. Only
browsers that acknowledged inside the grace window are considered, and a
session with none at all never pauses.

**Acknowledgements only move forwards.** They are cumulative counts, so a
stale or replayed one carrying a lower number must not rewind what a browser
is known to have consumed — that would invent congestion that is not there.

**Congestion is sticky, with hysteresis.** A browser becomes congested above
the high-water mark and stays so until it drains below the low-water mark.
Without the gap it would flap either side of a single threshold, pausing and
resuming on every frame.

**The producer pauses only when every active browser is congested.** If even
the fastest consumer can keep up there is something worth producing; pausing
on the slowest would let one browser throttle everybody else's session.

**A browser that recovers is reported once.** It missed frames while
congested, so it needs a fresh snapshot — and reporting it twice would send
two.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_cfflow_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.do.session_runtime.flow_control import PAUSE, RESUME, FlowController

OUT = Path(__file__).with_name("cfflow_golden.json")

HIGH = 1000
LOW = 200
GRACE = 10.0


def _controller() -> FlowController:
    """A controller with the corpus's own thresholds."""
    return FlowController(high_water=HIGH, low_water=LOW, ack_grace_s=GRACE)


def _state(controller: FlowController, now: float, ids: list[str]) -> dict[str, Any]:
    """Everything observable about a controller."""
    return {
        "paused": controller.paused,
        "max_inflight": controller.max_inflight(now),
        "all_active_congested": controller.all_active_congested(now),
        "congested": {ws_id: controller.is_congested(ws_id) for ws_id in ids},
    }


def _record_monotonic_acks() -> dict[str, Any]:
    """A replayed acknowledgement must not rewind what was consumed."""
    controller = _controller()
    controller.on_sent("a", 500)
    controller.on_ack("a", 400, now=1.0)
    after_first = controller.max_inflight(1.0)
    # An older acknowledgement arriving late.
    controller.on_ack("a", 100, now=2.0)
    after_stale = controller.max_inflight(2.0)
    controller.on_ack("a", 500, now=3.0)
    after_newer = controller.max_inflight(3.0)
    return {"after_first": after_first, "after_stale": after_stale, "after_newer": after_newer}


def _record_silent_client() -> dict[str, Any]:
    """A browser that stops acknowledging is left out of the decision."""
    controller = _controller()
    controller.on_sent("stuck", HIGH + 1)
    controller.on_ack("stuck", 0, now=0.0)
    # Well past the grace window.
    now = GRACE + 5
    return {
        "still_congested": controller.is_congested("stuck"),
        "max_inflight_ignores_it": controller.max_inflight(now),
        "all_active_congested": controller.all_active_congested(now),
        "decision": controller.decide(now),
        "inside_the_window": controller.all_active_congested(GRACE),
        "on_the_boundary": controller.all_active_congested(GRACE),
        "just_past_the_boundary": controller.all_active_congested(GRACE + 0.001),
    }


def _record_no_ackers() -> dict[str, Any]:
    """A session where nobody has ever acknowledged never pauses."""
    controller = _controller()
    controller.on_sent("a", HIGH * 10)
    return {
        "all_active_congested": controller.all_active_congested(1.0),
        "decision": controller.decide(1.0),
        "max_inflight": controller.max_inflight(1.0),
        "is_congested": controller.is_congested("a"),
    }


def _record_hysteresis() -> list[dict[str, Any]]:
    """Congestion sticks between the two marks."""
    controller = _controller()
    steps: list[dict[str, Any]] = []
    sent_total = 0
    acked_total = 0

    def step(name: str, inflight: int, now: float) -> None:
        """Drive the browser to an exact inflight figure and record the state.

        Sending accumulates and acknowledgements only move forwards, so a
        target is reached by sending more or by acknowledging more — never by
        rewinding either.
        """
        nonlocal sent_total, acked_total
        target_sent = acked_total + inflight
        if target_sent > sent_total:
            controller.on_sent("a", target_sent - sent_total)
            sent_total = target_sent
        else:
            acked_total = sent_total - inflight
            controller.on_ack("a", acked_total, now=now)
        controller.on_ack("a", acked_total, now=now)
        steps.append({"name": name, "inflight": inflight, **_state(controller, now, ["a"])})

    step("below the high mark", HIGH - 1, 1.0)
    step("exactly on the high mark", HIGH, 2.0)
    step("just above the high mark", HIGH + 1, 3.0)
    step("draining, still above the low mark", LOW + 1, 4.0)
    step("exactly on the low mark", LOW, 5.0)
    step("just below the low mark", LOW - 1, 6.0)
    step("back above the high mark", HIGH + 1, 7.0)
    return steps


def _record_fairness() -> dict[str, Any]:
    """One slow browser must not pause everybody."""
    controller = _controller()
    controller.on_sent("slow", HIGH + 1)
    controller.on_ack("slow", 0, now=1.0)
    controller.on_sent("fast", 10)
    controller.on_ack("fast", 10, now=1.0)

    one_congested = {
        "all_active_congested": controller.all_active_congested(1.0),
        "decision": controller.decide(1.0),
        "slow_is_congested": controller.is_congested("slow"),
        "fast_is_congested": controller.is_congested("fast"),
    }

    # Now the fast one falls behind too.
    controller.on_sent("fast", HIGH + 1)
    both_congested = {
        "all_active_congested": controller.all_active_congested(1.0),
        "decision": controller.decide(1.0),
        "paused": controller.paused,
    }

    # And recovers.
    controller.on_ack("fast", HIGH + 11, now=2.0)
    recovered = {
        "all_active_congested": controller.all_active_congested(2.0),
        "decision": controller.decide(2.0),
        "paused": controller.paused,
    }
    return {"one_congested": one_congested, "both_congested": both_congested, "recovered": recovered}


def _record_recovered() -> dict[str, Any]:
    """A browser that un-congests is reported once, then forgotten."""
    controller = _controller()
    controller.on_sent("a", HIGH + 1)
    controller.on_ack("a", 0, now=1.0)
    before = sorted(controller.take_recovered())
    controller.on_ack("a", HIGH + 1, now=2.0)
    after = sorted(controller.take_recovered())
    again = sorted(controller.take_recovered())
    return {"before": before, "after": after, "again": again}


def _record_forget() -> dict[str, Any]:
    """A disconnected browser leaves nothing behind."""
    controller = _controller()
    controller.on_sent("a", HIGH + 1)
    controller.on_ack("a", 0, now=1.0)
    controller.on_sent("b", 10)
    controller.on_ack("b", 10, now=1.0)
    before = _state(controller, 1.0, ["a", "b"])
    controller.forget("a")
    after = _state(controller, 1.0, ["a", "b"])
    forget_unknown_raises = False
    try:
        controller.forget("never-seen")
    except Exception:
        forget_unknown_raises = True
    return {"before": before, "after": after, "forget_unknown_raises": forget_unknown_raises}


def _record_decisions() -> list[dict[str, Any]]:
    """The producer is told to pause and resume once, not repeatedly."""
    controller = _controller()
    out: list[dict[str, Any]] = []

    def note(name: str, now: float) -> None:
        out.append({"name": name, "decision": controller.decide(now), "paused": controller.paused})

    controller.on_sent("a", HIGH + 1)
    controller.on_ack("a", 0, now=1.0)
    note("first congested check", 1.0)
    note("still congested", 1.0)
    controller.on_ack("a", HIGH + 1, now=2.0)
    note("first clear check", 2.0)
    note("still clear", 2.0)
    return out


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = {
        "high_water": HIGH,
        "low_water": LOW,
        "ack_grace_s": GRACE,
        "pause": PAUSE,
        "resume": RESUME,
        "monotonic_acks": _record_monotonic_acks(),
        "silent_client": _record_silent_client(),
        "no_ackers": _record_no_ackers(),
        "hysteresis": _record_hysteresis(),
        "fairness": _record_fairness(),
        "recovered": _record_recovered(),
        "forget": _record_forget(),
        "decisions": _record_decisions(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['hysteresis'])} hysteresis steps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
