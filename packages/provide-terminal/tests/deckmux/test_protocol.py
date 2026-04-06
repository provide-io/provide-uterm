#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.deckmux._protocol — message types and serialization."""

from __future__ import annotations

from provide.terminal.deckmux._protocol import (
    KEY_SYMBOLS,
    MSG_AUTO_TRANSFER_WARNING,
    MSG_CONTROL_REQUEST,
    MSG_CONTROL_TRANSFER,
    MSG_PRESENCE_LEAVE,
    MSG_PRESENCE_SYNC,
    MSG_PRESENCE_UPDATE,
    MSG_QUEUED_INPUT,
    encode_keys_display,
    make_control_transfer,
    make_presence_leave,
    make_presence_sync,
    make_presence_update,
)

# --- Message type constants ---


def test_message_type_constants() -> None:
    assert MSG_PRESENCE_UPDATE == "presence_update"
    assert MSG_PRESENCE_SYNC == "presence_sync"
    assert MSG_PRESENCE_LEAVE == "presence_leave"
    assert MSG_CONTROL_TRANSFER == "control_transfer"
    assert MSG_QUEUED_INPUT == "queued_input"
    assert MSG_CONTROL_REQUEST == "control_request"
    assert MSG_AUTO_TRANSFER_WARNING == "auto_transfer_warning"


# --- encode_keys_display ---


def test_encode_keys_display_arrows() -> None:
    assert encode_keys_display("\x1b[A") == "↑"
    assert encode_keys_display("\x1b[B") == "↓"
    assert encode_keys_display("\x1b[C") == "→"
    assert encode_keys_display("\x1b[D") == "←"


def test_encode_keys_display_special_keys() -> None:
    assert encode_keys_display("\r") == "↵"
    assert encode_keys_display("\n") == "↵"
    assert encode_keys_display("\t") == "⇥"
    assert encode_keys_display("\x7f") == "⌫"
    assert encode_keys_display("\x08") == "⌫"
    assert encode_keys_display("\x1b") == "⎋"


def test_encode_keys_display_printable() -> None:
    assert encode_keys_display("hello") == "hello"
    assert encode_keys_display("a b") == "a b"


def test_encode_keys_display_mixed() -> None:
    raw = "ls\r"
    assert encode_keys_display(raw) == "ls↵"


def test_encode_keys_display_mixed_arrows_and_text() -> None:
    raw = "\x1b[Ahello\x1b[D"
    assert encode_keys_display(raw) == "↑hello←"


def test_encode_keys_display_non_printable_skipped() -> None:
    # \x01 is a non-printable control char not in KEY_SYMBOLS
    assert encode_keys_display("\x01") == ""
    assert encode_keys_display("a\x02b") == "ab"


def test_encode_keys_display_empty() -> None:
    assert encode_keys_display("") == ""


def test_encode_keys_display_escape_at_end_without_sequence() -> None:
    """Bare escape at end of string (not part of a 3-char sequence)."""
    assert encode_keys_display("\x1b") == "⎋"


def test_encode_keys_display_escape_with_partial_sequence() -> None:
    """Escape followed by [ but no third char — bare escape then printable."""
    # \x1b is escape (in KEY_SYMBOLS), [ is printable
    assert encode_keys_display("\x1b[") == "⎋["


def test_key_symbols_completeness() -> None:
    """All documented key symbols are present."""
    assert len(KEY_SYMBOLS) == 10


# --- make_presence_update ---


def test_make_presence_update_minimal() -> None:
    msg = make_presence_update("u1", "Alice", "#fff", "admin")
    assert msg == {
        "type": "presence_update",
        "user_id": "u1",
        "name": "Alice",
        "color": "#fff",
        "role": "admin",
    }


def test_make_presence_update_with_optional_fields() -> None:
    msg = make_presence_update(
        "u1",
        "Alice",
        "#fff",
        "admin",
        scroll_line=42,
        typing=True,
        is_owner=True,
    )
    assert msg["scroll_line"] == 42
    assert msg["typing"] is True
    assert msg["is_owner"] is True


def test_make_presence_update_ignores_unknown_fields() -> None:
    msg = make_presence_update("u1", "Alice", "#fff", "admin", unknown_field="ignored")
    assert "unknown_field" not in msg


def test_make_presence_update_all_optional_fields() -> None:
    msg = make_presence_update(
        "u1",
        "Alice",
        "#fff",
        "admin",
        scroll_line=0,
        scroll_range=(0, 100),
        selection={"start": 0},
        pin={"line": 5},
        typing=False,
        queued_keys="abc",
        is_owner=False,
    )
    for key in ("scroll_line", "scroll_range", "selection", "pin", "typing", "queued_keys", "is_owner"):
        assert key in msg


# --- make_presence_sync ---


def test_make_presence_sync() -> None:
    users = [{"user_id": "u1", "name": "Alice"}]
    config = {"idle_timeout": 30}
    msg = make_presence_sync(users, config)
    assert msg == {
        "type": "presence_sync",
        "users": users,
        "config": config,
    }


# --- make_presence_leave ---


def test_make_presence_leave() -> None:
    msg = make_presence_leave("u1")
    assert msg == {"type": "presence_leave", "user_id": "u1"}


# --- make_control_transfer ---


def test_make_control_transfer_minimal() -> None:
    msg = make_control_transfer("u1", "u2", "handover")
    assert msg == {
        "type": "control_transfer",
        "from_user_id": "u1",
        "to_user_id": "u2",
        "reason": "handover",
        "queued_keys": "",
    }


def test_make_control_transfer_with_queued_keys() -> None:
    msg = make_control_transfer("u1", "u2", "auto_idle", queued_keys="ls\r")
    assert msg["queued_keys"] == "ls\r"
    assert msg["reason"] == "auto_idle"


def test_make_control_transfer_default_queued_keys_is_empty_string() -> None:
    """Default queued_keys must be '' not 'XXXX' or any other value."""
    msg = make_control_transfer("u1", "u2", "handover")
    assert msg["queued_keys"] == ""
    assert msg["queued_keys"] is not None


def test_encode_keys_3char_sequence_at_exact_boundary() -> None:
    """3-char escape sequence that starts at position len-3 must be recognized.

    i + 2 < len requires len > i + 2, i.e. len >= i + 3. For a 3-char sequence
    at i=0, len must be exactly 3. If condition were i + 2 <= len (i.e., len >= i + 2),
    it would match a 2-char prefix instead of requiring 3 chars.
    """
    # "\x1b[A" is exactly 3 chars; should decode to "↑"
    assert encode_keys_display("\x1b[A") == "↑"


def test_encode_keys_sequence_boundary_not_off_by_one() -> None:
    """i + 2 < len (not <=): at i=0 with len=3, i+2=2 < 3 is True → correct.
    If i + 2 <= len, then for i=0, len=2: i+2=2 <= 2 is True, but raw_keys[0:3]
    would index out of a 2-char string — the correct guard is strict <.
    """
    # Two chars ending with printable — NOT an escape sequence
    result = encode_keys_display("\x1b[")
    # \x1b is in KEY_SYMBOLS as "⎋", "[" is printable
    assert result == "⎋["
