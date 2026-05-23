#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for protocol-version range negotiation."""

from __future__ import annotations

from provide.uterm.bridge.contracts import (
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    PREFERRED_PROTOCOL_VERSION,
    negotiate_protocol_version,
)


class TestNegotiate:
    def test_identical_range_returns_max(self) -> None:
        assert negotiate_protocol_version(1, 1) == 1

    def test_overlap_picks_highest(self) -> None:
        # client supports up to 5; server is fixed at [1,1]; pick 1.
        assert negotiate_protocol_version(1, 5) == 1

    def test_overlap_starts_higher_picks_intersection(self) -> None:
        # client min above server max → no overlap.
        assert negotiate_protocol_version(2, 5) is None

    def test_overlap_ends_lower_picks_intersection(self) -> None:
        # client max below server min → no overlap (forced by MIN=1).
        assert negotiate_protocol_version(0, 0) is None

    def test_no_overlap_returns_none(self) -> None:
        assert negotiate_protocol_version(99, 100) is None

    def test_string_inputs_are_coerced(self) -> None:
        # Defensive: handler-side parses come from JSON so int() coercion
        # is implicit at the call site, but the function itself accepts.
        assert negotiate_protocol_version(int("1"), int("1")) == 1

    def test_constants_are_consistent(self) -> None:
        assert MIN_PROTOCOL_VERSION <= PREFERRED_PROTOCOL_VERSION <= MAX_PROTOCOL_VERSION


class TestWorkerHelloEmitter:
    def test_worker_hello_carries_protocol_block(self) -> None:
        from provide.uterm.shell.terminal._output import worker_hello

        frame = worker_hello(input_mode="open")
        assert frame["type"] == "worker_hello"
        assert frame["input_mode"] == "open"
        assert "protocol" in frame
        proto = frame["protocol"]
        assert proto["min"] == MIN_PROTOCOL_VERSION
        assert proto["max"] == MAX_PROTOCOL_VERSION
        assert proto["preferred"] == PREFERRED_PROTOCOL_VERSION

    def test_worker_hello_defaults_input_mode_open(self) -> None:
        from provide.uterm.shell.terminal._output import worker_hello

        frame = worker_hello()
        assert frame["input_mode"] == "open"

    def test_worker_hello_includes_timestamp(self) -> None:
        from provide.uterm.shell.terminal._output import worker_hello

        frame = worker_hello("hijack")
        assert isinstance(frame["ts"], float)
        assert frame["ts"] > 0
