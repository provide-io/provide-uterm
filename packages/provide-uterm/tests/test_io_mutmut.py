#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut-killer tests for ``provide.uterm.io``.

Targets remaining mutations in:
- ``InputSender.send_input`` — input_type branching, session checks.
- ``PromptWaiter._wait_if_not_idle`` — gating predicate.
- ``PromptWaiter._check_prompt_filters`` — callback args, return-value branches.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.io import InputSender, PromptWaiter

# ---------------------------------------------------------------------------
# InputSender.send_input — input_type branches
# ---------------------------------------------------------------------------


class _MockSession:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._connected = True

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def is_connected(self) -> bool:
        return self._connected


@pytest.mark.asyncio
class TestInputSenderSendInput:
    async def test_none_session_raises_connection_error(self) -> None:
        sender = InputSender(None)  # type: ignore[arg-type]
        with pytest.raises(ConnectionError, match="None"):
            await sender.send_input("hi", wait_after_sec=0)

    async def test_disconnected_session_raises_connection_error(self) -> None:
        session = _MockSession()
        session._connected = False
        sender = InputSender(session)  # type: ignore[arg-type]
        with pytest.raises(ConnectionError, match="disconnected"):
            await sender.send_input("hi", wait_after_sec=0)

    async def test_single_key_sends_keys_verbatim(self) -> None:
        session = _MockSession()
        sender = InputSender(session)  # type: ignore[arg-type]
        await sender.send_input("y", input_type="single_key", wait_after_sec=0)
        assert session.sent == ["y"]

    async def test_any_key_sends_single_space(self) -> None:
        session = _MockSession()
        sender = InputSender(session)  # type: ignore[arg-type]
        await sender.send_input("ignored", input_type="any_key", wait_after_sec=0)
        assert session.sent == [" "]

    async def test_multi_key_appends_carriage_return(self) -> None:
        session = _MockSession()
        sender = InputSender(session)  # type: ignore[arg-type]
        await sender.send_input("ls", input_type="multi_key", wait_after_sec=0)
        assert session.sent == ["ls\r"]

    async def test_unknown_input_type_treated_as_multi_key(self) -> None:
        """Comment in source says 'anything else: treated as multi_key'."""
        session = _MockSession()
        sender = InputSender(session)  # type: ignore[arg-type]
        await sender.send_input("xyz", input_type="unrecognised", wait_after_sec=0)
        assert session.sent == ["xyz\r"]

    async def test_none_input_type_treated_as_multi_key(self) -> None:
        session = _MockSession()
        sender = InputSender(session)  # type: ignore[arg-type]
        await sender.send_input("z", input_type=None, wait_after_sec=0)
        assert session.sent == ["z\r"]

    async def test_zero_wait_after_skips_sleep(self) -> None:
        """``wait_after_sec=0`` does not call asyncio.sleep at all."""
        import asyncio as _asyncio

        session = _MockSession()
        sender = InputSender(session)  # type: ignore[arg-type]
        original_sleep = _asyncio.sleep
        slept: list[float] = []

        async def tap_sleep(t: float) -> None:
            slept.append(t)
            await original_sleep(0)

        _asyncio.sleep = tap_sleep  # type: ignore[assignment]
        try:
            await sender.send_input("k", wait_after_sec=0)
        finally:
            _asyncio.sleep = original_sleep  # type: ignore[assignment]
        assert slept == [], "asyncio.sleep should not be called when wait_after_sec == 0"

    async def test_positive_wait_after_calls_sleep(self) -> None:
        import asyncio as _asyncio

        session = _MockSession()
        sender = InputSender(session)  # type: ignore[arg-type]
        slept: list[float] = []
        original_sleep = _asyncio.sleep

        async def tap_sleep(t: float) -> None:
            slept.append(t)
            await original_sleep(0)

        _asyncio.sleep = tap_sleep  # type: ignore[assignment]
        try:
            await sender.send_input("k", wait_after_sec=0.05)
        finally:
            _asyncio.sleep = original_sleep  # type: ignore[assignment]
        assert slept == [0.05]


# ---------------------------------------------------------------------------
# PromptWaiter._check_prompt_filters — args + return semantics
# ---------------------------------------------------------------------------


class _MockSessionWithUpdate:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.waits: list[int] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def wait_for_update(self, *, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)

    def is_connected(self) -> bool:
        return True


