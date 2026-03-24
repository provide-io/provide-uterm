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
            field_name = config.get("field")
            field_type = config.get("type", "string")
            pattern = config.get("regex")

            if not field_name or not pattern:
                continue

            # Try to extract using regex
            match = re.search(pattern, screen, re.MULTILINE | re.IGNORECASE)
            if not match:
                continue

            # Get captured group (first group or whole match)
            try:
                value_str = match.group(1) if match.lastindex else match.group(0)
            except IndexError:
                value_str = match.group(0)

            # Convert to target type
            try:
                converted_value = KVExtractor._convert_type(value_str, field_type)
                extracted[field_name] = converted_value
            except (ValueError, TypeError):
                # Conversion failed, skip this field
                continue

        if not extracted:
            return None

        # Run validation if requested
        if run_validation and kv_config:
            validation_result = KVExtractor._validate(extracted, kv_config)
            extracted["_validation"] = validation_result

        return extracted

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

            # Check required fields
            if is_required and value is None:
                errors.append(f"{field}: required but not found")
                continue

            # Skip validation if value is None and not required
            if value is None:
                continue

            # Type validation
            if field_type == "int":
                if not isinstance(value, int):
                    errors.append(f"{field}: expected int, got {type(value).__name__}")
                    continue

                # Check min/max constraints
                if "min" in validate_rules and value < validate_rules["min"]:
                    errors.append(f"{field}: value {value} below min {validate_rules['min']}")
                if "max" in validate_rules and value > validate_rules["max"]:
                    errors.append(f"{field}: value {value} exceeds max {validate_rules['max']}")

            elif field_type == "float":
                if not isinstance(value, float):
                    errors.append(f"{field}: expected float, got {type(value).__name__}")
                    continue

                # Check min/max constraints
                if "min" in validate_rules and value < validate_rules["min"]:
                    errors.append(f"{field}: value {value} below min {validate_rules['min']}")
                if "max" in validate_rules and value > validate_rules["max"]:
                    errors.append(f"{field}: value {value} exceeds max {validate_rules['max']}")

            elif field_type == "string":
                if not isinstance(value, str):
                    errors.append(f"{field}: expected string, got {type(value).__name__}")
                    continue

                # Check pattern constraint if present
                if "pattern" in validate_rules:
                    pattern = validate_rules["pattern"]
                    if not re.match(pattern, value):
                        errors.append(f"{field}: value '{value}' does not match pattern {pattern}")

                # Check allowed values
                if "allowed_values" in validate_rules:
                    allowed = validate_rules["allowed_values"]
                    if value not in allowed:
                        errors.append(f"{field}: value '{value}' not in allowed values {allowed}")

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
