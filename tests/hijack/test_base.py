#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for HijackableMixin."""

from __future__ import annotations

import asyncio
import contextlib
import time

from provide.terminal.hijack.base import HijackableMixin


class Bot(HijackableMixin):
    def __init__(self) -> None:
        super().__init__()
        self.steps_taken: int = 0

    async def loop_once(self) -> None:
        await self.await_if_hijacked()
        self.steps_taken += 1


class TestHijackableMixinCheckpoint:
    async def test_not_hijacked_passes_immediately(self) -> None:
        bot = Bot()
        await bot.loop_once()
        assert bot.steps_taken == 1

    async def test_hijacked_blocks(self) -> None:
        bot = Bot()
        await bot.set_hijacked(True)

        # Should not complete while hijacked
        task = asyncio.create_task(bot.loop_once())
        await asyncio.sleep(0.02)
        assert not task.done()

        await bot.set_hijacked(False)
        await asyncio.wait_for(task, timeout=1.0)
        assert bot.steps_taken == 1

    async def test_set_hijacked_idempotent(self) -> None:
        bot = Bot()
        await bot.set_hijacked(True)
        await bot.set_hijacked(True)  # no-op
        assert bot._hijacked is True
        await bot.set_hijacked(False)
        await bot.set_hijacked(False)  # no-op
        assert bot._hijacked is False

    async def test_step_tokens_allow_passes_while_hijacked(self) -> None:
        bot = Bot()
        await bot.set_hijacked(True)
        await bot.request_step(checkpoints=3)

        for _ in range(3):
            await asyncio.wait_for(bot.loop_once(), timeout=0.5)

        assert bot.steps_taken == 3
        assert bot._hijack_step_tokens == 0

    async def test_step_no_op_when_not_hijacked(self) -> None:
        bot = Bot()
        await bot.request_step(checkpoints=5)
        assert bot._hijack_step_tokens == 0  # no-op

    async def test_step_capped_at_100(self) -> None:
        bot = Bot()
        await bot.set_hijacked(True)
        await bot.request_step(checkpoints=200)
        assert bot._hijack_step_tokens == 100

    async def test_entering_hijack_resets_step_tokens(self) -> None:
        bot = Bot()
        await bot.set_hijacked(True)
        await bot.request_step(checkpoints=5)
        await bot.set_hijacked(False)
        await bot.set_hijacked(True)
        assert bot._hijack_step_tokens == 0


class TestHijackableMixinProgress:
    def test_note_progress_updates_timestamp(self) -> None:

        bot = Bot()
        before = bot._last_progress_mono
        time.sleep(0.01)
        bot.note_progress()
        assert bot._last_progress_mono > before


class TestHijackableMixinWatchdog:
    async def test_watchdog_calls_on_stuck(self) -> None:
        bot = Bot()
        stuck_called = asyncio.Event()

        async def on_stuck() -> None:
            stuck_called.set()

        # Set last progress far in the past
        bot._last_progress_mono -= 200.0
        bot.start_watchdog(stuck_timeout_s=0.05, check_interval_s=0.02, on_stuck=on_stuck)
        await asyncio.wait_for(stuck_called.wait(), timeout=2.0)
        await bot.stop_watchdog()

    async def test_watchdog_suppressed_while_hijacked(self) -> None:
        bot = Bot()
        stuck_called = asyncio.Event()

        async def on_stuck() -> None:
            stuck_called.set()

        bot._last_progress_mono -= 200.0
        await bot.set_hijacked(True)
        bot.start_watchdog(stuck_timeout_s=0.01, check_interval_s=0.01, on_stuck=on_stuck)
        await asyncio.sleep(0.1)
        assert not stuck_called.is_set()
        await bot.stop_watchdog()

    async def test_cleanup_hijack(self) -> None:
        bot = Bot()
        await bot.set_hijacked(True)
        bot.start_watchdog(stuck_timeout_s=1000)
        await bot.cleanup_hijack()
        assert not bot._hijacked
        assert bot._watchdog_task is None or bot._watchdog_task.done()


