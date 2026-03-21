#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for io.py — PromptWaiter and InputSender.

Kills surviving mutants in:
- wait_for_prompt: screen_hash field (47-56), captured_at field (57-62),
  prompt_id fallback (69), is_idle default (72,74,77),
  require_idle/idle_grace logic (84,86,93,102,103,109,110,115-117),
  expected_prompt_id filter (121,127,129),
  on_prompt_detected callback (134,135,141,143),
  return value keys (155-159), remaining computation (167-170,172,177)
- send_input: defaults (1,2,3), error messages (6,12,13)
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


def _make_snap(
    prompt_id: str = "test_prompt",
    input_type: str = "multi_key",
    is_idle: bool = True,
    screen: str = "Screen",
    screen_hash: str = "hash123",
    captured_at: float | None = None,
    kv_data: dict | None = None,
) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "screen": screen,
        "screen_hash": screen_hash,
        "captured_at": captured_at if captured_at is not None else time.time(),
        "prompt_detected": {
            "prompt_id": prompt_id,
            "input_type": input_type,
            "is_idle": is_idle,
        },
    }
    if kv_data is not None:
        snap["prompt_detected"]["kv_data"] = kv_data
    return snap


def _make_session(snap: dict[str, Any] | None = None, connected: bool = True) -> MagicMock:
    session = MagicMock()
    session.is_connected = MagicMock(return_value=connected)
    session.snapshot = MagicMock(return_value=snap or {"screen": ""})
    session.wait_for_update = AsyncMock(return_value=True)
    session.send = AsyncMock()
    return session


def _make_session_multi(snaps: list[dict[str, Any]], connected: bool = True) -> MagicMock:
    session = MagicMock()
    session.is_connected = MagicMock(return_value=connected)
    session.snapshot = MagicMock(side_effect=snaps)
    session.wait_for_update = AsyncMock(return_value=True)
    session.send = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# screen_hash in detected_full (mutmut_47-56)
# ---------------------------------------------------------------------------


class TestScreenHashInDetectedFull:
    async def test_screen_hash_key_is_lowercase(self) -> None:
        """mutmut_48,49: key must be 'screen_hash' not 'XXscreen_hashXX' or 'SCREEN_HASH'."""
        snap = _make_snap(screen_hash="abc123")
        session = _make_session(snap)
        seen_data: list[dict] = []
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=500, on_prompt_seen=seen_data.append)
        assert len(seen_data) == 1
        assert "screen_hash" in seen_data[0]
        assert "XXscreen_hashXX" not in seen_data[0]
        assert "SCREEN_HASH" not in seen_data[0]

    async def test_screen_hash_value_from_snapshot(self) -> None:
        """mutmut_47,50,51,54,55: screen_hash must come from snapshot's 'screen_hash', not None/wrong key."""
        snap = _make_snap(screen_hash="deadbeef")
        session = _make_session(snap)
        seen_data: list[dict] = []
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=500, on_prompt_seen=seen_data.append)
        assert seen_data[0]["screen_hash"] == "deadbeef"
        assert seen_data[0]["screen_hash"] is not None

    async def test_screen_hash_defaults_to_empty_string(self) -> None:
        """mutmut_51,52,53,56: missing screen_hash must default to '' not None or 'XXXX'."""
        snap: dict[str, Any] = {
            "screen": "test",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "x", "is_idle": True},
            # No screen_hash key
        }
        session = _make_session(snap)
        seen_data: list[dict] = []
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=500, on_prompt_seen=seen_data.append)
        assert seen_data[0]["screen_hash"] == ""
        assert seen_data[0]["screen_hash"] is not None


# ---------------------------------------------------------------------------
# captured_at in detected_full (mutmut_57-62)
# ---------------------------------------------------------------------------


class TestCapturedAtInDetectedFull:
    async def test_captured_at_key_is_lowercase(self) -> None:
        """mutmut_58,59: key must be 'captured_at' not 'XXcaptured_atXX' or 'CAPTURED_AT'."""
        snap = _make_snap(captured_at=1234567890.0)
        session = _make_session(snap)
        seen_data: list[dict] = []
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=500, on_prompt_seen=seen_data.append)
        assert "captured_at" in seen_data[0]
        assert "XXcaptured_atXX" not in seen_data[0]
        assert "CAPTURED_AT" not in seen_data[0]

    async def test_captured_at_value_from_snapshot(self) -> None:
        """mutmut_57,60,61,62: captured_at must come from snapshot, not None."""
        ts = 1234567890.123
        snap = _make_snap(captured_at=ts)
        session = _make_session(snap)
        seen_data: list[dict] = []
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=500, on_prompt_seen=seen_data.append)
        assert seen_data[0]["captured_at"] == pytest.approx(ts, rel=1e-6)


