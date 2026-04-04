#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for RuleSet, PromptRule, and KVExtractRule."""

from __future__ import annotations

import json
import re

import pytest

from provide.terminal.detection.rules import KVExtractRule, PromptRule, RegexRule, RuleSet


class TestRuleSetCreation:
    """Test RuleSet creation from PromptRule lists."""

    def test_empty_ruleset(self) -> None:
        rs = RuleSet(game="test", prompts=[])
        assert rs.game == "test"
        assert rs.prompts == []
        assert rs.version == "1.0"

    def test_ruleset_with_prompts(self) -> None:
        prompt = PromptRule(
            id="prompt.hello",
            match=RegexRule(pattern="Hello", match_mode="contains"),
        )
        rs = RuleSet(game="test", prompts=[prompt])
        assert len(rs.prompts) == 1
        assert rs.prompts[0].id == "prompt.hello"

    def test_ruleset_defaults(self) -> None:
        rs = RuleSet(game="mygame")
        assert rs.version == "1.0"
        assert rs.prompts == []
        assert rs.menus == []
        assert rs.flows == []
        assert rs.metadata == {}


class TestToPromptPatterns:
    """Test to_prompt_patterns() round-trip."""

    def test_empty_prompts(self) -> None:
        rs = RuleSet(game="test")
        assert rs.to_prompt_patterns() == []

    def test_contains_pattern_round_trip(self) -> None:
        rs = RuleSet(
            game="test",
            prompts=[
                PromptRule(
                    id="prompt.login",
                    match=RegexRule(pattern="Enter your name", match_mode="contains"),
                    input_type="multi_key",
                )
            ],
        )
        patterns = rs.to_prompt_patterns()
        assert len(patterns) == 1
        p = patterns[0]
        assert p["id"] == "prompt.login"
        assert p["input_type"] == "multi_key"
        assert p["regex"] == re.escape("Enter your name")
        assert p["auto_detected"] is False
        assert "negative_regex" not in p
        assert "kv_extract" not in p

    def test_regex_pattern_round_trip(self) -> None:
        rs = RuleSet(
            game="test",
            prompts=[
                PromptRule(
                    id="prompt.sector",
                    match=RegexRule(pattern=r"Sector\s+\d+", match_mode="regex"),
                )
            ],
        )
        patterns = rs.to_prompt_patterns()
        assert patterns[0]["regex"] == r"Sector\s+\d+"

    def test_unknown_match_mode_returns_none(self) -> None:
        # match_mode is a Literal — bypass validation to hit the implicit fall-through branch
        rule = RegexRule.model_construct(pattern="x", match_mode="unknown")  # type: ignore[arg-type]
        assert rule.to_regex() is None

    def test_exact_pattern_round_trip(self) -> None:
        rs = RuleSet(
            game="test",
            prompts=[
                PromptRule(
                    id="prompt.exact",
                    match=RegexRule(pattern="[Press ENTER]", match_mode="exact"),
                )
            ],
        )
        patterns = rs.to_prompt_patterns()
        assert patterns[0]["regex"] == r"^\[Press\ ENTER\]$"

    def test_kv_extract_included(self) -> None:
        rs = RuleSet(
            game="test",
            prompts=[
                PromptRule(
                    id="prompt.sector",
                    match=RegexRule(pattern="Sector", match_mode="contains"),
                    kv_extract=[
                        KVExtractRule(field="credits", regex=r"Credits:\s*(\d+)", type="int"),
                    ],
                )
            ],
        )
        patterns = rs.to_prompt_patterns()
        assert "kv_extract" in patterns[0]
        kv = patterns[0]["kv_extract"]
        assert len(kv) == 1
        assert kv[0]["field"] == "credits"
        assert kv[0]["regex"] == r"Credits:\s*(\d+)"
        assert kv[0]["type"] == "int"

    def test_negative_match_included(self) -> None:
        rs = RuleSet(
            game="test",
            prompts=[
                PromptRule(
                    id="prompt.menu",
                    match=RegexRule(pattern="choose", match_mode="contains"),
                    negative_match=RegexRule(pattern="do not show", match_mode="contains"),
                )
            ],
        )
        patterns = rs.to_prompt_patterns()
        assert "negative_regex" in patterns[0]
        assert patterns[0]["negative_regex"] == re.escape("do not show")

    def test_multiple_prompts_order_preserved(self) -> None:
        rs = RuleSet(
            game="test",
            prompts=[
                PromptRule(id="prompt.a", match=RegexRule(pattern="AAA", match_mode="contains")),
                PromptRule(id="prompt.b", match=RegexRule(pattern="BBB", match_mode="contains")),
                PromptRule(id="prompt.c", match=RegexRule(pattern="CCC", match_mode="contains")),
            ],
        )
        patterns = rs.to_prompt_patterns()
        assert [p["id"] for p in patterns] == ["prompt.a", "prompt.b", "prompt.c"]


class TestFromJsonFile:
    """Test RuleSet.from_json_file()."""

    def test_from_json_file_success(self, simple_rules_file) -> None:
        rs = RuleSet.from_json_file(simple_rules_file)
        assert rs.game == "test"
        assert len(rs.prompts) == 1
        assert rs.prompts[0].id == "prompt.hello"

    def test_from_json_file_with_kv(self, kv_rules_file) -> None:
        rs = RuleSet.from_json_file(kv_rules_file)
        assert len(rs.prompts) == 1
        assert len(rs.prompts[0].kv_extract) == 2

    def test_from_json_file_bad_json_raises_value_error(self, tmp_path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        with pytest.raises(ValueError, match="Failed to load rules"):
            RuleSet.from_json_file(bad)

    def test_from_json_file_missing_required_field_raises_value_error(self, tmp_path) -> None:
        # 'game' is required — omit it to trigger a validation error
        bad = tmp_path / "rules.json"
        bad.write_text(json.dumps({"version": "1.0", "prompts": []}))
        with pytest.raises(ValueError, match="Failed to load rules"):
            RuleSet.from_json_file(bad)
