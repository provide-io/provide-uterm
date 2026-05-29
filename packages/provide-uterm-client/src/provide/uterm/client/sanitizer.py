#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Keystroke unescaping + sanitization for AI/MCP-supplied input.

Both MCP code paths (``ai.server_impl`` and ``client.mcp_tools``) funnel
user/LLM keystrokes through :func:`prepare_keystrokes` so they cannot drift:
escape sequences are expanded first, then the resulting bytes are filtered to
a control-char allowlist and capped.
"""

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
    """Translate terminal-relevant escape sequences in *raw* to real characters.

    Recognises ``\\n``, ``\\r``, ``\\t``, ``\\e``, ``\\0``, ``\\\\``, ``\\'``,
    ``\\"``, ``\\xNN`` and ``\\uNNNN``. Unknown single-letter escapes such as
    ``\\a``, ``\\b``, ``\\c``, ``\\q`` are left untouched (passed through as
    the original two-character backslash sequence) so that callers may safely
    embed literal text without surprise translation.
    """

    def _replace(match: re.Match[str]) -> str:
        hex2, hex4, ch = match.groups()
        if hex2 is not None:
            return chr(int(hex2, 16))
        if hex4 is not None:
            return chr(int(hex4, 16))
        if ch in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[ch]
        # Unknown escape — preserve the original sequence verbatim.
        return match.group(0)

    return _ESCAPE_PATTERN.sub(_replace, raw)


def sanitize_keystrokes(keys: str, max_bytes: int = 4096) -> str:
    """Sanitize keystrokes for AI agents: strip non-printable chars except common controls."""
    # Allowed: printable + CR, LF, TAB, Ctrl+C (\x03), ESC (\x1b)
    allowed = set(string.printable) | {"\r", "\n", "\t", "\x03", "\x1b"}

    # Filter printable
    filtered = "".join(c for c in keys if c in allowed)

    # Enforce byte limit
    encoded = filtered.encode("utf-8")
    if len(encoded) <= max_bytes:
        return filtered

    # Truncate at character boundary
    return encoded[:max_bytes].decode("utf-8", "ignore")


def prepare_keystrokes(raw: str, max_bytes: int = 4096) -> str:
    """Unescape *then* sanitize keystrokes (the canonical MCP input path).

    Order matters: :func:`unescape_keys` runs first so ``\\x1b`` text becomes a
    real ESC byte, then :func:`sanitize_keystrokes` filters the now-real control
    bytes to the allowlist and caps them. Both MCP entry points call this so the
    two paths cannot diverge.
    """
    return sanitize_keystrokes(unescape_keys(raw), max_bytes=max_bytes)
