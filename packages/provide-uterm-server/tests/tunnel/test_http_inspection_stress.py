#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hypothesis property tests and stress tests for HTTP inspection code."""

from __future__ import annotations

import json
import time

from hypothesis import given, settings
from hypothesis import strategies as st

from provide.uterm.tunnel.http_proxy import BODY_MAX_BYTES, encode_body, format_log_line
from provide.uterm.tunnel.protocol import CHANNEL_HTTP, decode_frame, encode_frame

# ---------------------------------------------------------------------------
# Hypothesis: encode_body
# ---------------------------------------------------------------------------


@given(body=st.binary(min_size=0, max_size=BODY_MAX_BYTES + 100))
def test_encode_body_always_has_body_size(body):
    result = encode_body(body, "text/plain")
    assert result["body_size"] == len(body)


@given(body=st.binary(min_size=1, max_size=1000))
def test_encode_body_text_always_has_b64_or_truncated(body):
    result = encode_body(body, "text/plain")
    assert "body_b64" in result or "body_truncated" in result or result["body_size"] == 0


@given(
    ct=st.sampled_from(
        [
            "image/png",
            "audio/mp3",
            "video/mp4",
            "application/octet-stream",
            "application/pdf",
            "font/woff",
        ]
    )
)
def test_encode_body_binary_types_never_have_b64(ct):
    result = encode_body(b"data", ct)
    assert "body_b64" not in result
    assert result.get("body_binary") is True


@given(body=st.binary(min_size=0, max_size=BODY_MAX_BYTES))
def test_encode_body_non_binary_within_limit_has_b64_when_nonempty(body):
    result = encode_body(body, "application/json")
    if body:
        assert "body_b64" in result
    else:
        assert "body_b64" not in result


@given(extra=st.binary(min_size=1, max_size=2000))
def test_encode_body_over_limit_text_is_truncated(extra):
    # Build a body that exceeds BODY_MAX_BYTES without asking hypothesis to
    # generate 256 KB of entropy directly (avoids HealthCheck.data_too_large).
    body = b"x" * BODY_MAX_BYTES + extra
    result = encode_body(body, "text/plain")
    assert result.get("body_truncated") is True
    assert "body_b64" not in result
    assert result["body_size"] == len(body)


@given(body=st.binary(min_size=0, max_size=500))
def test_encode_body_result_always_dict(body):
    result = encode_body(body, "text/html")
    assert isinstance(result, dict)


@given(
    body=st.binary(min_size=1, max_size=500),
    ct=st.text(min_size=0, max_size=100),
)
def test_encode_body_body_size_always_matches(body, ct):
    result = encode_body(body, ct)
    assert result["body_size"] == len(body)


# ---------------------------------------------------------------------------
# Hypothesis: format_log_line
# ---------------------------------------------------------------------------


