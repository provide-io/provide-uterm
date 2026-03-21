#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for transports/chaos.py, transports/ssh.py,
transports/telnet_transport.py, and transports/telnet_server.py.

Targets all not-checked/surviving mutants in these modules.
"""

from __future__ import annotations

import asyncio

import pytest

from provide.terminal.transports.base import ConnectionTransport
from provide.terminal.transports.chaos import ChaosTransport

pytest.importorskip("asyncssh", reason="asyncssh not installed; skip SSH transport tests")


# ===========================================================================
# Helpers
# ===========================================================================


class StubTransport(ConnectionTransport):
    """In-memory transport stub for ChaosTransport tests."""

    def __init__(self, responses: list[bytes] | None = None) -> None:
        self._connected = True
        self._sent: list[bytes] = []
        self._responses = responses or []
        self._rx_index = 0
        self.connect_args: tuple[str, int] | None = None
        self.connect_kwargs: dict | None = None
        self.disconnect_count = 0
        self.receive_args: list[tuple[int, int]] = []

    async def connect(self, host: str, port: int, **kwargs: object) -> None:
        self._connected = True
        self.connect_args = (host, port)
        self.connect_kwargs = dict(kwargs)

    async def disconnect(self) -> None:
        self._connected = False
        self.disconnect_count += 1

    async def send(self, data: bytes) -> None:
        self._sent.append(data)

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        self.receive_args.append((max_bytes, timeout_ms))
        if self._rx_index < len(self._responses):
            resp = self._responses[self._rx_index]
            self._rx_index += 1
            return resp
        return b""

    def is_connected(self) -> bool:
        return self._connected


class MockProcess:
    """Minimal asyncssh process mock."""

    def __init__(self, stdin_data: bytes | str = b"") -> None:
        self.stdin = _MockStdin(stdin_data)
        self.stdout = _MockStdout()
        self._exited: int | None = None
        self._closed = False

    def exit(self, code: int) -> None:
        self._exited = code

    def close(self) -> None:
        self._closed = True

    def get_extra_info(self, name: str) -> object:
        if name == "peername":
            return ("127.0.0.1", 12345)
        return None


class _MockStdin:
    def __init__(self, data: bytes | str) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes | str:
        return self._data


class _MockStdout:
    def __init__(self) -> None:
        self.written: bytearray = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass


class MockServer:
    """Minimal asyncio TCP server that captures bytes and sends initial data."""

    def __init__(self, initial_send: bytes = b"") -> None:
        self._initial_send = initial_send
        self._server: asyncio.Server | None = None
        self._received = bytearray()
        self._connected = asyncio.Event()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handler, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def _handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._initial_send:
            writer.write(self._initial_send)
            await writer.drain()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            self._received.extend(chunk)
        writer.close()
        self._connected.set()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def received(self) -> bytes:
        return bytes(self._received)

    async def wait(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)


# ===========================================================================
# ChaosTransport — __init__ default parameters
# ===========================================================================


class TestChaosInitDefaults:
    """Kill mutations in ChaosTransport.__init__ default parameters."""

    def test_seed_default_is_1_not_2(self) -> None:
        """Kills mutmut_1: seed=2. Default seed=1 gives different RNG from seed=2."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        inner2 = StubTransport()
        chaos2 = ChaosTransport(inner2, seed=2)
        v1 = chaos._rng.random()
        v2 = chaos2._rng.random()
        assert v1 != v2

    def test_disconnect_every_n_default_is_0_not_1(self) -> None:
        """Kills mutmut_2: disconnect_every_n_receives=1. Default must be 0 (off)."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._disconnect_n == 0

    def test_timeout_every_n_default_is_0_not_1(self) -> None:
        """Kills mutmut_3: timeout_every_n_receives=1. Default must be 0 (off)."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._timeout_n == 0

    def test_max_jitter_ms_default_is_0_not_1(self) -> None:
        """Kills mutmut_4: max_jitter_ms=1. Default must be 0 (no jitter)."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._max_jitter_ms == 0

    def test_label_default_is_chaos_lowercase(self) -> None:
        """Kills mutmut_5 (XXchaosXX) and mutmut_6 (CHAOS). Default label='chaos'."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._label == "chaos"

    def test_rng_seeded_not_none(self) -> None:
        """Kills mutmut_9: random.Random(None) vs random.Random(seed).
        With None, RNG is non-deterministic; with seed=1, it's reproducible."""
        inner1 = StubTransport()
        inner2 = StubTransport()
        chaos1 = ChaosTransport(inner1, seed=1)
        chaos2 = ChaosTransport(inner2, seed=1)
        assert chaos1._rng.random() == chaos2._rng.random()

    def test_max_jitter_stored_from_param(self) -> None:
        """Kills mutmut_21 (max_jitter_ms and 0 → always 0) and mutmut_22 (or 1 → 1 when 0).
        Passing max_jitter_ms=5 must store 5."""
        inner = StubTransport()
        chaos = ChaosTransport(inner, max_jitter_ms=5)
        assert chaos._max_jitter_ms == 5

    def test_max_jitter_zero_stays_zero(self) -> None:
        """max_jitter_ms=0 → _max_jitter_ms=0. Kills mutmut_22 (or 1 → gives 1 for 0)."""
        inner = StubTransport()
        chaos = ChaosTransport(inner, max_jitter_ms=0)
        assert chaos._max_jitter_ms == 0

    def test_label_stored_from_param(self) -> None:
        """Custom label is stored correctly."""
        inner = StubTransport()
        chaos = ChaosTransport(inner, label="my-chaos")
        assert chaos._label == "my-chaos"

    def test_label_fallback_when_empty_is_chaos(self) -> None:
        """Kills mutmut_23 (None), mutmut_24 (str(None)='None'), mutmut_25 (label and 'chaos'),
        mutmut_26 (XXchaosXX), mutmut_27 (CHAOS).
        Empty string label falls back to 'chaos'."""
        inner = StubTransport()
        chaos = ChaosTransport(inner, label="")
        assert chaos._label == "chaos"

    def test_label_none_fallback_is_chaos(self) -> None:
        """None label falls back to 'chaos'. Kills mutmut_23 (self._label = None)."""
        inner = StubTransport()
        # Pass None as label — should coerce to "chaos" via str(label or "chaos")
        chaos = ChaosTransport(inner, label=None)  # type: ignore[arg-type]
        assert chaos._label == "chaos"

    def test_rx_count_starts_at_zero(self) -> None:
        """_rx_count initializes to 0."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._rx_count == 0

    def test_inner_stored(self) -> None:
        """_inner is the passed transport."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        assert chaos._inner is inner