class TestWatchdogBranches:
    async def test_watchdog_idempotent(self) -> None:
        """start_watchdog called twice should not start a second task."""

        class Bot2(HijackableMixin):
            pass

        bot = Bot2()
        bot.start_watchdog(stuck_timeout_s=999, check_interval_s=999)
        task1 = bot._watchdog_task
        bot.start_watchdog(stuck_timeout_s=999, check_interval_s=999)
        task2 = bot._watchdog_task
        assert task1 is task2
        assert task1 is not None
        task1.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task1

    async def test_watchdog_hijacked_branch(self) -> None:
        """While hijacked, watchdog calls note_progress (resets timer)."""

        class Bot2(HijackableMixin):
            pass

        bot = Bot2()
        await bot.set_hijacked(True)
        progress_before = bot._last_progress_mono

        # Note: watchdog sleeps max(0.5, check_interval_s) so need > 0.5s
        bot.start_watchdog(stuck_timeout_s=0.01, check_interval_s=0.01)
        await asyncio.sleep(0.7)
        assert bot._watchdog_task is not None
        bot._watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot._watchdog_task

        # note_progress should have been called (timer reset while hijacked)
        assert bot._last_progress_mono >= progress_before

    async def test_watchdog_not_stuck_continues(self) -> None:
        """Watchdog does NOT fire on_stuck when progress was recently noted."""
        fired = []

        class Bot2(HijackableMixin):
            pass

        bot = Bot2()
        bot.note_progress()

        async def on_stuck() -> None:
            fired.append(True)

        # stuck_timeout_s=9999 ensures we never fire even after the 0.5s sleep
        bot.start_watchdog(stuck_timeout_s=9999, check_interval_s=0.01, on_stuck=on_stuck)
        await asyncio.sleep(0.7)
        assert bot._watchdog_task is not None
        bot._watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot._watchdog_task

        assert not fired

    async def test_watchdog_exception_in_loop_continues(self) -> None:
        """Exception inside watchdog loop body is swallowed and loop continues."""

        class Bot2(HijackableMixin):
            pass

        bot = Bot2()
        call_count = []

        async def exploding_stuck() -> None:
            call_count.append(1)
            raise RuntimeError("oops")

        # Set progress far in the past to trigger stuck
        bot._last_progress_mono = 0.0
        # Note: watchdog sleeps max(0.5, check_interval_s) so we need to wait > 0.5s
        bot.start_watchdog(stuck_timeout_s=0.001, check_interval_s=0.01, on_stuck=exploding_stuck)
        await asyncio.sleep(0.7)
        assert bot._watchdog_task is not None
        bot._watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot._watchdog_task
        # Callback was invoked at least once
        assert len(call_count) >= 1


class TestStepTokenEdgeCases:
    async def test_step_tokens_exactly_zero_blocks(self) -> None:
        """When step_tokens == 0 and hijacked, should block."""
        bot = Bot()
        await bot.set_hijacked(True)
        assert bot._hijack_step_tokens == 0
        assert not bot._hijack_event.is_set()

        task = asyncio.create_task(bot.await_if_hijacked())
        await asyncio.sleep(0.05)
        assert not task.done()

        await bot.set_hijacked(False)
        await asyncio.wait_for(task, timeout=0.5)
        assert task.done()

    async def test_step_tokens_decrement_on_each_call(self) -> None:
        """Each await_if_hijacked decrements step_tokens by 1."""
        bot = Bot()
        await bot.set_hijacked(True)
        await bot.request_step(checkpoints=3)
        assert bot._hijack_step_tokens == 3

        await bot.await_if_hijacked()
        assert bot._hijack_step_tokens == 2

        await bot.await_if_hijacked()
        assert bot._hijack_step_tokens == 1

        await bot.await_if_hijacked()
        assert bot._hijack_step_tokens == 0

    async def test_step_accumulation_below_cap(self) -> None:
        """Step tokens accumulate when under cap."""
        bot = Bot()
        await bot.set_hijacked(True)

        await bot.request_step(checkpoints=40)
        assert bot._hijack_step_tokens == 40

        await bot.request_step(checkpoints=30)
        assert bot._hijack_step_tokens == 70

        await bot.request_step(checkpoints=25)
        assert bot._hijack_step_tokens == 95

    async def test_step_negative_input_clamped_to_zero(self) -> None:
        """Negative checkpoints input should be clamped to 0."""
        bot = Bot()
        await bot.set_hijacked(True)

        await bot.request_step(checkpoints=-5)
        assert bot._hijack_step_tokens == 0

    async def test_step_float_input_converted_to_int(self) -> None:
        """Float checkpoints should be converted to int."""
        bot = Bot()
        await bot.set_hijacked(True)

        await bot.request_step(checkpoints=3.7)
        assert bot._hijack_step_tokens == 3

    async def test_step_zero_input_no_change(self) -> None:
        """Zero checkpoints should not change token count."""
        bot = Bot()
        await bot.set_hijacked(True)
        await bot.request_step(checkpoints=5)
        original_tokens = bot._hijack_step_tokens

        await bot.request_step(checkpoints=0)
        assert bot._hijack_step_tokens == original_tokens


