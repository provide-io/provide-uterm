# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""PII policy engine with nested traversal support."""

from __future__ import annotations

__all__ = [
    "MaskMode",
    "PIIRule",
    "get_pii_rules",
    "register_pii_rule",
    "replace_pii_rules",
    "sanitize_payload",
]

import copy
import hashlib
import threading
from dataclasses import dataclass
from typing import Any, Literal

MaskMode = Literal["drop", "redact", "hash", "truncate"]


@dataclass(frozen=True)
class PIIRule:
    path: tuple[str, ...]
    mode: MaskMode = "redact"
    truncate_to: int = 8


_DEFAULT_SENSITIVE_KEYS = {"password", "token", "authorization", "api_key", "secret"}
_lock = threading.Lock()
_rules: list[PIIRule] = []


def replace_pii_rules(rules: list[PIIRule]) -> None:
    with _lock:
        _rules.clear()
        _rules.extend(rules)


def register_pii_rule(rule: PIIRule) -> None:
    with _lock:
        _rules.append(rule)


def get_pii_rules() -> tuple[PIIRule, ...]:
    with _lock:
        return tuple(_rules)


def _mask(value: Any, mode: MaskMode, truncate_to: int) -> Any:
    if mode == "drop":
        return None
    if mode == "redact":
        return "***"
    if mode == "hash":
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]  # pragma: no mutate
    text = str(value)
    limit = max(0, truncate_to)
    return text[:limit] + ("..." if len(text) > limit else "")


def _match(path: tuple[str, ...], target: tuple[str, ...]) -> bool:
    if len(path) != len(target):
        return False
    return all(part == "*" or part == elem for part, elem in zip(path, target, strict=True))  # pragma: no mutate


def _apply_rule(node: Any, rule: PIIRule, current_path: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        output: dict[str, Any] = {}
        for key, value in node.items():
            child_path = (*current_path, key)
            if _match(rule.path, child_path):
                masked = _mask(value, rule.mode, rule.truncate_to)
                if masked is not None:
                    output[key] = masked
            else:
                output[key] = _apply_rule(value, rule, child_path)
        return output
    if isinstance(node, list):
        return [_apply_rule(item, rule, (*current_path, "*")) for item in node]  # pragma: no mutate
    return node


def _apply_default_sensitive_key_redaction(
    node: Any, original: Any, rule_targeted_keys: frozenset[str] | None = None
) -> Any:
    if rule_targeted_keys is None:
        rule_targeted_keys = frozenset()
    if isinstance(node, dict) and isinstance(original, dict):
        output: dict[str, Any] = {}
        for key, value in node.items():
            orig_value = original.get(key, value)
            if key.lower() in _DEFAULT_SENSITIVE_KEYS:
                if key in rule_targeted_keys or value != orig_value:
                    output[key] = value
                else:
                    output[key] = "***"
            else:
                output[key] = _apply_default_sensitive_key_redaction(value, orig_value, rule_targeted_keys)
        return output
    if isinstance(node, list) and isinstance(original, list):  # pragma: no mutate
        return [
            _apply_default_sensitive_key_redaction(item, orig, rule_targeted_keys)
            for item, orig in zip(node, original, strict=False)  # pragma: no mutate
        ]
    return node


def _collect_rule_leaf_keys(rules: tuple[PIIRule, ...]) -> frozenset[str]:
    """Collect the leaf key names that custom rules target."""
    return frozenset(rule.path[-1] for rule in rules if rule.path)


def _needs_deep_copy(rules: tuple[PIIRule, ...]) -> bool:  # pragma: no mutate
    """Return True if any rule targets a nested path (depth > 1)."""
    return any(len(rule.path) > 1 for rule in rules)  # pragma: no mutate


def sanitize_payload(payload: dict[str, Any], enabled: bool) -> dict[str, Any]:
    if not enabled:
        return dict(payload)
    rules = get_pii_rules()
    cleaned: Any = copy.deepcopy(payload) if _needs_deep_copy(rules) else dict(payload)  # pragma: no mutate
    for rule in rules:
        cleaned = _apply_rule(cleaned, rule)
    rule_targeted_keys = _collect_rule_leaf_keys(rules)
    cleaned = _apply_default_sensitive_key_redaction(cleaned, payload, rule_targeted_keys)
    if isinstance(cleaned, dict):
        return cleaned
    return {}


def reset_pii_rules_for_tests() -> None:
    replace_pii_rules([])
