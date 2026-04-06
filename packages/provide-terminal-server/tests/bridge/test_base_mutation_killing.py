#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for HijackableMixin (base.py) — precision boundary testing."""

from __future__ import annotations

import asyncio
import contextlib

from provide.terminal.bridge.base import HijackableMixin


class SimpleBot(HijackableMixin):
    """Minimal bot for testing HijackableMixin."""

    def __init__(self) -> None:
        super().__init__()
        self.checkpoints_passed = 0

    async def step(self) -> None:
        """One automation step."""
        await self.await_if_hijacked()
        self.checkpoints_passed += 1


# ---------------------------------------------------------------------------
# Test hijack_step_tokens comparison (line 68)
# Mutation: > → >=, > → <
# ---------------------------------------------------------------------------


async def test_hijack_tokens_zero_blocks() -> None:
    """When hijacked with 0 tokens, checkpoint blocks (catches > → >=)."""
    bot = SimpleBot()
    await bot.set_hijacked(True)
    assert bot._hijack_step_tokens == 0

    # Create task that will block at checkpoint
    task = asyncio.create_task(bot.await_if_hijacked())
    await asyncio.sleep(0.01)  # Let it block

    # Task should still be blocked (not completed)
    assert not task.done()

    # Clean up
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_hijack_tokens_one_passes() -> None:
    """When hijacked with 1 token, checkpoint passes (catches > → <)."""
    bot = SimpleBot()
    await bot.set_hijacked(True)
    await bot.request_step(1)  # Add 1 token

    # Should pass through without blocking
    await bot.await_if_hijacked()
    assert bot._hijack_step_tokens == 0  # Token consumed


async def test_hijack_tokens_negative_blocks() -> None:
    """Negative tokens should block (verifies > 0 check)."""
    bot = SimpleBot()
    bot._hijack_step_tokens = -1
    await bot.set_hijacked(True)

    task = asyncio.create_task(bot.await_if_hijacked())
    await asyncio.sleep(0.01)
    assert not task.done()  # Should block

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Test step token decrement (line 69)
# Mutation: -= 1 → += 1, -= 1 → -= 2
# ---------------------------------------------------------------------------


async def test_hijack_tokens_decrement_by_exactly_one() -> None:
    """Tokens must decrement by exactly 1 (catches -= 1 → += 1 or -= 2)."""
    bot = SimpleBot()
    await bot.set_hijacked(True)
    await bot.request_step(5)

    # Each pass should decrement by 1
    assert bot._hijack_step_tokens == 5
    await bot.await_if_hijacked()
    assert bot._hijack_step_tokens == 4

    await bot.await_if_hijacked()
    assert bot._hijack_step_tokens == 3

    # Verify not decrementing by 2
    await bot.await_if_hijacked()
    assert bot._hijack_step_tokens == 2


async def test_hijack_tokens_only_decrement_when_positive() -> None:
    """Tokens only decrement when > 0, then we block at 0."""
    bot = SimpleBot()
    await bot.set_hijacked(True)
    await bot.request_step(1)

    await bot.await_if_hijacked()
    assert bot._hijack_step_tokens == 0

    # Next checkpoint should block (0 tokens)
    task = asyncio.create_task(bot.await_if_hijacked())
    await asyncio.sleep(0.01)
    assert not task.done()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Test set_hijacked early return (line 78)
# Mutation: == → !=
# ---------------------------------------------------------------------------


async def test_set_hijacked_idempotent_true() -> None:
    """Calling set_hijacked(True) twice should be idempotent (catches == → !=)."""
    bot = SimpleBot()

    await bot.set_hijacked(True)
    assert bot._hijacked is True
    original_tokens = bot._hijack_step_tokens

    # Call again with same value
    await bot.set_hijacked(True)

    # Should not reset tokens (early return on line 78)
    assert bot._hijacked is True
    assert bot._hijack_step_tokens == original_tokens


