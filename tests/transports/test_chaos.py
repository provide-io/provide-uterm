#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for ChaosTransport fault-injection wrapper."""

from __future__ import annotations

from provide.terminal.transports.base import ConnectionTransport
from provide.terminal.transports.chaos import ChaosTransport


class StubTransport(ConnectionTransport):
    """Simple in-memory transport stub for testing."""

    def __init__(self, responses: list[bytes] | None = None) -> None:
        self._connected = True
        self._sent: list[bytes] = []
        self._responses = responses or []
        self._rx_index = 0

    async def connect(self, host: str, port: int, **kwargs: object) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send(self, data: bytes) -> None:
        self._sent.append(data)

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        if self._rx_index < len(self._responses):
            resp = self._responses[self._rx_index]
            self._rx_index += 1
            return resp
        return b""

    def is_connected(self) -> bool:
        return self._connected


class TestChaosTransportPassthrough:
    async def test_passthrough_receive(self) -> None:
        inner = StubTransport(responses=[b"data1", b"data2"])
        chaos = ChaosTransport(inner)
        assert await chaos.receive(128, 100) == b"data1"
        assert await chaos.receive(128, 100) == b"data2"

    async def test_passthrough_send(self) -> None:
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        await chaos.send(b"hello")
        assert inner._sent == [b"hello"]

    def test_is_connected_delegates(self) -> None:
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos.is_connected() is True
        inner._connected = False
        assert chaos.is_connected() is False


class TestChaosTransportDisconnect:
    async def test_disconnect_injection(self) -> None:
        inner = StubTransport(responses=[b"ok"] * 10)
        chaos = ChaosTransport(inner, seed=1, disconnect_every_n_receives=3)
        results = []
        for _ in range(5):
            try:
                data = await chaos.receive(128, 100)
                results.append(("ok", data))
            except ConnectionError as exc:
                results.append(("err", str(exc)))

        errors = [r for r in results if r[0] == "err"]
        assert len(errors) >= 1
        assert "injected disconnect" in errors[0][1]

    async def test_timeout_injection_returns_empty(self) -> None:
        inner = StubTransport(responses=[b"data"] * 10)
        chaos = ChaosTransport(inner, seed=1, timeout_every_n_receives=2)
        # Second receive should return empty (timeout)
        await chaos.receive(128, 1)  # receive 1
        result = await chaos.receive(128, 1)  # receive 2 — injected timeout
        assert result == b""


class TestChaosTransportJitter:
    async def test_jitter_does_not_break_data(self) -> None:
        inner = StubTransport(responses=[b"abc"])
        chaos = ChaosTransport(inner, seed=42, max_jitter_ms=1)
        data = await chaos.receive(128, 100)
        assert data == b"abc"


class TestChaosTransportConnectDisconnect:
    async def test_connect_delegates_to_inner(self) -> None:
        inner = StubTransport()
        inner._connected = False
        chaos = ChaosTransport(inner)
        await chaos.connect("127.0.0.1", 9999)
        assert inner._connected is True

    async def test_disconnect_delegates_to_inner(self) -> None:
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert inner._connected is True
        await chaos.disconnect()
        assert inner._connected is False
