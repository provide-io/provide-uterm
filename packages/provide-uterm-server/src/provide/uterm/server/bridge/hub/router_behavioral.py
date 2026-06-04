#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Behavioral-heuristics / keystroke-timing helpers for the message router.

Extracted from :mod:`provide.uterm.server.bridge.hub.router_impl`. These
functions take the :class:`MessageRouter` as their first parameter and
operate on its per-browser keystroke ring buffers and the audit gates
configured on the composing hub. :class:`MessageRouter` keeps thin
wrapper methods (``record_keystroke`` / ``get_heuristics`` /
``forget_browser`` / ``run_behavioral_audit_loop`` /
``audit_all_browsers``) that forward here, so the public method surface
is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from provide.uterm.server.bridge.hub.router_impl import MessageRouter

logger = get_logger(__name__)


def record_keystroke(router: MessageRouter, source: Any) -> None:
    """Record the timing of a keystroke from a browser."""
    timestamps = router.keystroke_timestamps
    if source not in timestamps:
        timestamps[source] = deque(maxlen=50)
    timestamps[source].append(time.monotonic())


def get_heuristics(router: MessageRouter, source: Any) -> dict[str, float]:
    """Return behavioral metrics for the given browser."""
    timestamps = router.keystroke_timestamps.get(source)
    if not timestamps or len(timestamps) < 2:
        return {"cps": 0.0, "jitter": 0.0}

    duration = timestamps[-1] - timestamps[0]
    cps = (len(timestamps) - 1) / duration if duration > 0 else 0.0
    intervals = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
    jitter = statistics.variance(intervals) if len(intervals) > 1 else 0.0
    return {"cps": cps, "jitter": jitter}


def forget_browser(router: MessageRouter, ws: Any) -> None:
    """Drop heuristic state for a disconnected browser."""
    router.keystroke_timestamps.pop(ws, None)


async def run_behavioral_audit_loop(router: MessageRouter) -> None:
    """Periodically audit active connections for behavioral anomalies.

    ``router._hub._audit_all_browsers`` is invoked rather than the local
    function so existing tests that patch ``hub._audit_all_browsers``
    (e.g. to raise an exception and exercise the exception logger)
    keep intercepting. The hub-level shim forwards to
    :func:`audit_all_browsers` here, so the cycle terminates on the
    second hop.
    """
    hub = router._hub
    while True:
        await asyncio.sleep(hub._behavioral_audit_interval_s)
        try:
            await hub._audit_all_browsers()
        except Exception:
            logger.exception("behavioral_audit_loop_error")


async def audit_all_browsers(router: MessageRouter) -> None:
    """Iterate all active browsers and evaluate behavioral heuristics."""
    from fastapi import status

    from provide.uterm.server.bridge.hub.ext import ConnectionHeuristics

    hub = router._hub
    async with hub._lock:
        all_browsers = [(worker_id, ws) for worker_id, st in hub.registry.items() for ws in st.browsers]

    for worker_id, ws in all_browsers:
        heuristics_data = router.get_heuristics(ws)
        heuristics = ConnectionHeuristics(
            cps=heuristics_data["cps"],
            jitter=heuristics_data["jitter"],
            timestamp=time.time(),
        )
        context = await hub.prepare_policy_context(ws, worker_id, action="behavioral_audit")
        # _behavioral_audit_gate is Any | None; the guard above already
        # exited the loop when it's None (see run_behavioral_audit_loop),
        # but the narrow doesn't survive across awaits.
        assert hub._behavioral_audit_gate is not None
        decision = await hub._behavioral_audit_gate.audit_connection(heuristics, context, hub._behavioral_thresholds)
        if decision.action == "deny":
            logger.warning(
                "behavioral_audit_denied worker_id=%s reason=%s",
                worker_id,
                decision.reason or "anomaly detected",
            )
            with contextlib.suppress(Exception):
                await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason=decision.reason or "Behavioral anomaly")


__all__ = [
    "audit_all_browsers",
    "forget_browser",
    "get_heuristics",
    "record_keystroke",
    "run_behavioral_audit_loop",
]
