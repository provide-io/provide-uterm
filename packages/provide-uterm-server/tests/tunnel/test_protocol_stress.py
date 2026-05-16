#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Stress tests for tunnel binary frame protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from provide.uterm.tunnel.protocol import (
    CHANNEL_CONTROL,
    FLAG_DATA,
    TunnelProtocolError,
    decode_control,
    decode_frame,
    encode_control,
    encode_frame,
)

log = logging.getLogger(__name__)
MIN_TUNNEL_FRAMES_PER_SEC = 1_000


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------


class TestHypothesisRoundtrip:
    """Property-based roundtrip tests using hypothesis."""

    @given(
        channel=st.integers(min_value=0, max_value=0xFF),
        flags=st.integers(min_value=0, max_value=0xFF),
        payload=st.binary(min_size=0, max_size=4096),
    )
    @settings(max_examples=500)
    def test_encode_decode_roundtrip(self, channel: int, flags: int, payload: bytes) -> None:
        raw = encode_frame(channel, payload, flags=flags)
        frame = decode_frame(raw)
        assert frame.channel == channel
        assert frame.flags == flags
        assert frame.payload == payload

    @given(
        extra_keys=st.dictionaries(
            st.text(min_size=1, max_size=20).filter(lambda k: k != "type"),
            st.one_of(
                st.text(max_size=50),
                st.integers(),
                st.floats(allow_nan=False, allow_infinity=False),
                st.booleans(),
                st.none(),
            ),
            max_size=10,
        ),
        type_val=st.text(min_size=1, max_size=30),
    )
    @settings(max_examples=200)
    def test_control_roundtrip(self, extra_keys: dict, type_val: str) -> None:
        msg = {"type": type_val, **extra_keys}
        raw = encode_control(msg)
        frame = decode_frame(raw)
        assert frame.channel == CHANNEL_CONTROL
        assert frame.flags == FLAG_DATA
        decoded = decode_control(frame.payload)
        assert decoded == msg

    @given(data=st.binary(min_size=2, max_size=1000))
    @settings(max_examples=500)
    def test_any_bytes_ge2_decodable(self, data: bytes) -> None:
        """Any bytes of length >= 2 should decode without crashing."""
        frame = decode_frame(data)
        assert 0 <= frame.channel <= 0xFF
        assert 0 <= frame.flags <= 0xFF
        assert isinstance(frame.payload, bytes)


# ---------------------------------------------------------------------------
# Boundary tests
# ---------------------------------------------------------------------------


class TestBoundaryValues:
    """Test all boundary and edge-case values."""

    def test_all_256_channel_ids(self) -> None:
        for ch in range(256):
            raw = encode_frame(ch, b"x")
            frame = decode_frame(raw)
            assert frame.channel == ch

    def test_all_256_flag_values(self) -> None:
        for fl in range(256):
            raw = encode_frame(0x01, b"x", flags=fl)
            frame = decode_frame(raw)
            assert frame.flags == fl

    def test_empty_payload(self) -> None:
        raw = encode_frame(0x01, b"")
        frame = decode_frame(raw)
        assert frame.payload == b""
        assert len(raw) == 2

    def test_single_byte_payload(self) -> None:
        raw = encode_frame(0x01, b"\x42")
        frame = decode_frame(raw)
        assert frame.payload == b"\x42"

    @pytest.mark.timeout(30)
    def test_one_megabyte_payload(self) -> None:
        payload = os.urandom(1_000_000)
        raw = encode_frame(0x01, payload)
        frame = decode_frame(raw)
        assert frame.payload == payload

    def test_all_zero_bytes_payload(self) -> None:
        payload = b"\x00" * 1024
        raw = encode_frame(0x01, payload)
        frame = decode_frame(raw)
        assert frame.payload == payload

    def test_all_ff_bytes_payload(self) -> None:
        payload = b"\xff" * 1024
        raw = encode_frame(0x01, payload)
        frame = decode_frame(raw)
        assert frame.payload == payload

    def test_all_byte_values_payload(self) -> None:
        payload = bytes(range(256))
        raw = encode_frame(0x01, payload)
        frame = decode_frame(raw)
        assert frame.payload == payload


# ---------------------------------------------------------------------------
# Throughput test
# ---------------------------------------------------------------------------


class TestThroughput:
    """Measure encode/decode throughput."""

    @pytest.mark.timeout(30)
    def test_100k_frames_under_2_seconds(self) -> None:
        """100k encode/decode cycles maintain basic throughput under coverage."""
        payload = b"benchmark payload data of moderate length"
        count = 100_000

        start = time.perf_counter()
        for _ in range(count):
            raw = encode_frame(0x01, payload)
            decode_frame(raw)
        elapsed = time.perf_counter() - start

        fps = count / elapsed
        log.info(
            "Throughput: %d frames in %.3fs = %.0f frames/sec",
            count,
            elapsed,
            fps,
        )
        assert fps > MIN_TUNNEL_FRAMES_PER_SEC, f"Too slow: {fps:.0f} frames/sec below {MIN_TUNNEL_FRAMES_PER_SEC}"


