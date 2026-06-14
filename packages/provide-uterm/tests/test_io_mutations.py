#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for provide.uterm.io (part 1).

Classes: TestWaitForPromptDefaults, TestTimeoutComputation, TestLoopCondition,
         TestConnectionErrors, TestScreenFromSnapshot, TestDetectedFullFields,
         TestPromptIdAndIsIdle.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.io import (
    DEFAULT_PROMPT_IDLE_GRACE_RATIO,
    DEFAULT_PROMPT_READ_INTERVAL_MS,
    DEFAULT_PROMPT_REQUIRE_IDLE,
    DEFAULT_PROMPT_TIMEOUT_MS,
    PromptWaiter,
)

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
            **({"kv_data": kv_data} if kv_data else {}),
        },
    }
    session = MagicMock()
    session.is_connected = MagicMock(return_value=True)
    session.snapshot = MagicMock(return_value=snap)
    session.wait_for_update = AsyncMock(return_value=True)
    session.send = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Default parameter mutations (mutmut_1, 2, 3, 4)
# ---------------------------------------------------------------------------


class TestWaitForPromptDefaults:
    async def test_default_timeout_is_10000ms(self) -> None:
        """Default timeout remains 10000ms even though it is applied in-body."""
        assert DEFAULT_PROMPT_TIMEOUT_MS == 10000

    async def test_default_read_interval_is_250ms(self) -> None:
        """Default read interval remains 250ms even though it is applied in-body."""
        assert DEFAULT_PROMPT_READ_INTERVAL_MS == 250

    async def test_default_require_idle_is_true(self) -> None:
        """Default idle gating remains enabled even though it is applied in-body."""
        assert DEFAULT_PROMPT_REQUIRE_IDLE is True

    async def test_default_idle_grace_ratio_is_0_8(self) -> None:
        """Default idle grace ratio remains 0.8 even though it is applied in-body."""
        assert pytest.approx(0.8) == DEFAULT_PROMPT_IDLE_GRACE_RATIO


# ---------------------------------------------------------------------------
# Computation of timeout_sec (mutmut_8, 10, 11)
# ---------------------------------------------------------------------------


class TestTimeoutComputation:
    async def test_timeout_uses_1000_divisor(self) -> None:
        """mutmut_8: timeout_ms / 1000.0, not 1001.0."""
        session = _session_with_prompt(is_idle=True)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["prompt_id"] == "main_menu"

    async def test_read_interval_uses_1000_divisor(self) -> None:
        """mutmut_10,11: read_interval_ms / 1000.0, not * 1000.0 or / 1001.0."""
        session = _session_with_prompt(is_idle=True)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500, read_interval_ms=100)
        assert result["prompt_id"] == "main_menu"


# ---------------------------------------------------------------------------
# Loop condition (mutmut_13: < vs <=)
# ---------------------------------------------------------------------------


class TestLoopCondition:
    async def test_loop_exits_after_timeout(self) -> None:
        """mutmut_13: while < (not <=) timeout_sec — exits when time is up."""
        session = _make_session()
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError, match="No prompt detected"):
            await waiter.wait_for_prompt(timeout_ms=50)


# ---------------------------------------------------------------------------
# ConnectionError messages (mutmut_16, 22, 23)
# ---------------------------------------------------------------------------


class TestConnectionErrors:
    async def test_session_none_raises_connection_error(self) -> None:
        """mutmut_16: ConnectionError message must be 'Session is None'."""
        waiter = PromptWaiter(None)
        with pytest.raises(ConnectionError, match="Session is None"):
            await waiter.wait_for_prompt(timeout_ms=5000)

    async def test_session_none_error_message_exact_case(self) -> None:
        """mutmut_16: message must be exactly 'Session is None' (capitalized)."""
        waiter = PromptWaiter(None)
        with pytest.raises(ConnectionError) as exc_info:
            await waiter.wait_for_prompt(timeout_ms=5000)
        assert exc_info.value.args[0] == "Session is None"

    async def test_disconnected_session_raises_connection_error(self) -> None:
        """mutmut_22: ConnectionError message must be 'Session disconnected'."""
        session = _make_session(connected=False)
        waiter = PromptWaiter(session)
        with pytest.raises(ConnectionError, match="Session disconnected"):
            await waiter.wait_for_prompt(timeout_ms=5000)

    async def test_disconnected_error_message_capitalized(self) -> None:
        """mutmut_22,23: 'Session disconnected' with capital S."""
        session = _make_session(connected=False)
        waiter = PromptWaiter(session)
        with pytest.raises(ConnectionError) as exc_info:
            await waiter.wait_for_prompt(timeout_ms=5000)
        assert exc_info.value.args[0] == "Session disconnected"
        assert exc_info.value.args[0] != "session disconnected"


