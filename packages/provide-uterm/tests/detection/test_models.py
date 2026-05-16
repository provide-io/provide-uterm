#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.uterm.detection.models import (
    PromptDetection,
    PromptDetectionDiagnostics,
    PromptMatch,
    ScreenSnapshot,
)


def test_screen_snapshot_required_fields() -> None:
    snap: ScreenSnapshot = {"screen": "hello", "screen_hash": "abc123"}
    assert snap["screen"] == "hello"


def test_screen_snapshot_optional_fields() -> None:
    snap: ScreenSnapshot = {
        "screen": "hello",
        "screen_hash": "abc",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "cursor": {"y": 10, "x": 5},
        "captured_at": 1234567890.0,
    }
    assert snap["cursor"]["y"] == 10


def test_prompt_match_model() -> None:
    m = PromptMatch(prompt_id="p.a", pattern={"regex": "A"}, input_type="single_key", eol_pattern="$")
    assert m.prompt_id == "p.a"
    assert m.kv_extract is None


def test_prompt_match_with_kv_extract() -> None:
    m = PromptMatch(
        prompt_id="p.b",
        pattern={},
        input_type="multi_key",
        eol_pattern="$",
        kv_extract=[{"field": "credits", "regex": r"(\d+)", "type": "int"}],
    )
    assert len(m.kv_extract) == 1


def test_prompt_detection_defaults() -> None:
    d = PromptDetection(prompt_id="p.login", input_type="multi_key")
    assert d.kv_data == {}
    assert d.match is None


def test_prompt_detection_with_match() -> None:
    m = PromptMatch(prompt_id="p.a", pattern={}, input_type="single_key", eol_pattern="$")
    d = PromptDetection(prompt_id="p.a", input_type="single_key", kv_data={"x": 1}, match=m)
    assert d.match.prompt_id == "p.a"
    assert d.kv_data["x"] == 1


def test_diagnostics_empty() -> None:
    diag = PromptDetectionDiagnostics()
    assert diag.match is None
    assert diag.regex_matched_but_failed == []


def test_diagnostics_with_failed_matches() -> None:
    diag = PromptDetectionDiagnostics(
        regex_matched_but_failed=[{"id": "p.x", "reason": "cursor_miss"}],
    )
    assert len(diag.regex_matched_but_failed) == 1


def test_prompt_detection_is_idle_defaults_none() -> None:
    d = PromptDetection(prompt_id="p.x", input_type="single_key")
    assert d.is_idle is None


def test_prompt_detection_buffer_defaults_none() -> None:
    d = PromptDetection(prompt_id="p.x", input_type="single_key")
    assert d.buffer is None
