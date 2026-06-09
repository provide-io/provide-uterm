#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Keystroke unescaping and sanitization shared by direct sessions and MCP."""

from __future__ import annotations

import re
import string

_SIMPLE_ESCAPES: dict[str, str] = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "e": "\x1b",
    "0": "\x00",
    "\\": "\\",
    "'": "'",
    '"': '"',
}

_ESCAPE_PATTERN = re.compile(
    r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|(.))",
    re.DOTALL,
)


def unescape_keys(raw: str) -> str:
    """Translate terminal-relevant escape sequences in *raw*."""

    def _replace(match: re.Match[str]) -> str:
        hex2, hex4, ch = match.groups()
        if hex2 is not None:
            return chr(int(hex2, 16))
        if hex4 is not None:
            return chr(int(hex4, 16))
        if ch in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[ch]
        return match.group(0)

    return _ESCAPE_PATTERN.sub(_replace, raw)


def sanitize_keystrokes(keys: str, max_bytes: int = 4096) -> str:
    """Filter non-printable bytes while preserving terminal input controls."""
    allowed = set(string.printable) | {"\r", "\n", "\t", "\x03", "\x1b"}
    filtered = "".join(char for char in keys if char in allowed)
    encoded = filtered.encode("utf-8")
    if len(encoded) <= max_bytes:
        return filtered
    return encoded[:max_bytes].decode("utf-8", "ignore")


def prepare_keystrokes(raw: str, max_bytes: int = 4096) -> str:
    """Unescape then sanitize keystrokes."""
    return sanitize_keystrokes(unescape_keys(raw), max_bytes=max_bytes)
