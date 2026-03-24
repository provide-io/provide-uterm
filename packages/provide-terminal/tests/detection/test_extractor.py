#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for K/V extraction from screen text."""

from __future__ import annotations

import pytest

from provide.terminal.detection.extractor import KVExtractor, extract_kv


class TestKVExtractorBasics:
    """Test basic extraction functionality."""

    def test_extract_string_field(self) -> None:
        """Test extracting a simple string field."""
        screen = "Player: TestUser\nScore: 1000"
        config = {"field": "player", "type": "string", "regex": r"Player:\s*(\w+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["player"] == "TestUser"

    def test_extract_int_field(self) -> None:
        """Test extracting integer field."""
        screen = "Score: 1000\nLevel: 5"
        config = {"field": "score", "type": "int", "regex": r"Score:\s*(\d+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["score"] == 1000
        assert isinstance(result["score"], int)

    def test_extract_int_with_commas(self) -> None:
        """Test extracting integer with comma separators."""
        screen = "Credits: 1,234,567"
        config = {"field": "credits", "type": "int", "regex": r"Credits:\s*([\d,]+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["credits"] == 1234567

    def test_extract_float_field(self) -> None:
        """Test extracting float field."""
        screen = "Temperature: 98.6 degrees"
        config = {"field": "temp", "type": "float", "regex": r"Temperature:\s*([\d.]+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["temp"] == 98.6
        assert isinstance(result["temp"], float)

    def test_extract_bool_field(self) -> None:
        """Test extracting boolean field."""
        screen = "ANSI Graphics: Yes"
        config = {"field": "ansi", "type": "bool", "regex": r"ANSI Graphics:\s*(\w+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["ansi"] is True

    def test_extract_bool_false(self) -> None:
        """Test extracting false boolean."""
        screen = "Debug Mode: No"
        config = {"field": "debug", "type": "bool", "regex": r"Debug Mode:\s*(\w+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["debug"] is False

    def test_no_match_returns_none(self) -> None:
        """Test that no match returns None."""
        screen = "Player: TestUser"
        config = {"field": "score", "type": "int", "regex": r"Score:\s*(\d+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is None

    def test_invalid_config_returns_none(self) -> None:
        """Test that invalid config returns None."""
        screen = "Player: TestUser"
        result = KVExtractor.extract(screen, None, run_validation=False)
        assert result is None

        result = KVExtractor.extract(screen, {}, run_validation=False)
        assert result is None


class TestKVExtractorMultipleFields:
    """Test extracting multiple fields."""

    def test_extract_multiple_fields(self) -> None:
        """Test extracting multiple fields at once."""
        screen = """
        Player: TestUser
        Score: 1000
        Level: 5
        """
        configs = [
            {"field": "player", "type": "string", "regex": r"Player:\s*(\w+)"},
            {"field": "score", "type": "int", "regex": r"Score:\s*(\d+)"},
            {"field": "level", "type": "int", "regex": r"Level:\s*(\d+)"},
        ]

        result = KVExtractor.extract(screen, configs, run_validation=False)

        assert result is not None
        assert result["player"] == "TestUser"
        assert result["score"] == 1000
        assert result["level"] == 5

    def test_extract_partial_match(self) -> None:
        """Test that some fields can match while others don't."""
        screen = "Player: TestUser\nScore: 1000"
        configs = [
            {"field": "player", "type": "string", "regex": r"Player:\s*(\w+)"},
            {"field": "level", "type": "int", "regex": r"Level:\s*(\d+)"},  # Won't match
        ]

        result = KVExtractor.extract(screen, configs, run_validation=False)

        assert result is not None
        assert result["player"] == "TestUser"
        assert "level" not in result

    def test_single_field_as_dict(self) -> None:
        """Test that single field config as dict works."""
        screen = "Player: TestUser"
        config = {"field": "player", "type": "string", "regex": r"Player:\s*(\w+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["player"] == "TestUser"


class TestKVExtractorValidation:
    """Test validation functionality."""

    def test_validation_passes(self) -> None:
        """Test validation with valid data."""
        screen = "Score: 1000"
        config = {
            "field": "score",
            "type": "int",
            "regex": r"Score:\s*(\d+)",
            "validate": {"min": 0, "max": 2000},
        }

        result = KVExtractor.extract(screen, config, run_validation=True)

        assert result is not None
        assert result["_validation"]["valid"] is True
        assert len(result["_validation"]["errors"]) == 0

    def test_validation_min_constraint(self) -> None:
        """Test min validation constraint."""
        screen = "Score: -10"
        config = {
            "field": "score",
            "type": "int",
            "regex": r"Score:\s*(-?\d+)",
            "validate": {"min": 0},
        }

        result = KVExtractor.extract(screen, config, run_validation=True)

        assert result is not None
        assert result["_validation"]["valid"] is False
        assert any("below min" in err for err in result["_validation"]["errors"])

    def test_validation_max_constraint(self) -> None:
        """Test max validation constraint."""
        screen = "Score: 5000"
        config = {
            "field": "score",
            "type": "int",
            "regex": r"Score:\s*(\d+)",
            "validate": {"max": 2000},
        }

        result = KVExtractor.extract(screen, config, run_validation=True)

        assert result is not None
        assert result["_validation"]["valid"] is False
        assert any("exceeds max" in err for err in result["_validation"]["errors"])

    def test_validation_required_field_missing(self) -> None:
        """Test required field validation."""
        screen = "Player: TestUser"
        config = {
            "field": "score",
            "type": "int",
            "regex": r"Score:\s*(\d+)",
            "required": True,
        }

        result = KVExtractor.extract(screen, config, run_validation=True)

        # Should return None because required field is missing
        assert result is None

    def test_validation_required_field_present(self) -> None:
        """Test required field validation when present."""
        screen = "Score: 1000"
        config = {
            "field": "score",
            "type": "int",
            "regex": r"Score:\s*(\d+)",
            "required": True,
        }

        result = KVExtractor.extract(screen, config, run_validation=True)

        assert result is not None
        assert result["score"] == 1000
        assert result["_validation"]["valid"] is True

    def test_validation_pattern_constraint(self) -> None:
        """Test string pattern validation."""
        screen = "Name: Test123"
        config = {
            "field": "name",
            "type": "string",
            "regex": r"Name:\s*(\S+)",
            "validate": {"pattern": r"^[A-Za-z]+$"},  # Only letters
        }

        result = KVExtractor.extract(screen, config, run_validation=True)

        assert result is not None
        assert result["_validation"]["valid"] is False
        assert any("does not match pattern" in err for err in result["_validation"]["errors"])

    def test_validation_allowed_values(self) -> None:
        """Test allowed values constraint."""
        screen = "Mode: Debug"
        config = {
            "field": "mode",
            "type": "string",
            "regex": r"Mode:\s*(\w+)",
            "validate": {"allowed_values": ["Normal", "Test", "Production"]},
        }

        result = KVExtractor.extract(screen, config, run_validation=True)

        assert result is not None
        assert result["_validation"]["valid"] is False
        assert any("not in allowed values" in err for err in result["_validation"]["errors"])

    def test_validation_multiple_fields(self) -> None:
        """Test validation with multiple fields."""
        screen = "Score: 1000\nLevel: 150"
        configs = [
            {
                "field": "score",
                "type": "int",
                "regex": r"Score:\s*(\d+)",
                "validate": {"min": 0, "max": 2000},
            },
            {
                "field": "level",
                "type": "int",
                "regex": r"Level:\s*(\d+)",
                "validate": {"min": 1, "max": 100},
            },
        ]

        result = KVExtractor.extract(screen, configs, run_validation=True)

        assert result is not None
        assert result["score"] == 1000
        assert result["level"] == 150
        # Level should fail validation (> 100)
        assert result["_validation"]["valid"] is False


class TestKVExtractorTypeConversion:
    """Test type conversion edge cases."""

    def test_bool_conversion_true_variants(self) -> None:
        """Test various true boolean values."""
        for value in ["true", "True", "TRUE", "yes", "Yes", "y", "Y", "1", "on", "ON"]:
            config = {"field": "flag", "type": "bool", "regex": r"Flag:\s*(\w+)"}
            result = KVExtractor.extract(f"Flag: {value}", config, run_validation=False)
            assert result is not None
            assert result["flag"] is True, f"Failed for value: {value}"

    def test_bool_conversion_false_variants(self) -> None:
        """Test various false boolean values."""
        for value in ["false", "False", "FALSE", "no", "No", "n", "N", "0", "off", "OFF"]:
            config = {"field": "flag", "type": "bool", "regex": r"Flag:\s*(\w+)"}
            result = KVExtractor.extract(f"Flag: {value}", config, run_validation=False)
            assert result is not None
            assert result["flag"] is False, f"Failed for value: {value}"

    def test_bool_conversion_invalid(self) -> None:
        """Test invalid boolean conversion."""
        config = {"field": "flag", "type": "bool", "regex": r"Flag:\s*(\w+)"}
        result = KVExtractor.extract("Flag: maybe", config, run_validation=False)
        # Should return None because conversion failed
        assert result is None

    def test_int_conversion_failure(self) -> None:
        """Test integer conversion failure."""
        config = {"field": "score", "type": "int", "regex": r"Score:\s*(\w+)"}
        result = KVExtractor.extract("Score: abc", config, run_validation=False)
        assert result is None

    def test_float_conversion_with_commas(self) -> None:
        """Test float conversion with commas."""
        config = {"field": "value", "type": "float", "regex": r"Value:\s*([\d,.]+)"}
        result = KVExtractor.extract("Value: 1,234.56", config, run_validation=False)
        assert result is not None
        assert result["value"] == 1234.56

    def test_unknown_type_returns_string(self) -> None:
        """Test that unknown type returns string."""
        config = {"field": "value", "type": "unknown", "regex": r"Value:\s*(\w+)"}
        result = KVExtractor.extract("Value: test123", config, run_validation=False)
        assert result is not None
        assert result["value"] == "test123"
        assert isinstance(result["value"], str)


class TestKVExtractorRegexEdgeCases:
    """Test regex extraction edge cases."""

    def test_multiline_extraction(self) -> None:
        """Test extraction with multiline content."""
        screen = """
        Player Information:
        Name: TestUser
        Score: 1000
        """
        config = {"field": "name", "type": "string", "regex": r"Name:\s*(\w+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["name"] == "TestUser"

    def test_case_insensitive_extraction(self) -> None:
        """Test case-insensitive regex extraction."""
        screen = "PLAYER: TestUser"
        config = {"field": "player", "type": "string", "regex": r"player:\s*(\w+)"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["player"] == "TestUser"

    def test_no_capture_group(self) -> None:
        """Test extraction without capture group uses whole match."""
        screen = "Score: 1000"
        config = {"field": "score", "type": "int", "regex": r"\d+"}

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["score"] == 1000

    def test_multiple_capture_groups_uses_first(self) -> None:
        """Test that first capture group is used when multiple exist."""
        screen = "Player: TestUser (Level 5)"
        config = {
            "field": "player",
            "type": "string",
            "regex": r"Player:\s*(\w+)\s*\(Level\s*(\d+)\)",
        }

        result = KVExtractor.extract(screen, config, run_validation=False)

        assert result is not None
        assert result["player"] == "TestUser"  # First group, not "5"


class TestConvenienceFunction:
    """Test the extract_kv convenience function."""

    def test_extract_kv_function(self) -> None:
        """Test that extract_kv convenience function works."""
        screen = "Score: 1000"
        config = {"field": "score", "type": "int", "regex": r"Score:\s*(\d+)"}

        result = extract_kv(screen, config)

        assert result is not None
        assert result["score"] == 1000

    def test_extract_kv_with_none_config(self) -> None:
        """Test extract_kv with None config."""
        result = extract_kv("Score: 1000", None)
        assert result is None


class TestKVExtractorMissingCoverage:
    """Cover branches not exercised by the existing test suite."""

    def test_dict_config_without_field_key_returns_none(self) -> None:
        """A dict config that has no 'field' key falls to the else branch and returns None."""
        # A dict without 'field' key is not a single-field config and not a list
        result = KVExtractor.extract("Score: 100", {"regex": r"Score:\s*(\d+)"}, run_validation=False)
        assert result is None

    def test_config_entry_with_missing_field_or_pattern_is_skipped(self) -> None:
        """A list entry missing 'field' or 'regex' is skipped (continue branch)."""
        configs = [
            {"field": "", "regex": r"Score:\s*(\d+)", "type": "int"},  # empty field_name → falsy
            {"field": "score", "regex": "", "type": "int"},  # empty pattern → falsy
        ]
        result = KVExtractor.extract("Score: 100", configs, run_validation=False)
        assert result is None

    def test_validate_field_with_non_string_field_key_is_skipped(self) -> None:
        """_validate skips config entries where 'field' is not a string."""
        extracted = {"score": 100}
        kv_config = [
            {42: "not_a_string_key"},  # field will be None (not in dict → get returns None)
        ]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is True  # no errors because entry was skipped

    def test_validate_float_type_check_fails(self) -> None:
        """_validate records error when field_type='float' but value is not float."""
        # Inject a non-float value directly into extracted dict
        extracted = {"value": "not_a_float"}
        kv_config = [{"field": "value", "type": "float"}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is False
        assert any("expected float" in e for e in result["errors"])

    def test_validate_float_min_max_constraints(self) -> None:
        """_validate checks min/max for float values."""
        extracted = {"temp": 200.0}
        kv_config = [{"field": "temp", "type": "float", "validate": {"min": 0.0, "max": 100.0}}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is False
        assert any("exceeds max" in e for e in result["errors"])

    def test_validate_float_below_min(self) -> None:
        """_validate records error for float below min."""
        extracted = {"temp": -5.0}
        kv_config = [{"field": "temp", "type": "float", "validate": {"min": 0.0}}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is False
        assert any("below min" in e for e in result["errors"])

    def test_validate_string_type_check_fails(self) -> None:
        """_validate records error when field_type='string' but value is not str."""
        extracted = {"name": 42}  # int, not string
        kv_config = [{"field": "name", "type": "string"}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is False
        assert any("expected string" in e for e in result["errors"])

    def test_validate_string_pattern_passes(self) -> None:
        """_validate does not error when pattern constraint matches."""
        extracted = {"name": "Alice"}
        kv_config = [{"field": "name", "type": "string", "validate": {"pattern": r"^[A-Za-z]+$"}}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is True

    def test_validate_string_allowed_values_passes(self) -> None:
        """_validate does not error when value is in allowed_values."""
        extracted = {"mode": "Normal"}
        kv_config = [{"field": "mode", "type": "string", "validate": {"allowed_values": ["Normal", "Debug"]}}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is True

    def test_validate_value_none_not_required_skipped(self) -> None:
        """_validate skips further checks when value is None and not required."""
        extracted = {}  # 'score' not extracted
        kv_config = [{"field": "score", "type": "int", "required": False, "validate": {"min": 0}}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is True  # no errors; field skipped

    def test_extract_with_no_capture_group_index_error_fallback(self) -> None:
        """Regex with no groups falls back to match.group(0) without IndexError."""
        config = {"field": "match", "type": "string", "regex": r"Hello"}
        result = KVExtractor.extract("Hello World", config, run_validation=False)
        assert result is not None
        assert result["match"] == "Hello"

    def test_validate_required_field_missing_among_extracted(self) -> None:
        """_validate flags required fields not present in extracted dict (lines 116-117)."""
        # 'score' was extracted but 'level' is required and missing from extracted
        extracted = {"score": 100}
        kv_config = [
            {"field": "score", "type": "int"},
            {"field": "level", "type": "int", "required": True},  # required, not in extracted
        ]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is False
        assert any("required but not found" in e for e in result["errors"])

    def test_validate_int_type_check_fails(self) -> None:
        """_validate errors when field_type='int' but value is not an int (lines 126-127)."""
        extracted = {"score": "not_an_int"}
        kv_config = [{"field": "score", "type": "int"}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is False
        assert any("expected int" in e for e in result["errors"])

    def test_validate_unknown_type_falls_through_elif_chain(self) -> None:
        """_validate skips type checks for unknown types (line 146->104 branch)."""
        # field_type="bool" doesn't match int/float/string → falls through elif chain
        extracted = {"flag": True}
        kv_config = [{"field": "flag", "type": "bool"}]
        result = KVExtractor._validate(extracted, kv_config)
        # No type errors because no elif branch matched
        assert result["valid"] is True

    def test_extract_index_error_fallback_via_mock(self) -> None:
        """IndexError from match.group(1) falls back to match.group(0) (lines 64-65)."""
        from unittest.mock import MagicMock, patch

        mock_match = MagicMock()
        mock_match.lastindex = 1  # truthy — triggers group(1) path
        mock_match.group.side_effect = lambda n: (_ for _ in ()).throw(IndexError("no group")) if n == 1 else "fallback"

        config = {"field": "val", "type": "string", "regex": r"val: (\w+)"}
        with patch("provide.terminal.detection.extractor.re.search", return_value=mock_match):
            result = KVExtractor.extract("val: something", config, run_validation=False)
        assert result is not None
        assert result["val"] == "fallback"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
