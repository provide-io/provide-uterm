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