# ---------------------------------------------------------------------------
# Screen value from snapshot (mutmut_28, 30, 33)
# ---------------------------------------------------------------------------


class TestScreenFromSnapshot:
    async def test_screen_defaults_to_empty_string(self) -> None:
        """mutmut_28,30,33: screen must default to '' not None or 'XXXX'."""
        snap: dict[str, Any] = {"prompt_detected": {"prompt_id": "x", "is_idle": True}}
        session = _make_session(snapshot=snap)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["screen"] == ""
        assert result["screen"] is not None

    async def test_screen_from_snapshot_is_used(self) -> None:
        """Screen value from snapshot must appear in result."""
        session = _session_with_prompt(screen="Hello World")
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["screen"] == "Hello World"


# ---------------------------------------------------------------------------
# detected_full field assignments (mutmut_44, 45, 46, 47, 48, 49, 50-56, 57-62)
# ---------------------------------------------------------------------------


class TestDetectedFullFields:
    async def test_screen_key_in_detected_full(self) -> None:
        """mutmut_44,45,46: 'screen' key must be set in detected_full, not 'XXscreenXX' or None."""
        session = _session_with_prompt(screen="Test screen content")
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert "screen" in result
        assert result["screen"] == "Test screen content"

    async def test_screen_hash_key_in_result_via_callback(self) -> None:
        """mutmut_47,48,49: 'screen_hash' must be set in detected_full used by on_prompt_seen."""
        session = _session_with_prompt(screen_hash="deadbeef12345678")
        seen_data = []
        waiter = PromptWaiter(session)

        def on_seen(data: dict) -> None:
            seen_data.append(data)

        await waiter.wait_for_prompt(timeout_ms=500, on_prompt_seen=on_seen)
        assert len(seen_data) == 1
        assert seen_data[0].get("screen_hash") == "deadbeef12345678"
        assert "XXscreen_hashXX" not in seen_data[0]
        assert "SCREEN_HASH" not in seen_data[0]

    async def test_screen_hash_default_empty_string(self) -> None:
        """mutmut_51,52,53,54,55,56: screen_hash missing from snapshot defaults to ''."""
        snap: dict[str, Any] = {
            "screen": "test",
            "prompt_detected": {"prompt_id": "x", "is_idle": True},
        }
        session = _make_session(snapshot=snap)
        seen_data = []
        waiter = PromptWaiter(session)

        def on_seen(data: dict) -> None:
            seen_data.append(data)

        await waiter.wait_for_prompt(timeout_ms=500, on_prompt_seen=on_seen)
        assert seen_data[0].get("screen_hash") == ""
        assert seen_data[0].get("screen_hash") is not None

    async def test_captured_at_set_from_snapshot(self) -> None:
        """mutmut_57,58,59,60,61,62: captured_at must come from snapshot."""
        ts = time.time()
        session = _session_with_prompt(captured_at=ts)
        seen_data = []
        waiter = PromptWaiter(session)

        def on_seen(data: dict) -> None:
            seen_data.append(data)

        await waiter.wait_for_prompt(timeout_ms=500, on_prompt_seen=on_seen)
        assert seen_data[0].get("captured_at") == pytest.approx(ts, rel=1e-3)
        assert "XXcaptured_atXX" not in seen_data[0]
        assert "CAPTURED_AT" not in seen_data[0]


# ---------------------------------------------------------------------------
# prompt_id and is_idle from detected (mutmut_69, 72, 74, 77)
# ---------------------------------------------------------------------------


class TestPromptIdAndIsIdle:
    async def test_prompt_id_in_result(self) -> None:
        """mutmut_69: prompt_id must be from detected, not 'XXXX' fallback."""
        session = _session_with_prompt(prompt_id="login_screen")
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["prompt_id"] == "login_screen"

    async def test_prompt_id_empty_string_fallback_is_empty(self) -> None:
        """mutmut_69: fallback for missing prompt_id must be '' not 'XXXX'."""
        snap: dict[str, Any] = {
            "screen": "test",
            "prompt_detected": {"is_idle": True},
        }
        session = _make_session(snapshot=snap)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["prompt_id"] == ""
        assert result["prompt_id"] != "XXXX"

    async def test_is_idle_defaults_to_false_not_none_or_true(self) -> None:
        """mutmut_72,74,77: is_idle default must be False, not None or True."""
        snap: dict[str, Any] = {
            "screen": "test",
            "prompt_detected": {"prompt_id": "x"},
        }
        session = _make_session(snapshot=snap)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500, require_idle=False)
        assert result["is_idle"] is False
        assert result["is_idle"] is not None

    async def test_is_idle_true_when_set(self) -> None:
        """is_idle=True must be returned correctly."""
        session = _session_with_prompt(is_idle=True)
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["is_idle"] is True
