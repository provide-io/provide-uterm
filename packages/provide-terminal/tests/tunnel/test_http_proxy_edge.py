#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Edge case, boundary, and fuzz tests for http_proxy helpers."""

from __future__ import annotations

import base64

import pytest

from provide.terminal.tunnel.http_proxy import (
    BINARY_CONTENT_TYPES,
    BODY_MAX_BYTES,
    _human_size,
    _is_binary,
    encode_body,
    format_log_line,
)

# ---------------------------------------------------------------------------
# encode_body — boundary / edge cases
# ---------------------------------------------------------------------------


class TestEncodeBodyBoundary:
    def test_exactly_body_max_bytes_not_truncated(self):
        body = b"a" * BODY_MAX_BYTES
        result = encode_body(body, "text/plain")
        assert "body_b64" in result
        assert "body_truncated" not in result
        assert result["body_size"] == BODY_MAX_BYTES

    def test_body_max_bytes_plus_one_truncated(self):
        body = b"a" * (BODY_MAX_BYTES + 1)
        result = encode_body(body, "text/plain")
        assert "body_b64" not in result
        assert result["body_truncated"] is True
        assert result["body_size"] == BODY_MAX_BYTES + 1

    def test_content_type_charset_param_not_binary(self):
        body = b"<html>hello</html>"
        result = encode_body(body, "text/html; charset=utf-8")
        assert "body_b64" in result
        assert "body_binary" not in result

    def test_content_type_uppercase_image_is_binary(self):
        result = encode_body(b"\x89PNG", "Image/PNG")
        assert result["body_binary"] is True
        assert "body_b64" not in result

    def test_content_type_empty_string_not_binary(self):
        body = b"some bytes"
        result = encode_body(body, "")
        assert "body_b64" in result
        assert "body_binary" not in result

    def test_application_json_not_binary_body_included(self):
        body = b'{"key": "value"}'
        result = encode_body(body, "application/json")
        assert "body_binary" not in result
        assert "body_b64" in result
        assert base64.b64decode(result["body_b64"]) == body

    def test_application_pdf_is_binary(self):
        result = encode_body(b"%PDF-1.4", "application/pdf")
        assert result["body_binary"] is True
        assert "body_b64" not in result

    def test_application_gzip_is_binary(self):
        result = encode_body(b"\x1f\x8b\x08", "application/gzip")
        assert result["body_binary"] is True

    def test_font_woff2_is_binary(self):
        result = encode_body(b"wOF2", "font/woff2")
        assert result["body_binary"] is True

    def test_video_webm_is_binary(self):
        result = encode_body(b"\x1a\x45\xdf\xa3", "video/webm")
        assert result["body_binary"] is True

    def test_body_exactly_one_byte(self):
        body = b"X"
        result = encode_body(body, "text/plain")
        assert result["body_size"] == 1
        assert "body_b64" in result
        assert base64.b64decode(result["body_b64"]) == body

    def test_body_with_null_bytes(self):
        body = b"\x00\x00\x00\x00"
        result = encode_body(body, "text/plain")
        assert "body_b64" in result
        assert base64.b64decode(result["body_b64"]) == body

    def test_body_unicode_as_utf8(self):
        body = "こんにちは世界".encode()
        result = encode_body(body, "text/plain; charset=utf-8")
        assert "body_b64" in result
        assert base64.b64decode(result["body_b64"]) == body


# ---------------------------------------------------------------------------
# format_log_line — edge cases
# ---------------------------------------------------------------------------


