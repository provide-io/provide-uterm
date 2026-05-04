#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.screen."""

from __future__ import annotations

from provide.terminal.screen import (
    clean_screen_for_display,
    decode_cp437,
    encode_cp437,
    extract_action_tags,
    extract_key_value_pairs,
    normalize_terminal_text,
    strip_ansi,
)


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


# ---------------------------------------------------------------------------
# Mutation-killing tests for normalize_terminal_text
# ---------------------------------------------------------------------------


class TestNormalizeTerminalTextMutants:
    """Kill surviving mutants in normalize_terminal_text.

    mutmut_27: _BARE_SGR_LINE_PREFIX_RE.sub('', ...) → sub('XXXX', ...)
    mutmut_33: _BARE_SGR_RE.sub('', ...) → sub('XXXX', ...)
    """

    def test_line_prefix_bare_sgr_not_replaced_with_xxxx(self) -> None:
        """Bare SGR at line start before uppercase letter must be REMOVED, not replaced.

        _BARE_SGR_LINE_PREFIX_RE matches e.g. '1;31m' before 'SOME' at line start.
        Mutation: sub('XXXX', ...) would leave 'XXXX' in output.
        """
        # '1;31m' at line start followed by uppercase letter matches the pattern
        text = "1;31mSOME TEXT"
        result = normalize_terminal_text(text)
        assert "1;31m" not in result, "Bare SGR prefix should be removed"
        assert "XXXX" not in result, "Replacement should be empty string, not 'XXXX'"
        assert "SOME TEXT" in result, "Non-SGR content must be preserved"

    def test_bare_sgr_not_replaced_with_xxxx(self) -> None:
        """Isolated bare SGR fragment between whitespace/start and escape/whitespace/end
        must be REMOVED (empty string sub), not replaced with 'XXXX'.

        _BARE_SGR_RE matches e.g. '1;31m' between newline and ESC.
        Mutation: sub('XXXX', ...) would insert 'XXXX'.
        """
        # bare SGR fragment after newline before ESC matches _BARE_SGR_RE
        text = "before\n1;31m\x1bmore"
        result = normalize_terminal_text(text)
        assert "1;31m" not in result, "Bare SGR should be removed"
        assert "XXXX" not in result, "Sub replacement must be '' not 'XXXX'"
        assert "before" in result
        assert "more" in result


# ---------------------------------------------------------------------------
# Mutation-killing tests for extract_action_tags
# ---------------------------------------------------------------------------


class TestExtractActionTagsMutants:
    """Kill surviving mutants in extract_action_tags.

    mutmut_1: default max_tags=8 → max_tags=9
    mutmut_15: continue → break (when tag already in seen)
    """

    def test_default_max_tags_is_8_not_9(self) -> None:
        """With 9 unique tags, default max_tags=8 returns 8 items.

        Mutation max_tags=9 would return 9 items.
        """
        text = " ".join(f"<Tag{i}>" for i in range(9))
        result = extract_action_tags(text)  # uses default max_tags
        assert len(result) == 8, f"Default max_tags must be 8, got {len(result)} items"

    def test_duplicate_before_unique_does_not_stop_processing(self) -> None:
        """After a duplicate tag is encountered, processing must CONTINUE (not break).

        With continue: <A> <A> <B> → ['A', 'B'] (two items)
        With break: <A> <A> <B> → ['A'] (stops at first duplicate)
        """
        result = extract_action_tags("<A> then <A> then <B>")
        assert "A" in result, "First tag must be included"
        assert "B" in result, "Tag after duplicate must also be included (continue, not break)"
        assert len(result) == 2, f"Expected 2 unique tags, got {result}"


# ---------------------------------------------------------------------------
# Mutation-killing tests for clean_screen_for_display
# ---------------------------------------------------------------------------


class TestCleanScreenForDisplayMutants:
    """Kill mutmut_1: default max_lines=30 → max_lines=31."""

    def test_default_max_lines_is_30_not_31(self) -> None:
        """With 31 content lines, default max_lines=30 returns exactly 30 lines.

        Mutation max_lines=31 would return 31 lines.
        """
        screen = "\n".join(f"content line {i}" for i in range(31))
        result = clean_screen_for_display(screen)  # uses default max_lines
        assert len(result) == 30, f"Default max_lines must be 30, got {len(result)}"


# ---------------------------------------------------------------------------
# Mutation-killing tests for extract_key_value_pairs
# ---------------------------------------------------------------------------


class TestExtractKeyValuePairsMutants:
    """Kill mutmut_9: continue → break when regex pattern is invalid."""

    def test_invalid_pattern_first_does_not_stop_valid_pattern(self) -> None:
        """When an invalid regex pattern comes FIRST, processing must CONTINUE.

        With continue: invalid first, valid second → valid result found.
        With break: invalid first → breaks immediately → valid result missed.
        """
        patterns = {
            "invalid": r"(bad(?P<invalid>)",  # invalid regex — comes first
            "credits": r"Credits:\s*(\d+)",  # valid regex — comes second
        }
        screen = "Credits: 100"
        result = extract_key_value_pairs(screen, patterns)
        assert "credits" in result, "Valid pattern after invalid must be processed (continue not break)"
        assert result["credits"] == "100"
        assert "invalid" not in result
