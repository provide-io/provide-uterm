from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def snap_factory():
    """Factory for creating ScreenSnapshot dicts."""

    def _snap(screen: str, *, cursor_at_end: bool = True, cursor_y: int | None = None) -> dict[str, Any]:
        return {
            "screen": screen,
            "screen_hash": hashlib.sha256(screen.encode()).hexdigest(),
            "cursor_at_end": cursor_at_end,
            "has_trailing_space": False,
            "cursor": {"y": cursor_y if cursor_y is not None else screen.count("\n"), "x": 0},
            "captured_at": 1000.0,
        }

    return _snap


@pytest.fixture
def rules_file_factory(tmp_path: Path):
    """Factory for creating rules.json files."""

    def _rules(prompts: list[dict[str, Any]], *, version: str = "1.0", game: str = "test") -> Path:
        p = tmp_path / "rules.json"
        p.write_text(json.dumps({"version": version, "game": game, "prompts": prompts}))
        return p

    return _rules


@pytest.fixture
def simple_rules_file(rules_file_factory):
    """A rules.json with one contains-match pattern."""
    return rules_file_factory(
        [
            {
                "id": "prompt.hello",
                "match": {"pattern": "Hello there", "match_mode": "contains"},
                "input_type": "single_key",
            },
        ]
    )


@pytest.fixture
def kv_rules_file(rules_file_factory):
    """A rules.json with KV extraction."""
    return rules_file_factory(
        [
            {
                "id": "prompt.sector",
                "match": {"pattern": "Sector\\s+\\d+\\s*:", "match_mode": "regex"},
                "input_type": "single_key",
                "kv_extract": [
                    {"field": "sector", "regex": "Sector\\s+(\\d+)", "type": "int"},
                    {"field": "credits", "regex": "Credits:\\s+([\\d,]+)", "type": "int"},
                ],
            }
        ]
    )
