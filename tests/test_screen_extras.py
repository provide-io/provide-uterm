#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for screen.py helper extensions."""

from __future__ import annotations

from provide.terminal.screen import (
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


class TestExtractMenuOptions:
    def test_angle_bracket_menu(self) -> None:
        screen = "<A> Option One  <B> Option Two"
        result = extract_menu_options(screen)
        assert ("A", "Option One") in result
        assert ("B", "Option Two") in result

    def test_square_bracket_menu(self) -> None:
        screen = "[X] Exit  [C] Continue"
        result = extract_menu_options(screen)
        assert ("X", "Exit") in result

    def test_empty_screen(self) -> None:
        assert extract_menu_options("") == []

    def test_custom_pattern(self) -> None:
        screen = "1: First option\n2: Second option"
        result = extract_menu_options(screen, pattern=r"(\d+): (.+)")
        assert ("1", "First option") in result


class TestExtractNumberedList:
    def test_dot_format(self) -> None:
        screen = "1. Alpha\n2. Beta\n3. Gamma"
        result = extract_numbered_list(screen)
        assert result == [("1", "Alpha"), ("2", "Beta"), ("3", "Gamma")]

    def test_paren_format(self) -> None:
        screen = "1) First\n2) Second"
        result = extract_numbered_list(screen)
        assert ("1", "First") in result

    def test_empty(self) -> None:
        assert extract_numbered_list("no list here") == []

    def test_custom_pattern(self) -> None:
        screen = "  Item 1 - description\n  Item 2 - other"
        result = extract_numbered_list(screen, pattern=r"Item (\d+) - (.+)")
        assert ("1", "description") in result


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

    def test_missing_field(self) -> None:
        result = extract_key_value_pairs("nothing here", {"foo": r"foo:\s*(\w+)"})
        assert "foo" not in result

    def test_case_insensitive(self) -> None:
        screen = "CREDITS: 100"
        result = extract_key_value_pairs(screen, {"credits": r"credits:\s*(\d+)"})
        assert result["credits"] == "100"
