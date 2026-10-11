#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generic screen parsing utilities for BBS terminals.

Provides reusable screen parsing functions that can be used across different
games and BBS systems.
"""

from __future__ import annotations

import re

# The escape grammar strip_ansi removes, in named parts so the chunk-carry check
# below (split_incomplete_escape) is built from the SAME pieces and cannot drift:
#   CSI:  ESC "[" <parameter bytes 0x30-0x3F>* <intermediate bytes 0x20-0x2F>* <final 0x40-0x7E>
#   Fe:   ESC <one byte 0x40-0x5F>   (a two-byte escape; "[" itself is one, so a
#         bare "ESC [" with nothing after it is ambiguous until more arrives)
_ESC = "\x1b"
_CSI_INTRODUCER = r"\["
_CSI_PARAMETER_BYTES = r"[0-?]*"
_CSI_INTERMEDIATE_BYTES = r"[ -/]*"
_CSI_FINAL_BYTE = r"[@-~]"
_FE_FINAL_BYTE = r"[@-_]"
_CSI_PREFIX = _CSI_INTRODUCER + _CSI_PARAMETER_BYTES + _CSI_INTERMEDIATE_BYTES
_ANSI_ESCAPE_RE = re.compile(f"{_ESC}(?:{_CSI_PREFIX}{_CSI_FINAL_BYTE}|{_FE_FINAL_BYTE})")
# A proper prefix of something _ANSI_ESCAPE_RE strips: a bare ESC, or a CSI whose
# parameter/intermediate bytes have not yet reached a final byte.
_INCOMPLETE_ESCAPE_RE = re.compile(f"{_ESC}(?:{_CSI_PREFIX})?")
# Longest tail split_incomplete_escape holds back, in characters, ESC included. A
# real sequence is a handful of bytes; past this the "sequence" is not one (or is
# hostile), and it is released into the text rather than carried forever.
MAX_ESCAPE_CARRY = 64
# Some BBS/telnet servers occasionally leak bare SGR fragments like `1;31m` at line
# starts without the ESC prefix. Strip only when they look isolated.
_BARE_SGR_RE = re.compile(r"(?:(?<=^)|(?<=\n)|(?<=\r)|(?<=\s))(?:\d{1,3}(?:;\d{1,3})*)m(?=\x1b|\s|$)")
_BARE_SGR_LINE_PREFIX_RE = re.compile(r"(?m)^(?:\d{1,3}(?:;\d{1,3})*)m(?=[A-Z<])")
_ACTION_TAG_RE = re.compile(r"<([^<>\r\n]{1,80})>")


def normalize_terminal_text(text: str) -> str:
    """Normalize terminal text for robust prompt/context parsing.

    - Removes ANSI escape/control sequences.
    - Removes isolated bare SGR fragments (e.g. ``1;31m``) seen in some BBS server output.
    - Normalizes line endings.
    """
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _ANSI_ESCAPE_RE.sub("", cleaned)
    cleaned = _BARE_SGR_LINE_PREFIX_RE.sub("", cleaned)
    cleaned = _BARE_SGR_RE.sub("", cleaned)
    return cleaned  # noqa: RET504


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text.

    Args:
        text: Text potentially containing ANSI escape codes.

    Returns:
        Text with all ANSI escape codes removed.
    """
    return normalize_terminal_text(text)


def split_incomplete_escape(text: str, max_carry: int = MAX_ESCAPE_CARRY) -> tuple[str, str]:
    """Split *text* into ``(complete, carry)``: carry is an unterminated trailing escape.

    For text that arrives in chunks: :func:`strip_ansi` sees one chunk at a time,
    so ``"\x1b[1"`` + ``"msudo"`` would strip to ``"\x1b[1"`` (no final byte, left
    alone) and ``"msudo"``. The caller strips *complete* now and prepends *carry*
    to the next chunk, so the whole sequence is stripped once it completes.

    *carry* is non-empty exactly when the text from the LAST ESC to the end is a
    proper prefix of a sequence strip_ansi removes (a bare ESC, or ``ESC [`` plus
    parameter/intermediate bytes with no final byte) and is at most *max_carry*
    characters long. Only the last ESC can start one: no byte of an incomplete
    sequence is ESC. Built from the same grammar constants as strip_ansi.
    """
    start = text.rfind(_ESC)
    if start < 0 or len(text) - start > max_carry:
        return text, ""
    if _INCOMPLETE_ESCAPE_RE.fullmatch(text, start) is None:
        return text, ""
    return text[:start], text[start:]


