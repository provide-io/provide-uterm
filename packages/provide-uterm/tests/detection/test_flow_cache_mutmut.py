#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FlowEngine detector-cache mutants (kept out of test_flow.py for max-LOC)."""

from __future__ import annotations

import pytest

from provide.uterm.detection import FlowEngine, RuleSet


@pytest.fixture
def login_ruleset() -> RuleSet:
    return RuleSet.model_validate(
        {
            "version": "1.0",
            "game": "test",
            "prompts": [
                {
                    "id": "login.name",
                    "match": {"pattern": "Enter your name", "match_mode": "contains"},
                    "input_type": "multi_key",
                },
            ],
            "flows": [
                {
                    "id": "login",
                    "description": "login flow",
                    "steps": [
                        {
                            "id": "send_name",
                            "kind": "send_keys",
                            "keys": "alice\r",
                            "expects_prompt": "login.name",
                            "gate_prompts": ["login.name"],
                        },
                    ],
                }
            ],
        }
    )


def test_detect_prompt_reuses_cached_detector(login_ruleset: RuleSet) -> None:
    """Cache hit must return the same PromptDetector instance (kills cache no-ops).

    Mutants that force a miss every call (``get(key) → None``, ``get(None)``, or
    store ``None`` instead of the detector) still produce correct matches on a
    single call, so only identity-of-cached-detector assertions pin the cache.
    """
    engine = FlowEngine(login_ruleset)
    snap = engine._snapshot("Enter your name:", None)
    ids = ["login.name"]
    key = tuple(ids)

    first = engine._detect_prompt(snap, ids)
    assert first is not None
    assert key in engine._detector_cache
    cached = engine._detector_cache[key]
    assert cached is not None

    second = engine._detect_prompt(snap, ids)
    assert second is not None
    # Same detector object on the second lookup — not a rebuild.
    assert engine._detector_cache[key] is cached