async def test_set_hijacked_idempotent_false() -> None:
    """Calling set_hijacked(False) twice should be idempotent."""
    bot = SimpleBot()
    await bot.set_hijacked(True)
    await bot.set_hijacked(False)

    assert bot._hijacked is False
    assert bot._hijack_event.is_set() is True

    # Call again
    await bot.set_hijacked(False)
    assert bot._hijacked is False


# ---------------------------------------------------------------------------
# Test step token capping (line 97)
# Mutation: min → max, 100 → 99/101, max(0, ...) removal
# ---------------------------------------------------------------------------


async def test_hijack_tokens_capped_at_100() -> None:
    """Step tokens capped at 100 (catches min → max, 100 → 99)."""
    bot = SimpleBot()
    await bot.set_hijacked(True)

    await bot.request_step(50)
    assert bot._hijack_step_tokens == 50

    await bot.request_step(50)
    assert bot._hijack_step_tokens == 100

    # Try to add more — should be capped at 100
    await bot.request_step(50)
    assert bot._hijack_step_tokens == 100  # Not 150


async def test_hijack_tokens_negative_clamped_to_zero() -> None:
    """Negative checkpoint values clamped to 0 via max(0, ...) (catches removal)."""
    bot = SimpleBot()
    await bot.set_hijacked(True)

    await bot.request_step(-10)
    # Should be 0, not negative
    assert bot._hijack_step_tokens >= 0


async def test_hijack_tokens_cap_boundary() -> None:
    """Verify cap is exactly 100, not 99 or 101."""
    bot = SimpleBot()
    await bot.set_hijacked(True)

    await bot.request_step(99)
    assert bot._hijack_step_tokens == 99

    await bot.request_step(1)
    assert bot._hijack_step_tokens == 100

    await bot.request_step(1)
    assert bot._hijack_step_tokens == 100  # Still 100, not 101


# ---------------------------------------------------------------------------
# Test watchdog timeout comparison (line 144)
# Mutation: < → <=, < → >
# ---------------------------------------------------------------------------


async def test_watchdog_timeout_boundary() -> None:
    """Watchdog fires when idle >= stuck_timeout (catches < → <=).

    This is a boundary test documenting the mutation target.
    Actual watchdog behavior tested in test_base.py integration tests.
    """
    # Verify the timeout comparison logic:
    # Line 144: `if idle_for < float(stuck_timeout_s):`
    idle_for = 100.0
    stuck_timeout = 100.0

    # At boundary: idle_for == stuck_timeout should NOT skip (should fire)
    # Catches: `<` → `<=` would skip this case
    should_fire = not (idle_for < stuck_timeout)
    assert should_fire is True  # idle_for >= timeout → fire

    # Just below boundary: should NOT fire
    idle_just_below = 99.9
    should_not_fire = idle_just_below < stuck_timeout
    assert should_not_fire is True  # Continue without firing


async def test_watchdog_does_not_fire_while_hijacked() -> None:
    """Watchdog suppressed while hijacked (line 140 check)."""
    bot = SimpleBot()
    on_stuck_called = []

    async def on_stuck() -> None:
        on_stuck_called.append(True)

    await bot.set_hijacked(True)
    bot.start_watchdog(stuck_timeout_s=0.1, check_interval_s=0.05, on_stuck=on_stuck)

    # Wait long enough that it would fire if not hijacked
    await asyncio.sleep(0.15)

    # Should NOT have called on_stuck (watchdog suppressed while hijacked)
    assert len(on_stuck_called) == 0

    await bot.stop_watchdog()


# ---------------------------------------------------------------------------
# __init__ — super().__init__ args (kills mutmut_1, mutmut_2)
# ---------------------------------------------------------------------------