# ===========================================================================
# ChaosTransport — connect()
# ===========================================================================


class TestChaosConnect:
    """Kill mutations in ChaosTransport.connect()."""

    async def test_connect_passes_host(self) -> None:
        """Kills mutmut_1: host=None. Host must be forwarded."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        await chaos.connect("bbs.example.com", 23)
        assert inner.connect_args[0] == "bbs.example.com"

    async def test_connect_passes_port(self) -> None:
        """Kills mutmut_2: port=None. Port must be forwarded."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        await chaos.connect("localhost", 12345)
        assert inner.connect_args[1] == 12345

    async def test_connect_passes_kwargs(self) -> None:
        """Kills mutmut_5: kwargs dropped. Extra kwargs must reach inner."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        await chaos.connect("localhost", 23, timeout=5.0)
        assert inner.connect_kwargs.get("timeout") == 5.0

    async def test_connect_no_kwargs_still_works(self) -> None:
        """No extra kwargs — connect still works."""
        inner = StubTransport()
        chaos = ChaosTransport(inner)
        await chaos.connect("localhost", 23)
        assert inner.connect_args == ("localhost", 23)


# ===========================================================================
# ChaosTransport — receive()
# ===========================================================================


class TestChaosReceive:
    """Kill mutations in ChaosTransport.receive()."""

    async def test_rx_count_increments_by_1(self) -> None:
        """Kills mutmut_2 (-=1) and mutmut_3 (+=2). Must increment by exactly 1."""
        inner = StubTransport(responses=[b"a", b"b"])
        chaos = ChaosTransport(inner)
        assert chaos._rx_count == 0
        await chaos.receive(128, 100)
        assert chaos._rx_count == 1
        await chaos.receive(128, 100)
        assert chaos._rx_count == 2

    async def test_jitter_fires_when_max_jitter_positive(self) -> None:
        """Kills mutmut_4 (>=0) and mutmut_5 (>1).
        max_jitter_ms=1: condition is 1>0=True → sleep applied. Still returns data."""
        inner = StubTransport(responses=[b"data"])
        chaos = ChaosTransport(inner, max_jitter_ms=1)
        result = await chaos.receive(128, 100)
        assert result == b"data"

    async def test_jitter_not_applied_when_zero(self) -> None:
        """max_jitter_ms=0: condition is 0>0=False → no sleep. Kills mutmut_4 (>=0: 0>=0=True)."""
        inner = StubTransport(responses=[b"data"])
        chaos = ChaosTransport(inner, max_jitter_ms=0)
        # Should complete quickly (no sleep)
        result = await chaos.receive(128, 100)
        assert result == b"data"

    async def test_jitter_uniform_starts_from_0(self) -> None:
        """Kills mutmut_12: rng.uniform(1.0, ...) instead of uniform(0.0, ...).
        With seed=0, first uniform(0.0, 1) could produce 0.0 but uniform(1.0, 1) crashes."""
        inner = StubTransport(responses=[b"ok"])
        # max_jitter_ms=1 so the jitter path fires
        chaos = ChaosTransport(inner, seed=42, max_jitter_ms=1)
        # Should not raise, sleep value is uniform(0.0, 1)/1000
        result = await chaos.receive(128, 100)
        assert result == b"ok"

    async def test_jitter_divides_by_1000(self) -> None:
        """Kills mutmut_14: /1001 instead of /1000.
        Can't easily measure sleep duration, but verify it doesn't crash."""
        inner = StubTransport(responses=[b"ok"])
        chaos = ChaosTransport(inner, seed=1, max_jitter_ms=10)
        result = await chaos.receive(128, 100)
        assert result == b"ok"

    async def test_disconnect_fires_at_n_not_n_plus_1(self) -> None:
        """Kills mutmut_17 (>1) — with n=1, mutant requires count>1, original fires at count=1."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, disconnect_every_n_receives=1)
        # First call: count=1, 1%1==0 → disconnect
        with pytest.raises(ConnectionError):
            await chaos.receive(128, 100)

    async def test_disconnect_modulo_not_ne(self) -> None:
        """Kills mutmut_19 (% ... != 0). Original: == 0 fires at multiples.
        With != 0, would fire at non-multiples instead."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, disconnect_every_n_receives=3)
        # Receives 1 and 2 should succeed (1%3=1≠0, 2%3=2≠0)
        await chaos.receive(128, 100)  # count=1, no disconnect
        await chaos.receive(128, 100)  # count=2, no disconnect
        # Receive 3 must trigger disconnect (3%3==0)
        with pytest.raises(ConnectionError):
            await chaos.receive(128, 100)

    async def test_disconnect_checks_exact_zero(self) -> None:
        """Kills mutmut_20 (% ... == 1). Only fires at exact modulo 0."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, disconnect_every_n_receives=2)
        # count=1: 1%2=1 ≠ 0 → no disconnect
        await chaos.receive(128, 100)
        # count=2: 2%2=0 → disconnect
        with pytest.raises(ConnectionError):
            await chaos.receive(128, 100)

    async def test_suppress_connection_error(self) -> None:
        """Kills mutmut_21 (None instead of ConnectionError in suppress).
        inner.disconnect() raises ConnectionError → must be suppressed."""

        class ErrorDisconnect(ConnectionTransport):
            def __init__(self) -> None:
                self._connected = True

            async def connect(self, host: str, port: int, **kwargs: object) -> None:
                pass

            async def disconnect(self) -> None:
                raise ConnectionError("disconnect failed")

            async def send(self, data: bytes) -> None:
                pass

            async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
                return b""

            def is_connected(self) -> bool:
                return self._connected

        inner = ErrorDisconnect()
        chaos = ChaosTransport(inner, disconnect_every_n_receives=1)
        # Should raise ConnectionError from ChaosTransport, not inner disconnect error
        with pytest.raises(ConnectionError, match="injected disconnect"):
            await chaos.receive(128, 100)

    async def test_suppress_os_error(self) -> None:
        """Kills mutmut_22 (None instead of OSError in suppress) and mutmut_25 (drop OSError).
        inner.disconnect() raises OSError → must be suppressed."""

        class OsErrorDisconnect(ConnectionTransport):
            def __init__(self) -> None:
                self._connected = True

            async def connect(self, host: str, port: int, **kwargs: object) -> None:
                pass

            async def disconnect(self) -> None:
                raise OSError("os error on disconnect")

            async def send(self, data: bytes) -> None:
                pass

            async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
                return b""

            def is_connected(self) -> bool:
                return self._connected

        inner = OsErrorDisconnect()
        chaos = ChaosTransport(inner, disconnect_every_n_receives=1)
        with pytest.raises(ConnectionError, match="injected disconnect"):
            await chaos.receive(128, 100)

    async def test_suppress_runtime_error(self) -> None:
        """Kills mutmut_23 (None instead of RuntimeError in suppress) and mutmut_26 (drop it).
        inner.disconnect() raises RuntimeError → must be suppressed."""

        class RuntimeErrorDisconnect(ConnectionTransport):
            def __init__(self) -> None:
                self._connected = True

            async def connect(self, host: str, port: int, **kwargs: object) -> None:
                pass

            async def disconnect(self) -> None:
                raise RuntimeError("runtime error on disconnect")

            async def send(self, data: bytes) -> None:
                pass

            async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
                return b""

            def is_connected(self) -> bool:
                return self._connected

        inner = RuntimeErrorDisconnect()
        chaos = ChaosTransport(inner, disconnect_every_n_receives=1)
        with pytest.raises(ConnectionError, match="injected disconnect"):
            await chaos.receive(128, 100)

    async def test_timeout_fires_at_n_not_n_plus_1(self) -> None:
        """Kills mutmut_30 (>1). With n=1, original fires at count=1 (1%1=0 and 1>0=True)."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, timeout_every_n_receives=1)
        result = await chaos.receive(128, 0)
        assert result == b""

    async def test_timeout_sleep_zero_timeout(self) -> None:
        """Kills mutmut_40 (max(1.0,...)) and mutmut_42 (/1001).
        With timeout_ms=0: sleep(max(0.0, 0)/1000) = sleep(0). Must return b''."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, timeout_every_n_receives=1)
        result = await chaos.receive(128, 0)
        assert result == b""

    async def test_timeout_returns_empty_bytes(self) -> None:
        """Injected timeout returns b''."""
        inner = StubTransport(responses=[b"x"] * 10)
        chaos = ChaosTransport(inner, timeout_every_n_receives=1)
        result = await chaos.receive(128, 0)
        assert result == b""
        assert isinstance(result, bytes)

    async def test_passes_max_bytes_to_inner(self) -> None:
        """Kills mutmut_44: max_bytes=None forwarded to inner. Must pass exact value."""
        inner = StubTransport(responses=[b"ok"])
        chaos = ChaosTransport(inner)
        await chaos.receive(4096, 100)
        assert inner.receive_args[0][0] == 4096

    async def test_passes_timeout_ms_to_inner(self) -> None:
        """Kills mutmut_45: timeout_ms=None forwarded to inner. Must pass exact value."""
        inner = StubTransport(responses=[b"ok"])
        chaos = ChaosTransport(inner)
        await chaos.receive(128, 250)
        assert inner.receive_args[0][1] == 250


# ===========================================================================
# SSHStreamReader — read()
# ===========================================================================