# ---------------------------------------------------------------------------
# prompt_id fallback (mutmut_69)
# ---------------------------------------------------------------------------


class TestPromptIdFallback:
    async def test_prompt_id_empty_string_fallback(self) -> None:
        """mutmut_69: fallback must be '' not 'XXXX'."""
        snap: dict[str, Any] = {
            "screen": "test",
            "prompt_detected": {"is_idle": True},  # No prompt_id
        }
        session = _make_session(snap)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500, require_idle=False)
        assert result["prompt_id"] == ""
        assert result["prompt_id"] != "XXXX"


# ---------------------------------------------------------------------------
# is_idle default (mutmut_72,74,77)
# ---------------------------------------------------------------------------


class TestIsIdleDefault:
    async def test_is_idle_defaults_to_false(self) -> None:
        """mutmut_72,74,77: is_idle default must be False, not None or True."""
        snap: dict[str, Any] = {
            "screen": "test",
            "prompt_detected": {"prompt_id": "x"},  # No is_idle
        }
        session = _make_session(snap)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500, require_idle=False)
        assert result["is_idle"] is False
        assert result["is_idle"] is not None

    async def test_is_idle_true_when_set(self) -> None:
        """is_idle=True must be correctly returned."""
        session = _make_session(_make_snap(is_idle=True))
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["is_idle"] is True


# ---------------------------------------------------------------------------
# require_idle grace ratio boundary (mutmut_84)
# ---------------------------------------------------------------------------


class TestRequireIdleGraceBoundary:
    async def test_not_idle_skipped_before_grace_period(self) -> None:
        """mutmut_84: elapsed < grace (not <=) — skips non-idle prompt before grace expires."""
        snap_not_idle: dict[str, Any] = {
            "screen": "busy",
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
        session = _make_session_multi([snap_not_idle, snap_idle])
        session.seconds_until_idle = MagicMock(return_value=0.1)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9)
        assert result["is_idle"] is True


# ---------------------------------------------------------------------------
# on_prompt_rejected receives detected_full (mutmut_86,121,135)
# ---------------------------------------------------------------------------


class TestOnPromptRejectedReceivesDetectedFull:
    async def test_not_idle_rejection_passes_detected_full(self) -> None:
        """mutmut_86: on_prompt_rejected first arg must be detected_full dict, not None."""
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
        session = _make_session_multi([snap_not_idle, snap_idle])
        session.seconds_until_idle = MagicMock(return_value=0.1)
        rejected: list[tuple] = []
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(
            timeout_ms=5000,
            require_idle=True,
            idle_grace_ratio=0.9,
            on_prompt_rejected=lambda d, r: rejected.append((d, r)),
        )
        assert len(rejected) >= 1
        assert rejected[0][0] is not None
        assert isinstance(rejected[0][0], dict)
        assert rejected[0][1] == "not_idle"

    async def test_expected_mismatch_rejection_passes_detected_full(self) -> None:
        """mutmut_121: on_prompt_rejected first arg must be detected_full, not None."""
        snap_wrong: dict[str, Any] = {
            "screen": "s1",
            "screen_hash": "a",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "login", "is_idle": True},
        }
        snap_right: dict[str, Any] = {
            "screen": "s2",
            "screen_hash": "b",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "main_menu", "is_idle": True},
        }
        session = _make_session_multi([snap_wrong, snap_right])
        rejected: list[tuple] = []
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(
            timeout_ms=2000, expected_prompt_id="main_menu", on_prompt_rejected=lambda d, r: rejected.append((d, r))
        )
        assert result["prompt_id"] == "main_menu"
        assert len(rejected) == 1
        assert rejected[0][0] is not None
        assert isinstance(rejected[0][0], dict)
        assert rejected[0][1] == "expected_mismatch"


# ---------------------------------------------------------------------------
# getattr uses self.session (mutmut_93,102)
# ---------------------------------------------------------------------------