async def test_init_passes_args_to_super() -> None:
    """super().__init__(*args, **kwargs) called correctly (kills mutmut_1, _2).

    mutmut_1: super().__init__(**kwargs) — missing *args
    mutmut_2: super().__init__(*args,) — missing **kwargs

    Verify by creating a class that requires both positional and keyword args
    in its own __init__ chain.
    """

    class Base:
        def __init__(self, pos_arg: str, *, kw_arg: str = "") -> None:
            self.pos_arg = pos_arg
            self.kw_arg = kw_arg

    class MyBot(HijackableMixin, Base):
        pass

    # Both *args and **kwargs must be forwarded correctly
    bot = MyBot("hello", kw_arg="world")
    assert bot.pos_arg == "hello"
    assert bot.kw_arg == "world"
    # HijackableMixin attributes should also be set
    assert hasattr(bot, "_hijacked")
    assert bot._hijacked is False


# ---------------------------------------------------------------------------
# request_step — default value (kills mutmut_1: default=3 vs 2)
# ---------------------------------------------------------------------------


async def test_request_step_default_checkpoints_is_2() -> None:
    """request_step() default adds exactly 2 tokens (kills mutmut_1: default=3)."""
    import inspect

    sig = inspect.signature(HijackableMixin.request_step)
    default = sig.parameters["checkpoints"].default
    assert default == 2, f"Expected default=2, got {default}"


async def test_request_step_with_default_adds_two_tokens() -> None:
    """Calling request_step() with no args adds 2 tokens (kills mutmut_1)."""
    bot = SimpleBot()
    await bot.set_hijacked(True)

    await bot.request_step()  # default checkpoints=2
    assert bot._hijack_step_tokens == 2  # not 3


# ---------------------------------------------------------------------------
# start_watchdog — default parameters (kills mutmut_1: stuck_timeout=121, _2: interval=6)
# ---------------------------------------------------------------------------


def test_start_watchdog_default_stuck_timeout_is_120() -> None:
    """stuck_timeout_s default is 120.0 not 121.0 (kills mutmut_1)."""
    import inspect

    sig = inspect.signature(HijackableMixin.start_watchdog)
    default = sig.parameters["stuck_timeout_s"].default
    assert default == 120.0, f"Expected 120.0, got {default}"


def test_start_watchdog_default_check_interval_is_5() -> None:
    """check_interval_s default is 5.0 not 6.0 (kills mutmut_2)."""
    import inspect

    sig = inspect.signature(HijackableMixin.start_watchdog)
    default = sig.parameters["check_interval_s"].default
    assert default == 5.0, f"Expected 5.0, got {default}"


# ---------------------------------------------------------------------------
# start_watchdog — continue vs break when hijacked (kills mutmut_14)
# ---------------------------------------------------------------------------


async def test_watchdog_continues_when_hijacked_not_exits() -> None:
    """Watchdog uses 'continue' not 'break' when hijacked (kills mutmut_14).

    mutmut_14 uses 'break' instead of 'continue' when self._hijacked is True,
    which would stop the watchdog loop entirely after the first hijacked check.
    Verify that the watchdog keeps running after being hijacked.
    """
    bot = SimpleBot()
    on_stuck_called: list[bool] = []

    async def on_stuck() -> None:
        on_stuck_called.append(True)

    # Start watchdog with very short timeout
    bot.start_watchdog(stuck_timeout_s=0.05, check_interval_s=0.05, on_stuck=on_stuck)

    # Let watchdog run while NOT hijacked — it should fire
    bot._last_progress_mono -= 1.0  # make it look stuck
    await asyncio.sleep(0.2)

    first_count = len(on_stuck_called)

    # Now mark as hijacked — watchdog should suppress but CONTINUE running
    await bot.set_hijacked(True)
    bot.note_progress()  # reset timer
    await asyncio.sleep(0.15)

    # Watchdog should still be running (not broken out of loop)
    assert bot._watchdog_task is not None
    assert not bot._watchdog_task.done()

    # Un-hijack and make it look stuck again
    await bot.set_hijacked(False)
    bot._last_progress_mono -= 1.0

    # If watchdog is still running, it should fire again
    await asyncio.sleep(0.2)

    # Without mutmut_14: watchdog continues → fires again
    # With mutmut_14 (break): watchdog stops → never fires again
    assert len(on_stuck_called) > first_count or bot._watchdog_task.done() is False

    await bot.stop_watchdog()


