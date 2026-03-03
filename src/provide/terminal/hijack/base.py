#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Hijack/watchdog mixin for worker bots.

No optional dependencies required.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class HijackableMixin:
    """Mixin that makes an async worker class hijackable by a human operator.

    Adds pause/resume/step/watchdog primitives. Intended usage — add as a base
    class to your worker/bot class, then call :meth:`await_if_hijacked` at
    checkpoints in your automation loop::

        class MyBot(HijackableMixin):
            async def run_loop(self) -> None:
                while True:
                    await self.await_if_hijacked()  # pauses here when hijacked
                    await self.do_action()

    The hub or manager calls :meth:`set_hijacked` to pause/resume.
    The dashboard calls :meth:`request_step` to allow one loop iteration while paused.
    """

    def __init__(self) -> None:
        self._hijacked: bool = False
        self._hijack_event: asyncio.Event = asyncio.Event()
        self._hijack_event.set()  # not hijacked by default → event is set → no blocking
        # Step tokens: allow N checkpoint passes while hijacked without blocking.
        # Two tokens per "Step" = one loop iteration (plan + act).
        self._hijack_step_tokens: int = 0
        # Watchdog
        self._watchdog_task: asyncio.Task[None] | None = None
        self._last_progress_mono: float = time.monotonic()

    # ------------------------------------------------------------------
    # Core checkpoints
    # ------------------------------------------------------------------

    async def await_if_hijacked(self) -> None:
        """Block automation while a human is hijacking this bot.

        Call this at every checkpoint in the automation loop (e.g. top-of-loop
        and pre-action). Returns immediately when not hijacked; blocks until
        :meth:`set_hijacked` is called with ``False`` otherwise, unless step
        tokens are available.
        """
        if not self._hijacked:
            return
        if self._hijack_step_tokens > 0:
            self._hijack_step_tokens -= 1
            return
        await self._hijack_event.wait()

    async def set_hijacked(self, enabled: bool) -> None:
        """Pause (``True``) or resume (``False``) automation.

        Idempotent — safe to call multiple times with the same value.
        """
        if enabled == self._hijacked:
            return
        self._hijacked = enabled
        if enabled:
            self._hijack_step_tokens = 0
            self._hijack_event.clear()
        else:
            self._hijack_event.set()

    async def request_step(self, checkpoints: int = 2) -> None:
        """Allow automation to pass *checkpoints* hijack gates while still hijacked.

        The default (2) lets one full loop iteration run (plan + act).
        Capped at 100 to prevent unbounded accumulation from a misbehaving client.

        No-op when not currently hijacked.
        """
        if not self._hijacked:
            return
        self._hijack_step_tokens = min(self._hijack_step_tokens + max(0, int(checkpoints)), 100)

    # ------------------------------------------------------------------
    # Progress tracking (used by watchdog)
    # ------------------------------------------------------------------

    def note_progress(self) -> None:
        """Signal that the bot is making progress (resets the watchdog timer).

        Call this whenever meaningful work occurs (e.g. after each turn, screen change,
        or successful action) to prevent the watchdog from firing.
        """
        self._last_progress_mono = time.monotonic()

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def start_watchdog(
        self,
        *,
        stuck_timeout_s: float = 120.0,
        check_interval_s: float = 5.0,
        on_stuck: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Start a background task that triggers *on_stuck* if the bot stops progressing.

        The watchdog fires when no call to :meth:`note_progress` has been seen for
        *stuck_timeout_s* seconds. While hijacked, the timer is suppressed.

        Args:
            stuck_timeout_s: Seconds without progress before firing.
            check_interval_s: How often to check (default 5 s).
            on_stuck: Async callback called when stuck. Typical use: disconnect the
                      session so the outer reconnect loop triggers.
        """
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(max(0.5, float(check_interval_s)))
                try:
                    if self._hijacked:
                        self.note_progress()
                        continue
                    idle_for = time.monotonic() - self._last_progress_mono
                    if idle_for < float(stuck_timeout_s):
                        continue
                    if on_stuck is not None:
                        try:
                            await on_stuck()
                        except Exception as exc:  # pragma: no cover
                            logger.warning("watchdog on_stuck callback raised: %s", exc, exc_info=True)
                    # Reset so we don't spam if reconnect is slow
                    self.note_progress()
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception:  # pragma: no cover
                    logger.warning("watchdog loop error", exc_info=True)
                    continue

        self._watchdog_task = asyncio.create_task(_loop())

    async def stop_watchdog(self) -> None:
        """Cancel the watchdog task (idempotent)."""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None

    async def cleanup_hijack(self) -> None:
        """Release hijack and stop watchdog. Call from your bot's cleanup/shutdown."""
        await self.set_hijacked(False)
        await self.stop_watchdog()