def decode_cp437(data: bytes) -> str:
    """Decode bytes from CP437 (DOS OEM) encoding to a Unicode string.

    CP437 is the character encoding used by most BBS systems and DOS-era
    software. It maps bytes 0x80-0xFF to box-drawing and special characters.

    Args:
        data: Raw bytes in CP437 encoding.

    Returns:
        Unicode string.
    """
    return data.decode("cp437")


def encode_cp437(text: str) -> bytes:
    """Encode a Unicode string to CP437 (DOS OEM) bytes.

    Characters that have no CP437 representation are replaced with ``?``.

    Args:
        text: Unicode string to encode.

    Returns:
        Bytes in CP437 encoding.
    """
    return text.encode("cp437", errors="replace")


def extract_action_tags(text: str, *, max_tags: int = 8) -> list[str]:
    """Extract angle-bracket action tags like ``<Move>`` from a screen snapshot."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in _ACTION_TAG_RE.findall(text):
        tag = str(raw or "").strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= max(1, int(max_tags)):
            break
    return out


def clean_screen_for_display(screen: str, max_lines: int = 30) -> list[str]:
    """Clean screen for display by removing padding lines.

    Args:
        screen: Raw screen text.
        max_lines: Maximum lines to return.

    Returns:
        List of non-empty content lines (up to max_lines).
    """
    lines = []
    for line in screen.split("\n"):
        if line.strip() or not line.startswith(" " * 80):
            lines.append(line)
            if len(lines) >= max_lines:
                break
    return lines


def extract_menu_options(screen: str, pattern: str | None = None) -> list[tuple[str, str]]:
    """Extract menu options from screen text.

    Supports common menu formats like ``<A> Option``, ``[A] Option``, ``(A) Option``.

    Args:
        screen: Screen text containing menu options.
        pattern: Optional custom regex with two capture groups (key, description).

    Returns:
        List of (key, description) tuples.
    """
    if pattern is None:
        pattern = r"[<\[\(]([A-Z0-9])[>\]\)]\s+([^<\[\(\n]+?)(?=\s*[<\[\(]|$)"

    options = []
    try:
        for match in re.finditer(pattern, screen):
            key = match.group(1)
            description = match.group(2).strip()
            if description:
                options.append((key, description))
    except re.error:
        return options
    return options


def extract_numbered_list(screen: str, pattern: str | None = None) -> list[tuple[str, str]]:
    """Extract numbered lists from screen text.

    Supports common formats like ``1. Item``, ``1) Item``, ``1 - Item``.

    Args:
        screen: Screen text containing numbered list.
        pattern: Optional custom regex with two capture groups (number, description).

    Returns:
        List of (number, description) tuples.
    """
    if pattern is None:
        pattern = r"^\s*(\d+)[\.\)]\s+(.+)$"

    options = []
    try:
        for line in screen.splitlines():
            match = re.search(pattern, line)
            if match:
                number = match.group(1)
                description = match.group(2).strip()
                if description:
                    options.append((number, description))
    except re.error:
        return options
    return options


def extract_key_value_pairs(screen: str, patterns: dict[str, str]) -> dict[str, str]:
    """Extract key-value pairs from screen text using provided patterns.

    Args:
        screen: Screen text to parse.
        patterns: Mapping of field names to regex patterns (one capture group each).

    Returns:
        Dictionary of extracted string values.

    Example::

        result = extract_key_value_pairs(screen, {
            "credits": r"Credits?:?\\s*([\\d,]+)",
            "sector": r"Sector\\s*:?\\s*(\\d+)",
        })
    """
    data: dict[str, str] = {}
    for field, pat in patterns.items():
        try:
            match = re.search(pat, screen, re.IGNORECASE)
        except re.error:
            continue
        if match:
            data[field] = match.group(1)
    return data
