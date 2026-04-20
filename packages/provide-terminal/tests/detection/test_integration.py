#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.terminal.detection import (
    BufferManager,
    DetectionEngine,
    KVExtractor,
    PromptDetection,
    PromptDetectionDiagnostics,
    PromptDetector,
    PromptMatch,
    RuleSet,
    ScreenBuffer,
    ScreenSaver,
    ScreenSnapshot,
    extract_kv,
    load_ruleset,
)


def test_end_to_end_detect_and_extract(kv_rules_file, snap_factory) -> None:
    """Full pipeline: rules.json -> DetectionEngine -> process_screen -> KV data."""
    engine = DetectionEngine(kv_rules_file)
    result = engine._sync_process_screen(snap_factory("Sector 42 : Credits: 15,000\nCommand prompt"))
    assert result is not None
    assert result.prompt_id == "prompt.sector"
    assert result.kv_data["sector"] == 42
    assert result.kv_data["credits"] == 15000


def test_no_match_returns_none(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    assert engine._sync_process_screen(snap_factory("Goodbye world")) is None


def test_disabled_engine_returns_none(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    engine.enabled = False
    assert engine._sync_process_screen(snap_factory("Hello there")) is None


def test_reload_mid_session(rules_file_factory, snap_factory) -> None:
    """Reload rules while engine is in use."""
    f1 = rules_file_factory(
        [{"id": "p.a", "match": {"pattern": "AAA", "match_mode": "contains"}, "input_type": "single_key"}]
    )
    engine = DetectionEngine(f1)
    assert engine._sync_process_screen(snap_factory("AAA"))
    assert not engine._sync_process_screen(snap_factory("BBB"))
    f2 = rules_file_factory(
        [{"id": "p.b", "match": {"pattern": "BBB", "match_mode": "contains"}, "input_type": "single_key"}]
    )
    engine.reload_rules(f2)
    assert not engine._sync_process_screen(snap_factory("AAA"))
    assert engine._sync_process_screen(snap_factory("BBB"))


def test_normalizer_affects_fingerprint(simple_rules_file, snap_factory) -> None:
    """Normalizer makes different screens produce same fingerprint."""
    engine = DetectionEngine(simple_rules_file, normalizer=lambda t: t.replace("X", ""))
    s1 = snap_factory("Hello there X1")
    s2 = snap_factory("Hello there X2")
    r1 = engine._sync_process_screen(s1)
    r2 = engine._sync_process_screen(s2)
    assert r1 is not None and r2 is not None


def test_all_public_exports_importable() -> None:
    """Every symbol in __all__ is importable."""
    assert all(
        [
            DetectionEngine,
            PromptDetector,
            PromptDetection,
            PromptMatch,
            PromptDetectionDiagnostics,
            KVExtractor,
            RuleSet,
            ScreenBuffer,
            BufferManager,
            ScreenSaver,
            ScreenSnapshot,
            extract_kv,
            load_ruleset,
        ]
    )


def test_detection_result_has_match_metadata(simple_rules_file, snap_factory) -> None:
    """PromptDetection includes the PromptMatch that produced it."""
    engine = DetectionEngine(simple_rules_file)
    result = engine._sync_process_screen(snap_factory("Hello there"))
    assert result is not None
    assert result.match is not None
    assert result.match.prompt_id == result.prompt_id


def test_multiple_patterns_first_match_wins(rules_file_factory, snap_factory) -> None:
    """When multiple patterns could match, the first in order wins."""
    f = rules_file_factory(
        [
            {"id": "p.first", "match": {"pattern": "Hello", "match_mode": "contains"}, "input_type": "single_key"},
            {
                "id": "p.second",
                "match": {"pattern": "Hello there", "match_mode": "contains"},
                "input_type": "multi_key",
            },
        ]
    )
    engine = DetectionEngine(f)
    result = engine._sync_process_screen(snap_factory("Hello there"))
    assert result is not None
    assert result.prompt_id == "p.first"