@pytest.mark.asyncio
class TestCheckPromptFilters:
    async def test_expected_mismatch_calls_rejected_with_detected_full(self) -> None:
        session = _MockSessionWithUpdate()
        waiter = PromptWaiter(session)  # type: ignore[arg-type]
        captured: list[tuple[Any, str]] = []

        def on_reject(detected: dict[str, Any], reason: str) -> None:
            captured.append((detected, reason))

        detected = {"prompt_id": "foo.X", "data": 42}
        rejected = await waiter._check_prompt_filters(
            detected_full=detected,
            prompt_id="foo.X",
            expected_prompt_id="other.Y",
            on_prompt_detected=None,
            on_prompt_rejected=on_reject,
            read_interval_sec=0.001,
        )
        assert rejected is True
        assert len(captured) == 1
        # The detected dict is passed through (mutation that replaces it with None fails).
        assert captured[0][0] is detected
        assert captured[0][1] == "expected_mismatch"

    async def test_callback_reject_calls_rejected_with_detected_full(self) -> None:
        session = _MockSessionWithUpdate()
        waiter = PromptWaiter(session)  # type: ignore[arg-type]
        captured: list[tuple[Any, str]] = []

        def on_reject(detected: dict[str, Any], reason: str) -> None:
            captured.append((detected, reason))

        detected = {"prompt_id": "foo", "k": 1}
        rejected = await waiter._check_prompt_filters(
            detected_full=detected,
            prompt_id="foo",
            expected_prompt_id=None,
            on_prompt_detected=lambda _d: False,  # callback rejects
            on_prompt_rejected=on_reject,
            read_interval_sec=0.001,
        )
        assert rejected is True
        assert captured == [(detected, "callback_reject")]

    async def test_no_filter_no_rejection_returns_false(self) -> None:
        session = _MockSessionWithUpdate()
        waiter = PromptWaiter(session)  # type: ignore[arg-type]
        rejected = await waiter._check_prompt_filters(
            detected_full={"prompt_id": "foo"},
            prompt_id="foo",
            expected_prompt_id=None,
            on_prompt_detected=None,
            on_prompt_rejected=None,
            read_interval_sec=0.001,
        )
        assert rejected is False

    async def test_callback_accepts_returns_false(self) -> None:
        session = _MockSessionWithUpdate()
        waiter = PromptWaiter(session)  # type: ignore[arg-type]
        rejected = await waiter._check_prompt_filters(
            detected_full={"prompt_id": "foo"},
            prompt_id="foo",
            expected_prompt_id=None,
            on_prompt_detected=lambda _d: True,
            on_prompt_rejected=None,
            read_interval_sec=0.001,
        )
        assert rejected is False

    async def test_expected_match_substring_passes(self) -> None:
        """``expected_prompt_id in prompt_id`` — substring containment, not equality."""
        session = _MockSessionWithUpdate()
        waiter = PromptWaiter(session)  # type: ignore[arg-type]
        rejected = await waiter._check_prompt_filters(
            detected_full={"prompt_id": "shell.bash.prompt"},
            prompt_id="shell.bash.prompt",
            expected_prompt_id="bash",
            on_prompt_detected=None,
            on_prompt_rejected=None,
            read_interval_sec=0.001,
        )
        assert rejected is False


# ---------------------------------------------------------------------------
# PromptWaiter._wait_if_not_idle — gating predicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWaitIfNotIdle:
    @staticmethod
    def _waiter() -> PromptWaiter:
        session = MagicMock()
        session.wait_for_update = AsyncMock()
        session.seconds_until_idle = MagicMock(return_value=0.1)
        return PromptWaiter(session)

    async def test_returns_false_when_require_idle_false(self) -> None:
        waiter = self._waiter()
        result = await waiter._wait_if_not_idle(
            detected_full={},
            is_idle=False,
            elapsed=0.0,
            timeout_sec=10.0,
            idle_grace_ratio=0.5,
            read_interval_sec=0.05,
            require_idle=False,
            on_prompt_rejected=None,
        )
        assert result is False

    async def test_returns_false_when_already_idle(self) -> None:
        waiter = self._waiter()
        result = await waiter._wait_if_not_idle(
            detected_full={},
            is_idle=True,
            elapsed=0.0,
            timeout_sec=10.0,
            idle_grace_ratio=0.5,
            read_interval_sec=0.05,
            require_idle=True,
            on_prompt_rejected=None,
        )
        assert result is False

    async def test_returns_false_when_past_grace_window(self) -> None:
        """elapsed >= timeout_sec * idle_grace_ratio → don't wait, return False."""
        waiter = self._waiter()
        result = await waiter._wait_if_not_idle(
            detected_full={},
            is_idle=False,
            elapsed=10.0,  # >= 10 * 0.5 = 5.0
            timeout_sec=10.0,
            idle_grace_ratio=0.5,
            read_interval_sec=0.05,
            require_idle=True,
            on_prompt_rejected=None,
        )
        assert result is False

    async def test_returns_true_when_in_grace_window_and_not_idle(self) -> None:
        waiter = self._waiter()
        result = await waiter._wait_if_not_idle(
            detected_full={"prompt_id": "foo"},
            is_idle=False,
            elapsed=1.0,  # < 10 * 0.5 = 5.0
            timeout_sec=10.0,
            idle_grace_ratio=0.5,
            read_interval_sec=0.05,
            require_idle=True,
            on_prompt_rejected=None,
        )
        assert result is True

    async def test_calls_on_prompt_rejected_with_not_idle_reason(self) -> None:
        waiter = self._waiter()
        captured: list[tuple[Any, str]] = []
        await waiter._wait_if_not_idle(
            detected_full={"prompt_id": "x"},
            is_idle=False,
            elapsed=1.0,
            timeout_sec=10.0,
            idle_grace_ratio=0.5,
            read_interval_sec=0.05,
            require_idle=True,
            on_prompt_rejected=lambda d, r: captured.append((d, r)),
        )
        assert captured == [({"prompt_id": "x"}, "not_idle")]

    async def test_at_exact_grace_boundary_returns_false(self) -> None:
        """elapsed == timeout_sec * idle_grace_ratio exactly → predicate is False
        (the comparison is ``<``, not ``<=``)."""
        waiter = self._waiter()
        result = await waiter._wait_if_not_idle(
            detected_full={},
            is_idle=False,
            elapsed=5.0,
            timeout_sec=10.0,
            idle_grace_ratio=0.5,
            read_interval_sec=0.05,
            require_idle=True,
            on_prompt_rejected=None,
        )
        assert result is False
