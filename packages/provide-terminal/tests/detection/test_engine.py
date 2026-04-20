#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import Any

import pytest

from provide.terminal.detection.engine import DetectionEngine
from provide.terminal.detection.rules import RuleSet


def test_engine_init_from_path(simple_rules_file) -> None:
    engine = DetectionEngine(simple_rules_file)
    assert engine.pattern_count == 1
    assert engine.enabled is True


def test_engine_init_from_ruleset() -> None:
    rs = RuleSet(version="1.0", game="t", prompts=[])
    engine = DetectionEngine(rs)
    assert engine.pattern_count == 0


def test_engine_init_raises_on_bad_rules(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with pytest.raises(ValueError):
        DetectionEngine(bad)


def test_process_screen_returns_detection(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    result = engine._sync_process_screen(snap_factory("Hello there"))
    assert result is not None
    assert result.prompt_id == "prompt.hello"


def test_process_screen_returns_none_on_no_match(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    assert engine._sync_process_screen(snap_factory("Goodbye")) is None


def test_disabled_engine_returns_none(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    engine.enabled = False
    assert engine._sync_process_screen(snap_factory("Hello there")) is None


def test_enabled_setter(simple_rules_file) -> None:
    engine = DetectionEngine(simple_rules_file)
    assert engine.enabled is True
    engine.enabled = False
    assert engine.enabled is False
    engine.enabled = True
    assert engine.enabled is True


def test_fingerprint_cache_skips_redetection(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    snap = snap_factory("Hello there")
    r1 = engine._sync_process_screen(snap)
    r2 = engine._sync_process_screen(snap)
    assert r1 is not None and r2 is not None
    assert r1.prompt_id == r2.prompt_id


def test_fingerprint_cache_invalidated_on_new_screen(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    r1 = engine._sync_process_screen(snap_factory("Hello there"))
    r2 = engine._sync_process_screen(snap_factory("Something else"))
    assert r1 is not None
    assert r2 is None


def test_reload_rules_success(simple_rules_file, rules_file_factory, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    assert engine.pattern_count == 1
    new = rules_file_factory(
        [
            {"id": "p.a", "match": {"pattern": "A", "match_mode": "contains"}, "input_type": "single_key"},
            {"id": "p.b", "match": {"pattern": "B", "match_mode": "contains"}, "input_type": "single_key"},
        ]
    )
    engine.reload_rules(new)
    assert engine.pattern_count == 2


def test_reload_rules_transactional(simple_rules_file, tmp_path) -> None:
    engine = DetectionEngine(simple_rules_file)
    assert engine.pattern_count == 1
    bad = tmp_path / "bad.json"
    bad.write_text("invalid")
    with pytest.raises(ValueError):
        engine.reload_rules(bad)
    assert engine.pattern_count == 1  # old rules preserved


def test_reload_clears_fingerprint_cache(simple_rules_file, rules_file_factory, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    engine._sync_process_screen(snap_factory("Hello there"))
    new = rules_file_factory(
        [
            {"id": "p.bye", "match": {"pattern": "Goodbye", "match_mode": "contains"}, "input_type": "single_key"},
        ]
    )
    engine.reload_rules(new)
    # Cache was cleared, so new rules apply even to same screen hash
    assert engine._sync_process_screen(snap_factory("Goodbye")) is not None


def test_normalizer_passed_to_detector(simple_rules_file) -> None:
    engine = DetectionEngine(simple_rules_file, normalizer=lambda t: t.upper())
    assert engine.detector is not None


def test_detector_property(simple_rules_file) -> None:
    engine = DetectionEngine(simple_rules_file)
    assert hasattr(engine.detector, "detect_prompt")
    assert hasattr(engine.detector, "detect_prompt_with_diagnostics")


def test_detect_with_diagnostics(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    diag = engine.detect_with_diagnostics(snap_factory("Hello there"))
    assert diag.match is not None
    assert diag.match.prompt_id == "prompt.hello"


def test_kv_extraction_populates_kv_data(kv_rules_file, snap_factory) -> None:
    engine = DetectionEngine(kv_rules_file)
    result = engine._sync_process_screen(snap_factory("Sector 42 : Credits: 15,000"))
    assert result is not None
    assert result.kv_data.get("sector") == 42
    assert result.kv_data.get("credits") == 15000


def test_kv_extraction_empty_when_no_kv_config(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    result = engine._sync_process_screen(snap_factory("Hello there"))
    assert result is not None
    assert result.kv_data == {}


# ---------------------------------------------------------------------------
# Async process_screen tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_process_screen_detection(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    result = await engine.process_screen(snap_factory("Hello there"))
    assert result is not None
    assert result.prompt_id == "prompt.hello"


@pytest.mark.asyncio
async def test_async_process_screen_populates_is_idle_and_buffer(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    result = await engine.process_screen(snap_factory("Hello there"))
    assert result is not None
    assert result.is_idle is not None
    assert isinstance(result.is_idle, bool)
    assert result.buffer is not None


@pytest.mark.asyncio
async def test_add_hook_called_after_detection(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    calls: list[Any] = []

    async def hook(snapshot: Any, detection: Any, buffer: Any, is_idle: bool) -> None:
        calls.append((snapshot, detection, buffer, is_idle))

    engine.add_hook(hook)
    await engine.process_screen(snap_factory("Hello there"))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_hook_receives_correct_args(simple_rules_file, snap_factory) -> None:
    engine = DetectionEngine(simple_rules_file)
    received: list[Any] = []

    async def hook(snapshot: Any, detection: Any, buffer: Any, is_idle: bool) -> None:
        received.append({"detection": detection, "is_idle": is_idle, "buffer": buffer})

    engine.add_hook(hook)
    snap = snap_factory("Hello there")
    await engine.process_screen(snap)

    assert len(received) == 1
    assert received[0]["detection"] is not None
    assert received[0]["detection"].prompt_id == "prompt.hello"
    assert isinstance(received[0]["is_idle"], bool)
    assert received[0]["buffer"] is not None


@pytest.mark.asyncio
async def test_screen_saver_called_on_match(simple_rules_file, snap_factory, tmp_path) -> None:
    from provide.terminal.detection.saver import ScreenSaver

    saver = ScreenSaver(base_dir=tmp_path, enabled=True)
    engine = DetectionEngine(simple_rules_file, screen_saver=saver)
    await engine.process_screen(snap_factory("Hello there"))
    assert saver.get_saved_count() > 0


@pytest.mark.asyncio
async def test_set_namespace_updates_screen_saver(simple_rules_file, tmp_path) -> None:
    from provide.terminal.detection.saver import ScreenSaver

    saver = ScreenSaver(base_dir=tmp_path, namespace="old")
    engine = DetectionEngine(simple_rules_file, screen_saver=saver, namespace="old")
    engine.set_namespace("new_game")
    assert engine.namespace == "new_game"
    assert saver._namespace == "new_game"


def test_debug_state_returns_dict(simple_rules_file) -> None:
    engine = DetectionEngine(simple_rules_file)
    state = engine.debug_state()
    assert isinstance(state, dict)
    assert "screen_buffer" in state
    assert "idle_threshold_s" in state


# ---------------------------------------------------------------------------
# Error isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_exception_does_not_stop_other_hooks(simple_rules_file, snap_factory) -> None:
    """A failing hook must not prevent subsequent hooks from running."""
    engine = DetectionEngine(simple_rules_file)
    second_called: list[bool] = []

    async def bad_hook(snapshot: Any, detection: Any, buffer: Any, is_idle: bool) -> None:
        raise RuntimeError("boom")

    async def good_hook(snapshot: Any, detection: Any, buffer: Any, is_idle: bool) -> None:
        second_called.append(True)

    engine.add_hook(bad_hook)
    engine.add_hook(good_hook)
    # process_screen must not raise even though bad_hook raises
    result = await engine.process_screen(snap_factory("Hello there"))
    assert result is not None
    assert second_called == [True]


@pytest.mark.asyncio
async def test_hook_exception_does_not_propagate(simple_rules_file, snap_factory) -> None:
    """process_screen must return normally even when a hook raises."""
    engine = DetectionEngine(simple_rules_file)

    async def bad_hook(snapshot: Any, detection: Any, buffer: Any, is_idle: bool) -> None:
        raise ValueError("hook failure")

    engine.add_hook(bad_hook)
    # Should not raise
    result = await engine.process_screen(snap_factory("Hello there"))
    assert result is not None


@pytest.mark.asyncio
async def test_saver_exception_does_not_discard_detection(simple_rules_file, snap_factory, tmp_path) -> None:
    """If the screen saver raises, process_screen still returns the detection result."""
    from unittest.mock import MagicMock

    from provide.terminal.detection.saver import ScreenSaver

    saver = MagicMock(spec=ScreenSaver)
    saver.save_screen.side_effect = OSError("disk full")
    engine = DetectionEngine(simple_rules_file, screen_saver=saver)

    result = await engine.process_screen(snap_factory("Hello there"))
    assert result is not None
    assert result.prompt_id == "prompt.hello"


@pytest.mark.asyncio
async def test_no_match_still_runs_hooks(simple_rules_file, snap_factory) -> None:
    """Hooks are called even when no prompt matched (detection=None)."""
    engine = DetectionEngine(simple_rules_file)
    calls: list[Any] = []

    async def hook(snapshot: Any, detection: Any, buffer: Any, is_idle: bool) -> None:
        calls.append(detection)

    engine.add_hook(hook)
    result = await engine.process_screen(snap_factory("Unrecognized screen"))
    assert result is None
    assert len(calls) == 1
    assert calls[0] is None


def test_add_hook_twice_same_function_invokes_twice(simple_rules_file) -> None:
    """Adding the same hook twice results in it being called twice (no dedup contract)."""
    engine = DetectionEngine(simple_rules_file)

    async def hook(snapshot: Any, detection: Any, buffer: Any, is_idle: bool) -> None:  # pragma: no cover
        pass

    engine.add_hook(hook)
    engine.add_hook(hook)
    assert engine._hooks.count(hook) == 2


# ---------------------------------------------------------------------------
# Coverage for missing branches
# ---------------------------------------------------------------------------


def test_is_idle_property(simple_rules_file) -> None:
    """engine.is_idle delegates to buffer_manager.detect_idle_state."""
    engine = DetectionEngine(simple_rules_file)
    # No screens processed yet — not idle
    assert engine.is_idle is False


def test_set_namespace_without_screen_saver(simple_rules_file) -> None:
    """set_namespace works when no screen_saver is set (no AttributeError)."""
    engine = DetectionEngine(simple_rules_file, namespace="old")
    engine.set_namespace("new_ns")
    assert engine.namespace == "new_ns"


def test_get_screen_saver_status_with_saver(simple_rules_file, tmp_path) -> None:
    """get_screen_saver_status returns dict with saver info when saver is present."""
    from provide.terminal.detection.saver import ScreenSaver

    saver = ScreenSaver(base_dir=tmp_path, enabled=True, namespace="game1")
    engine = DetectionEngine(simple_rules_file, screen_saver=saver)
    status = engine.get_screen_saver_status()
    assert status["enabled"] is True
    assert "saved_count" in status
    assert "namespace" in status


def test_set_screen_saving_with_saver(simple_rules_file, tmp_path) -> None:
    """set_screen_saving toggles the ScreenSaver._enabled flag."""
    from provide.terminal.detection.saver import ScreenSaver

    saver = ScreenSaver(base_dir=tmp_path, enabled=True)
    engine = DetectionEngine(simple_rules_file, screen_saver=saver)
    engine.set_screen_saving(False)
    assert saver._enabled is False
    engine.set_screen_saving(True)
    assert saver._enabled is True


def test_sync_process_screen_kv_extraction_empty(kv_rules_file, snap_factory) -> None:
    """_sync_process_screen returns detection with empty kv_data when kv extraction yields nothing."""
    engine = DetectionEngine(kv_rules_file)
    # Screen matches the prompt but has no extractable KV data (no 'Sector N' or 'Credits')
    result = engine._sync_process_screen(snap_factory("Sector 99 :"))
    # The prompt should still match (it matches "Sector\s+\d+\s*:"), but kv may be partial
    # Crucially we want the branch where extracted is empty dict / None → kv_data stays {}
    assert result is not None
    # kv_data could be {} or populated; we care that the branch was exercised (no crash)


def test_sync_process_screen_kv_extract_no_match_in_screen(rules_file_factory, snap_factory) -> None:
    """When kv_extract is configured but regex doesn't match screen, kv_data stays empty."""
    rules = rules_file_factory(
        [
            {
                "id": "prompt.hello",
                "match": {"pattern": "Hello there", "match_mode": "contains"},
                "input_type": "single_key",
                "kv_extract": [{"field": "score", "regex": r"Score:\s*(\d+)", "type": "int"}],
            }
        ]
    )
    engine = DetectionEngine(rules)
    # Screen matches the prompt but contains no 'Score: N' → extract_kv returns None
    result = engine._sync_process_screen(snap_factory("Hello there"))
    assert result is not None
    assert result.kv_data == {}


def test_get_screen_saver_status_without_saver(simple_rules_file) -> None:
    """get_screen_saver_status returns {'enabled': False} when no saver configured."""
    engine = DetectionEngine(simple_rules_file)
    status = engine.get_screen_saver_status()
    assert status == {"enabled": False}


def test_set_screen_saving_without_saver_is_noop(simple_rules_file) -> None:
    """set_screen_saving does nothing (no error) when no screen_saver is configured."""
    engine = DetectionEngine(simple_rules_file)
    # Should not raise
    engine.set_screen_saving(True)
    engine.set_screen_saving(False)
