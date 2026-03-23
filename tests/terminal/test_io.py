#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for PromptWaiter and InputSender."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from provide.terminal.io import InputSender, PromptWaiter


class MockSession:
    """Minimal session stub for I/O tests."""

    def __init__(
        self,
        snapshots: list[dict[str, Any]] | None = None,
        connected: bool = True,
    ) -> None:
        self._snapshots = snapshots or [{}]
        self._snap_index = 0
        self._connected = connected
        self.sent: list[str] = []

    async def wait_for_update(self, *, timeout_ms: int, since: int | None = None) -> bool:
        await asyncio.sleep(0)
        return True

    def snapshot(self) -> dict[str, Any]:
        if self._snap_index < len(self._snapshots):
            snap = self._snapshots[self._snap_index]
            self._snap_index += 1
            return snap
        return self._snapshots[-1]

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def is_connected(self) -> bool:
        return self._connected


class TestPromptWaiter:
    async def test_returns_when_prompt_detected(self) -> None:
        snap = {
            "screen": "Enter command:",
            "prompt_detected": {"prompt_id": "cmd.prompt", "input_type": "multi_key", "is_idle": True},
        }
        session = MockSession(snapshots=[snap])
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(timeout_ms=500)
        assert result["prompt_id"] == "cmd.prompt"
        assert result["screen"] == "Enter command:"

    async def test_timeout_raises(self) -> None:
        session = MockSession(snapshots=[{"screen": "nothing"}])
        waiter = PromptWaiter(session)
        with pytest.raises(TimeoutError):
            await waiter.wait_for_prompt(timeout_ms=50)

    async def test_none_session_raises(self) -> None:
        waiter = PromptWaiter(None)
        with pytest.raises(ConnectionError, match="Session is None"):
            await waiter.wait_for_prompt(timeout_ms=50)

    async def test_disconnected_session_raises(self) -> None:
        session = MockSession(connected=False)
        waiter = PromptWaiter(session)
        with pytest.raises(ConnectionError, match="disconnected"):
            await waiter.wait_for_prompt(timeout_ms=50)

    async def test_expected_prompt_id_filter(self) -> None:
        wrong = {
            "screen": "wrong screen",
            "prompt_detected": {"prompt_id": "other.prompt", "input_type": "multi_key", "is_idle": True},
        }
        right = {
            "screen": "right screen",
            "prompt_detected": {"prompt_id": "cmd.prompt", "input_type": "multi_key", "is_idle": True},
        }
        session = MockSession(snapshots=[wrong, right])
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(expected_prompt_id="cmd.prompt", timeout_ms=1000)
        assert result["prompt_id"] == "cmd.prompt"

    async def test_expected_prompt_id_mismatch_calls_on_rejected(self) -> None:
        """on_prompt_rejected called with 'expected_mismatch' when prompt_id doesn't match."""
        rejected: list[tuple] = []
        wrong = {
            "screen": "wrong screen",
            "prompt_detected": {"prompt_id": "other.prompt", "input_type": "multi_key", "is_idle": True},
        }
        right = {
            "screen": "right screen",
            "prompt_detected": {"prompt_id": "cmd.prompt", "input_type": "multi_key", "is_idle": True},
        }
        session = MockSession(snapshots=[wrong, right])
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(
            expected_prompt_id="cmd.prompt",
            timeout_ms=1000,
            on_prompt_rejected=lambda d, r: rejected.append((d, r)),
        )
        assert result["prompt_id"] == "cmd.prompt"
        assert len(rejected) >= 1
        assert rejected[0][1] == "expected_mismatch"


class TestInputSender:
    async def test_multi_key_appends_cr(self) -> None:
        session = MockSession()
        sender = InputSender(session)
        await sender.send_input("hello", input_type="multi_key", wait_after_sec=0)
        assert session.sent == ["hello\r"]

    async def test_single_key_no_cr(self) -> None:
        session = MockSession()
        sender = InputSender(session)
        await sender.send_input("x", input_type="single_key", wait_after_sec=0)
        assert session.sent == ["x"]

    async def test_any_key_sends_space(self) -> None:
        session = MockSession()
        sender = InputSender(session)
        await sender.send_input("ignored", input_type="any_key", wait_after_sec=0)
        assert session.sent == [" "]

    async def test_none_session_raises(self) -> None:
        sender = InputSender(None)
        with pytest.raises(ConnectionError):
            await sender.send_input("x", wait_after_sec=0)

    async def test_disconnected_raises(self) -> None:
        session = MockSession(connected=False)
        sender = InputSender(session)
        with pytest.raises(ConnectionError):
            await sender.send_input("x", wait_after_sec=0)


