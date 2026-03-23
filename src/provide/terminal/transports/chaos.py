#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fault-injection transport wrapper for deterministic resilience testing.

Wraps any :class:`~provide.terminal.transports.base.ConnectionTransport` and
injects disconnects, timeouts, and jitter at configurable intervals.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from typing import Any

from provide.terminal.transports.base import ConnectionTransport


class ChaosTransport(ConnectionTransport):
    """Wraps an inner transport and injects faults deterministically.

    Args:
        inner: The real transport to wrap.
        seed: RNG seed for reproducibility.
        disconnect_every_n_receives: Inject a disconnect every N :meth:`receive` calls (0 = off).
        timeout_every_n_receives: Return empty bytes every N :meth:`receive` calls (0 = off).
        max_jitter_ms: Add up to this many ms of random delay per receive (0 = off).
        label: Label prefix included in injected error messages.
    """

    def __init__(
        self,
        inner: ConnectionTransport,
        *,
        seed: int = 1,
        disconnect_every_n_receives: int = 0,
        timeout_every_n_receives: int = 0,
        max_jitter_ms: int = 0,
        label: str = "chaos",
    ) -> None:
        self._inner = inner
        self._rng = random.Random(int(seed))  # nosec B311 — chaos testing, not crypto
        self._disconnect_n = int(disconnect_every_n_receives or 0)
        self._timeout_n = int(timeout_every_n_receives or 0)
        self._max_jitter_ms = int(max_jitter_ms or 0)
        self._label = str(label or "chaos")
        self._rx_count = 0

    async def connect(self, host: str, port: int, **kwargs: Any) -> None:
        await self._inner.connect(host, port, **kwargs)

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    async def send(self, data: bytes) -> None:
        await self._inner.send(data)

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        self._rx_count += 1

        if self._max_jitter_ms > 0:
            await asyncio.sleep(self._rng.uniform(0.0, float(self._max_jitter_ms)) / 1000.0)

        if self._disconnect_n > 0 and (self._rx_count % self._disconnect_n) == 0:
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                await self._inner.disconnect()
            raise ConnectionError(f"{self._label}: injected disconnect on receive #{self._rx_count}")

        if self._timeout_n > 0 and (self._rx_count % self._timeout_n) == 0:
            await asyncio.sleep(max(0.0, float(timeout_ms)) / 1000.0)
            return b""

        return await self._inner.receive(max_bytes=max_bytes, timeout_ms=timeout_ms)

    def is_connected(self) -> bool:
        return self._inner.is_connected()
