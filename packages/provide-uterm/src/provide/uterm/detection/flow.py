#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Declarative flow execution over prompt-detection rules."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from provide.uterm.detection.detector import PromptDetector
from provide.uterm.detection.extractor import extract_kv

if TYPE_CHECKING:
    from provide.uterm.detection.rules import ActionRule, RuleSet


@dataclass(frozen=True)
class FlowStep:
    """Decision returned by :meth:`FlowEngine.advance`."""

    flow_id: str
    current_prompt_id: str | None
    next_action: str | None
    done: bool
    kv_data: dict[str, Any] = field(default_factory=dict)


class FlowEngine:
    """Advance named flows using existing prompt detectors and rule metadata."""

    def __init__(self, ruleset: RuleSet) -> None:
        self._ruleset = ruleset
        self._flows = {flow.id: flow for flow in ruleset.flows}
        self._prompt_patterns = {pattern["id"]: pattern for pattern in ruleset.to_prompt_patterns()}

    def advance(self, flow_id: str, screen: str, cursor: tuple[int, int] | None = None) -> FlowStep:
        """Return the next action for *flow_id* on the current screen."""
        flow = self._flows.get(flow_id)
        if flow is None:
            raise ValueError(f"unknown flow: {flow_id}")

        snapshot = self._snapshot(screen, cursor)
        last_index = len(flow.steps) - 1
        for index, action in enumerate(flow.steps):
            prompt_ids = self._candidate_prompt_ids(action)
            match = self._detect_prompt(snapshot, prompt_ids)
            if match is None:
                continue
            pattern = self._prompt_patterns.get(match.prompt_id, {})
            kv_data = extract_kv(screen, pattern.get("kv_extract")) or {}
            terminal = self._is_terminal(action, is_last=index == last_index)
            send_keys = action.keys if action.kind == "send_keys" else None
            return FlowStep(
                flow_id=flow.id,
                current_prompt_id=match.prompt_id,
                next_action=None if terminal else send_keys,
                done=terminal,
                kv_data=kv_data,
            )

        return FlowStep(flow_id=flow.id, current_prompt_id=None, next_action=None, done=False, kv_data={})

    def _candidate_prompt_ids(self, action: ActionRule) -> list[str]:
        candidates = list(action.gate_prompts)
        if action.expects_prompt and action.expects_prompt not in candidates:
            candidates.append(action.expects_prompt)
        return candidates

    def _detect_prompt(self, snapshot: dict[str, Any], prompt_ids: list[str]) -> Any | None:
        if not prompt_ids:
            return None
        patterns = [self._prompt_patterns[prompt_id] for prompt_id in prompt_ids if prompt_id in self._prompt_patterns]
        if not patterns:
            return None
        return PromptDetector(patterns).detect_prompt(snapshot)

    def _is_terminal(self, action: ActionRule, *, is_last: bool) -> bool:
        if action.kind == "noop":
            return True
        return is_last and action.keys is None

    def _snapshot(self, screen: str, cursor: tuple[int, int] | None) -> dict[str, Any]:
        if cursor is None:
            cursor_dict = {"x": 0, "y": screen.count("\n")}
        else:
            cursor_dict = {"x": cursor[0], "y": cursor[1]}
        return {
            "screen": screen,
            "screen_hash": hashlib.sha256(screen.encode()).hexdigest(),
            "cursor_at_end": True,
            "has_trailing_space": screen.endswith(" "),
            "cursor": cursor_dict,
        }
