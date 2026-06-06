#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tier-A backpressure wiring on SessionRuntime's I/O mixin.

Exercises the methods that bridge the pure FlowController to the worker control
channel: note_browser_ack (ACK ingestion), _apply_flow_control (decision), and
_signal_worker_flow (XOFF/XON emission). A minimal host carrying just the mixin
plus the attributes those methods touch keeps the test independent of the full
Durable Object runtime.
"""

from __future__ import annotations

from provide.uterm.cloudflare.do.session_runtime.flow_control import PAUSE, FlowController
from provide.uterm.cloudflare.do.session_runtime.io import _SessionRuntimeIoMixin


class _Host(_SessionRuntimeIoMixin):
    def __init__(self, *, worker_ws: object | None, high: int = 100, low: int = 40) -> None:
        self.worker_ws = worker_ws
        self._flow = FlowController(high_water=high, low_water=low, ack_grace_s=1000.0)
        self.sent: list[dict[str, object]] = []

    async def send_ws(self, ws: object, frame: dict[str, object]) -> None:
        self.sent.append(frame)


async def test_note_browser_ack_records_without_signal_when_idle() -> None:
    """An ACK with nothing in flight updates state but emits no flow-control."""
    host = _Host(worker_ws=object())
    await host.note_browser_ack("a", 100)
    assert host.sent == []


async def test_congestion_emits_flow_pause_then_recovery_emits_resume() -> None:
    """Crossing the high-water mark pauses the producer; draining below low resumes it."""
    host = _Host(worker_ws=object(), high=100, low=40)
    # Register the browser as ACK-capable, then push past the high-water mark.
    await host.note_browser_ack("a", 0)
    host._flow.on_sent("a", 250)  # inflight 250 > 100
    await host._apply_flow_control()
    assert host.sent[-1] == {"type": "control", "action": "flow_pause", "ts": host.sent[-1]["ts"]}
    # Drain below the low-water mark via a fresh ACK → resume.
    await host.note_browser_ack("a", 230)  # inflight 20 < 40
    assert host.sent[-1]["action"] == "flow_resume"


async def test_signal_worker_flow_noop_without_external_worker() -> None:
    """No external worker_ws (e.g. ushell session) → flow-control is a no-op."""
    host = _Host(worker_ws=None)
    await host._signal_worker_flow(PAUSE)
    assert host.sent == []
