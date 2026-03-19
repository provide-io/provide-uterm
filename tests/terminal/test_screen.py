#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for provide.terminal.screen."""

from __future__ import annotations

from provide.terminal.screen import decode_cp437, encode_cp437, normalize_terminal_text, strip_ansi


class TestStripAnsi:
    def test_removes_csi_sequence(self) -> None:
        assert strip_ansi("\033[1;31mred\033[0m") == "red"

    def test_plain_text_unchanged(self) -> None:
        assert strip_ansi("hello world") == "hello world"

    def test_empty_string(self) -> None:
        assert strip_ansi("") == ""

    def test_normalizes_crlf(self) -> None:
        result = strip_ansi("line1\r\nline2")
        assert "\r" not in result
        assert result == "line1\nline2"


class TestNormalizeTerminalText:
    def test_removes_ansi_codes(self) -> None:
        result = normalize_terminal_text("\033[1;36mhello\033[0m")
        assert result == "hello"

    def test_normalizes_crlf(self) -> None:
        result = normalize_terminal_text("a\r\nb")
        assert result == "a\nb"

    def test_bare_sgr_fragment(self) -> None:
        # Bare SGR at line start (BBS server artifact) should be stripped
        result = normalize_terminal_text("1;31mSOME TEXT")
        # The bare fragment should not appear in output
        assert "1;31m" not in result

    def test_empty_string(self) -> None:
        assert normalize_terminal_text("") == ""

    def test_iac_bytes_in_string(self) -> None:
        # IAC (0xFF) bytes decoded as latin-1 should pass through normalize
        iac_str = chr(255) + chr(251) + chr(1) + "text"
        result = normalize_terminal_text(iac_str)
        assert "text" in result


class TestDecodeCp437:
    def test_ascii_range(self) -> None:
        data = b"hello world"
        assert decode_cp437(data) == "hello world"

    def test_box_drawing(self) -> None:
        # 0xC4 in CP437 is the horizontal bar ─
        result = decode_cp437(b"\xc4")
        assert result == "─"

    def test_round_trip(self) -> None:
        original = b"\xc4\xb3\xda\xbf\xc0\xd9"
        decoded = decode_cp437(original)
        assert isinstance(decoded, str)
        assert len(decoded) == 6


class TestEncodeCp437:
    def test_ascii_range(self) -> None:
        assert encode_cp437("hello") == b"hello"

    def test_unencodable_replaced(self) -> None:
        # Emoji has no CP437 equivalent — replaced with b"?"
        result = encode_cp437("hello \U0001f600")
        assert result.startswith(b"hello ")
        assert b"?" in result

    def test_round_trip(self) -> None:
        original = "─│┌┐└┘"
        encoded = encode_cp437(original)
        decoded = decode_cp437(encoded)
        assert decoded == original


class TestExtractActionTagsEmptyTag:
    def test_empty_raw_tag_skipped(self) -> None:
        from unittest.mock import patch

        from provide.terminal.screen import extract_action_tags

        # The findall returns empty string which should be skipped
        with patch("provide.terminal.screen._ACTION_TAG_RE") as mock_re:
            mock_re.findall.return_value = ["", "valid_tag", "VALID_TAG"]
            result = extract_action_tags("anything")
        # Empty tag should be skipped; "valid_tag" and "VALID_TAG" deduplicated
        assert "valid_tag" in result
        assert "" not in result
        assert len(result) == 1


class TestActionTagsBoundaries:
    def test_max_tags_zero_defaults_to_one(self) -> None:
        from provide.terminal.screen import extract_action_tags

        result = extract_action_tags("<Tag1> <Tag2> <Tag3>", max_tags=0)
        assert len(result) == 1
        assert result[0] == "Tag1"

    def test_max_tags_clamps_to_one(self) -> None:
        from provide.terminal.screen import extract_action_tags

        result = extract_action_tags("<Only>", max_tags=0)
        assert result == ["Only"]

    def test_long_tag_names(self) -> None:
        from provide.terminal.screen import extract_action_tags

        long_tag = "x" * 80
        screen = f"<{long_tag}>"
        result = extract_action_tags(screen)
        assert long_tag in result

    def test_tag_too_long_rejected(self) -> None:
        from provide.terminal.screen import extract_action_tags

        long_tag = "x" * 81
        screen = f"<{long_tag}>"
        result = extract_action_tags(screen)
        assert long_tag not in result


class TestNormalizeTerminalTextEdgeCases:
    def test_preserves_newlines(self) -> None:
        result = normalize_terminal_text("line1\nline2\nline3")
        assert result == "line1\nline2\nline3"

    def test_mixed_line_endings(self) -> None:
        result = normalize_terminal_text("a\r\nb\rc\nd")
        assert result == "a\nb\nc\nd"
        assert "\r" not in result

    def test_complex_ansi_sequence(self) -> None:
        # Complex SGR with many parameters
        result = normalize_terminal_text("\033[1;2;3;4;5;6;7;8;9mtext")
        assert result == "text"

    def test_multiple_bare_sgr(self) -> None:
        result = normalize_terminal_text("1;31m\n2;32mtext")
        assert "1;31m" not in result
        assert "text" in result
