#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the detection differential golden from the REAL Python detection
stack (provide.uterm.detection). Run from the repo root:

    uv run python packages/provide-uterm-go/detection/testdata/gen_differential_golden.py

Writes differential_golden.json next to this script. differential_test.go
replays all four families through the Go port, so a divergence in prompt
matching, flow advancement, idle detection or input-type inference fails CI.

The idle cases pin the clock. BufferManager reads time.time() both when it
stamps a screen without a captured_at and when it measures staleness, so a
generator that let the real clock run would produce a different corpus every
second. Go patches its own nowSeconds for the same reason; this patches
detection.buffer.time.time to the per-case "now".
"""

from __future__ import annotations

import json
import pathlib
from typing import Any
from unittest.mock import patch

from provide.uterm.detection.buffer import BufferManager
from provide.uterm.detection.detector import PromptDetector
from provide.uterm.detection.extractor import extract_kv
from provide.uterm.detection.flow import FlowEngine
from provide.uterm.detection.input_type import auto_detect_input_type
from provide.uterm.detection.loader import load_ruleset

# The case INPUTS live beside this script as JSON rather than inline: they are
# mostly embedded rulesets, and inlining them pushed this file to 986 lines
# against the repo's 777 cap. The sidecar holds inputs ONLY — no expected
# values — so the generator cannot copy an answer forward instead of
# recomputing it from the Python reference.
INPUTS = json.loads((pathlib.Path(__file__).with_name("differential_inputs.json")).read_text())
DETECTOR_CASES: list[dict[str, Any]] = INPUTS["detector_cases"]
FLOW_CASES: list[dict[str, Any]] = INPUTS["flow_cases"]
IDLE_CASES: list[dict[str, Any]] = INPUTS["idle_cases"]
INPUT_TYPE_CASES: list[dict[str, Any]] = INPUTS["input_type_cases"]


def detector_expected(case: dict[str, Any]) -> dict[str, Any]:
    patterns = case.get("patterns")
    if case.get("rules"):
        patterns = load_ruleset(json.dumps(case["rules"])).to_prompt_patterns()
    match = PromptDetector(patterns or []).detect_prompt(case["snapshot"])
    if match is None:
        return {"matched": False}
    kv_data: dict[str, Any] = {}
    if match.kv_extract:
        extracted = extract_kv(case["snapshot"].get("screen", ""), match.kv_extract)
        if extracted is not None:
            kv_data = extracted
    return {
        "matched": True,
        "prompt_id": match.prompt_id,
        "input_type": match.input_type,
        "kv_data": kv_data,
    }


def flow_expected(case: dict[str, Any]) -> dict[str, Any]:
    engine = FlowEngine(load_ruleset(json.dumps(case["rules"])))
    cursor = tuple(case["cursor"]) if case.get("cursor") else None
    step = engine.advance(case["flow"], case.get("screen", ""), cursor)
    return {
        "current_prompt_id": step.current_prompt_id,
        "next_action": step.next_action,
        "done": step.done,
        "kv_data": step.kv_data,
    }


def idle_expected(case: dict[str, Any]) -> dict[str, Any]:
    # Both the stamp and the staleness comparison read time.time().
    with patch("provide.uterm.detection.buffer.time.time", return_value=case["now"]):
        manager = BufferManager(50)
        for screen in case["screens"]:
            manager.add_screen(screen)
        return {"is_idle": manager.detect_idle_state(case["threshold"])}


def build() -> dict[str, Any]:
    return {
        "detector_cases": [{**c, "expected": detector_expected(c)} for c in DETECTOR_CASES],
        "flow_cases": [{**c, "expected": flow_expected(c)} for c in FLOW_CASES],
        "idle_cases": [{**c, "expected": idle_expected(c)} for c in IDLE_CASES],
        "input_type_cases": [{**c, "expected": auto_detect_input_type(c["screen"])} for c in INPUT_TYPE_CASES],
    }


def main() -> None:
    corpus = build()
    out = pathlib.Path(__file__).with_name("differential_golden.json")
    # indent=2, key-sorted, and NO trailing newline: this corpus's own style.
    out.write_text(json.dumps(corpus, indent=2, sort_keys=True), encoding="utf-8")
    total = sum(len(v) for v in corpus.values())
    print(f"wrote {out} ({total} cases across {len(corpus)} families)")


if __name__ == "__main__":
    main()