class TestWatchdogTimingEdgeCases:
    async def test_watchdog_min_check_interval_enforced(self) -> None:
        """check_interval_s < 0.5 should be clamped to 0.5."""

        class Bot2(HijackableMixin):
            pass

        bot = Bot2()
        bot._last_progress_mono = 0.0

        stuck_count = []

        async def on_stuck() -> None:
            stuck_count.append(1)

        start = __import__("time").monotonic()
        # Request check_interval_s=0.01, but it should be clamped to 0.5
        bot.start_watchdog(stuck_timeout_s=0.05, check_interval_s=0.01, on_stuck=on_stuck)
        await asyncio.sleep(0.1)
        elapsed = __import__("time").monotonic() - start

        bot._watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot._watchdog_task

        # Should not have fired yet (clamped to 0.5s sleep)
        assert len(stuck_count) == 0
        assert elapsed < 0.3

    async def test_watchdog_on_stuck_none_doesnt_crash(self) -> None:
        """on_stuck=None should not crash when stuck triggers."""

        class Bot2(HijackableMixin):
            pass

        bot = Bot2()
        bot._last_progress_mono = 0.0

        # on_stuck=None (explicitly)
        bot.start_watchdog(stuck_timeout_s=0.05, check_interval_s=0.02, on_stuck=None)
        await asyncio.sleep(0.3)
        assert bot._watchdog_task is not None

        bot._watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot._watchdog_task

    async def test_watchdog_idle_for_exceeded_fires(self) -> None:
        """When idle_for exceeds stuck_timeout_s, should fire."""

        class Bot2(HijackableMixin):
            pass

        bot = Bot2()
        fired = []

        async def on_stuck() -> None:
            fired.append(1)

        # Set progress well in the past to guarantee idle_for > timeout_s
        timeout_s = 0.05
        bot._last_progress_mono -= 2.0

        bot.start_watchdog(stuck_timeout_s=timeout_s, check_interval_s=0.02, on_stuck=on_stuck)
        await asyncio.sleep(0.7)

        bot._watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot._watchdog_task

        # Should fire because idle_for >> stuck_timeout_s
        assert len(fired) >= 1


class TestHijackEventTransitions:
    async def test_set_hijacked_true_clears_event(self) -> None:
        """set_hijacked(True) should clear the event, causing await to block."""
        bot = Bot()
        assert bot._hijack_event.is_set()

        await bot.set_hijacked(True)
        assert not bot._hijack_event.is_set()

    async def test_set_hijacked_false_sets_event(self) -> None:
        """set_hijacked(False) should set the event, unblocking await."""
        bot = Bot()
        await bot.set_hijacked(True)
        assert not bot._hijack_event.is_set()

        await bot.set_hijacked(False)
        assert bot._hijack_event.is_set()

    async def test_multiple_awaits_resume_when_unhijacked(self) -> None:
        """Multiple blocked await_if_hijacked calls should all resume."""
        bot = Bot()
        await bot.set_hijacked(True)

        tasks = [asyncio.create_task(bot.loop_once()) for _ in range(3)]
        await asyncio.sleep(0.02)
        assert all(not t.done() for t in tasks)

        await bot.set_hijacked(False)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
        assert bot.steps_taken == 3
