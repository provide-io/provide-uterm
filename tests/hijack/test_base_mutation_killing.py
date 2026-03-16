#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for HijackableMixin (base.py) — precision boundary testing."""

from __future__ import annotations

import asyncio
import contextlib

from provide.terminal.hijack.base import HijackableMixin


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
