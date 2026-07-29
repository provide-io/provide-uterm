#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the HTTP proxy's reporting.

What crosses a tunnel is shown to an operator, and how much of it is shown is
a decision with two sides:

* **A body is only sent on when it is worth reading.** Something binary is
  reported by size alone — a megabyte of base64 nobody can read is a megabyte
  spent — and something too large is marked truncated rather than carried.
  Both say the size, which is the part an operator is actually looking at.
* **The content type decides**, read as the type alone: `text/html;
  charset=utf-8` is the same type as `text/html`, and a type shouted in
  capitals is the same type again.

The log line is the other half: one line per exchange, with a direction, a
duration, a human-sized body and a mark on anything that failed at the far
end.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_httpproxy_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.tunnel import http_proxy

OUT = Path(__file__).resolve().parent / "httpproxy_golden.json"

BODIES: list[tuple[str, bytes, str]] = [
    ("nothing at all", b"", "text/plain"),
    ("a little text", b"hello", "text/plain"),
    ("text with no type given", b"hello", ""),
    ("json", b'{"a":1}', "application/json"),
    ("html with a charset", b"<p>hi</p>", "text/html; charset=utf-8"),
    ("html with a charset and spaces", b"<p>hi</p>", "  text/html ; charset=utf-8  "),
    ("a type in capitals", b"<p>hi</p>", "TEXT/HTML"),
    ("an image", b"\x89PNG\r\n", "image/png"),
    ("an image in capitals", b"\x89PNG\r\n", "IMAGE/PNG"),
    ("audio", b"\x00\x01", "audio/mpeg"),
    ("video", b"\x00\x01", "video/mp4"),
    ("a font", b"\x00\x01", "font/woff2"),
    ("something with no type at all", b"\x00\x01", "application/octet-stream"),
    ("a zip", b"PK\x03\x04", "application/zip"),
    ("gzip", b"\x1f\x8b", "application/gzip"),
    ("a pdf", b"%PDF-", "application/pdf"),
    ("web assembly", b"\x00asm", "application/wasm"),
    ("a type that only starts like a binary one", b"hello", "imagex/png"),
    ("a type that ends like a binary one", b"hello", "x-image/png"),
    ("empty and binary", b"", "image/png"),
    ("exactly the limit", b"x" * (256 * 1024), "text/plain"),
    ("one byte past the limit", b"x" * (256 * 1024 + 1), "text/plain"),
    ("one byte under the limit", b"x" * (256 * 1024 - 1), "text/plain"),
    ("too large and binary", b"x" * (256 * 1024 + 1), "image/png"),
    ("bytes that are not text", b"\xff\xfe\x00", "text/plain"),
]

LOGS: list[tuple[str, str, str, int | None, float | None, int]] = [
    ("a request going out", "GET", "https://example.test/", None, None, 0),
    ("a request with a body", "POST", "https://example.test/x", None, None, 512),
    ("an answer", "GET", "https://example.test/", 200, 12.4, 1024),
    ("an answer that took no time", "GET", "https://example.test/", 200, 0.0, 0),
    ("an answer with no duration", "GET", "https://example.test/", 200, None, 0),
    ("a redirect", "GET", "https://example.test/", 302, 5.0, 0),
    ("a refusal", "GET", "https://example.test/", 404, 5.0, 0),
    ("a failure at the far end", "GET", "https://example.test/", 500, 5.0, 0),
    ("a failure further up", "GET", "https://example.test/", 503, 5.0, 0),
    ("just below a failure", "GET", "https://example.test/", 499, 5.0, 0),
    ("a duration that rounds", "GET", "https://example.test/", 200, 12.6, 0),
    ("a duration that rounds down", "GET", "https://example.test/", 200, 12.4, 0),
    ("a duration ending in a half", "GET", "https://example.test/", 200, 12.5, 0),
    ("a duration ending in a half, the other way", "GET", "https://example.test/", 200, 11.5, 0),
    ("a long duration", "GET", "https://example.test/", 200, 1234.5, 0),
    ("a body of one byte", "GET", "https://example.test/", 200, 1.0, 1),
    ("a body just under a kilobyte", "GET", "https://example.test/", 200, 1.0, 1023),
    ("a body of exactly a kilobyte", "GET", "https://example.test/", 200, 1.0, 1024),
    ("a body of a kilobyte and a half", "GET", "https://example.test/", 200, 1.0, 1536),
    ("a body just under a megabyte", "GET", "https://example.test/", 200, 1.0, 1024 * 1024 - 1),
    ("a body of exactly a megabyte", "GET", "https://example.test/", 200, 1.0, 1024 * 1024),
    ("a body of several megabytes", "GET", "https://example.test/", 200, 1.0, 5 * 1024 * 1024 + 512 * 1024),
]


def main() -> None:
    corpus = {
        "body_max_bytes": http_proxy.BODY_MAX_BYTES,
        "binary_types": sorted(http_proxy.BINARY_CONTENT_TYPES),
        "bodies": [
            {
                "name": name,
                "size": len(body),
                "content_type": content_type,
                "encoded": http_proxy.encode_body(body, content_type),
            }
            for name, body, content_type in BODIES
        ],
        "logs": [
            {
                "name": name,
                "method": method,
                "url": url,
                "status": status,
                "duration_ms": duration,
                "body_size": size,
                "line": http_proxy.format_log_line(method, url, status, duration, size),
            }
            for name, method, url, status, duration, size in LOGS
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['bodies'])} bodies)")


if __name__ == "__main__":
    main()