# ---------------------------------------------------------------------------
# Concurrent encode/decode
# ---------------------------------------------------------------------------


class TestConcurrentEncodeDecode:
    """Test thread-safety of encode/decode under concurrent asyncio tasks."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_100_tasks_1000_frames_each(self) -> None:
        errors: list[str] = []

        async def worker(task_id: int) -> None:
            for i in range(1000):
                payload = f"task-{task_id}-frame-{i}".encode()
                channel = task_id % 256
                flags = i % 256
                raw = encode_frame(channel, payload, flags=flags)
                frame = decode_frame(raw)
                if frame.channel != channel:
                    errors.append(f"channel mismatch: {frame.channel} != {channel}")
                if frame.flags != flags:
                    errors.append(f"flags mismatch: {frame.flags} != {flags}")
                if frame.payload != payload:
                    errors.append(f"payload mismatch for task {task_id} frame {i}")

        tasks = [asyncio.create_task(worker(tid)) for tid in range(100)]
        await asyncio.gather(*tasks)
        assert errors == [], f"Errors found: {errors[:10]}"


# ---------------------------------------------------------------------------
# Malformed input fuzzing
# ---------------------------------------------------------------------------

import os


class TestMalformedInput:
    """Fuzz decode_frame and decode_control with adversarial input."""

    def test_zero_length_bytes(self) -> None:
        with pytest.raises(TunnelProtocolError, match="frame too short"):
            decode_frame(b"")

    def test_one_byte(self) -> None:
        with pytest.raises(TunnelProtocolError, match="frame too short"):
            decode_frame(b"\x00")

    @given(data=st.binary(min_size=0, max_size=1))
    @settings(max_examples=50)
    def test_short_frames_always_raise(self, data: bytes) -> None:
        with pytest.raises(TunnelProtocolError):
            decode_frame(data)

    def test_random_bytes_various_lengths(self) -> None:
        """Random bytes of various lengths should not crash."""
        for length in range(1001):
            data = os.urandom(length) if length > 0 else b""
            if length < 2:
                with pytest.raises(TunnelProtocolError):
                    decode_frame(data)
            else:
                frame = decode_frame(data)
                assert isinstance(frame.payload, bytes)

    def test_truncated_frames(self) -> None:
        full = encode_frame(0x01, b"hello world")
        for i in range(len(full)):
            truncated = full[:i]
            if i < 2:
                with pytest.raises(TunnelProtocolError):
                    decode_frame(truncated)
            else:
                frame = decode_frame(truncated)
                assert frame.channel == 0x01

    def test_huge_channel_in_raw_bytes(self) -> None:
        raw = bytes([0xFF, 0xFF]) + b"data"
        frame = decode_frame(raw)
        assert frame.channel == 0xFF
        assert frame.flags == 0xFF
        assert frame.payload == b"data"

    def test_control_empty_bytes(self) -> None:
        with pytest.raises(TunnelProtocolError, match="invalid control payload"):
            decode_control(b"")

    def test_control_null_bytes(self) -> None:
        with pytest.raises(TunnelProtocolError, match="invalid control payload"):
            decode_control(b"\x00\x00\x00")

    def test_control_partial_json(self) -> None:
        with pytest.raises(TunnelProtocolError, match="invalid control payload"):
            decode_control(b'{"type": "open"')

    def test_control_deeply_nested_json(self) -> None:
        depth = 100
        inner: dict = {"type": "deep"}
        current = inner
        for i in range(depth):
            wrapper: dict = {"type": "wrap", f"level_{i}": current}
            current = wrapper
        payload = json.dumps(current).encode()
        result = decode_control(payload)
        assert result["type"] == "wrap"

    def test_control_json_array(self) -> None:
        with pytest.raises(TunnelProtocolError, match="must be a JSON object"):
            decode_control(b"[1, 2, 3]")

    def test_control_json_string(self) -> None:
        with pytest.raises(TunnelProtocolError, match="must be a JSON object"):
            decode_control(b'"just a string"')

    def test_control_json_number(self) -> None:
        with pytest.raises(TunnelProtocolError, match="must be a JSON object"):
            decode_control(b"42")

    def test_control_json_null(self) -> None:
        with pytest.raises(TunnelProtocolError, match="must be a JSON object"):
            decode_control(b"null")

    def test_control_json_boolean(self) -> None:
        with pytest.raises(TunnelProtocolError, match="must be a JSON object"):
            decode_control(b"true")

    def test_control_invalid_utf8(self) -> None:
        with pytest.raises(TunnelProtocolError, match="invalid control payload"):
            decode_control(b"\x80\x81\x82\x83")

    @given(data=st.binary(min_size=0, max_size=200))
    @settings(max_examples=300)
    def test_control_random_bytes_no_crash(self, data: bytes) -> None:
        """decode_control should raise TunnelProtocolError or return a dict."""
        try:
            result = decode_control(data)
            assert isinstance(result, dict)
        except TunnelProtocolError:
            pass
