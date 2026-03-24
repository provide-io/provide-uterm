#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ChaosTransport (transports/chaos.py).

Targets surviving mutants in __init__ defaults, connect, and receive methods.
"""

from __future__ import annotations

import contextlib

import pytest

from provide.terminal.transports.base import ConnectionTransport
from provide.terminal.transports.chaos import ChaosTransport


class StubTransport(ConnectionTransport):
    """In-memory transport for testing."""

    def __init__(self, responses: list[bytes] | None = None) -> None:
        self._connected = True
        self._sent: list[bytes] = []
        self._responses = responses or []
        self._rx_index = 0
        self.connect_called_with: tuple[str, int] | None = None
        self.disconnect_count = 0

    async def connect(self, host: str, port: int, **kwargs: object) -> None:
        self._connected = True
        self.connect_called_with = (host, port)

    async def disconnect(self) -> None:
        self._connected = False
        self.disconnect_count += 1

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


# ---------------------------------------------------------------------------
# __init__ — default parameter values
# ---------------------------------------------------------------------------


class TestChaosTransportDefaults:
    """Kill mutations that change default parameter values."""

    def test_seed_default_is_1(self) -> None:
        """seed=1 default. Mutant seed=2 produces different RNG sequence."""
        inner1 = StubTransport()
        inner2 = StubTransport()
        # With same seed=1, two ChaosTransports with jitter should produce same sequence
        chaos1 = ChaosTransport(inner1, seed=1, max_jitter_ms=0)
        chaos2 = ChaosTransport(inner2, seed=1, max_jitter_ms=0)
        assert chaos1._rng.random() == chaos2._rng.random()

    def test_seed_default_differs_from_2(self) -> None:
        """Default seed=1 gives different RNG state than seed=2."""
        inner = StubTransport()
        chaos_default = ChaosTransport(inner)
        inner2 = StubTransport()
        chaos_seed2 = ChaosTransport(inner2, seed=2)
        # The first random float should differ between seed=1 and seed=2
        v1 = chaos_default._rng.random()
        v2 = chaos_seed2._rng.random()
        assert v1 != v2

    def test_disconnect_every_n_default_is_0(self) -> None:
        """disconnect_every_n_receives=0 means off (no injected disconnects)."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner)
        assert chaos._disconnect_n == 0

    def test_timeout_every_n_default_is_0(self) -> None:
        """timeout_every_n_receives=0 means off (no injected timeouts)."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner)
        assert chaos._timeout_n == 0

    def test_max_jitter_ms_default_is_0(self) -> None:
        """max_jitter_ms=0 means no jitter added."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._max_jitter_ms == 0

    def test_label_default_is_chaos(self) -> None:
        """label='chaos' default."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._label == "chaos"

    def test_rx_count_starts_at_0(self) -> None:
        """_rx_count starts at 0 (not 1)."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._rx_count == 0

    def test_inner_stored_correctly(self) -> None:
        """_inner is set to the passed-in transport."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._inner is inner

    def test_disconnect_n_set_from_param(self) -> None:
        """disconnect_every_n_receives=1 differs from default 0."""
        inner = StubTransport()
        chaos = ChaosTransport(inner, disconnect_every_n_receives=1)
        assert chaos._disconnect_n == 1

    def test_timeout_n_set_from_param(self) -> None:
        """timeout_every_n_receives=1 differs from default 0."""
        inner = StubTransport()
        chaos = ChaosTransport(inner, timeout_every_n_receives=1)
        assert chaos._timeout_n == 1

    def test_label_set_from_param(self) -> None:
        """Custom label is stored."""
        inner = StubTransport()
        chaos = ChaosTransport(inner, label="test-label")
        assert chaos._label == "test-label"

    def test_label_fallback_when_empty(self) -> None:
        """Empty label falls back to 'chaos'."""
        inner = StubTransport()
        chaos = ChaosTransport(inner, label="")
        assert chaos._label == "chaos"


# ---------------------------------------------------------------------------
# connect — delegates host and port to inner
# ---------------------------------------------------------------------------


class TestChaosTransportConnect:
    async def test_connect_passes_host_and_port(self) -> None:
        """connect(host, port) is forwarded to the inner transport."""
        inner = StubTransport()
        inner._connected = False
        chaos = ChaosTransport(inner)
        await chaos.connect("bbs.example.com", 23)
        assert inner.connect_called_with == ("bbs.example.com", 23)
        assert inner._connected is True

    async def test_connect_passes_correct_host(self) -> None:
        """host argument is passed unchanged."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        await chaos.connect("myhost.test", 9999)
        assert inner.connect_called_with[0] == "myhost.test"

    async def test_connect_passes_correct_port(self) -> None:
        """port argument is passed unchanged."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        await chaos.connect("localhost", 12345)
        assert inner.connect_called_with[1] == 12345


# ---------------------------------------------------------------------------
# receive — rx_count increments correctly
# ---------------------------------------------------------------------------


class TestChaosTransportReceive:
    async def test_rx_count_increments_each_call(self) -> None:
        """_rx_count increments by 1 per receive call (kills +1 mutation)."""
        inner = StubTransport(responses=[b"a", b"b", b"c"])
        chaos = ChaosTransport(inner)
        assert chaos._rx_count == 0
        await chaos.receive(128, 100)
        assert chaos._rx_count == 1
        await chaos.receive(128, 100)
        assert chaos._rx_count == 2
        await chaos.receive(128, 100)
        assert chaos._rx_count == 3

    async def test_disconnect_fires_at_exact_n(self) -> None:
        """disconnect_every_n_receives fires exactly at multiples of n (not n-1 or n+1)."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, disconnect_every_n_receives=3)
        # receive 1: count=1, 1%3!=0 → ok
        await chaos.receive(128, 100)
        # receive 2: count=2, 2%3!=0 → ok
        await chaos.receive(128, 100)
        # receive 3: count=3, 3%3==0 → disconnect injected
        with pytest.raises(ConnectionError, match="injected disconnect on receive #3"):
            await chaos.receive(128, 100)

    async def test_disconnect_not_injected_before_n(self) -> None:
        """No disconnect before reaching the nth receive."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, disconnect_every_n_receives=5)
        # Receives 1-4 should succeed
        for _i in range(4):
            result = await chaos.receive(128, 100)
            assert result == b"x"

    async def test_timeout_fires_at_exact_n(self) -> None:
        """timeout_every_n_receives returns b'' at multiples of n."""
        inner = StubTransport(responses=[b"data"] * 10)
        chaos = ChaosTransport(inner, seed=1, timeout_every_n_receives=2)
        data1 = await chaos.receive(128, 0)
        assert data1 == b"data"
        # receive 2: count=2, 2%2==0 → returns empty
        empty = await chaos.receive(128, 0)
        assert empty == b""

    async def test_timeout_not_before_n(self) -> None:
        """No timeout before reaching the nth receive."""
        inner = StubTransport(responses=[b"data"] * 10)
        chaos = ChaosTransport(inner, timeout_every_n_receives=4)
        for _ in range(3):
            result = await chaos.receive(128, 100)
            assert result == b"data"

    async def test_error_message_contains_receive_number(self) -> None:
        """Error message includes the rx_count and the label."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, label="my-chaos", disconnect_every_n_receives=2)
        await chaos.receive(128, 100)  # count=1
        with pytest.raises(ConnectionError) as exc_info:
            await chaos.receive(128, 100)  # count=2 → disconnect
        assert "my-chaos" in str(exc_info.value)
        assert "#2" in str(exc_info.value)

    async def test_jitter_fires_when_max_jitter_ms_positive(self) -> None:
        """max_jitter_ms > 0 applies jitter delay but still returns data."""
        inner = StubTransport(responses=[b"jittery"])
        chaos = ChaosTransport(inner, seed=1, max_jitter_ms=1)
        data = await chaos.receive(128, 100)
        assert data == b"jittery"

    async def test_jitter_not_applied_when_zero(self) -> None:
        """max_jitter_ms=0 means no sleep (checked via _max_jitter_ms attribute)."""
        inner = StubTransport()
        chaos = ChaosTransport(inner, max_jitter_ms=0)
        assert chaos._max_jitter_ms == 0

    async def test_passes_max_bytes_and_timeout_to_inner(self) -> None:
        """max_bytes and timeout_ms are forwarded to the inner transport."""
        received_args: list[tuple[int, int]] = []

        class TrackingTransport(ConnectionTransport):
            def __init__(self) -> None:
                self._connected = True

            async def connect(self, host: str, port: int, **kwargs: object) -> None:
                pass

            async def disconnect(self) -> None:
                self._connected = False

            async def send(self, data: bytes) -> None:
                pass

            async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
                received_args.append((max_bytes, timeout_ms))
                return b"ok"

            def is_connected(self) -> bool:
                return self._connected

        inner = TrackingTransport()
        chaos = ChaosTransport(inner)
        await chaos.receive(4096, 250)
        assert received_args == [(4096, 250)]

    async def test_timeout_sleep_duration(self) -> None:
        """When timeout is injected, asyncio.sleep is called with timeout_ms/1000."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, timeout_every_n_receives=1)
        # Receive 1 → count=1, 1%1==0 → timeout injected
        # With timeout_ms=0, sleep(0.0) and returns b""
        result = await chaos.receive(128, 0)
        assert result == b""

    async def test_disconnect_calls_inner_disconnect_before_raise(self) -> None:
        """When disconnect is injected, inner.disconnect() is called first."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, disconnect_every_n_receives=1)
        with contextlib.suppress(ConnectionError):
            await chaos.receive(128, 100)
        # inner should have been disconnected
        assert inner._connected is False

    async def test_disconnect_raises_connection_error(self) -> None:
        """Injected disconnect raises ConnectionError (not another exception type)."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, disconnect_every_n_receives=1)
        with pytest.raises(ConnectionError):
            await chaos.receive(128, 100)

    async def test_timeout_returns_empty_bytes_not_none(self) -> None:
        """Injected timeout returns b'' (not None or other falsy value)."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, timeout_every_n_receives=1)
        result = await chaos.receive(128, 100)
        assert result == b""
        assert isinstance(result, bytes)
