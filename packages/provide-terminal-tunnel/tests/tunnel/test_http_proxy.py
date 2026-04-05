#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for HTTP proxy helpers."""

from __future__ import annotations

import base64

from provide.terminal.tunnel.http_proxy import BODY_MAX_BYTES, _human_size, _is_binary, encode_body, format_log_line


class TestEncodeBody:
    def test_small_json(self):
        body = b'{"user": "admin"}'
        result = encode_body(body, "application/json")
        assert result["body_b64"] == base64.b64encode(body).decode()
        assert result["body_size"] == len(body)
        assert "body_truncated" not in result

    def test_large_body_truncated(self):
        body = b"x" * (BODY_MAX_BYTES + 1)
        result = encode_body(body, "text/plain")
        assert "body_b64" not in result
        assert result["body_truncated"] is True
        assert result["body_size"] == len(body)

    def test_binary_content_type(self):
        result = encode_body(b"\x89PNG\r\n", "image/png")
        assert "body_b64" not in result
        assert result["body_binary"] is True

    def test_empty_body(self):
        result = encode_body(b"", "text/plain")
        assert result["body_size"] == 0
        assert "body_b64" not in result
        assert "body_truncated" not in result

    def test_audio_binary(self):
        result = encode_body(b"\xff\xfb", "audio/mpeg")
        assert result["body_binary"] is True

    def test_video_binary(self):
        result = encode_body(b"\x00\x00", "video/mp4")
        assert result["body_binary"] is True

    def test_octet_stream_binary(self):
        result = encode_body(b"\x00", "application/octet-stream")
        assert result["body_binary"] is True

    def test_content_type_with_charset(self):
        body = b"hello"
        result = encode_body(body, "text/plain; charset=utf-8")
        assert "body_b64" in result

    def test_exact_max_size_not_truncated(self):
        body = b"x" * BODY_MAX_BYTES
        result = encode_body(body, "text/plain")
        assert "body_b64" in result
        assert "body_truncated" not in result


class TestFormatLogLine:
    def test_200_get(self):
        line = format_log_line("GET", "/api/users", 200, 142.3, 3200)
        assert "200" in line
        assert "GET" in line
        assert "/api/users" in line
        assert "142ms" in line

    def test_500_warning(self):
        line = format_log_line("POST", "/api/crash", 500, 34.0, 128)
        assert "500" in line
        assert "⚠" in line

    def test_request_only_no_status(self):
        line = format_log_line("POST", "/api/login", None, None, 1100)
        assert "→" in line
        assert "POST" in line

    def test_404_no_warning(self):
        line = format_log_line("GET", "/missing", 404, 5.0, 0)
        assert "404" in line
        assert "⚠" not in line

    def test_no_duration(self):
        line = format_log_line("GET", "/", 200, None, 10)
        assert "?" in line


class TestIsBinary:
    def test_image(self):
        assert _is_binary("image/png") is True

    def test_json(self):
        assert _is_binary("application/json") is False

    def test_font(self):
        assert _is_binary("font/woff2") is True

    def test_pdf(self):
        assert _is_binary("application/pdf") is True

    def test_wasm(self):
        assert _is_binary("application/wasm") is True


class TestHumanSize:
    def test_bytes(self):
        assert _human_size(512) == "512B"

    def test_kilobytes(self):
        assert _human_size(3200) == "3.1KB"

    def test_megabytes(self):
        assert _human_size(2 * 1024 * 1024) == "2.0MB"

    def test_zero(self):
        assert _human_size(0) == "0B"
