#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Rule schema and loader for prompt/menu/flow definitions."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path

InputType = Literal["single_key", "multi_key", "any_key", "menu_choice", "none"]
MatchMode = Literal["regex", "contains", "exact"]
PromptKind = Literal[
    "login_name",
    "login_pass",
    "game_pass",
    "pause",
    "confirm",
    "menu",
    "input",
    "unknown",
]
ActionKind = Literal["send_keys", "wait", "noop"]


class RegexRule(BaseModel):
    pattern: str
    flags: int = re.MULTILINE | re.IGNORECASE
    match_mode: MatchMode = "regex"

    def to_regex(self) -> str:
        match self.match_mode:
            case "regex":
                return self.pattern
            case "contains":
                return re.escape(self.pattern)
            case "exact":
                return rf"^{re.escape(self.pattern)}$"


class ScreenConstraint(BaseModel):
    expect_cursor_at_end: bool = True
    cursor_row_min: int | None = None
    cursor_row_max: int | None = None
    cursor_col_min: int | None = None
    cursor_col_max: int | None = None


class KVExtractRule(BaseModel):
    field: str
    regex: str
    type: str = "string"
    flags: int = re.MULTILINE | re.IGNORECASE
    validate_rule: dict[str, Any] | None = Field(default=None, alias="validate")
    required: bool = False

    model_config = ConfigDict(populate_by_name=True)


class PromptRule(BaseModel):
    id: str
    kind: PromptKind = "unknown"
    input_type: InputType = "multi_key"
    match: RegexRule
    screen: ScreenConstraint = Field(default_factory=ScreenConstraint)
    kv_extract: list[KVExtractRule] = Field(default_factory=list)
    notes: str | None = None
    negative_match: RegexRule | None = None
    default_action: ActionRule | None = None


class MenuOption(BaseModel):
    key: str
    label: str


class MenuRule(BaseModel):
    id: str
    title_match: RegexRule | None = None
    prompt_match: RegexRule
    options: list[MenuOption] = Field(default_factory=list)
    notes: str | None = None


class TimingRule(BaseModel):
    min_wait_ms: int = 0
    max_wait_ms: int = 8000
    retry_ms: int = 250
    require_stable_screen: bool = True


class ActionRule(BaseModel):
    id: str
    kind: ActionKind
    keys: str | None = None
    expects_prompt: str | None = None
    timing: TimingRule = Field(default_factory=TimingRule)
    gate_prompts: list[str] = Field(default_factory=list)
    block_if_matches: list[RegexRule] = Field(default_factory=list)


class FlowRule(BaseModel):
    id: str
    description: str
    steps: list[ActionRule]


class RuleSet(BaseModel):
    version: str = "1.0"
    game: str
    prompts: list[PromptRule] = Field(default_factory=list)
    menus: list[MenuRule] = Field(default_factory=list)
    flows: list[FlowRule] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_prompt_patterns(self) -> list[dict[str, Any]]:
        patterns: list[dict[str, Any]] = []
        for prompt in self.prompts:
            regex = prompt.match.to_regex()
            pattern: dict[str, Any] = {
                "id": prompt.id,
                "regex": regex,
                "input_type": prompt.input_type,
                "expect_cursor_at_end": prompt.screen.expect_cursor_at_end,
                "notes": prompt.notes or "",
                "auto_detected": False,
            }
            if prompt.negative_match:
                pattern["negative_regex"] = prompt.negative_match.to_regex()
            if prompt.kv_extract:
                pattern["kv_extract"] = [
                    {
                        "field": item.field,
                        "regex": item.regex,
                        "type": item.type,
                        "flags": item.flags,
                        "validate": item.validate_rule,
                        "required": item.required,
                    }
                    for item in prompt.kv_extract
                ]
            patterns.append(pattern)
        return patterns

    @classmethod
    def from_json_file(cls, path: Path) -> RuleSet:
        try:
            data = json.loads(path.read_text())
            return cls.model_validate(data)
        except Exception as exc:
            raise ValueError(f"Failed to load rules from {path}: {exc}") from exc


class RuleLoadResult(BaseModel):
    source: str
    patterns: list[dict[str, Any]]
    metadata: dict[str, Any]

    model_config = ConfigDict(frozen=True)
