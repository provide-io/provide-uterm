#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""HTTP proxy helpers: body encoding, log formatting, content type detection."""

from __future__ import annotations

import base64
from typing import Any

BODY_MAX_BYTES = 256 * 1024  # 256 KB

BINARY_CONTENT_TYPES = frozenset(
    {
        "image/",
        "audio/",
        "video/",
        "application/octet-stream",
        "application/zip",
        "application/gzip",
        "application/pdf",
        "application/wasm",
        "font/",
    }
)


def _is_binary(content_type: str) -> bool:
    ct = content_type.lower().split(";")[0].strip()
    return any(ct.startswith(prefix) for prefix in BINARY_CONTENT_TYPES)


def encode_body(body: bytes, content_type: str) -> dict[str, Any]:
    """Encode a request/response body per the spec rules."""
    result: dict[str, Any] = {"body_size": len(body)}
    if not body:
        return result
    if _is_binary(content_type):
        result["body_binary"] = True
        return result
    if len(body) > BODY_MAX_BYTES:
        result["body_truncated"] = True
        return result
    result["body_b64"] = base64.b64encode(body).decode("ascii")
    return result


def format_log_line(
    method: str,
    url: str,
    status: int | None,
    duration_ms: float | None,
    body_size: int,
) -> str:
    """Format a compact mitmproxy-style log line."""
    size_str = _human_size(body_size)
    if status is None:
        return f"→ {method} {url} ({size_str})"
    warn = " ⚠" if status >= 500 else ""
    dur = f"{duration_ms:.0f}ms" if duration_ms is not None else "?"
    return f"← {status} {method} {url} ({dur}, {size_str}){warn}"


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"
