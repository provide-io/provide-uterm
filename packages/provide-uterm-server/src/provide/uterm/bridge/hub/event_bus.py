#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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

_DEFAULT_MAX_PATTERN_LENGTH = 512
_DEFAULT_MAX_MATCH_INPUT_CHARS = 8192


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

    def __init__(
        self,
        max_queue_depth: int = 500,
        max_subscribers_per_worker: int = 100,
        *,
        max_pattern_length: int = _DEFAULT_MAX_PATTERN_LENGTH,
        max_match_input_chars: int = _DEFAULT_MAX_MATCH_INPUT_CHARS,
    ) -> None:
        self._max_queue_depth = max(1, int(max_queue_depth))
        self._max_subscribers_per_worker = max(1, int(max_subscribers_per_worker))
        self._max_pattern_length = max(1, int(max_pattern_length))
        self._max_match_input_chars = max(1, int(max_match_input_chars))
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
            if not isinstance(screen, str):
                screen = str(screen)
            screen = screen[: self._max_match_input_chars]
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
        """Put None into *sub*'s queue, dropping oldest if full.

        The sentinel MUST be delivered — a missing sentinel leaves subscribers
        hanging forever.  If normal drop-oldest fails, the queue is cleared.
        """
        try:
            sub.queue.put_nowait(None)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover — race guard
                sub.queue.get_nowait()
            sub.dropped += 1
            try:
                sub.queue.put_nowait(None)
            except asyncio.QueueFull:
                # Clear queue entirely to guarantee sentinel delivery
                while not sub.queue.empty():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        sub.queue.get_nowait()
                sub.queue.put_nowait(None)
                logger.warning("event_bus_sentinel_forced worker_id=%s sub_id=%s", sub.worker_id, sub.sub_id)

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
        current = len(self._subs.get(worker_id, []))
        if current >= self._max_subscribers_per_worker:
            raise RuntimeError(
                f"EventBus: max subscribers ({self._max_subscribers_per_worker}) reached for worker {worker_id!r}"
            )
        compiled = _compile_pattern(pattern, max_pattern_length=self._max_pattern_length)
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


def _compile_pattern(
    pattern: str | None,
    *,
    max_pattern_length: int = _DEFAULT_MAX_PATTERN_LENGTH,
) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    if len(pattern) > max(1, int(max_pattern_length)):
        raise ValueError(f"watch pattern is too long: {len(pattern)} > {max_pattern_length}")
    _validate_pattern_safety(pattern)
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid watch pattern regex: {exc}") from exc


def _validate_pattern_safety(pattern: str) -> None:
    # Each entry on the stack is [has_inner_quantifier, has_alternation].
    # ``has_inner_quantifier`` is True once we see ``+``, ``*``, or ``{n,...}``
    # inside the group. ``has_alternation`` is True once we see an unescaped
    # ``|`` at this group's depth.
    group_stack: list[list[bool]] = []
    previous_kind = ""
    last_closed_group_had_quantifier = False
    last_closed_group_had_alternation = False
    escaped = False
    in_class = False
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if escaped:
            escaped = False
            previous_kind = "literal"
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        if in_class:
            if char == "]":
                in_class = False
                previous_kind = "literal"
            i += 1
            continue
        if char == "[":
            in_class = True
            i += 1
            continue
        if char == "(":
            group_stack.append([False, False])
            previous_kind = ""
            last_closed_group_had_quantifier = False
            last_closed_group_had_alternation = False
            # Skip past any group-prefix sequence like ``(?:``, ``(?=``,
            # ``(?!``, ``(?<=``, ``(?<!``, ``(?P<name>``, etc. The prefix
            # characters themselves are non-content and should not affect the
            # alternation/quantifier tracking below.
            i += 1
            if i < n and pattern[i] == "?":
                # Advance to the end of the group-prefix metadata. Conservative:
                # stop at ':' (non-capturing), '=' / '!' (lookarounds), or
                # past ``(?P<name>``. We stop on the first character that is
                # part of the group body.
                i += 1
                # ``(?<...`` lookbehind: skip the '<' and the '=' or '!' after
                if i < n and pattern[i] == "<" and i + 1 < n and pattern[i + 1] in "=!":
                    i += 2
                # ``(?P<name>``: skip until '>'
                elif i < n and pattern[i] == "P":
                    end = pattern.find(">", i)
                    if end != -1:
                        i = end + 1
                # ``(?:``, ``(?=``, ``(?!``: just skip the single marker char
                elif i < n and pattern[i] in ":=!":
                    i += 1
                # else: unknown / inline flag like ``(?i)``; leave i alone
            continue
        if char == ")" and group_stack:
            frame = group_stack.pop()
            last_closed_group_had_quantifier = frame[0]
            last_closed_group_had_alternation = frame[1]
            # Propagate inner-quantifier / inner-alternation up so that a
            # parent group enclosing a quantified-or-alternated subgroup
            # is itself flagged when later followed by a quantifier
            # (catches e.g. ``(?=(a+))+`` and ``((a|b))+``).
            if group_stack:
                if frame[0]:
                    group_stack[-1][0] = True
                if frame[1]:
                    group_stack[-1][1] = True
            previous_kind = "group"
            i += 1
            continue
        if char == "|":
            if group_stack:
                group_stack[-1][1] = True
            previous_kind = "alternation"
            i += 1
            continue
        if char in "+*" or (char == "{" and _looks_like_counted_quantifier(pattern, i)):
            if previous_kind == "group":
                if last_closed_group_had_quantifier:
                    raise ValueError("unsafe watch pattern: nested quantified groups are not allowed")
                if last_closed_group_had_alternation:
                    raise ValueError("unsafe watch pattern: quantified groups containing alternation are not allowed")
            if group_stack:
                group_stack[-1][0] = True
            previous_kind = "quantifier"
            if char == "{":
                i = pattern.find("}", i) + 1
            else:
                i += 1
            continue
        previous_kind = "literal"
        last_closed_group_had_quantifier = False
        last_closed_group_had_alternation = False
        i += 1


def _looks_like_counted_quantifier(pattern: str, start: int) -> bool:
    end = pattern.find("}", start + 1)
    if end == -1:
        return False
    body = pattern[start + 1 : end]
    if not body:
        return False
    left, sep, right = body.partition(",")
    return left.isdigit() and (not sep or right == "" or right.isdigit())
