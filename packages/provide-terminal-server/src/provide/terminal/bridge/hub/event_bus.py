#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""EventBus: real-time event fanout for TermHub.

Pure asyncio, zero framework dependencies.  Wired into :meth:`TermHub.append_event`
to deliver events to subscribers without blocking the broadcast hot path.

Usage::

    bus = EventBus()
    hub = TermHub(..., event_bus=bus)

    async with bus.watch("worker-1", event_types=["snapshot"]) as sub:
        deadline = asyncio.get_event_loop().time() + 10
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(sub.queue.get(), timeout=remaining)
            except TimeoutError:
                break
            if event is None:   # worker disconnected sentinel
                break
            process(event)
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from provide.telemetry import get_logger

logger = get_logger(__name__)


@dataclass
class _Subscription:
    sub_id: str
    worker_id: str
    queue: asyncio.Queue[dict[str, Any] | None]  # None = worker-disconnected sentinel
    event_types: frozenset[str] | None  # None = accept all types
    pattern: re.Pattern[str] | None  # None = no text filter
    dropped: int = field(default=0)


class EventBus:
    """Fanout layer for TermHub events.

    Subscribers open a context via :meth:`watch`; the hub calls :meth:`_enqueue`
    from inside :meth:`~TermHub.append_event` (outside the hub lock) to deliver
    events synchronously via ``put_nowait``.  When a worker disconnects, the hub
    calls :meth:`close_worker` to push a ``None`` sentinel and release all
    subscriber queues for that worker.

    Args:
        max_queue_depth: Maximum events buffered per subscriber before the oldest
            is dropped to make room for new ones.  Higher values reduce drops for
            slow consumers at the cost of more memory.
    """

    def __init__(self, max_queue_depth: int = 500) -> None:
        self._max_queue_depth = max(1, int(max_queue_depth))
        # worker_id -> list of active subscriptions
        self._subs: dict[str, list[_Subscription]] = {}

    # ------------------------------------------------------------------
    # Hot path — called outside hub lock, must never block
    # ------------------------------------------------------------------

    def _enqueue(self, worker_id: str, event: dict[str, Any]) -> None:
        """Deliver *event* to all subscribers for *worker_id*.

        Called synchronously from :meth:`TermHub.append_event` after the hub
        lock is released.  Uses ``put_nowait`` exclusively — never blocks.
        Any internal error is caught and logged so it never propagates into
        the append_event call site.
        """
        try:
            targets = list(self._subs.get(worker_id, []))
            for sub in targets:
                self._deliver(sub, worker_id, event)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("event_bus_enqueue_error worker_id=%s error=%s", worker_id, exc)

    def _deliver(self, sub: _Subscription, worker_id: str, event: dict[str, Any]) -> None:
        """Filter and enqueue *event* to a single subscription."""
        if sub.event_types is not None and event.get("type") not in sub.event_types:
            return
        if sub.pattern is not None:
            screen = event.get("data", {}).get("screen", "")
            if not sub.pattern.search(screen):
                return
        item: dict[str, Any] = {"worker_id": worker_id, **event}
        try:
            sub.queue.put_nowait(item)
        except asyncio.QueueFull:
            # Ring-buffer semantics: drop oldest, enqueue new.
            with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover — race guard
                sub.queue.get_nowait()
            sub.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(item)

    # ------------------------------------------------------------------
    # Worker disconnect — called when a worker WS is torn down
    # ------------------------------------------------------------------

    def close_worker(self, worker_id: str) -> None:
        """Signal end-of-stream to all subscribers for *worker_id*.

        Puts a ``None`` sentinel into every active subscription queue and
        removes the worker's subscription list.  After this call, new
        subscriptions for *worker_id* are still accepted (for the next
        worker connection).
        """
        subs = self._subs.pop(worker_id, [])
        for sub in subs:
            self._put_sentinel(sub)

    def _put_sentinel(self, sub: _Subscription) -> None:
        """Put None into *sub*'s queue, dropping oldest if full."""
        try:
            sub.queue.put_nowait(None)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover — race guard
                sub.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def watch(
        self,
        worker_id: str,
        *,
        event_types: list[str] | None = None,
        pattern: str | None = None,
    ) -> AsyncIterator[_Subscription]:
        """Context manager that yields a :class:`_Subscription` for *worker_id*.

        The subscription is registered on enter and automatically removed on
        exit — even if the caller raises or is cancelled.

        Args:
            worker_id: Worker session to subscribe to.
            event_types: If given, only events whose ``"type"`` is in this list
                are delivered.  Pass ``None`` to receive all event types.
            pattern: If given, only ``snapshot`` events whose ``data.screen``
                matches this regex are delivered.  Pass ``None`` to skip text
                filtering.

        Yields:
            A :class:`_Subscription` whose ``queue`` the caller drains with
            ``await asyncio.wait_for(sub.queue.get(), timeout=...)``.
            A ``None`` item signals worker disconnect.
        """
        compiled = _compile_pattern(pattern)
        sub = _Subscription(
            sub_id=uuid.uuid4().hex,
            worker_id=worker_id,
            queue=asyncio.Queue(maxsize=self._max_queue_depth),
            event_types=frozenset(event_types) if event_types is not None else None,
            pattern=compiled,
        )
        self._subs.setdefault(worker_id, []).append(sub)
        try:
            yield sub
        finally:
            self._remove(sub)

    def _remove(self, sub: _Subscription) -> None:
        """Remove *sub* from the registry (idempotent)."""
        worker_subs = self._subs.get(sub.worker_id)
        if worker_subs is None:
            return
        remaining = [s for s in worker_subs if s.sub_id != sub.sub_id]
        if remaining:
            self._subs[sub.worker_id] = remaining
        else:
            self._subs.pop(sub.worker_id, None)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def subscriber_count(self, worker_id: str) -> int:
        """Return the number of active subscriptions for *worker_id*."""
        return len(self._subs.get(worker_id, []))


def _compile_pattern(pattern: str | None) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid watch pattern regex: {exc}") from exc
