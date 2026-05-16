#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.io — PromptWaiter and InputSender primitives."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.io import InputSender, PromptWaiter, _session_is_connected

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    connected: bool = True,
    snapshot: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock session conforming to the Session protocol."""
    session = MagicMock()
    session.is_connected = MagicMock(return_value=connected)
    session.snapshot = MagicMock(return_value=snapshot or {"screen": ""})
    session.wait_for_update = AsyncMock(return_value=True)
    session.send = AsyncMock()
    return session


def _session_with_prompt(
    prompt_id: str = "main_menu",
    input_type: str = "multi_key",
    screen: str = "Choose an option:",
    is_idle: bool = True,
    **kv_data: Any,
) -> MagicMock:
    """Build a session whose snapshot contains a detected prompt."""
    snap: dict[str, Any] = {
        "screen": screen,
        "screen_hash": "abc123",
        "captured_at": time.time(),
        "prompt_detected": {
            "prompt_id": prompt_id,
            "input_type": input_type,
            "is_idle": is_idle,
            **({"kv_data": kv_data} if kv_data else {}),
        },
    }
    return _make_session(snapshot=snap)


# ---------------------------------------------------------------------------
# _session_is_connected
# ---------------------------------------------------------------------------


class TestSessionIsConnected:
    async def test_no_is_connected_attribute_returns_true(self) -> None:
        session = MagicMock(spec=[])
        assert await _session_is_connected(session) is True

    async def test_sync_is_connected_true(self) -> None:
        session = MagicMock()
        session.is_connected = MagicMock(return_value=True)
        assert await _session_is_connected(session) is True

    async def test_sync_is_connected_false(self) -> None:
        session = MagicMock()
        session.is_connected = MagicMock(return_value=False)
        assert await _session_is_connected(session) is False

    async def test_async_is_connected_true(self) -> None:
        session = MagicMock()
        session.is_connected = AsyncMock(return_value=True)
        assert await _session_is_connected(session) is True

    async def test_async_is_connected_false(self) -> None:
        session = MagicMock()
        session.is_connected = AsyncMock(return_value=False)
        assert await _session_is_connected(session) is False

    async def test_property_style_is_connected(self) -> None:
        """is_connected as a non-callable property (bool value)."""
        session = MagicMock()
        session.is_connected = True
        assert await _session_is_connected(session) is True

    async def test_property_style_is_connected_false(self) -> None:
        session = MagicMock()
        session.is_connected = False
        assert await _session_is_connected(session) is False


# ---------------------------------------------------------------------------
# PromptWaiter — session errors
# ---------------------------------------------------------------------------


class TestPromptWaiterErrors:
    async def test_none_session_raises_connection_error(self) -> None:
        waiter = PromptWaiter(session=None)
        with pytest.raises(ConnectionError, match="Session is None"):
            await waiter.wait_for_prompt(timeout_ms=100)

    async def test_disconnected_session_raises_connection_error(self) -> None:
        session = _make_session(connected=False)
        waiter = PromptWaiter(session=session)
        with pytest.raises(ConnectionError, match="disconnected"):
            await waiter.wait_for_prompt(timeout_ms=100)

    async def test_timeout_when_no_prompt_detected(self) -> None:
        session = _make_session(snapshot={"screen": "loading..."})
        waiter = PromptWaiter(session=session)
        with pytest.raises(TimeoutError, match="No prompt detected"):
            await waiter.wait_for_prompt(timeout_ms=100, read_interval_ms=50)


# ---------------------------------------------------------------------------
# PromptWaiter — on_screen_update callback
# ---------------------------------------------------------------------------


class TestPromptWaiterOnScreenUpdate:
    async def test_on_screen_update_fires_each_poll(self) -> None:
        session = _session_with_prompt()
        seen_screens: list[str] = []
        waiter = PromptWaiter(session=session, on_screen_update=seen_screens.append)
        await waiter.wait_for_prompt(timeout_ms=500)
        assert len(seen_screens) >= 1
        assert "Choose an option:" in seen_screens[0]

    async def test_on_screen_update_not_called_when_none(self) -> None:
        """No callback set — should not raise."""
        session = _session_with_prompt()
        waiter = PromptWaiter(session=session, on_screen_update=None)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["prompt_id"] == "main_menu"


# ---------------------------------------------------------------------------
# PromptWaiter — prompt matching logic
# ---------------------------------------------------------------------------


class TestPromptWaiterMatching:
    async def test_basic_prompt_detection(self) -> None:
        session = _session_with_prompt(prompt_id="login", input_type="single_key")
        waiter = PromptWaiter(session=session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["prompt_id"] == "login"
        assert result["input_type"] == "single_key"
        assert result["is_idle"] is True
        assert "screen" in result

    async def test_expected_prompt_id_filter(self) -> None:
        session = _session_with_prompt(prompt_id="main_menu")
        waiter = PromptWaiter(session=session)
        result = await waiter.wait_for_prompt(
            expected_prompt_id="main_menu",
            timeout_ms=500,
        )
        assert result["prompt_id"] == "main_menu"

    async def test_expected_prompt_id_mismatch_times_out(self) -> None:
        session = _session_with_prompt(prompt_id="login_screen")
        waiter = PromptWaiter(session=session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(
                expected_prompt_id="totally_different",
                timeout_ms=150,
                read_interval_ms=50,
            )

    async def test_on_prompt_detected_callback_accept(self) -> None:
        session = _session_with_prompt(prompt_id="menu")
        waiter = PromptWaiter(session=session)
        result = await waiter.wait_for_prompt(
            on_prompt_detected=lambda d: True,
            timeout_ms=500,
        )
        assert result["prompt_id"] == "menu"

    async def test_on_prompt_detected_callback_reject_times_out(self) -> None:
        session = _session_with_prompt(prompt_id="menu")
        waiter = PromptWaiter(session=session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(
                on_prompt_detected=lambda d: False,
                timeout_ms=150,
                read_interval_ms=50,
            )

    async def test_on_prompt_seen_fires(self) -> None:
        session = _session_with_prompt(prompt_id="cmd")
        seen: list[dict[str, Any]] = []
        waiter = PromptWaiter(session=session)
        await waiter.wait_for_prompt(
            on_prompt_seen=seen.append,
            timeout_ms=500,
        )
        assert len(seen) >= 1
        assert seen[0]["prompt_id"] == "cmd"

    async def test_on_prompt_rejected_fires_on_callback_reject(self) -> None:
        session = _session_with_prompt(prompt_id="menu")
        rejected: list[tuple[dict[str, Any], str]] = []
        waiter = PromptWaiter(session=session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(
                on_prompt_detected=lambda d: False,
                on_prompt_rejected=lambda d, reason: rejected.append((d, reason)),
                timeout_ms=150,
                read_interval_ms=50,
            )
        assert any(r == "callback_reject" for _, r in rejected)

    async def test_on_prompt_rejected_fires_on_expected_mismatch(self) -> None:
        session = _session_with_prompt(prompt_id="login")
        rejected: list[tuple[dict[str, Any], str]] = []
        waiter = PromptWaiter(session=session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(
                expected_prompt_id="combat",
                on_prompt_rejected=lambda d, reason: rejected.append((d, reason)),
                timeout_ms=150,
                read_interval_ms=50,
            )
        assert any(r == "expected_mismatch" for _, r in rejected)


# ---------------------------------------------------------------------------
# PromptWaiter — require_idle logic
# ---------------------------------------------------------------------------


class TestPromptWaiterIdleLogic:
    async def test_require_idle_false_accepts_non_idle(self) -> None:
        session = _session_with_prompt(prompt_id="menu", is_idle=False)
        waiter = PromptWaiter(session=session)
        result = await waiter.wait_for_prompt(
            require_idle=False,
            timeout_ms=500,
        )
        assert result["is_idle"] is False
        assert result["prompt_id"] == "menu"

    async def test_require_idle_true_waits_for_idle(self) -> None:
        """Non-idle prompt is rejected until idle_grace_ratio is exceeded."""
        call_count = 0

        def make_snapshot() -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            # After a few calls, make it idle
            is_idle = call_count > 3
            return {
                "screen": "prompt>",
                "screen_hash": "h",
                "captured_at": time.time(),
                "prompt_detected": {
                    "prompt_id": "cmd",
                    "input_type": "multi_key",
                    "is_idle": is_idle,
                },
            }

        session = _make_session()
        session.snapshot = make_snapshot
        session.seconds_until_idle = MagicMock(return_value=0.01)
        waiter = PromptWaiter(session=session)
        result = await waiter.wait_for_prompt(
            require_idle=True,
            timeout_ms=2000,
            read_interval_ms=10,
        )
        assert result["is_idle"] is True
        assert call_count > 3

    async def test_idle_grace_ratio_accepts_non_idle_after_threshold(self) -> None:
        """After idle_grace_ratio fraction of timeout, non-idle is accepted."""
        session = _session_with_prompt(prompt_id="cmd", is_idle=False)
        # Use very short timeout and 0.0 grace ratio so it passes immediately
        waiter = PromptWaiter(session=session)
        result = await waiter.wait_for_prompt(
            require_idle=True,
            idle_grace_ratio=0.0,
            timeout_ms=500,
        )
        assert result["is_idle"] is False

    async def test_on_prompt_rejected_not_idle_reason(self) -> None:
        """Not-idle rejection fires on_prompt_rejected with 'not_idle' reason."""
        call_count = 0
        rejected: list[tuple[dict[str, Any], str]] = []

        def make_snapshot() -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            # Make idle on 4th call
            is_idle = call_count > 3
            return {
                "screen": "prompt>",
                "screen_hash": "h",
                "captured_at": time.time(),
                "prompt_detected": {
                    "prompt_id": "cmd",
                    "input_type": "multi_key",
                    "is_idle": is_idle,
                },
            }

        session = _make_session()
        session.snapshot = make_snapshot
        session.seconds_until_idle = MagicMock(return_value=0.01)
        waiter = PromptWaiter(session=session)
        await waiter.wait_for_prompt(
            require_idle=True,
            on_prompt_rejected=lambda d, reason: rejected.append((d, reason)),
            timeout_ms=2000,
            read_interval_ms=10,
        )
        assert any(r == "not_idle" for _, r in rejected)


# ---------------------------------------------------------------------------
# InputSender — errors
# ---------------------------------------------------------------------------


class TestInputSenderErrors:
    async def test_none_session_raises_connection_error(self) -> None:
        sender = InputSender(session=None)
        with pytest.raises(ConnectionError, match="Session is None"):
            await sender.send_input("hello")

    async def test_disconnected_session_raises_connection_error(self) -> None:
        session = _make_session(connected=False)
        sender = InputSender(session=session)
        with pytest.raises(ConnectionError, match="disconnected"):
            await sender.send_input("hello")


# ---------------------------------------------------------------------------
# InputSender — input types
# ---------------------------------------------------------------------------


class TestInputSenderTypes:
    async def test_single_key_sends_as_is(self) -> None:
        session = _make_session()
        sender = InputSender(session=session)
        await sender.send_input("Y", input_type="single_key", wait_after_sec=0)
        session.send.assert_awaited_once_with("Y")

    async def test_multi_key_appends_cr(self) -> None:
        session = _make_session()
        sender = InputSender(session=session)
        await sender.send_input("hello", input_type="multi_key", wait_after_sec=0)
        session.send.assert_awaited_once_with("hello\r")

    async def test_any_key_sends_space(self) -> None:
        session = _make_session()
        sender = InputSender(session=session)
        await sender.send_input("ignored", input_type="any_key", wait_after_sec=0)
        session.send.assert_awaited_once_with(" ")

    async def test_unknown_type_treated_as_multi_key(self) -> None:
        session = _make_session()
        sender = InputSender(session=session)
        await sender.send_input("cmd", input_type="weird_type", wait_after_sec=0)
        session.send.assert_awaited_once_with("cmd\r")

    async def test_none_type_treated_as_multi_key(self) -> None:
        session = _make_session()
        sender = InputSender(session=session)
        await sender.send_input("cmd", input_type=None, wait_after_sec=0)
        session.send.assert_awaited_once_with("cmd\r")

    async def test_default_input_type_is_multi_key(self) -> None:
        session = _make_session()
        sender = InputSender(session=session)
        await sender.send_input("test", wait_after_sec=0)
        session.send.assert_awaited_once_with("test\r")

    async def test_wait_after_sec_delays(self) -> None:
        session = _make_session()
        sender = InputSender(session=session)
        start = asyncio.get_event_loop().time()
        await sender.send_input("x", input_type="single_key", wait_after_sec=0.05)
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed >= 0.04  # small tolerance

    async def test_wait_after_sec_zero_skips_sleep(self) -> None:
        session = _make_session()
        sender = InputSender(session=session)
        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await sender.send_input("x", input_type="single_key", wait_after_sec=0)
        mock_sleep.assert_not_awaited()