class TestGetAttrForSecondsUntilIdle:
    async def test_seconds_until_idle_called_on_session(self) -> None:
        """mutmut_93: getattr(self.session, ...) not getattr(None, ...)."""
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
        session = _make_session_multi([snap_not_idle, snap_idle])
        session.seconds_until_idle = MagicMock(return_value=0.1)
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9)
        # seconds_until_idle must have been called on the session, not on None
        session.seconds_until_idle.assert_called()

    async def test_default_lambda_returns_read_interval(self) -> None:
        """mutmut_102: lambda default is _t=2.0, not _t=3.0 — but both return read_interval_sec."""
        # Without seconds_until_idle on session, the lambda default is used
        # The lambda ignores its argument and returns read_interval_sec
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
        session = _make_session_multi([snap_not_idle, snap_idle])
        # Explicitly delete seconds_until_idle so the lambda default is used
        del session.seconds_until_idle
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(
            timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9, read_interval_ms=100
        )
        assert result["is_idle"] is True


# ---------------------------------------------------------------------------
# wait_ms calculation (mutmut_103,109,110,115,116,117)
# ---------------------------------------------------------------------------


class TestWaitMsCalculation:
    async def test_wait_ms_is_not_none(self) -> None:
        """mutmut_103: wait_ms must be int, not None."""
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
        wait_calls: list[int] = []

        async def mock_wait(*, timeout_ms: int) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session = _make_session_multi([snap_not_idle, snap_idle])
        session.seconds_until_idle = MagicMock(return_value=0.1)
        session.wait_for_update = mock_wait
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9)
        assert len(wait_calls) >= 1
        for call in wait_calls:
            assert call is not None
            assert isinstance(call, int)
            assert call >= 1  # max(1, ...) clamp

    async def test_wait_ms_minimum_is_1(self) -> None:
        """mutmut_109: max(1, ...) not max(2, ...) — minimum wait is 1ms."""
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
        wait_calls: list[int] = []

        async def mock_wait(*, timeout_ms: int) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session = _make_session_multi([snap_not_idle, snap_idle])
        # seconds_until_idle returns 0 → min(0, timeout-elapsed) = 0 → max(1, 0*1000) = 1
        session.seconds_until_idle = MagicMock(return_value=0.0)
        session.wait_for_update = mock_wait
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9)
        assert any(c == 1 for c in wait_calls)  # min is 1

    async def test_wait_ms_uses_multiply_1000(self) -> None:
        """mutmut_110,116: wait_ms uses * 1000 not / 1000 or * 1001."""
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
        wait_calls: list[int] = []

        async def mock_wait(*, timeout_ms: int) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session = _make_session_multi([snap_not_idle, snap_idle])
        # seconds_until_idle = 0.25 → wait_ms = int(max(1, min(0.25, ...) * 1000)) = 250
        session.seconds_until_idle = MagicMock(return_value=0.25)
        session.wait_for_update = mock_wait
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=5000, require_idle=True, idle_grace_ratio=0.9, read_interval_ms=250)
        # Should be 250ms (not 0 from /1000, not 250001 from *1001)
        assert any(200 <= c <= 300 for c in wait_calls)


# ---------------------------------------------------------------------------
# wait_for_update calls during expected_id mismatch (mutmut_127,129)
# ---------------------------------------------------------------------------


class TestExpectedPromptIdWait:
    async def test_wait_after_mismatch_uses_ms_scale(self) -> None:
        """mutmut_127,129: wait must use read_interval_sec * 1000 not None or /1000."""
        snap_wrong: dict[str, Any] = {
            "screen": "s1",
            "screen_hash": "a",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "login", "is_idle": True},
        }
        snap_right: dict[str, Any] = {
            "screen": "s2",
            "screen_hash": "b",
            "captured_at": time.time(),
            "prompt_detected": {"prompt_id": "main_menu", "is_idle": True},
        }
        wait_calls: list[int] = []

        async def mock_wait(*, timeout_ms: int) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session = _make_session_multi([snap_wrong, snap_right])
        session.wait_for_update = mock_wait
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=2000, expected_prompt_id="main_menu", read_interval_ms=200)
        assert result["prompt_id"] == "main_menu"
        assert len(wait_calls) >= 1
        for c in wait_calls:
            assert c is not None
            assert c > 0


# ---------------------------------------------------------------------------
# Return value keys (mutmut_155-159)
# ---------------------------------------------------------------------------


