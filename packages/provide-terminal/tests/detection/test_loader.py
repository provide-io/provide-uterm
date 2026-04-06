#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for load_ruleset loader function."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from provide.terminal.detection.loader import load_ruleset
from provide.terminal.detection.rules import RuleSet


class TestLoadRulesetFromPath:
    """Test loading RuleSet from a Path."""

    def test_load_from_valid_path(self, simple_rules_file: Path) -> None:
        rs = load_ruleset(simple_rules_file)
        assert isinstance(rs, RuleSet)
        assert rs.game == "test"
        assert len(rs.prompts) == 1

    def test_load_from_path_with_kv(self, kv_rules_file: Path) -> None:
        rs = load_ruleset(kv_rules_file)
        assert len(rs.prompts[0].kv_extract) == 2

    def test_bad_path_raises_value_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.json"
        with pytest.raises(ValueError, match="Rules file not found"):
            load_ruleset(missing)


class TestLoadRulesetFromString:
    """Test loading RuleSet from a JSON string."""

    def test_load_from_json_string(self) -> None:
        data = json.dumps(
            {
                "version": "1.0",
                "game": "testgame",
                "prompts": [
                    {
                        "id": "prompt.hello",
                        "match": {"pattern": "Hello", "match_mode": "contains"},
                    }
                ],
            }
        )
        rs = load_ruleset(data)
        assert isinstance(rs, RuleSet)
        assert rs.game == "testgame"
        assert rs.prompts[0].id == "prompt.hello"

    def test_bad_json_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Failed to parse rules"):
            load_ruleset("{not valid json")

    def test_valid_json_missing_game_raises_value_error(self) -> None:
        data = json.dumps({"version": "1.0", "prompts": []})
        with pytest.raises(ValueError, match="Failed to parse rules"):
            load_ruleset(data)


class TestLoadRulesetPassthrough:
    """Test that an existing RuleSet is passed through unchanged."""

    def test_ruleset_passthrough(self) -> None:
        original = RuleSet(game="passthrough")
        result = load_ruleset(original)
        assert result is original

    def test_ruleset_passthrough_preserves_data(self) -> None:
        original = RuleSet(game="myapp", version="2.0")
        result = load_ruleset(original)
        assert result.game == "myapp"
        assert result.version == "2.0"
