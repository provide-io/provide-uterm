#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The committed behavior vectors are what the generator produces today.

``close_cases`` is computed from the Python reference, so a change to the
reference (a summary format, the WebSocket attribution) that is not followed by
regenerating the vectors would leave the other ports tested against the old
contract. This fails first, naming the regeneration command.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _ROOT / "scripts" / "generate_behavior_vectors.py"
_spec = importlib.util.spec_from_file_location("generate_behavior_vectors", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
generate_behavior_vectors = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate_behavior_vectors)


def test_committed_vectors_match_the_generator() -> None:
    spec = json.loads(generate_behavior_vectors.SPEC_PATH.read_text(encoding="utf-8"))
    committed = json.loads(generate_behavior_vectors.OUT_PATH.read_text(encoding="utf-8"))

    assert committed == generate_behavior_vectors.build_vectors(spec), (
        "spec/behavior_vectors.json is stale: run uv run python scripts/generate_behavior_vectors.py"
    )


def test_close_cases_cover_every_summary_shape() -> None:
    summaries = generate_behavior_vectors.build_close_cases()["summary"]

    shapes = {
        (case["initiator"], case["code"] is not None, case["reason"] != "", case["detail"] != "") for case in summaries
    }
    assert len(shapes) == 3 * 2 * 2 * 2
    assert any(case["code"] == 0 for case in summaries), "a zero code must be a code, not an absent one"


def test_capture_geometry_frames_are_the_reference_reading() -> None:
    cases = {case["name"]: case for case in generate_behavior_vectors.build_capture_geometry_cases()}

    # 05 | 00000004 | 0064 0028: channel, length, cols 100, rows 40.
    assert cases["100x40"] == {"name": "100x40", "frame": "05000000040064" + "0028", "size": {"cols": 100, "rows": 40}}
    assert cases["three-byte-payload"]["size"] is None
    assert cases["1001-cols"]["size"] is None
    # Both bounds are inclusive, and both are held to.
    assert {name for name, case in cases.items() if case["size"] is not None} >= {"1x1", "1000x1000"}
    assert {"0-cols", "0-rows", "1001-rows", "five-byte-payload", "empty-payload"} <= set(cases)


def test_every_copy_of_the_vectors_is_written() -> None:
    for name in ("GO_PTY_COPY", "PLATFORM_TEST_COPY"):
        assert getattr(generate_behavior_vectors, name) in generate_behavior_vectors.COPIES
