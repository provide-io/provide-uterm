#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Reconnection helpers for uterm transport sessions.

This module provides a lightweight wrapper that reconnects a transport session
when operations fail with known transport errors. Reconnection is limited to
rebuilding the session object and re-running a caller-provided hook; it does not
implement application-level relogin.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias, TypeVar

from provide.uterm.defaults import TerminalDefaults
from provide.uterm.transport_session import TransportSession

_WebsocketsConnectionClosed: type[BaseException] | None
try:  # pragma: no cover - optional dependency in environments without websockets
    from websockets.exceptions import ConnectionClosed as _ImportedWebsocketsConnectionClosed

    _WebsocketsConnectionClosed = _ImportedWebsocketsConnectionClosed
except Exception:  # pragma: no cover - optional dependency in environments without websockets
    _WebsocketsConnectionClosed = None

__all__ = [
    "OnReconnect",
    "ReconnectPolicy",
    "ReconnectingSession",
    "connect_with_reconnect",
    "connect_with_retries",
]

T = TypeVar("T")


def _policy_delay(policy: ReconnectPolicy, attempt: int) -> float:
    """Compute bounded exponential backoff for a one-based attempt number."""
    power = max(attempt - 1, 0)
    return float(min(policy.base_backoff_s * (2**power), policy.max_backoff_s))


@dataclass(frozen=True)
class ReconnectPolicy:
    """Retry budget and backoff for reconnect attempts."""

    max_retries: int = TerminalDefaults.RECONNECT_MAX_RETRIES
    base_backoff_s: float = TerminalDefaults.RECONNECT_BASE_BACKOFF_S
    max_backoff_s: float = TerminalDefaults.RECONNECT_MAX_BACKOFF_S


OnReconnect: TypeAlias = Callable[[TransportSession], Awaitable[None]]
SessionFactory: TypeAlias = Callable[[], Awaitable[TransportSession]]

_DEFAULT_RECONNECT_POLICY = ReconnectPolicy()


class ReconnectingSession:
    """Proxy session that transparently reconnects on transport drop."""

    def __init__(
        self,
        session: TransportSession,
        *,
        connect: SessionFactory,
        policy: ReconnectPolicy,
        on_reconnect: OnReconnect | None,
    ) -> None:
        self._session = session
        self._connect = connect
        self._policy = policy
        self._on_reconnect = on_reconnect

    async def reconnect(self) -> None:
        """Rebuild the live session and run the reconnect hook."""
        await self._reconnect()

    @property
    def session(self) -> TransportSession:
        """Expose the active transport session."""
        return self._session

    def is_connected(self) -> bool:
        return self._session.is_connected()

    async def close(self) -> None:
        await self._session.close()

    def snapshot(self) -> dict[str, Any]:
        return self._session.snapshot()

    def ansi_screen(self) -> str:
        return self._session.ansi_screen()

    def screen_change_seq(self) -> int:
        return self._session.screen_change_seq()

    update_seq = screen_change_seq

    def add_watch(self, *args: Any, **kwargs: Any) -> None:
        self._session.add_watch(*args, **kwargs)

    async def send(self, data: str) -> None:
        await self._run_with_reconnect(lambda session: session.send(data))

    async def wait_for_update(self, *, timeout_ms: int, since: int | None = None) -> bool:
        return await self._run_with_reconnect(
            lambda session: session.wait_for_update(timeout_ms=timeout_ms, since=since)
        )

    async def wait_for_screen_change(self, *, timeout_ms: int = 5000, since: int | None = None) -> bool:
        return await self._run_with_reconnect(
            lambda session: session.wait_for_screen_change(timeout_ms=timeout_ms, since=since),
        )

    async def _run_with_reconnect(self, op: Callable[[TransportSession], Awaitable[T]]) -> T:
        retries = 0
        while True:
            try:
                return await op(self._session)
            except Exception as exc:
                if not self._is_retryable_error(exc):
                    raise
                if retries >= self._policy.max_retries:
                    with contextlib.suppress(Exception):
                        await self._session.close()
                    raise ConnectionError("reconnect retries exhausted") from exc
                retries += 1
                await self._reconnect(attempt=retries)

    async def _reconnect(self, *, attempt: int = 1) -> None:
        try:
            await self._session.close()
        except Exception:
            pass

        if self._policy.base_backoff_s > 0:
            delay = _policy_delay(self._policy, attempt)
            if delay > 0:
                await asyncio.sleep(delay)

        self._session = await self._connect_with_retries()
        if self._on_reconnect is not None:
            await self._on_reconnect(self._session)

    async def _connect_with_retries(self) -> TransportSession:
        last_error: Exception | None = None
        retries = 0
        while True:
            try:
                return await self._connect()
            except Exception as exc:
                last_error = exc
                if retries >= self._policy.max_retries:
                    break
                retries += 1
                await asyncio.sleep(_policy_delay(self._policy, retries))
        assert last_error is not None
        raise ConnectionError("reconnect retries exhausted") from last_error

    def _is_retryable_error(self, exc: Exception) -> bool:
        retryable: tuple[type[BaseException], ...] = (ConnectionError, OSError)
        if _WebsocketsConnectionClosed is not None:
            retryable = (*retryable, _WebsocketsConnectionClosed)
        return isinstance(exc, retryable)


async def connect_with_reconnect(
    connect: SessionFactory,
    *,
    policy: ReconnectPolicy = _DEFAULT_RECONNECT_POLICY,
    on_reconnect: OnReconnect | None = None,
) -> ReconnectingSession:
    """Connect once and wrap the session with automatic transport reconnect."""
    first_session = await connect_with_retries(connect=connect, policy=policy)
    return ReconnectingSession(
        first_session,
        connect=connect,
        policy=policy,
        on_reconnect=on_reconnect,
    )


async def connect_with_retries(connect: SessionFactory, *, policy: ReconnectPolicy) -> TransportSession:
    """Connect via ``connect`` with an exponential backoff budget."""
    retries = 0
    while True:
        try:
            return await connect()
        except Exception as exc:
            if retries >= policy.max_retries:
                raise ConnectionError("connect retries exhausted") from exc
            retries += 1
            await asyncio.sleep(_policy_delay(policy, retries))
