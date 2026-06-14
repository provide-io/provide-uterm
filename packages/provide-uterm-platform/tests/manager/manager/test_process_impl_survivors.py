#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Focused survivor tests for ``provide.uterm.manager.process_impl``."""

from __future__ import annotations

import asyncio
import signal
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from provide.uterm.manager import process_impl
from provide.uterm.manager.process import AgentProcessManager


def test_resolve_stop_pid_process_none_defaults_to_zero() -> None:
    assert AgentProcessManager._resolve_stop_pid(None, None) == 0


def test_resolve_stop_pid_non_int_explicit_pid_defaults_to_zero() -> None:
    assert AgentProcessManager._resolve_stop_pid(None, "123") == 0  # type: ignore[arg-type]


def test_signal_posix_process_group_uses_resolved_pgid_and_signal() -> None:
    with (
        patch.object(process_impl.os, "getpgid", return_value=4321) as getpgid,
        patch.object(process_impl.os, "killpg") as killpg,
    ):
        AgentProcessManager._signal_posix_process_group(1234, signal.SIGTERM)

    getpgid.assert_called_once_with(1234)
    killpg.assert_called_once_with(4321, signal.SIGTERM)


def test_try_set_subreaper_calls_prctl_with_exact_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    cdll_calls: list[tuple[str, bool]] = []
    prctl_calls: list[tuple[object, ...]] = []

    class FakeLibc:
        def prctl(self, *args: object) -> int:
            prctl_calls.append(args)
            return 0

    def fake_cdll(name: str, *, use_errno: bool = False) -> FakeLibc:
        cdll_calls.append((name, use_errno))
        return FakeLibc()

    monkeypatch.setattr(process_impl.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "ctypes", SimpleNamespace(CDLL=fake_cdll))

    fake_logger = MagicMock()
    monkeypatch.setattr(process_impl, "logger", fake_logger)
    AgentProcessManager._try_set_subreaper()

    assert cdll_calls == [("libc.so.6", True)]
    assert prctl_calls == [(36, 1, 0, 0, 0)]
    fake_logger.debug.assert_called_once_with("subreaper_set")


async def test_wait_for_process_exit_awaits_coroutine_wait_with_timeout() -> None:
    calls: list[str] = []

    class AsyncWaitProcess:
        async def wait(self) -> None:
            calls.append("waited")

    await AgentProcessManager._wait_for_process_exit(AsyncWaitProcess(), 0.5)  # type: ignore[arg-type]

    assert calls == ["waited"]


async def test_wait_for_process_exit_coroutine_wait_uses_direct_coroutine_branch() -> None:
    class AsyncWaitProcess:
        async def wait(self) -> None:
            return None

    with patch.object(process_impl.asyncio, "get_running_loop") as get_running_loop:
        await AgentProcessManager._wait_for_process_exit(AsyncWaitProcess(), 0.5)  # type: ignore[arg-type]

    get_running_loop.assert_not_called()


async def test_wait_for_process_exit_coroutine_wait_passes_timeout() -> None:
    class AsyncWaitProcess:
        async def wait(self) -> None:
            return None

    real_wait_for = asyncio.wait_for
    timeouts: list[float | None] = []

    async def wait_for_spy(awaitable: object, *, timeout: float | None = None) -> object:
        timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)  # type: ignore[arg-type]

    with patch.object(process_impl.asyncio, "wait_for", side_effect=wait_for_spy):
        await AgentProcessManager._wait_for_process_exit(AsyncWaitProcess(), 0.5)  # type: ignore[arg-type]

    assert timeouts == [0.5]


async def test_wait_for_process_exit_awaits_awaitable_result_from_sync_wait() -> None:
    calls: list[str] = []

    async def inner_wait() -> None:
        calls.append("awaited")

    class AwaitableReturningProcess:
        def wait(self) -> object:
            return inner_wait()

    await AgentProcessManager._wait_for_process_exit(AwaitableReturningProcess(), 0.5)  # type: ignore[arg-type]

    assert calls == ["awaited"]


async def test_wait_for_process_exit_applies_timeout_to_awaitable_result() -> None:
    class AwaitableSentinel:
        def __await__(self) -> object:
            if False:
                yield None
            return None

    class AwaitableReturningProcess:
        def wait(self) -> object:
            return AwaitableSentinel()

    real_wait_for = asyncio.wait_for
    timeouts: list[float | None] = []

    async def wait_for_spy(awaitable: object, *, timeout: float | None = None) -> object:
        timeouts.append(timeout)
        if isinstance(awaitable, asyncio.Future):
            return await real_wait_for(awaitable, timeout=timeout)
        return None

    with patch.object(process_impl.asyncio, "wait_for", side_effect=wait_for_spy):
        await AgentProcessManager._wait_for_process_exit(AwaitableReturningProcess(), 0.5)  # type: ignore[arg-type]

    assert timeouts == [0.5, 0.5]
