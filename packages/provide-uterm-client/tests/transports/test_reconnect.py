#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from provide.uterm.transports import reconnect


class _FakeSession:
    def __init__(self, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.closed = False
        self.send_calls: list[str] = []
        self.wait_calls = 0

    def is_connected(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True

    def snapshot(self) -> dict[str, object]:
        return {"screen": "".join(self.send_calls)}

    def ansi_screen(self) -> str:
        return str(self.send_calls)

    def screen_change_seq(self) -> int:
        return len(self.send_calls)

    def add_watch(self, _callback: Callable[[dict[str, object], bytes], None]) -> None:
        return None

    async def send(self, data: str) -> None:
        self.send_calls.append(data)
        if self.fail_send:
            raise ConnectionError("transport dropped")

    async def wait_for_update(self, *, timeout_ms: int, since: int | None = None) -> bool:
        self.wait_calls += 1
        return since is not None and self.screen_change_seq() > since

    async def wait_for_screen_change(self, *, timeout_ms: int, since: int | None = None) -> bool:
        self.wait_calls += 1
        return since is not None and self.screen_change_seq() > since


class _CloseFailSession(_FakeSession):
    async def close(self) -> None:
        self.closed = True
        raise RuntimeError("close failed")


async def _sleep_noop(_seconds: float) -> None:
    return None


async def test_connect_with_reconnect_without_drop() -> None:
    session = _FakeSession()
    session_factory = AsyncMock(return_value=session)

    on_reconnect = AsyncMock()
    rs = await reconnect.connect_with_reconnect(session_factory, on_reconnect=on_reconnect)
    await rs.send("hello")

    assert session_factory.call_count == 1
    assert session.send_calls == ["hello"]
    on_reconnect.assert_not_awaited()
    assert rs.snapshot() == {"screen": "hello"}


async def test_send_reconnects_and_calls_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    fail_once = _FakeSession(fail_send=True)
    recovered = _FakeSession()
    calls = 0

    async def _factory() -> _FakeSession:
        nonlocal calls
        calls += 1
        if calls == 1:
            return fail_once
        return recovered

    monkeypatch.setattr(reconnect.asyncio, "sleep", _sleep_noop)
    hook = AsyncMock()
    rs = await reconnect.connect_with_reconnect(_factory, on_reconnect=hook)
    await rs.send("A")

    assert calls == 2
    assert hook.await_count == 1
    hook.assert_awaited_once_with(recovered)
    assert fail_once.closed is True
    assert recovered.send_calls == ["A"]


async def test_send_reconnect_retries_exhausted() -> None:
    fail_session_1 = _FakeSession(fail_send=True)
    fail_session_2 = _FakeSession(fail_send=True)
    calls = 0

    async def _factory() -> _FakeSession:
        nonlocal calls
        calls += 1
        if calls == 1:
            return fail_session_1
        return fail_session_2

    policy = reconnect.ReconnectPolicy(max_retries=1, base_backoff_s=0.0)
    rs = await reconnect.connect_with_reconnect(_factory, policy=policy)

    with pytest.raises(ConnectionError, match="reconnect retries exhausted"):
        await rs.send("x")

    assert calls == 2
    assert fail_session_1.closed is True
    assert fail_session_2.closed is True


async def test_connect_with_retries_backoff_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    sleep_calls: list[float] = []

    async def _factory() -> _FakeSession:
        nonlocal call_count
        call_count += 1
        if call_count < 4:
            raise RuntimeError("connection unavailable")
        return session

    call_count = 0

    async def _sleep(duration: float) -> None:
        sleep_calls.append(duration)

    monkeypatch.setattr(reconnect.asyncio, "sleep", _sleep)
    policy = reconnect.ReconnectPolicy(max_retries=3, base_backoff_s=0.5, max_backoff_s=1.0)
    got = await reconnect.connect_with_retries(_factory, policy=policy)

    assert got is session
    assert call_count == 4
    assert sleep_calls == [0.5, 1.0, 1.0]


async def test_proxy_methods_delegate_to_active_session() -> None:
    session = _FakeSession()
    rs = await reconnect.connect_with_reconnect(AsyncMock(return_value=session))

    def watch(_snapshot: object, _data: object) -> None:
        return None

    assert rs.session is session
    assert rs.is_connected() is True
    rs.add_watch(watch)
    await rs.send("abc")

    assert await rs.wait_for_update(timeout_ms=10, since=0) is True
    assert await rs.wait_for_screen_change(timeout_ms=10, since=0) is True
    assert rs.ansi_screen() == "['abc']"
    assert rs.screen_change_seq() == 1
    assert rs.update_seq() == 1

    await rs.close()
    assert session.closed is True


async def test_public_reconnect_suppresses_close_error(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _CloseFailSession()
    second = _FakeSession()
    calls = 0

    async def _factory() -> _FakeSession:
        nonlocal calls
        calls += 1
        return first if calls == 1 else second

    monkeypatch.setattr(reconnect.asyncio, "sleep", _sleep_noop)
    rs = await reconnect.connect_with_reconnect(_factory)
    await rs.reconnect()

    assert first.closed is True
    assert rs.session is second


async def test_non_retryable_operation_error_is_not_reconnected() -> None:
    session = _FakeSession()

    async def _bad_op(_session: _FakeSession) -> None:
        raise ValueError("not a transport failure")

    rs = await reconnect.connect_with_reconnect(AsyncMock(return_value=session))

    with pytest.raises(ValueError, match="not a transport failure"):
        await rs._run_with_reconnect(_bad_op)


async def test_reconnect_exhaustion_from_connect_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(fail_send=True)
    attempts = 0

    async def _factory() -> _FakeSession:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return session
        raise OSError("dial failed")

    monkeypatch.setattr(reconnect.asyncio, "sleep", _sleep_noop)
    policy = reconnect.ReconnectPolicy(max_retries=1, base_backoff_s=0.0)
    rs = await reconnect.connect_with_reconnect(_factory, policy=policy)

    with pytest.raises(ConnectionError, match="reconnect retries exhausted"):
        await rs.send("x")


async def test_reconnect_zero_computed_delay_skips_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _FakeSession()
    second = _FakeSession()
    calls = 0
    sleep = AsyncMock()

    async def _factory() -> _FakeSession:
        nonlocal calls
        calls += 1
        return first if calls == 1 else second

    monkeypatch.setattr(reconnect.asyncio, "sleep", sleep)
    policy = reconnect.ReconnectPolicy(max_retries=1, base_backoff_s=0.5, max_backoff_s=0.0)
    rs = await reconnect.connect_with_reconnect(_factory, policy=policy)
    await rs.reconnect()

    sleep.assert_not_awaited()
    assert rs.session is second


async def test_connect_with_retries_exhausted() -> None:
    calls = 0

    async def _factory() -> _FakeSession:
        nonlocal calls
        calls += 1
        raise OSError("unreachable")

    policy = reconnect.ReconnectPolicy(max_retries=1, base_backoff_s=0.0)

    with pytest.raises(ConnectionError, match="connect retries exhausted"):
        await reconnect.connect_with_retries(_factory, policy=policy)

    assert calls == 2


def test_retryable_error_without_websockets_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    rs = reconnect.ReconnectingSession(
        _FakeSession(),
        connect=AsyncMock(return_value=_FakeSession()),
        policy=reconnect.ReconnectPolicy(max_retries=0),
        on_reconnect=None,
    )
    monkeypatch.setattr(reconnect, "_WebsocketsConnectionClosed", None)

    assert rs._is_retryable_error(ConnectionError("drop")) is True
    assert rs._is_retryable_error(ValueError("bad")) is False


def test_policy_defaults_track_terminal_defaults() -> None:
    """The reconnect backoff budget is centralised in TerminalDefaults."""
    from provide.uterm.defaults import TerminalDefaults

    policy = reconnect.ReconnectPolicy()
    assert policy.max_retries == TerminalDefaults.RECONNECT_MAX_RETRIES
    assert policy.base_backoff_s == TerminalDefaults.RECONNECT_BASE_BACKOFF_S
    assert policy.max_backoff_s == TerminalDefaults.RECONNECT_MAX_BACKOFF_S
