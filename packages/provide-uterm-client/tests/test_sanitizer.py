#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from provide.uterm.client.sanitizer import sanitize_keystrokes


def test_sanitizer_keeps_printable():
    assert sanitize_keystrokes("hello world 123!") == "hello world 123!"


def test_sanitizer_strips_binary():
    # \x00 is null, \x01 is SOH, etc.
    assert sanitize_keystrokes("hello\x00\x01world") == "helloworld"


def test_sanitizer_keeps_controls():
    assert sanitize_keystrokes("\r\n\t\x03\x1b") == "\r\n\t\x03\x1b"


def test_sanitizer_truncates_long_string():
    long_str = "A" * 5000
    sanitized = sanitize_keystrokes(long_str, max_bytes=100)
    assert len(sanitized) == 100
    assert sanitized == "A" * 100
