#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for screen.py helper extensions."""

from __future__ import annotations

from provide.uterm.screen import (
    clean_screen_for_display,
    extract_action_tags,
    extract_key_value_pairs,
    extract_menu_options,
    extract_numbered_list,
)


class TestExtractActionTags:
    def test_single_tag(self) -> None:
        assert extract_action_tags("Press <Move> to continue") == ["Move"]

    def test_multiple_tags(self) -> None:
        result = extract_action_tags("<Attack> or <Retreat>")
        assert result == ["Attack", "Retreat"]

    def test_deduplicates(self) -> None:
        result = extract_action_tags("<Move> then <Move>")
        assert result == ["Move"]

    def test_case_insensitive_dedup(self) -> None:
        result = extract_action_tags("<Move> and <move>")
        assert result == ["Move"]

    def test_max_tags(self) -> None:
        text = " ".join(f"<Tag{i}>" for i in range(20))
        result = extract_action_tags(text, max_tags=3)
        assert len(result) == 3

    def test_empty_string(self) -> None:
        assert extract_action_tags("") == []

    def test_no_tags(self) -> None:
        assert extract_action_tags("no tags here") == []


class TestCleanScreenForDisplay:
    def test_returns_content_lines(self) -> None:
        screen = "line one\nline two\n"
        result = clean_screen_for_display(screen)
        assert "line one" in result
        assert "line two" in result

    def test_max_lines(self) -> None:
        screen = "\n".join(f"line {i}" for i in range(50))
        result = clean_screen_for_display(screen, max_lines=5)
        assert len(result) <= 5

    def test_skips_wide_padding(self) -> None:
        padding = " " * 80
        screen = f"content\n{padding}\nmore content"
        result = clean_screen_for_display(screen)
        assert padding not in result

    def test_empty_lines_included_if_stripped_nonempty(self) -> None:
        screen = "line1\n  \nline3"
        result = clean_screen_for_display(screen)
        assert len(result) == 3
        assert result[0] == "line1"
        assert result[1] == "  "
        assert result[2] == "line3"

    def test_max_lines_is_enforced(self) -> None:
        screen = "\n".join(f"line {i}" for i in range(10))
        result = clean_screen_for_display(screen, max_lines=3)
        assert len(result) == 3
        assert "line 0" in result[0]
        assert "line 1" in result[1]
        assert "line 2" in result[2]


class TestExtractMenuOptions:
    def test_angle_bracket_menu(self) -> None:
        screen = "<A> Option One  <B> Option Two"
        result = extract_menu_options(screen)
        assert ("A", "Option One") in result
        assert ("B", "Option Two") in result
        assert len(result) == 2

    def test_square_bracket_menu(self) -> None:
        screen = "[X] Exit  [C] Continue"
        result = extract_menu_options(screen)
        assert ("X", "Exit") in result
        assert ("C", "Continue") in result

    def test_paren_menu(self) -> None:
        screen = "(Q) Quit  (S) Save"
        result = extract_menu_options(screen)
        assert ("Q", "Quit") in result
        assert ("S", "Save") in result

    def test_empty_screen(self) -> None:
        assert extract_menu_options("") == []

    def test_custom_pattern(self) -> None:
        screen = "1: First option\n2: Second option"
        result = extract_menu_options(screen, pattern=r"(\d+): (.+)")
        assert ("1", "First option") in result
        assert len(result) == 2

    def test_invalid_pattern_returns_empty(self) -> None:
        result = extract_menu_options("anything", pattern=r"(invalid(?P<bad>)")
        assert result == []

    def test_extracts_multiple_same_key(self) -> None:
        screen = "<Q> First\n<Q> Second"
        result = extract_menu_options(screen)
        # Default pattern captures both
        assert len(result) >= 1


class TestExtractMenuOptionsEmptyDescription:
    """Covers the ``if description`` false branch in extract_menu_options."""

    def test_whitespace_only_description_skipped(self) -> None:
        from provide.uterm.screen import extract_menu_options

        # <A> has only a space before the next menu entry → description is empty
        screen = "<A>   <B> real option"
        result = extract_menu_options(screen)
        keys = [k for k, _d in result]
        assert "A" not in keys  # empty description → skipped
        assert "B" in keys


class TestExtractNumberedList:
    def test_dot_format(self) -> None:
        screen = "1. Alpha\n2. Beta\n3. Gamma"
        result = extract_numbered_list(screen)
        assert result == [("1", "Alpha"), ("2", "Beta"), ("3", "Gamma")]
        assert len(result) == 3

    def test_paren_format(self) -> None:
        screen = "1) First\n2) Second"
        result = extract_numbered_list(screen)
        assert ("1", "First") in result
        assert ("2", "Second") in result

    def test_empty(self) -> None:
        assert extract_numbered_list("no list here") == []

    def test_custom_pattern(self) -> None:
        screen = "  Item 1 - description\n  Item 2 - other"
        result = extract_numbered_list(screen, pattern=r"Item (\d+) - (.+)")
        assert ("1", "description") in result
        assert len(result) == 2

    def test_empty_description_is_skipped(self) -> None:
        """A matching line whose description is whitespace is dropped.

        Covers the ``if description`` false branch — without this the
        extractor would append (number, "") to the result.
        """
        screen = "1.    \n2. real item"
        result = extract_numbered_list(screen)
        assert result == [("2", "real item")]

    def test_invalid_pattern_returns_empty(self) -> None:
        result = extract_numbered_list("1. test", pattern=r"(invalid(?P<bad>)")
        assert result == []

    def test_strips_description(self) -> None:
        screen = "1.  Description with trailing spaces   \n2. Another"
        result = extract_numbered_list(screen)
        assert ("1", "Description with trailing spaces") in result

    def test_excludes_empty_descriptions(self) -> None:
        screen = "1. Item\n2.\n3. Another"
        result = extract_numbered_list(screen)
        assert ("1", "Item") in result
        assert ("3", "Another") in result
        assert len(result) == 2


class TestExtractKeyValuePairs:
    def test_basic_extraction(self) -> None:
        screen = "Credits: 5,000  Sector: 42"
        patterns = {
            "credits": r"Credits:\s*([\d,]+)",
            "sector": r"Sector:\s*(\d+)",
        }
        result = extract_key_value_pairs(screen, patterns)
        assert result["credits"] == "5,000"
        assert result["sector"] == "42"
        assert len(result) == 2

    def test_missing_field(self) -> None:
        result = extract_key_value_pairs("nothing here", {"foo": r"foo:\s*(\w+)"})
        assert "foo" not in result
        assert len(result) == 0

    def test_case_insensitive(self) -> None:
        screen = "CREDITS: 100"
        result = extract_key_value_pairs(screen, {"credits": r"credits:\s*(\d+)"})
        assert result["credits"] == "100"

    def test_partial_extraction(self) -> None:
        screen = "Credits: 50  Health: not-found"
        patterns = {
            "credits": r"Credits:\s*(\d+)",
            "health": r"Health:\s*(\d+)",
        }
        result = extract_key_value_pairs(screen, patterns)
        assert result["credits"] == "50"
        assert "health" not in result

    def test_empty_patterns(self) -> None:
        result = extract_key_value_pairs("anything here", {})
        assert result == {}

    def test_invalid_pattern_skipped(self) -> None:
        patterns = {
            "valid": r"Credits:\s*(\d+)",
            "invalid": r"(bad(?P<invalid>)",
        }
        screen = "Credits: 100"
        result = extract_key_value_pairs(screen, patterns)
        assert result["valid"] == "100"
        assert "invalid" not in result
