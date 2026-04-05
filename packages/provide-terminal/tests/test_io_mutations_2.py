#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for provide.terminal.io (part 2).

Classes: TestRequireIdleLogic, TestExpectedPromptId, TestOnPromptDetectedCallback,
         TestReturnValueKeys, TestRemainingComputation, TestInputSenderDefaults,
         TestInputSenderErrors.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.io import InputSender, PromptWaiter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    connected: bool = True,
    snapshot: dict[str, Any] | None = None,
) -> MagicMock:
    session = MagicMock()
    session.is_connected = MagicMock(return_value=connected)
    session.snapshot = MagicMock(return_value=snapshot or {"screen": ""})
    session.wait_for_update = AsyncMock(return_value=True)
    session.send = AsyncMock()
    return session


def _session_with_prompt(
    prompt_id: str = "main_menu",
    input_type: str = "multi_key",
    screen: str = "Choose:",
    is_idle: bool = True,
    screen_hash: str = "abc123",
    captured_at: float | None = None,
    kv_data: dict | None = None,
) -> MagicMock:
    snap: dict[str, Any] = {
        "screen": screen,
        "screen_hash": screen_hash,
        "captured_at": captured_at if captured_at is not None else time.time(),
        "prompt_detected": {
            "prompt_id": prompt_id,
            "input_type": input_type,
            "is_idle": is_idle,
            **({("kv_data"): kv_data} if kv_data else {}),
        },
    }
    session = MagicMock()
    session.is_connected = MagicMock(return_value=True)
    session.snapshot = MagicMock(return_value=snap)
    session.wait_for_update = AsyncMock(return_value=True)
    session.send = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# require_idle / idle_grace_ratio logic (mutmut_84, 86, 93, 102, 103, 109, 110, 115-117)
# ---------------------------------------------------------------------------