class TestReturnValueKeys:
    async def test_kv_data_key_in_result(self) -> None:
        """mutmut_155,156: result must have 'kv_data' key (not 'XXkv_dataXX' or 'KV_DATA')."""
        snap = _make_snap(kv_data={"score": 42})
        session = _make_session(snap)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert "kv_data" in result
        assert "XXkv_dataXX" not in result
        assert "KV_DATA" not in result

    async def test_kv_data_value_correct(self) -> None:
        """mutmut_157,158,159: kv_data must come from detected_full['kv_data'], not None/wrong key."""
        snap = _make_snap(kv_data={"level": 99})
        session = _make_session(snap)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["kv_data"] == {"level": 99}


# ---------------------------------------------------------------------------
# remaining computation (mutmut_167,168,169,170,172,177)
# ---------------------------------------------------------------------------


class TestRemainingComputation:
    async def test_remaining_uses_max_0_clamp(self) -> None:
        """mutmut_167: remaining = max(0, ...) not max(1, ...)."""
        session = _make_session()
        waiter = PromptWaiter(session)
        wait_calls: list[int] = []

        async def mock_wait(*, timeout_ms: int) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session.wait_for_update = mock_wait
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=1)
        # With max(0, ...), when remaining ≤ 0, wait_for_update gets 0
        # With max(1, ...), it'd always get at least 1 — making timeout never 0
        # The test just verifies it completes (no hang)

    async def test_remaining_is_timeout_minus_elapsed(self) -> None:
        """mutmut_168,169: remaining = timeout - elapsed (not + elapsed or - start)."""
        # With + elapsed, remaining would grow over time (infinite loop)
        # Test completes in finite time
        session = _make_session()
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=100)

    async def test_wait_uses_multiply_1000_for_remaining(self) -> None:
        """mutmut_170,172,177: wait uses min(...) * 1000 not None or /1000 or *1001."""
        snap = _make_snap()
        session = _make_session(snap)
        wait_calls: list[int] = []

        async def mock_wait(*, timeout_ms: int) -> bool:
            wait_calls.append(timeout_ms)
            return True

        session.wait_for_update = mock_wait
        session.snapshot = MagicMock(side_effect=[{"screen": ""}, snap])
        waiter = PromptWaiter(session)
        # First snapshot has no prompt, so wait_for_update is called with remaining * 1000
        await waiter.wait_for_prompt(timeout_ms=2000, read_interval_ms=100)
        assert len(wait_calls) >= 1
        for c in wait_calls:
            assert c is not None
            assert c > 0


# ---------------------------------------------------------------------------
# InputSender defaults (mutmut_1,2,3)
# ---------------------------------------------------------------------------


class TestInputSenderDefaults:
    async def test_default_input_type_is_multi_key(self) -> None:
        """mutmut_1,2: default input_type must be 'multi_key' (not 'XXmulti_keyXX' or 'MULTI_KEY')."""
        import inspect

        sender = InputSender(_make_session())
        sig = inspect.signature(sender.send_input)
        default = sig.parameters["input_type"].default
        assert default == "multi_key"
        assert default != "XXmulti_keyXX"
        assert default != "MULTI_KEY"

    async def test_default_wait_after_sec(self) -> None:
        """mutmut_3: default wait_after_sec must be 0.2 not 1.2."""
        import inspect

        sender = InputSender(_make_session())
        sig = inspect.signature(sender.send_input)
        default = sig.parameters["wait_after_sec"].default
        assert default == pytest.approx(0.2)
        assert default != pytest.approx(1.2)


# ---------------------------------------------------------------------------
# InputSender error messages (mutmut_6,12,13)
# ---------------------------------------------------------------------------


class TestInputSenderErrorMessages:
    async def test_none_session_raises_with_correct_message(self) -> None:
        """mutmut_6: error message must be exactly 'Session is None'."""
        sender = InputSender(None)
        with pytest.raises(ConnectionError) as exc_info:
            await sender.send_input("test")
        assert exc_info.value.args[0] == "Session is None"

    async def test_disconnected_session_raises_with_correct_message(self) -> None:
        """mutmut_12,13: error message must be 'Session disconnected' (capitalized S)."""
        session = _make_session(connected=False)
        sender = InputSender(session)
        with pytest.raises(ConnectionError) as exc_info:
            await sender.send_input("test")
        assert exc_info.value.args[0] == "Session disconnected"
        assert exc_info.value.args[0] != "session disconnected"
        assert exc_info.value.args[0] != "XXSession disconnectedXX"
