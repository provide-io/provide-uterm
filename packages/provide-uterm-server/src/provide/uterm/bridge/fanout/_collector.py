#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""OutputCollector — adaptive EventBus output accumulator for a single session.

Subscribes to EventBus ``term`` and ``snapshot`` events for a given worker,
accumulates the output text, and returns when the stream quiesces or the hard
cap is hit.

Primary source: ``term`` event deltas (PTY / raw-output connectors).
Fallback source: last ``snapshot.screen`` value, used when no ``term`` events
arrive (e.g. the shell and SSH control connectors that are snapshot-only).

Usage::

    collector = OutputCollector()
    delta, elapsed_ms = await collector.collect(hub, "worker-1", quiesce_ms=500)
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provide.uterm.bridge.hub import TermHub


class OutputCollector:
    """Collect terminal output from a single session via the EventBus.

    Subscribes to both ``term`` and ``snapshot`` events, returning when:

    * no new output has arrived for *quiesce_ms* milliseconds (adaptive), or
    * the total elapsed time reaches *max_ms* (hard cap), or
    * the worker disconnects (``None`` sentinel from the bus).

    **Output selection** — ``term`` events (PTY deltas) take priority.  If any
    ``term`` events arrive, their ``data.data`` text is concatenated and
    returned.  When *only* ``snapshot`` events arrive (e.g. the shell connector
    and SSH control operations), the last snapshot's ``data.screen`` text is
    returned instead so callers always get meaningful output regardless of
    connector type.

    If *hub* has no EventBus attached, returns ``("", 0)`` immediately.
    """

    async def collect(
        self,
        hub: TermHub,
        worker_id: str,
        *,
        quiesce_ms: int = 500,
        max_ms: int = 10_000,
    ) -> tuple[str, int]:
        """Subscribe to EventBus, accumulate term event output.

        Args:
            hub: The :class:`~provide.uterm.bridge.hub.TermHub` instance.
            worker_id: The worker session to watch.
            quiesce_ms: Milliseconds of silence before returning early.
            max_ms: Hard-cap total collection time in milliseconds.

        Returns:
            A ``(delta_string, elapsed_ms)`` tuple.  *delta_string* contains
            all text received; *elapsed_ms* is the total wall-clock time spent.
        """
        if hub.event_bus is None:
            return ("", 0)

        quiesce_s = quiesce_ms / 1000.0
        max_s = max_ms / 1000.0
        term_chunks: list[str] = []
        last_snapshot_screen: str = ""
        start = time.monotonic()

        async with hub.event_bus.watch(worker_id, event_types=["term", "snapshot"]) as sub:
            while True:
                elapsed = time.monotonic() - start
                remaining = max_s - elapsed
                if remaining <= 0:
                    break
                # Wait at most quiesce_s for the next event
                timeout = min(remaining, quiesce_s)
                try:
                    event: dict[str, Any] | None = await asyncio.wait_for(sub.queue.get(), timeout=timeout)
                except TimeoutError:
                    # No new output — stream quiesced
                    break
                if event is None:
                    # Worker disconnected sentinel
                    break
                event_type = event.get("type")
                data = event.get("data") or {}
                # The EventBus filter at line 79 restricts to {"term", "snapshot"},
                # so the else branch is the snapshot path — no further type check
                # needed (and coverage would treat ``elif event_type == "snapshot"``
                # as a partially-covered branch because the False path is
                # unreachable given the filter).
                if event_type == "term":
                    text = data.get("data", "") if isinstance(data, dict) else ""
                    if text:
                        term_chunks.append(text)
                else:
                    # Track last snapshot screen as fallback for snapshot-only connectors.
                    screen = data.get("screen", "") if isinstance(data, dict) else ""
                    if screen:
                        last_snapshot_screen = screen

        elapsed_ms = int((time.monotonic() - start) * 1000)
        # Use term deltas when present; fall back to last snapshot screen for
        # connectors (e.g. shell, SSH control) that never emit term events.
        output = "".join(term_chunks) if term_chunks else last_snapshot_screen
        return (output, elapsed_ms)