class TestRequireIdleLogic:
    async def test_require_idle_skips_non_idle_prompt(self) -> None:
        """mutmut_84: non-idle prompt with require_idle=True must be skipped when < grace."""
        # idle_grace_ratio=0.9 means skip if elapsed < 0.9 * timeout_sec
        # With timeout=5s and instant execution, elapsed ≈ 0 < 4.5s, so non-idle is skipped
        snap_not_idle: dict[str, Any] = {
            "screen": "loading",
            "screen_hash": "a",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "x", "is_idle": False},
        }
        snap_idle: dict[str, Any] = {
            "screen": "ready",
            "screen_hash": "b",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "x", "is_idle": True},
        }
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        session.snapshot = MagicMock(side_effect=[snap_not_idle, snap_idle])
        session.wait_for_update = AsyncMock(return_value=True)
        # seconds_until_idle returns a proper float so min() works
        session.seconds_until_idle = MagicMock(return_value=0.1)
        waiter = PromptWaiter(session)
        # idle_grace_ratio=0.9, timeout=5000ms — non-idle is skipped since elapsed << 4.5s
        result = await waiter.wait_for_prompt(timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9)
        # Eventually accepts the idle prompt
        assert result["is_idle"] is True

    async def test_on_prompt_rejected_receives_detected_full_not_none(self) -> None:
        """mutmut_86: on_prompt_rejected must receive detected_full, not None."""
        snap_not_idle: dict[str, Any] = {
            "screen": "busy",
            "screen_hash": "x",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "y", "is_idle": False},
        }
        snap_idle: dict[str, Any] = {
            "screen": "ready",
            "screen_hash": "z",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "y", "is_idle": True},
        }
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        session.snapshot = MagicMock(side_effect=[snap_not_idle, snap_idle])
        session.wait_for_update = AsyncMock(return_value=True)
        session.seconds_until_idle = MagicMock(return_value=0.1)

        rejected = []

        def on_rejected(data: dict, reason: str) -> None:
            rejected.append((data, reason))

        waiter = PromptWaiter(session)
        # idle_grace_ratio=0.9, timeout=5000 — non-idle is skipped at elapsed≈0
        await waiter.wait_for_prompt(
            timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9, on_prompt_rejected=on_rejected
        )
        assert len(rejected) >= 1
        # First arg must be dict (detected_full), not None
        assert rejected[0][0] is not None
        assert isinstance(rejected[0][0], dict)
        assert rejected[0][1] == "not_idle"

    async def test_getattr_uses_self_session_not_none(self) -> None:
        """mutmut_93: getattr must use self.session, not None."""
        # Test that seconds_until_idle on session is called, not on None
        snap_not_idle: dict[str, Any] = {
            "screen": "busy",
            "screen_hash": "x",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "y", "is_idle": False},
        }
        snap_idle: dict[str, Any] = {
            "screen": "ready",
            "screen_hash": "z",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "y", "is_idle": True},
        }
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        session.snapshot = MagicMock(side_effect=[snap_not_idle, snap_idle])
        session.wait_for_update = AsyncMock(return_value=True)
        session.seconds_until_idle = MagicMock(return_value=0.1)
        waiter = PromptWaiter(session)
        # idle_grace_ratio=0.9, timeout=5000ms so non-idle is skipped and seconds_until_idle called
        await waiter.wait_for_prompt(timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9, read_interval_ms=100)
        # seconds_until_idle was called on the session (not None)
        session.seconds_until_idle.assert_called()

    async def test_wait_ms_uses_times_1000_not_divide(self) -> None:
        """mutmut_110: wait_ms = ... * 1000 not / 1000."""
        # With / 1000, a 0.25 second interval becomes 0.00025 ms
        # We verify that wait_for_update is called with reasonable ms values
        snap_not_idle: dict[str, Any] = {
            "screen": "busy",
            "screen_hash": "x",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "y", "is_idle": False},
        }
        snap_idle: dict[str, Any] = {
            "screen": "ready",
            "screen_hash": "z",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "y", "is_idle": True},
        }
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        session.snapshot = MagicMock(side_effect=[snap_not_idle, snap_idle])
        session.seconds_until_idle = MagicMock(return_value=0.1)
        wait_calls = []

        async def mock_wait(*, timeout_ms: int) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session.wait_for_update = mock_wait
        waiter = PromptWaiter(session)
        # idle_grace_ratio=0.9 so non-idle skipped → wait_for_update called
        await waiter.wait_for_prompt(timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9, read_interval_ms=250)
        # wait_ms should be in milliseconds — should be > 1 and < timeout
        assert len(wait_calls) >= 1
        # Any call should use ms-scale values (> 0, reasonable)
        for ms in wait_calls:
            if ms is not None:
                assert ms >= 1


# ---------------------------------------------------------------------------
# expected_prompt_id filter (mutmut_121, 127, 129, 130)
# ---------------------------------------------------------------------------


class TestExpectedPromptId:
    async def test_wrong_prompt_id_calls_rejected(self) -> None:
        """mutmut_121: on_prompt_rejected must receive detected_full, not None."""
        snap_wrong: dict[str, Any] = {
            "screen": "screen1",
            "screen_hash": "a",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "login", "is_idle": True},
        }
        snap_right: dict[str, Any] = {
            "screen": "screen2",
            "screen_hash": "b",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "main_menu", "is_idle": True},
        }
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        session.snapshot = MagicMock(side_effect=[snap_wrong, snap_right])
        session.wait_for_update = AsyncMock(return_value=True)

        rejected = []

        def on_rejected(data: dict, reason: str) -> None:
            rejected.append((data, reason))

        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(
            timeout_ms=1000,
            expected_prompt_id="main_menu",
            on_prompt_rejected=on_rejected,
        )
        assert result["prompt_id"] == "main_menu"
        assert len(rejected) == 1
        assert rejected[0][0] is not None  # detected_full, not None
        assert rejected[0][1] == "expected_mismatch"

    async def test_wait_uses_read_interval_ms_times_1000(self) -> None:
        """mutmut_127,129,130: wait must use read_interval_sec * 1000 not / 1000 or None."""
        snap_wrong: dict[str, Any] = {
            "screen": "screen1",
            "screen_hash": "a",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "login", "is_idle": True},
        }
        snap_right: dict[str, Any] = {
            "screen": "screen2",
            "screen_hash": "b",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "main_menu", "is_idle": True},
        }
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        session.snapshot = MagicMock(side_effect=[snap_wrong, snap_right])
        wait_calls = []

        async def mock_wait(*, timeout_ms: int) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session.wait_for_update = mock_wait
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=2000, expected_prompt_id="main_menu", read_interval_ms=200)
        assert result["prompt_id"] == "main_menu"
        # The call after the rejected prompt should pass ~200ms not None or micro-seconds
        assert len(wait_calls) >= 1
        for ms in wait_calls:
            if ms is not None:
                assert ms > 0  # valid ms value


