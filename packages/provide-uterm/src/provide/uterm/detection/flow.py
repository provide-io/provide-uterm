#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Declarative flow execution over prompt-detection rules."""

from __future__ import annotations

import hashlib
import re
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
        self._flows = {flow.id: flow for flow in ruleset.flows}
        self._prompt_patterns = {pattern["id"]: pattern for pattern in ruleset.to_prompt_patterns()}

    def advance(self, flow_id: str, screen: str, cursor: tuple[int, int] | None = None) -> FlowStep:
        """Return the next action for *flow_id* on the current screen.

        When several flow steps' gate prompts match (e.g. a stale prompt left in
        scrollback above the live one), the prompt whose match sits closest to the
        tail — the current cursor region — wins, so scrollback does not beat the
        live prompt. Ties keep the earliest flow step.
        """
        flow = self._flows.get(flow_id)
        if flow is None:
            raise ValueError(f"unknown flow: {flow_id}")

        snapshot = self._snapshot(screen, cursor)
        last_index = len(flow.steps) - 1
        best: tuple[tuple[int, int], int, ActionRule, Any] | None = None
        for index, action in enumerate(flow.steps):
            prompt_ids = self._candidate_prompt_ids(action)
            match = self._detect_prompt(snapshot, prompt_ids)
            if match is None:
                continue
            position = self._match_position(screen, match.prompt_id)
            if best is None or position > best[0]:
                best = (position, index, action, match)

        if best is None:
            return FlowStep(flow_id=flow.id, current_prompt_id=None, next_action=None, done=False)

        _position, index, action, match = best
        pattern = self._prompt_patterns[match.prompt_id]
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

    def _match_position(self, screen: str, prompt_id: str) -> tuple[int, int]:
        """Tail-most ranking key for the prompt's regex in *screen*.

        Returns ``(end, -start)`` for the prompt's tail-most match, so that
        :meth:`advance` can pick the prompt closest to the live cursor region
        and break a same-line tie toward the more-anchored match:

        - **End offset (primary, larger wins).** A match further down the screen
          ends at a larger absolute offset, so the live cursor-region prompt beats
          a stale scrollback match of an earlier flow step. Two matches that end at
          the same offset are necessarily on the same line.
        - **Negated start offset (secondary, larger ``-start`` = earlier start
          wins).** When two prompts match the SAME line and end at the same column
          — e.g. ``Enter your password:`` matched whole-line (start 0) vs. the
          generic suffix regex ``password[?:]\\s*$`` (start 11) — the anchored,
          longer match (earlier start) must win, not the suffix. Ranking by the raw
          tail-most start instead made the suffix steal the resolution.

        The detector only offers candidates it already matched, but an end-anchored
        pattern (``$``/``\\Z``) can match the detector's tail *region* while finding
        nothing in the full *screen* when trailing content shifts the anchor. In
        that case the prompt is at the tail, so the empty ``finditer`` falls back to
        ``(len(screen), 0)`` (tail-most end, neutral start) rather than crashing on
        ``max()`` of an empty iterator.
        """
        regex = self._prompt_patterns[prompt_id]["regex"]
        return max(((hit.end(), -hit.start()) for hit in re.finditer(regex, screen)), default=(len(screen), 0))

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