@given(
    method=st.sampled_from(["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
    url=st.text(
        min_size=1,
        max_size=500,
        alphabet=st.characters(whitelist_categories=("L", "N", "P")),
    ),
    status=st.integers(min_value=100, max_value=599),
    duration=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
    size=st.integers(min_value=0, max_value=10**10),
)
def test_format_log_line_never_crashes(method, url, status, duration, size):
    line = format_log_line(method, url, status, duration, size)
    assert isinstance(line, str)
    assert method in line


@given(
    method=st.sampled_from(["GET", "POST", "PUT", "DELETE"]),
    url=st.text(min_size=1, max_size=200),
    status=st.integers(min_value=500, max_value=599),
    duration=st.floats(min_value=0.0, max_value=9999.0, allow_nan=False, allow_infinity=False),
    size=st.integers(min_value=0, max_value=10**6),
)
def test_format_log_line_5xx_always_has_warning(method, url, status, duration, size):
    line = format_log_line(method, url, status, duration, size)
    assert "⚠" in line


@given(
    method=st.sampled_from(["GET", "POST"]),
    url=st.text(min_size=1, max_size=200),
    status=st.integers(min_value=100, max_value=499),
    duration=st.floats(min_value=0.0, max_value=9999.0, allow_nan=False, allow_infinity=False),
    size=st.integers(min_value=0, max_value=10**6),
)
def test_format_log_line_non_5xx_no_warning(method, url, status, duration, size):
    line = format_log_line(method, url, status, duration, size)
    assert "⚠" not in line


@given(
    method=st.sampled_from(["GET", "POST"]),
    url=st.text(min_size=1, max_size=200),
    size=st.integers(min_value=0, max_value=10**6),
)
def test_format_log_line_none_status_produces_arrow(method, url, size):
    line = format_log_line(method, url, None, None, size)
    assert "→" in line
    assert method in line


@given(
    method=st.sampled_from(["GET", "POST"]),
    url=st.text(min_size=1, max_size=200),
    status=st.integers(min_value=100, max_value=599),
    size=st.integers(min_value=0, max_value=10**6),
)
def test_format_log_line_none_duration_shows_question_mark(method, url, status, size):
    line = format_log_line(method, url, status, None, size)
    assert "?" in line


# ---------------------------------------------------------------------------
# Hypothesis: encode_frame / decode_frame with CHANNEL_HTTP
# ---------------------------------------------------------------------------


@given(payload=st.binary(min_size=0, max_size=10000))
def test_channel_http_roundtrip(payload):
    raw = encode_frame(CHANNEL_HTTP, payload)
    frame = decode_frame(raw)
    assert frame.channel == CHANNEL_HTTP
    assert frame.payload == payload
    assert not frame.is_control
    assert not frame.is_eof


@given(
    channel=st.integers(min_value=0, max_value=255),
    payload=st.binary(min_size=0, max_size=5000),
)
def test_encode_decode_any_channel_roundtrip(channel, payload):
    raw = encode_frame(channel, payload)
    frame = decode_frame(raw)
    assert frame.channel == channel
    assert frame.payload == payload


@given(payload=st.binary(min_size=0, max_size=5000))
def test_encode_frame_output_always_two_bytes_longer(payload):
    raw = encode_frame(CHANNEL_HTTP, payload)
    assert len(raw) == len(payload) + 2


# ---------------------------------------------------------------------------
# Stress tests
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(body=st.binary(min_size=0, max_size=BODY_MAX_BYTES))
def test_encode_body_extended_examples(body):
    """Run encode_body over a wide range of inputs with no crash."""
    result = encode_body(body, "text/plain")
    assert result["body_size"] == len(body)


def test_encode_body_throughput():
    """Encode 10,000 bodies in under 2 seconds."""
    body = b"x" * 1000
    start = time.time()
    for _ in range(10000):
        encode_body(body, "application/json")
    elapsed = time.time() - start
    assert elapsed < 2.0, f"Too slow: {elapsed:.2f}s"


def test_format_log_line_throughput():
    """Format 100,000 log lines in under 2 seconds."""
    start = time.time()
    for i in range(100000):
        format_log_line("GET", f"/api/item/{i}", 200, 5.0, 100)
    elapsed = time.time() - start
    assert elapsed < 2.0, f"Too slow: {elapsed:.2f}s"


def test_http_frame_encode_decode_throughput():
    """Encode/decode 50,000 HTTP channel frames in under 2 seconds."""
    payload = json.dumps({"type": "http_req", "id": "r1", "method": "GET", "url": "/test"}).encode()
    start = time.time()
    for _ in range(50000):
        raw = encode_frame(CHANNEL_HTTP, payload)
        frame = decode_frame(raw)
        assert frame.channel == CHANNEL_HTTP
    elapsed = time.time() - start
    assert elapsed < 2.0, f"Too slow: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Malformed / edge-case inputs
# ---------------------------------------------------------------------------


def test_encode_body_with_all_byte_values():
    body = bytes(range(256))
    result = encode_body(body, "text/plain")
    assert "body_b64" in result


def test_encode_body_with_null_content_type():
    result = encode_body(b"data", "")
    assert "body_b64" in result  # empty CT is not binary


def test_encode_body_empty_produces_only_body_size():
    result = encode_body(b"", "text/plain")
    assert result == {"body_size": 0}


def test_format_log_line_with_empty_url():
    line = format_log_line("GET", "", 200, 1.0, 0)
    assert "GET" in line


def test_format_log_line_with_special_chars():
    line = format_log_line("GET", "/api?q=hello&foo=<script>", 200, 1.0, 0)
    assert "GET" in line
    assert "<script>" in line


def test_encode_body_exact_boundary_is_not_truncated():
    body = b"y" * BODY_MAX_BYTES
    result = encode_body(body, "text/plain")
    assert "body_b64" in result
    assert "body_truncated" not in result


def test_encode_body_one_over_boundary_is_truncated():
    body = b"y" * (BODY_MAX_BYTES + 1)
    result = encode_body(body, "text/plain")
    assert result.get("body_truncated") is True
    assert "body_b64" not in result


def test_encode_body_large_binary_never_b64():
    body = b"\x00" * (BODY_MAX_BYTES + 1000)
    result = encode_body(body, "image/jpeg")
    assert "body_b64" not in result
    assert result.get("body_binary") is True


def test_encode_body_content_type_case_insensitive():
    result = encode_body(b"png data", "IMAGE/PNG")
    assert result.get("body_binary") is True
    assert "body_b64" not in result


def test_encode_body_content_type_with_params():
    result = encode_body(b"hello", "text/plain; charset=utf-8")
    assert "body_b64" in result


def test_format_log_line_very_large_size():
    line = format_log_line("GET", "/big", 200, 100.0, 10**10)
    assert "GET" in line
    assert "GB" in line or "MB" in line or "KB" in line or "B" in line


def test_format_log_line_zero_duration():
    line = format_log_line("GET", "/fast", 200, 0.0, 0)
    assert "0ms" in line


def test_channel_http_empty_payload_roundtrip():
    raw = encode_frame(CHANNEL_HTTP, b"")
    frame = decode_frame(raw)
    assert frame.payload == b""
    assert frame.channel == CHANNEL_HTTP