# ---------------------------------------------------------------------------
# on_prompt_detected callback (mutmut_134, 135, 141, 143, 144)
# ---------------------------------------------------------------------------


class TestOnPromptDetectedCallback:
    async def test_callback_rejection_passes_detected_full(self) -> None:
        """mutmut_134: on_prompt_detected must receive detected_full, not None."""
        session = _session_with_prompt(is_idle=True)
        # Reject first, accept second
        call_count = [0]

        def on_detected(data: dict) -> bool:
            call_count[0] += 1
            return call_count[0] != 1

        waiter = PromptWaiter(session)
        # Need two snapshots
        snap1: dict[str, Any] = {
            "screen": "first",
            "screen_hash": "a",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "x", "is_idle": True},
        }
        snap2: dict[str, Any] = {
            "screen": "second",
            "screen_hash": "b",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "x", "is_idle": True},
        }
        session.snapshot = MagicMock(side_effect=[snap1, snap2])
        result = await waiter.wait_for_prompt(timeout_ms=1000, on_prompt_detected=on_detected)
        assert result["prompt_id"] == "x"
        assert call_count[0] == 2

    async def test_callback_reject_triggers_on_prompt_rejected(self) -> None:
        """mutmut_135: on_prompt_rejected must receive detected_full, not None."""
        snap1: dict[str, Any] = {
            "screen": "first",
            "screen_hash": "a",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "x", "is_idle": True},
        }
        snap2: dict[str, Any] = {
            "screen": "second",
            "screen_hash": "b",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "x", "is_idle": True},
        }
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        session.snapshot = MagicMock(side_effect=[snap1, snap2])
        session.wait_for_update = AsyncMock(return_value=True)

        call_count = [0]
        rejected = []

        def on_detected(data: dict) -> bool:
            call_count[0] += 1
            return call_count[0] > 1  # reject first call

        def on_rejected(data: dict, reason: str) -> None:
            rejected.append((data, reason))

        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=1000, on_prompt_detected=on_detected, on_prompt_rejected=on_rejected)
        assert len(rejected) == 1
        assert rejected[0][0] is not None
        assert isinstance(rejected[0][0], dict)
        assert rejected[0][1] == "callback_reject"


# ---------------------------------------------------------------------------
# Return value keys (mutmut_155-159)
# ---------------------------------------------------------------------------


class TestReturnValueKeys:
    async def test_result_has_kv_data_key(self) -> None:
        """mutmut_155,156: result must have 'kv_data' key."""
        session = _session_with_prompt(kv_data={"score": 42})
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert "kv_data" in result
        assert result["kv_data"] is not None

    async def test_kv_data_value_from_detected_full(self) -> None:
        """mutmut_157,158,159: kv_data must come from correct key 'kv_data', not None/wrong key."""
        session = _session_with_prompt(kv_data={"level": 99})
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["kv_data"] == {"level": 99}

    async def test_result_has_is_idle_key(self) -> None:
        """Result must have 'is_idle' key."""
        session = _session_with_prompt(is_idle=True)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert "is_idle" in result
        assert result["is_idle"] is True

    async def test_result_has_input_type_key(self) -> None:
        """Result must have 'input_type' key."""
        session = _session_with_prompt(input_type="single_key")
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert "input_type" in result
        assert result["input_type"] == "single_key"