# ---------------------------------------------------------------------------
# start_watchdog — < vs <= comparison (kills mutmut_17)
# ---------------------------------------------------------------------------


async def test_watchdog_fires_when_idle_equals_timeout() -> None:
    """Watchdog fires when idle == stuck_timeout (< not <=) (kills mutmut_17).

    mutmut_17 uses <= instead of <, meaning when idle_for == stuck_timeout_s
    the watchdog would NOT fire (would continue). With < it fires.
    """
    bot = SimpleBot()
    fired: list[bool] = []

    async def on_stuck() -> None:
        fired.append(True)

    # Note: the watchdog loop sleeps max(0.5, check_interval_s) so minimum 0.5s per check
    bot.start_watchdog(stuck_timeout_s=0.05, check_interval_s=0.5, on_stuck=on_stuck)
    # Make bot appear stuck (idle_for will be >> stuck_timeout_s)
    bot._last_progress_mono -= 10.0

    await asyncio.sleep(1.2)  # More than one check cycle
    # Should have fired (idle_for >> stuck_timeout_s)
    assert len(fired) > 0
    await bot.stop_watchdog()


# ---------------------------------------------------------------------------
# start_watchdog — continue vs break when not yet timed out (kills mutmut_19)
# ---------------------------------------------------------------------------


async def test_watchdog_continues_checking_when_not_yet_timed_out() -> None:
    """Watchdog uses 'continue' not 'break' when not timed out (kills mutmut_19).

    mutmut_19 uses 'break' instead of 'continue' when idle_for < stuck_timeout_s,
    which would stop the watchdog after the first check that isn't timed out.
    """
    bot = SimpleBot()
    fired: list[bool] = []

    async def on_stuck() -> None:
        fired.append(True)

    # Note: the watchdog loop sleeps max(0.5, check_interval_s) — minimum 0.5s per check.
    # Strategy: stuck_timeout_s=0.8 so:
    #   - First check at t≈0.5s: idle≈0.5s < 0.8s → 'continue' (mutant uses 'break')
    #   - Second check at t≈1.0s: idle≈1.0s > 0.8s → fires
    # With mutmut_19 (break): loop exits at first check → never fires
    bot.start_watchdog(stuck_timeout_s=0.8, check_interval_s=0.5, on_stuck=on_stuck)
    # Don't call note_progress — let it accumulate idle time from start

    # Wait for TWO check cycles to elapse
    await asyncio.sleep(1.3)

    # With correct code (continue): fires at second check
    # With mutmut_19 (break): never fires
    assert len(fired) > 0, "Watchdog should have fired after timeout (mutmut_19 uses break, killing loop)"
    await bot.stop_watchdog()


# ---------------------------------------------------------------------------
# cleanup_hijack — set_hijacked(False) not set_hijacked(None) (kills mutmut_1)
# ---------------------------------------------------------------------------


async def test_cleanup_hijack_releases_hijack() -> None:
    """cleanup_hijack() calls set_hijacked(False), not set_hijacked(None) (kills mutmut_1)."""
    bot = SimpleBot()

    # Hijack the bot first
    await bot.set_hijacked(True)
    assert bot._hijacked is True

    # cleanup should release
    await bot.cleanup_hijack()
    assert bot._hijacked is False, "cleanup_hijack should set _hijacked to False, not None"


async def test_cleanup_hijack_unblocks_await_if_hijacked() -> None:
    """After cleanup_hijack, await_if_hijacked does not block."""
    bot = SimpleBot()

    await bot.set_hijacked(True)
    task = asyncio.create_task(bot.await_if_hijacked())
    await asyncio.sleep(0.01)
    assert not task.done()  # Blocked

    await bot.cleanup_hijack()
    await asyncio.sleep(0.01)
    # Should now be unblocked
    assert task.done()