class TestSessionIsConnected:
    async def test_no_is_connected_attr_returns_true(self) -> None:
        from provide.terminal.io import _session_is_connected

        class SessionWithoutChecker:
            pass

        assert await _session_is_connected(SessionWithoutChecker())

    async def test_async_is_connected(self) -> None:
        from provide.terminal.io import _session_is_connected

        class AsyncSession:
            async def is_connected(self) -> bool:
                return True

        assert await _session_is_connected(AsyncSession())

    async def test_callable_is_connected_false(self) -> None:
        from provide.terminal.io import _session_is_connected

        class SyncSession:
            def is_connected(self) -> bool:
                return False

        assert not await _session_is_connected(SyncSession())


class TestPromptWaiterCallbacks:
    async def test_on_screen_update_called(self) -> None:
        screens = []
        snap = {
            "screen": "hello",
            "prompt_detected": {"prompt_id": "p1", "input_type": "multi_key", "is_idle": True},
        }
        session = MockSession(snapshots=[snap])
        waiter = PromptWaiter(session, on_screen_update=screens.append)
        await waiter.wait_for_prompt(timeout_ms=500)
        assert "hello" in screens

    async def test_on_prompt_seen_called(self) -> None:
        seen = []
        snap = {
            "screen": "hello",
            "prompt_detected": {"prompt_id": "p1", "input_type": "multi_key", "is_idle": True},
        }
        session = MockSession(snapshots=[snap])
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(timeout_ms=500, on_prompt_seen=seen.append)
        assert len(seen) == 1
        assert seen[0]["prompt_id"] == "p1"

    async def test_on_prompt_rejected_idle(self) -> None:
        """Not-idle prompt before grace period → on_prompt_rejected called."""
        rejected = []
        # First snap: not idle; second: idle
        snap_not_idle = {
            "screen": "loading",
            "prompt_detected": {"prompt_id": "p1", "input_type": "multi_key", "is_idle": False},
        }
        snap_idle = {
            "screen": "ready",
            "prompt_detected": {"prompt_id": "p1", "input_type": "multi_key", "is_idle": True},
        }
        session = MockSession(snapshots=[snap_not_idle, snap_idle])
        waiter = PromptWaiter(session)
        result = await waiter.wait_for_prompt(
            timeout_ms=1000,
            require_idle=True,
            idle_grace_ratio=0.8,
            on_prompt_rejected=lambda d, r: rejected.append((d, r)),
        )
        assert result["is_idle"] is True

    async def test_on_prompt_detected_false_rejects(self) -> None:
        """on_prompt_detected returning False causes rejection."""
        rejected = []
        snap_reject = {
            "screen": "bad",
            "prompt_detected": {"prompt_id": "p1", "input_type": "multi_key", "is_idle": True},
        }
        snap_accept = {
            "screen": "good",
            "prompt_detected": {"prompt_id": "p1", "input_type": "multi_key", "is_idle": True},
        }
        call_count = [0]

        def detector(d: dict) -> bool:
            call_count[0] += 1
            return call_count[0] >= 2  # accept on second call

        session = MockSession(snapshots=[snap_reject, snap_accept])
        waiter = PromptWaiter(session)
        await waiter.wait_for_prompt(
            timeout_ms=1000,
            on_prompt_detected=detector,
            on_prompt_rejected=lambda d, r: rejected.append((d, r)),
        )
        assert len(rejected) >= 1


class TestInputSenderWaitAfter:
    async def test_wait_after_sec_is_respected(self) -> None:
        import time

        session = MockSession()
        sender = InputSender(session)
        start = time.monotonic()
        await sender.send_input("x", input_type="single_key", wait_after_sec=0.05)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.04  # generous lower bound
