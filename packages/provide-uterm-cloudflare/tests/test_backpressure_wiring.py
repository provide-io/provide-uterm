#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Backpressure wiring on SessionRuntime's I/O mixin (Tier A + Tier B).

A minimal host carrying just the mixin plus the attributes its broadcast/flow
methods touch keeps these tests independent of the full Durable Object runtime.
Exercises ACK ingestion (note_browser_ack), the producer pause/resume decision
(_apply_flow_control → _signal_worker_flow), the per-viewer term drop in
broadcast_to_browsers, and the recovery resync (_request_worker_snapshot).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from provide.uterm.cloudflare.do.session_runtime.flow_control import PAUSE, FlowController
from provide.uterm.cloudflare.do.session_runtime.io import _SessionRuntimeIoMixin


class _Host(_SessionRuntimeIoMixin):
    def __init__(self, *, worker_ws: object | None, high: int = 100, low: int = 40) -> None:
        self.worker_ws = worker_ws
        self._flow = FlowController(high_water=high, low_water=low, ack_grace_s=1000.0)
        self.sent: list[dict[str, object]] = []  # control frames sent to the worker
        self.delivered: dict[str, list[dict[str, object]]] = {}  # ws_id -> frames delivered
        self.worker_id = "w"
        self._queue_bytes = 0
        self.max_buffer_bytes = 1_000_000
        self.browser_sockets: dict[str, object] = {}
        self.browser_hijack_owner: dict[str, str] = {}
        self._ushell = None
        self.ctx = SimpleNamespace(getWebSockets=list)  # [] → fall back to browser_sockets

    def ws_key(self, ws: object) -> str:
        return str(id(ws))

    def _socket_role(self, ws: object) -> str:
        return "browser"

    async def send_ws(self, ws: object, frame: dict[str, object]) -> None:
        if ws is self.worker_ws:
            self.sent.append(frame)
        else:
            self.delivered.setdefault(self.ws_key(ws), []).append(frame)


async def test_note_browser_ack_records_without_signal_when_idle() -> None:
    """An ACK with nothing in flight updates state but emits no flow-control."""
    host = _Host(worker_ws=object())
    await host.note_browser_ack("a", 100)
    assert host.sent == []


async def test_congestion_emits_flow_pause_then_recovery_emits_resume_and_resync() -> None:
    """Crossing high-water pauses the producer; draining below low resumes it and
    requests a resync snapshot for the recovered viewer."""
    host = _Host(worker_ws=object(), high=100, low=40)
    await host.note_browser_ack("a", 0)  # register ACK-capable
    host._flow.on_sent("a", 250)  # inflight 250 > 100 → congested
    await host._apply_flow_control()
    assert host.sent[-1] == {"type": "control", "action": "flow_pause", "ts": host.sent[-1]["ts"]}
    # Drain below the low-water mark via a fresh ACK → resume + resync.
    await host.note_browser_ack("a", 230)  # inflight 20 < 40
    actions = [f["action"] for f in host.sent]
    assert "flow_resume" in actions
    assert "snapshot_request" in actions


async def test_signal_worker_flow_noop_without_external_worker() -> None:
    """No external worker_ws (e.g. ushell session) → flow-control is a no-op."""
    host = _Host(worker_ws=None)
    await host._signal_worker_flow(PAUSE)
    assert host.sent == []


async def test_request_worker_snapshot_noop_without_external_worker() -> None:
    """No external worker_ws → the resync snapshot request is a no-op."""
    host = _Host(worker_ws=None)
    await host._request_worker_snapshot()
    assert host.sent == []


async def test_broadcast_drops_term_to_congested_viewer_only() -> None:
    """A congested viewer's term frame is dropped; a keeping-up viewer still gets it."""
    host = _Host(worker_ws=object(), high=100, low=40)
    slow, fast = object(), object()
    host.browser_sockets = {host.ws_key(slow): slow, host.ws_key(fast): fast}
    now = time.monotonic()
    host._flow.on_ack(host.ws_key(slow), 0, now=now)
    host._flow.on_sent(host.ws_key(slow), 250)  # slow congested
    host._flow.on_ack(host.ws_key(fast), 0, now=now)  # fast keeps up
    await host.broadcast_to_browsers({"type": "term", "data": "xxxx"})
    assert host.ws_key(slow) not in host.delivered  # dropped
    assert host.ws_key(fast) in host.delivered  # delivered


async def test_recovery_via_ack_requests_worker_snapshot() -> None:
    """When a congested viewer drains below low-water, the DO pulls a fresh snapshot."""
    host = _Host(worker_ws=object(), high=100, low=40)
    host._flow.on_ack("a", 0, now=time.monotonic())
    host._flow.on_sent("a", 250)  # congested
    await host.note_browser_ack("a", 230)  # inflight 20 < low → recovered
    assert any(f.get("action") == "snapshot_request" for f in host.sent)


class _Ushell:
    """Stub in-DO ushell producer recording the control actions it receives."""

    def __init__(self) -> None:
        self.controls: list[str] = []

    async def handle_control(self, action: str) -> list[dict[str, object]]:
        self.controls.append(action)
        if action == "snapshot_request":
            return [{"type": "snapshot", "screen": "S"}]
        return []


async def test_signal_worker_flow_routes_to_ushell() -> None:
    """An in-DO ushell producer is flow-paused via handle_control, not a worker WS."""
    host = _Host(worker_ws=None)
    ush = _Ushell()
    host._ushell = ush
    await host._signal_worker_flow(PAUSE)
    assert ush.controls == ["flow_pause"]
    assert host.sent == []  # no external worker WS involved


async def test_request_worker_snapshot_routes_to_ushell_and_broadcasts() -> None:
    """ushell resync: snapshot_request returns frames inline → broadcast to browsers."""
    host = _Host(worker_ws=None)
    ush = _Ushell()
    host._ushell = ush
    viewer = object()
    host.browser_sockets = {host.ws_key(viewer): viewer}
    await host._request_worker_snapshot()
    assert ush.controls == ["snapshot_request"]
    delivered = host.delivered.get(host.ws_key(viewer), [])
    assert any(f.get("type") == "snapshot" for f in delivered)
