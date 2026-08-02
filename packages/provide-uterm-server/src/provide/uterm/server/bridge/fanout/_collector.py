#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""OutputCollector — adaptive operational output accumulator for one session.

Subscribes to the hub's private raw ``term`` and ``snapshot`` stream for a
given worker, accumulates the output text, and returns when the stream quiesces
or the hard cap is hit. The public EventBus remains a redacted diagnostic
egress and is never used for supervised-operation output.

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
    from provide.uterm.server.bridge.hub import TermHub


class OutputCapture:
    """One explicitly opened EventBus subscription with idempotent cleanup."""

    def __init__(self, hub: TermHub, worker_id: str) -> None:
        self._hub = hub
        self._worker_id = worker_id
        self._watch: Any = None
        self._subscription: Any = None
        self._closed = False

    async def open(self) -> OutputCapture:
        """Subscribe to authorized raw output before notification or input."""
        open_raw = getattr(self._hub, "_watch_authorized_operation_output", None)
        if open_raw is not None:
            self._watch = open_raw(self._worker_id)
        elif self._hub.event_bus is not None:
            # Compatibility for narrow test doubles; real TermHub instances
            # always provide the private authorized stream above.
            self._watch = self._hub.event_bus.watch(self._worker_id, event_types=["term", "snapshot"])
        if self._watch is not None:
            self._subscription = await self._watch.__aenter__()
        return self

    async def collect(
        self,
        *,
        quiesce_ms: int = 500,
        max_ms: int = 10_000,
        started_at: float | None = None,
    ) -> tuple[str, int]:
        """Consume output from the already-open subscription."""
        if self._subscription is None:
            return ("", 0)

        quiesce_s = quiesce_ms / 1000.0
        max_s = max_ms / 1000.0
        term_chunks: list[str] = []
        last_snapshot_screen = ""
        start = started_at if started_at is not None else time.monotonic()

        while True:
            elapsed = time.monotonic() - start
            remaining = max_s - elapsed
            if remaining <= 0:
                break
            try:
                event: dict[str, Any] | None = await asyncio.wait_for(
                    self._subscription.queue.get(), timeout=min(remaining, quiesce_s)
                )
            except TimeoutError:
                break
            if event is None:
                break
            event_type = event.get("type")
            data = event.get("data") or {}
            if event_type == "term":
                text = data.get("data", "") if isinstance(data, dict) else ""
                if text:
                    term_chunks.append(text)
            else:
                screen = data.get("screen", "") if isinstance(data, dict) else ""
                if screen:
                    last_snapshot_screen = screen

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ("".join(term_chunks) if term_chunks else last_snapshot_screen, elapsed_ms)

    async def close(self) -> None:
        """Unsubscribe exactly once; repeated cleanup calls are harmless."""
        if self._closed:
            return
        self._closed = True
        if self._watch is not None:
            await self._watch.__aexit__(None, None, None)


class OutputCollector:
    """Open and collect raw terminal output for a supervised operation.

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

    If *hub* has no configured event infrastructure, returns ``("", 0)``
    immediately.
    """

    async def open(self, hub: TermHub, worker_id: str) -> OutputCapture:
        """Create and open a capture handle for *worker_id*."""
        return await OutputCapture(hub, worker_id).open()

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
            hub: The :class:`~provide.uterm.server.bridge.hub.TermHub` instance.
            worker_id: The worker session to watch.
            quiesce_ms: Milliseconds of silence before returning early.
            max_ms: Hard-cap total collection time in milliseconds.

        Returns:
            A ``(delta_string, elapsed_ms)`` tuple.  *delta_string* contains
            all text received; *elapsed_ms* is the total wall-clock time spent.
        """
        capture = await self.open(hub, worker_id)
        try:
            return await capture.collect(quiesce_ms=quiesce_ms, max_ms=max_ms)
        finally:
            await capture.close()