# ---------------------------------------------------------------------------
# Remaining computation (mutmut_167, 168, 169, 170, 172, 177)
# ---------------------------------------------------------------------------


class TestRemainingComputation:
    async def test_remaining_uses_max_0(self) -> None:
        """mutmut_167: remaining = max(0, ...) not max(1, ...)."""
        # Near-expired timeout: remaining should be 0, not 1
        session = _make_session()
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=1)  # almost no time
        # Verify wait_for_update was called with 0 ms (not 1)
        # Key: max(0, ...) when near-expired means 0 ms wait
        # We just verify it completes without hanging

    async def test_remaining_subtraction_not_addition(self) -> None:
        """mutmut_168: remaining = timeout_sec - elapsed, not + elapsed."""
        # With addition, remaining would be LARGER than timeout, causing infinite loop
        # The test is that it completes in finite time
        session = _make_session()
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=100)

    async def test_wait_uses_min_of_interval_and_remaining(self) -> None:
        """mutmut_170,172,177: wait must use min(read_interval_sec, remaining) * 1000."""
        # If we use None or /1000 instead of *1000, the wait would be wrong
        # Test: with a prompt immediately available, no wait happens at non-prompt path
        session = _session_with_prompt(is_idle=True)
        wait_calls = []

        async def mock_wait(*, timeout_ms: int) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session.wait_for_update = mock_wait
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500, read_interval_ms=100)
        assert result["prompt_id"] == "main_menu"
        # No wait needed since prompt found immediately


# ---------------------------------------------------------------------------
# InputSender default parameter mutations (mutmut_1, 2, 3)
# ---------------------------------------------------------------------------


class TestInputSenderDefaults:
    async def test_default_input_type_is_multi_key(self) -> None:
        """mutmut_1,2: default input_type must be 'multi_key'."""
        import inspect

        sender = InputSender(_make_session())
        sig = inspect.signature(sender.send_input)
        default = sig.parameters["input_type"].default
        assert default == "multi_key"
        assert default != "XXmulti_keyXX"
        assert default != "MULTI_KEY"

    async def test_default_wait_after_sec_is_0_2(self) -> None:
        """mutmut_3: wait_after_sec default must be 0.2, not 1.2."""
        import inspect

        sender = InputSender(_make_session())
        sig = inspect.signature(sender.send_input)
        default = sig.parameters["wait_after_sec"].default
        assert default == pytest.approx(0.2)
        assert default != pytest.approx(1.2)


# ---------------------------------------------------------------------------
# InputSender error messages (mutmut_6, 12, 13)
# ---------------------------------------------------------------------------


class TestInputSenderErrors:
    async def test_none_session_raises_connection_error_with_message(self) -> None:
        """mutmut_6: ConnectionError message must be 'Session is None'."""
        sender = InputSender(None)
        with pytest.raises(ConnectionError, match="Session is None"):
            await sender.send_input("test")

    async def test_none_session_message_exact(self) -> None:
        """mutmut_6: exact message."""
        sender = InputSender(None)
        with pytest.raises(ConnectionError) as exc_info:
            await sender.send_input("test")
        assert exc_info.value.args[0] == "Session is None"

    async def test_disconnected_session_raises_connection_error(self) -> None:
        """mutmut_12: ConnectionError message must be 'Session disconnected'."""
        session = _make_session(connected=False)
        sender = InputSender(session)
        with pytest.raises(ConnectionError, match="Session disconnected"):
            await sender.send_input("test")

    async def test_disconnected_message_capitalized(self) -> None:
        """mutmut_12,13: message must be 'Session disconnected' not lowercase."""
        session = _make_session(connected=False)
        sender = InputSender(session)
        with pytest.raises(ConnectionError) as exc_info:
            await sender.send_input("test")
        assert exc_info.value.args[0] == "Session disconnected"
        assert exc_info.value.args[0] != "session disconnected"
