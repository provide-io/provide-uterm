#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Key-value extraction from screen text using regex patterns."""

from __future__ import annotations

import re
from typing import Any


class KVExtractor:
    """Extract structured key-value data from screen text."""

    @staticmethod
    def _extract_single_field(screen: str, config: Any) -> tuple[str, Any] | None:
        """Extract one field from the screen using its config dict.

        Args:
            screen: Screen text to extract from
            config: Single field config dict with 'field', 'type', and 'regex' keys

        Returns:
            ``(field_name, converted_value)`` on success, ``None`` if no match or skip.
        """
        field_name = config.get("field")
        field_type = config.get("type", "string")
        pattern = config.get("regex")

        if not field_name or not pattern:
            return None

        # Use findall to get all matches and take the last one.
        # Screen buffers contain scroll history, so the most recent value
        # is at the end of the screen — re.search() would find old values first.
        compiled = re.compile(pattern, re.MULTILINE | re.IGNORECASE)
        matches = list(compiled.finditer(screen))
        if not matches:
            return None
        match = matches[-1]

        # Get captured group (first group or whole match)
        try:
            value_str = match.group(1) if match.lastindex else match.group(0)
        except IndexError:
            value_str = match.group(0)

        # Convert to target type
        try:
            converted_value = KVExtractor._convert_type(value_str, field_type)
        except (ValueError, TypeError):
            return None

        return field_name, converted_value

    @staticmethod
    def extract(
        screen: str,
        kv_config: dict[str, Any] | list[dict[str, Any]] | None,
        run_validation: bool = True,
    ) -> dict[str, Any] | None:
        """Extract key-value data from screen using configured patterns.

        Args:
            screen: Screen text to extract from
            kv_config: Extraction configuration from prompt pattern
                Can be a single field config or list of field configs
            run_validation: Whether to validate extracted values (default True)

        Returns:
            Dictionary of extracted values, None if config invalid or extraction failed
            May include "_validation" key with validation results
        """
        if not kv_config:
            return None

        # Handle single field config (convert to list)
        if isinstance(kv_config, dict) and "field" in kv_config:
            configs = [kv_config]
        elif isinstance(kv_config, list):
            configs = kv_config
        else:
            return None

        extracted: dict[str, Any] = {}

        for config in configs:
            result = KVExtractor._extract_single_field(screen, config)
            if result is not None:
                field_name, converted_value = result
                extracted[field_name] = converted_value

        if not extracted:
            return None

        # Run validation if requested
        if run_validation and kv_config:
            validation_result = KVExtractor._validate(extracted, kv_config)
            extracted["_validation"] = validation_result

        return extracted

    @staticmethod
    def _validate_numeric(field: str, value: Any, rules: dict[str, Any], errors: list[str], field_type: str) -> None:
        """Validate an int or float field against type and min/max constraints."""
        expected: type = int if field_type == "int" else float
        if not isinstance(value, expected):
            errors.append(f"{field}: expected {field_type}, got {type(value).__name__}")
            return
        if "min" in rules and value < rules["min"]:
            errors.append(f"{field}: value {value} below min {rules['min']}")
        if "max" in rules and value > rules["max"]:
            errors.append(f"{field}: value {value} exceeds max {rules['max']}")

    @staticmethod
    def _validate_string(field: str, value: Any, rules: dict[str, Any], errors: list[str]) -> None:
        """Validate a string field against type, pattern, and allowed_values constraints."""
        if not isinstance(value, str):
            errors.append(f"{field}: expected string, got {type(value).__name__}")
            return
        if "pattern" in rules and not re.match(rules["pattern"], value):
            errors.append(f"{field}: value '{value}' does not match pattern {rules['pattern']}")
        if "allowed_values" in rules and value not in rules["allowed_values"]:
            errors.append(f"{field}: value '{value}' not in allowed values {rules['allowed_values']}")

    @staticmethod
    def _validate(
        extracted: dict[str, Any],
        kv_config: dict[str, Any] | list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Validate extracted values against constraints.

        Args:
            extracted: Dictionary of extracted values
            kv_config: Extraction configuration (single dict or list)

        Returns:
            Dictionary with keys:
                valid: bool - True if all validations pass
                errors: list - Validation error messages
        """
        errors: list[str] = []
        configs = kv_config if isinstance(kv_config, list) else [kv_config]

        for cfg in configs:
            field = cfg.get("field")
            if not isinstance(field, str):
                continue

            value = extracted.get(field)
            validate_rules = cfg.get("validate") or {}
            is_required = cfg.get("required", False)
            field_type = cfg.get("type", "string")

            if is_required and value is None:
                errors.append(f"{field}: required but not found")
                continue
            if value is None:
                continue

            if field_type in ("int", "float"):
                KVExtractor._validate_numeric(field, value, validate_rules, errors, field_type)
            elif field_type == "string":
                KVExtractor._validate_string(field, value, validate_rules, errors)

        return {"valid": len(errors) == 0, "errors": errors}

    @staticmethod
    def _convert_type(value_str: str, target_type: str) -> Any:
        """Convert string value to target type.

        Args:
            value_str: String value to convert
            target_type: Target type name ("string", "int", "float", "bool")

        Returns:
            Converted value

        Raises:
            ValueError: If conversion fails
        """
        value_str = value_str.strip()

        if target_type == "string":
            return value_str

        if target_type == "int":
            # Remove commas for number parsing
            return int(value_str.replace(",", ""))

        if target_type == "float":
            # Remove commas for number parsing
            return float(value_str.replace(",", ""))

        if target_type == "bool":
            # Boolean conversion
            lower_val = value_str.lower()
            if lower_val in ("true", "yes", "y", "1", "on"):
                return True
            if lower_val in ("false", "no", "n", "0", "off"):
                return False
            raise ValueError(f"Cannot convert '{value_str}' to bool")

        # Unknown type, return as string
        return value_str


def extract_kv(screen: str, kv_config: dict[str, Any] | list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Convenience function for extracting K/V data.

    Args:
        screen: Screen text
        kv_config: Extraction configuration

    Returns:
        Extracted data or None
    """
    return KVExtractor.extract(screen, kv_config)
