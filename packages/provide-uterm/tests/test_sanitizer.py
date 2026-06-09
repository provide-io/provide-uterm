#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.uterm.sanitizer import prepare_keystrokes, sanitize_keystrokes, unescape_keys


def test_unescape_keys_handles_supported_escapes() -> None:
    assert unescape_keys(r"\r\n\t\e\0\\\'\"") == "\r\n\t\x1b\x00\\'\""


def test_unescape_keys_handles_hex_and_unicode() -> None:
    assert unescape_keys(r"\x41\u00e9") == "Aé"


def test_unescape_keys_preserves_unknown_escape() -> None:
    assert unescape_keys(r"\q") == r"\q"


def test_sanitize_keystrokes_strips_binary_and_keeps_terminal_controls() -> None:
    assert sanitize_keystrokes("a\x00\x01\r\n\t\x03\x1bb") == "a\r\n\t\x03\x1bb"


def test_sanitize_keystrokes_truncates_at_utf8_boundary() -> None:
    assert sanitize_keystrokes("abcdef", max_bytes=3) == "abc"


def test_prepare_keystrokes_unescapes_then_sanitizes() -> None:
    assert prepare_keystrokes(r"a\0\r") == "a\r"
