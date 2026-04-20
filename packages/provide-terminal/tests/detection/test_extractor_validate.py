#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for KVExtractor._validate internals and extraction edge cases."""

from __future__ import annotations

import pytest

from provide.terminal.detection.extractor import KVExtractor


class TestKVExtractorValidateInternals:
    """Cover _validate branches and extraction edge cases not exercised elsewhere."""

    def test_dict_config_without_field_key_returns_none(self) -> None:
        """A dict config that has no 'field' key falls to the else branch and returns None."""
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
        extracted = {"value": "not_a_float"}
        kv_config = [{"field": "value", "type": "float"}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is False
        assert any("expected float" in e for e in result["errors"])

    def test_validate_float_max_constraint(self) -> None:
        """_validate checks max for float values."""
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
        """_validate flags required fields not present in extracted dict."""
        extracted = {"score": 100}
        kv_config = [
            {"field": "score", "type": "int"},
            {"field": "level", "type": "int", "required": True},  # required, not in extracted
        ]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is False
        assert any("required but not found" in e for e in result["errors"])

    def test_validate_int_type_check_fails(self) -> None:
        """_validate errors when field_type='int' but value is not an int."""
        extracted = {"score": "not_an_int"}
        kv_config = [{"field": "score", "type": "int"}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is False
        assert any("expected int" in e for e in result["errors"])

    def test_validate_unknown_type_falls_through_elif_chain(self) -> None:
        """_validate skips type checks for unknown types (no elif branch matches)."""
        extracted = {"flag": True}
        kv_config = [{"field": "flag", "type": "bool"}]
        result = KVExtractor._validate(extracted, kv_config)
        assert result["valid"] is True

    def test_extract_index_error_fallback_via_mock(self) -> None:
        """IndexError from match.group(1) falls back to match.group(0)."""
        from unittest.mock import MagicMock, patch

        mock_match = MagicMock()
        mock_match.lastindex = 1  # truthy — triggers group(1) path
        mock_match.group.side_effect = lambda n: (_ for _ in ()).throw(IndexError("no group")) if n == 1 else "fallback"
        mock_pattern = MagicMock()
        mock_pattern.finditer.return_value = [mock_match]

        config = {"field": "val", "type": "string", "regex": r"val: (\w+)"}
        with patch("provide.terminal.detection.extractor.re.compile", return_value=mock_pattern):
            result = KVExtractor.extract("val: something", config, run_validation=False)
        assert result is not None
        assert result["val"] == "fallback"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
