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

DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024


def _newest_utf8_suffix(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


class OutputCapture:
    """One explicitly opened EventBus subscription with idempotent cleanup."""

    def __init__(self, hub: TermHub, worker_id: str, *, max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> None:
        self._hub = hub
        self._worker_id = worker_id
        self._watch: Any = None
        self._subscription: Any = None
        self._closed = False
        self._max_output_bytes = max(1, int(max_output_bytes))
        #: True when the last ``collect`` ended on ``max_ms`` rather than on
        #: quiet. Read by the controller, which reports such a member as not
        #: ok: a response cut off at the budget is not a complete one, and
        #: returning it as ok makes truncation indistinguishable from a member
        #: that simply finished quickly.
        self.deadline_exceeded = False

    @property
    def queued_bytes(self) -> int:
        """UTF-8 JSON bytes waiting in this capture's private subscription."""
        return int(getattr(self._subscription, "queued_bytes", 0))

    async def open(self) -> OutputCapture:
        """Subscribe to authorized raw output before notification or input."""
        open_raw = getattr(self._hub, "_watch_authorized_operation_output", None)
        if open_raw is not None:
            self._watch = open_raw(self._worker_id, max_queue_bytes=self._max_output_bytes)
        elif self._hub.event_bus is not None:
            # Compatibility for narrow test doubles; real TermHub instances
            # always provide the private authorized stream above.
            self._watch = self._hub.event_bus.watch(
                self._worker_id,
                event_types=["term", "snapshot"],
                max_queue_bytes=self._max_output_bytes,
            )
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
        self.deadline_exceeded = False
        if self._subscription is None:
            return ("", 0)

        quiesce_s = quiesce_ms / 1000.0
        max_s = max_ms / 1000.0
        term_output = ""
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
                # Quiet, or the cap cut a quiesce window short. Which of the two
                # it was cannot be told from here, and does not need to be: what
                # is still queued at exit answers it.
                break
            if event is None:
                break
            event_type = event.get("type")
            data = event.get("data") or {}
            if event_type == "term":
                text = data.get("data", "") if isinstance(data, dict) else ""
                if isinstance(text, str) and text:
                    term_output = _newest_utf8_suffix(term_output + text, self._max_output_bytes)
            else:
                screen = data.get("screen", "") if isinstance(data, dict) else ""
                if isinstance(screen, str) and screen:
                    last_snapshot_screen = _newest_utf8_suffix(screen, self._max_output_bytes)

        # Truncated means we stopped with more still queued -- the member had
        # not finished talking. Deriving it from what is LEFT, rather than from
        # WHICH exit fired, is the rule go's suite settled on and the one C# and
        # typescript carry. Python kept the exit-based version a while longer
        # and it flaked on a loaded runner exactly as predicted: the loop-top
        # exit needs the LAST queue.get() to succeed, so a producer that misses
        # its 1ms quiesce window ends the collect through the timeout with the
        # truncation unrecorded. It also keeps a group whose quiesce_ms exceeds
        # its max_ms correct, since a member that answered and went quiet leaves
        # nothing pending.
        self.deadline_exceeded = self._subscription.queue.qsize() > 0

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return (term_output if term_output else last_snapshot_screen, elapsed_ms)

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

    async def open(
        self,
        hub: TermHub,
        worker_id: str,
        *,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> OutputCapture:
        """Create and open a capture handle for *worker_id*."""
        return await OutputCapture(hub, worker_id, max_output_bytes=max_output_bytes).open()

    async def collect(
        self,
        hub: TermHub,
        worker_id: str,
        *,
        quiesce_ms: int = 500,
        max_ms: int = 10_000,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> tuple[str, int]:
        """Subscribe to EventBus, accumulate term event output.

        Args:
            hub: The :class:`~provide.uterm.server.bridge.hub.TermHub` instance.
            worker_id: The worker session to watch.
            quiesce_ms: Milliseconds of silence before returning early.
            max_ms: Hard-cap total collection time in milliseconds.
            max_output_bytes: Maximum UTF-8 bytes retained in the private
                subscription and returned output. Newest text is preserved.

        Returns:
            A ``(delta_string, elapsed_ms)`` tuple.  *delta_string* contains
            all text received; *elapsed_ms* is the total wall-clock time spent.
        """
        capture = await self.open(hub, worker_id, max_output_bytes=max_output_bytes)
        try:
            return await capture.collect(quiesce_ms=quiesce_ms, max_ms=max_ms)
        finally:
            await capture.close()
