#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut-killer tests for ``DetectionEngine``.

Targets the high-density mutation surfaces: ``__init__`` (default flag values
and stored attribute identity), ``process_screen`` (async detection + buffer
+ idle + hooks + screen saver), ``debug_state`` (dict-key + structure), and
``reload_rules`` (transactional swap + cache reset).
"""

from __future__ import annotations

from typing import Any

import pytest

from provide.uterm.detection.engine import DetectionEngine
from provide.uterm.detection.rules import RuleSet

# ---------------------------------------------------------------------------
# Fixtures: small reusable RuleSets
# ---------------------------------------------------------------------------


def _ruleset(*rule_ids: str) -> RuleSet:
    """Build a minimal RuleSet from rule ids."""
    return RuleSet.model_validate(
        {
            "version": "1.0",
            "game": "test",
            "prompts": [
                {
                    "id": rid,
                    "match": {"pattern": rf"prompt-{rid}"},
                    "input_type": "multi_key",
                }
                for rid in rule_ids
            ],
        }
    )


def _snap(screen: str, *, screen_hash: str | None = None) -> dict[str, Any]:
    return {
        "screen": screen,
        "screen_hash": screen_hash or screen,
        "cursor_at_end": True,
        "has_trailing_space": False,
        "cursor": {"x": 0, "y": 0},
    }


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


class TestEngineInit:
    def test_enabled_defaults_to_true(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e._enabled is True

    def test_last_fingerprint_defaults_to_empty_string(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e._last_fingerprint == ""

    def test_last_match_defaults_to_none(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e._last_match is None

    def test_namespace_stored(self) -> None:
        e = DetectionEngine(_ruleset("p"), namespace="ns.value")
        assert e._namespace == "ns.value"

    def test_namespace_defaults_to_none(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e._namespace is None

    def test_idle_threshold_stored(self) -> None:
        e = DetectionEngine(_ruleset("p"), idle_threshold_s=3.5)
        assert e._idle_threshold_s == 3.5

    def test_idle_threshold_defaults_to_2(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e._idle_threshold_s == 2.0

    def test_normalizer_stored(self) -> None:
        def norm(s: str) -> str:
            return s.upper()

        e = DetectionEngine(_ruleset("p"), normalizer=norm)
        assert e._normalizer is norm

    def test_normalizer_defaults_to_none(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e._normalizer is None

    def test_screen_saver_defaults_to_none(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e._screen_saver is None

    def test_buffer_size_governs_buffer_capacity(self) -> None:
        e = DetectionEngine(_ruleset("p"), buffer_size=7)
        assert e._buffer_manager._buffer.maxlen == 7

    def test_buffer_size_defaults_to_50(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e._buffer_manager._buffer.maxlen == 50

    def test_detector_constructed_with_normalizer(self) -> None:
        def norm(s: str) -> str:
            return s.upper()

        e = DetectionEngine(_ruleset("p"), normalizer=norm)
        assert e._detector._normalizer is norm

    def test_hooks_default_to_empty_list(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e._hooks == []


# ---------------------------------------------------------------------------
# process_screen + _sync_process_screen
# ---------------------------------------------------------------------------


class TestProcessScreen:
    @pytest.mark.asyncio
    async def test_returns_detection_when_pattern_matches(self) -> None:
        e = DetectionEngine(_ruleset("foo"))
        det = await e.process_screen(_snap("prompt-foo"))
        assert det is not None
        assert det.prompt_id == "foo"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self) -> None:
        e = DetectionEngine(_ruleset("foo"))
        det = await e.process_screen(_snap("nothing"))
        assert det is None

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self) -> None:
        e = DetectionEngine(_ruleset("foo"))
        e._enabled = False
        det = await e.process_screen(_snap("prompt-foo"))
        assert det is None

    @pytest.mark.asyncio
    async def test_detection_uses_fingerprint_cache_when_unchanged(self) -> None:
        """Second call with same screen reuses cached match (no re-detection).

        Verified by patching ``detect_prompt`` to count invocations.
        """
        e = DetectionEngine(_ruleset("foo"))
        first = await e.process_screen(_snap("prompt-foo"))
        # Count calls to detect_prompt after the cache should be warm.
        calls: list[int] = []
        orig_detect = e._detector.detect_prompt

        def counting_detect(snap: Any) -> Any:
            calls.append(1)
            return orig_detect(snap)

        e._detector.detect_prompt = counting_detect  # type: ignore[method-assign]
        second = await e.process_screen(_snap("prompt-foo"))
        assert first is not None and second is not None
        assert first.prompt_id == second.prompt_id
        # detect_prompt must NOT have been called on the second pass —
        # the fingerprint cache short-circuits it.
        assert calls == [], "fingerprint cache did not short-circuit re-detection"

    @pytest.mark.asyncio
    async def test_detection_attaches_buffer(self) -> None:
        e = DetectionEngine(_ruleset("foo"))
        det = await e.process_screen(_snap("prompt-foo"))
        assert det is not None
        assert det.buffer is not None

    @pytest.mark.asyncio
    async def test_detection_attaches_is_idle_flag(self) -> None:
        e = DetectionEngine(_ruleset("foo"))
        det = await e.process_screen(_snap("prompt-foo"))
        assert det is not None
        # ``is_idle`` is a bool — value depends on timing; just check it's set.
        assert isinstance(det.is_idle, bool)

    @pytest.mark.asyncio
    async def test_buffer_matched_prompt_id_is_set(self) -> None:
        e = DetectionEngine(_ruleset("foo"))
        await e.process_screen(_snap("prompt-foo"))
        # The buffer's last entry carries the matched_prompt_id.
        recent = e._buffer_manager.get_recent(n=1)
        assert recent and recent[0].matched_prompt_id == "foo"

    @pytest.mark.asyncio
    async def test_hooks_fire_with_snapshot_detection_buffer_is_idle(self) -> None:
        e = DetectionEngine(_ruleset("foo"))
        captured: list[tuple[Any, ...]] = []

        async def hook(snap: Any, det: Any, buf: Any, is_idle: Any) -> None:
            captured.append((snap, det, buf, is_idle))

        e._hooks.append(hook)
        snap = _snap("prompt-foo")
        await e.process_screen(snap)
        assert len(captured) == 1
        # Hook receives positional args in this order:
        # (snapshot, detection, buffer, is_idle)
        snap_arg, det_arg, buf_arg, idle_arg = captured[0]
        assert snap_arg is snap
        assert det_arg is not None  # detection
        assert buf_arg is not None  # buffer
        assert isinstance(idle_arg, bool)

    @pytest.mark.asyncio
    async def test_hook_exception_does_not_propagate(self) -> None:
        e = DetectionEngine(_ruleset("foo"))

        async def hook(*_args: Any) -> None:
            raise RuntimeError("hook failure")

        e._hooks.append(hook)
        # Must NOT raise:
        det = await e.process_screen(_snap("prompt-foo"))
        assert det is not None


# ---------------------------------------------------------------------------
# debug_state
# ---------------------------------------------------------------------------


class TestDebugState:
    def test_debug_state_contains_idle_threshold_key(self) -> None:
        e = DetectionEngine(_ruleset("p"), idle_threshold_s=1.5)
        state = e.debug_state()
        assert state["idle_threshold_s"] == 1.5

    def test_debug_state_contains_namespace_key(self) -> None:
        e = DetectionEngine(_ruleset("p"), namespace="my.ns")
        state = e.debug_state()
        assert state["namespace"] == "my.ns"

    def test_debug_state_contains_screen_buffer_dict(self) -> None:
        e = DetectionEngine(_ruleset("p"), buffer_size=12)
        state = e.debug_state()
        assert "screen_buffer" in state
        sb = state["screen_buffer"]
        assert sb["size"] == 0
        assert sb["max_size"] == 12
        assert sb["is_idle"] is False
        assert sb["last_change_seconds_ago"] == 0.0

    def test_debug_state_screen_saver_none_when_unset(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        state = e.debug_state()
        assert state["screen_saver"] is None


# ---------------------------------------------------------------------------
# reload_rules
# ---------------------------------------------------------------------------


class TestReloadRules:
    def test_reload_rules_swaps_detector(self) -> None:
        e = DetectionEngine(_ruleset("old"))
        old = e._detector
        e.reload_rules(_ruleset("new"))
        assert e._detector is not old

    def test_reload_rules_resets_fingerprint_cache(self) -> None:
        e = DetectionEngine(_ruleset("foo"))
        e._last_fingerprint = "stale"
        e._last_match = object()  # type: ignore[assignment]
        e.reload_rules(_ruleset("foo"))
        assert e._last_fingerprint == ""
        assert e._last_match is None

    def test_reload_rules_preserves_normalizer(self) -> None:
        def norm(s: str) -> str:
            return s.upper()

        e = DetectionEngine(_ruleset("p"), normalizer=norm)
        e.reload_rules(_ruleset("q"))
        assert e._detector._normalizer is norm


# ---------------------------------------------------------------------------
# get_screen_saver_status
# ---------------------------------------------------------------------------


class TestGetScreenSaverStatus:
    def test_returns_disabled_when_no_saver(self) -> None:
        e = DetectionEngine(_ruleset("p"))
        assert e.get_screen_saver_status() == {"enabled": False}

    def test_reflects_saver_fields_when_set(self, tmp_path: Any) -> None:
        from provide.uterm.detection.saver import ScreenSaver

        saver = ScreenSaver(base_dir=tmp_path / "screens", namespace="ns")
        e = DetectionEngine(_ruleset("p"), screen_saver=saver, namespace="ns")
        status = e.get_screen_saver_status()
        assert status["enabled"] == saver._enabled
        assert status["namespace"] == "ns"
        assert status["saved_count"] == 0
        assert "screens" in status["screens_dir"]