class TestFormatLogLineEdge:
    def test_url_with_query_params(self):
        line = format_log_line("GET", "/search?q=hello&page=2", 200, 12.0, 512)
        assert "q=hello" in line
        assert "200" in line

    def test_very_long_url(self):
        url = "/path/" + "x" * 500
        line = format_log_line("GET", url, 200, 5.0, 100)
        assert url in line

    def test_status_100(self):
        line = format_log_line("GET", "/", 100, 1.0, 0)
        assert "100" in line
        assert "⚠" not in line
        assert "←" in line

    def test_status_301(self):
        line = format_log_line("GET", "/old", 301, 2.5, 0)
        assert "301" in line
        assert "⚠" not in line

    def test_status_403(self):
        line = format_log_line("GET", "/secret", 403, 3.0, 50)
        assert "403" in line
        assert "⚠" not in line

    def test_status_502(self):
        line = format_log_line("POST", "/api", 502, 100.0, 200)
        assert "502" in line
        assert "⚠" in line

    def test_duration_ms_zero(self):
        line = format_log_line("GET", "/fast", 200, 0, 10)
        assert "0ms" in line

    def test_duration_ms_large(self):
        line = format_log_line("GET", "/slow", 200, 999999, 1024)
        assert "999999ms" in line

    def test_body_size_zero(self):
        line = format_log_line("HEAD", "/", 200, 5.0, 0)
        assert "0B" in line

    def test_body_size_very_large(self):
        # 1 GB
        line = format_log_line("GET", "/big", 200, 50.0, 1024 * 1024 * 1024)
        assert "1024.0MB" in line

    def test_method_options(self):
        line = format_log_line("OPTIONS", "/", 200, 1.0, 0)
        assert "OPTIONS" in line

    def test_method_head(self):
        line = format_log_line("HEAD", "/health", 200, 1.0, 0)
        assert "HEAD" in line

    def test_method_patch(self):
        line = format_log_line("PATCH", "/resource/1", 200, 5.0, 100)
        assert "PATCH" in line


# ---------------------------------------------------------------------------
# _human_size — boundary values
# ---------------------------------------------------------------------------


class TestHumanSizeEdge:
    def test_zero(self):
        assert _human_size(0) == "0B"

    def test_one_byte(self):
        assert _human_size(1) == "1B"

    def test_1023_bytes(self):
        assert _human_size(1023) == "1023B"

    def test_1024_bytes_is_kb(self):
        assert _human_size(1024) == "1.0KB"

    def test_1025_bytes_is_kb(self):
        assert _human_size(1025) == "1.0KB"

    def test_just_below_mb(self):
        # 1048575 bytes = 1024*1024 - 1
        val = 1048575
        result = _human_size(val)
        assert result.endswith("KB")

    def test_exactly_mb(self):
        assert _human_size(1024 * 1024) == "1.0MB"

    def test_just_above_mb(self):
        result = _human_size(1048577)
        assert result.endswith("MB")

    def test_ten_gb(self):
        val = 10 * 1024 * 1024 * 1024
        result = _human_size(val)
        assert result == "10240.0MB"


# ---------------------------------------------------------------------------
# _is_binary — all BINARY_CONTENT_TYPES entries and negatives
# ---------------------------------------------------------------------------


class TestIsBinaryEdge:
    @pytest.mark.parametrize("prefix", sorted(BINARY_CONTENT_TYPES))
    def test_each_binary_prefix(self, prefix: str):
        # For prefixes like "image/", construct "image/png"; for exact matches use as-is.
        ct = prefix + "testformat" if prefix.endswith("/") else prefix
        assert _is_binary(ct) is True, f"expected {ct!r} to be binary"

    def test_application_javascript_not_binary(self):
        assert _is_binary("application/javascript") is False

    def test_text_xml_not_binary(self):
        assert _is_binary("text/xml") is False

    def test_empty_string_not_binary(self):
        assert _is_binary("") is False

    def test_case_insensitive_audio(self):
        assert _is_binary("AUDIO/OGG") is True

    def test_case_insensitive_font(self):
        assert _is_binary("Font/TTF") is True

    def test_application_zip_binary(self):
        assert _is_binary("application/zip") is True

    def test_application_wasm_binary(self):
        assert _is_binary("application/wasm") is True
