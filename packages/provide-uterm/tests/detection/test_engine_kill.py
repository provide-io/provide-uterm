#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Full-perimeter-gate survivors for ``DetectionEngine``.

A full (non ``--changed-only``) mutmut run surfaced latent survivors that the
existing ``test_engine_mutmut.py`` never bound: the two ``logger.warning`` calls
in ``process_screen`` (screen-saver failure + hook failure — their message,
args, and ``exc_info=True``), the ``prompt_id`` passed to the screen saver, and
``debug_state``'s ``get_recent``/index access. ``engine`` logs through stdlib
``logging``, so exact ``getMessage()`` + ``record.exc_info`` assertions pin them.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from provide.uterm.detection.engine import DetectionEngine
from provide.uterm.detection.rules import RuleSet

_LOGGER = "provide.uterm.detection.engine"


def _ruleset(*rule_ids: str) -> RuleSet:
    return RuleSet.model_validate(
        {
            "version": "1.0",
            "game": "test",
            "prompts": [
                {"id": rid, "match": {"pattern": rf"prompt-{rid}"}, "input_type": "multi_key"} for rid in rule_ids
            ],
        }
    )


def _snap(screen: str) -> dict[str, Any]:
    return {
        "screen": screen,
        "screen_hash": screen,
        "cursor_at_end": True,
        "has_trailing_space": False,
        "cursor": {"x": 0, "y": 0},
    }


# == process_screen: prompt_id passed to the screen saver ====================


async def test_process_screen_saves_with_detected_prompt_id() -> None:
    """A matched prompt's id is forwarded to ``save_screen(snapshot, prompt_id=...)``.

    Pins ``prompt_id = detection.prompt_id if detection else None`` and the
    ``prompt_id=prompt_id`` kwarg (→ None / dropped).
    """
    saver = MagicMock()
    engine = DetectionEngine(_ruleset("login"), screen_saver=saver)
    snap = _snap("prompt-login")

    await engine.process_screen(snap)

    saver.save_screen.assert_called_once_with(snap, prompt_id="login")


# == process_screen: screen-saver-failure warning ===========================


async def test_process_screen_logs_screen_saver_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A raising screen saver is swallowed and logged with a traceback.

    Pins the exact warning text + ``exc_info=True``; detection still proceeds.
    """
    saver = MagicMock()
    saver.save_screen.side_effect = RuntimeError("disk full")
    engine = DetectionEngine(_ruleset("login"), screen_saver=saver)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        result = await engine.process_screen(_snap("prompt-login"))

    assert result is not None  # detection continues despite the saver failure
    recs = [r for r in caplog.records if r.getMessage() == "Screen saver failed; detection continues"]
    assert recs, "expected the exact screen-saver-failure warning"
    assert recs[0].exc_info  # truthy exc tuple — kills exc_info=True -> False/None


# == process_screen: hook-failure warning ====================================


async def test_process_screen_logs_hook_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A raising hook is swallowed and logged with its repr + a traceback."""

    class _RaisingHook:
        def __repr__(self) -> str:
            return "<raising-hook>"

        async def __call__(self, *args: object) -> None:
            raise ValueError("boom")

    hook = _RaisingHook()
    engine = DetectionEngine(_ruleset("login"))
    engine.add_hook(hook)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await engine.process_screen(_snap("prompt-login"))

    recs = [r for r in caplog.records if r.getMessage() == "Hook <raising-hook> raised; continuing"]
    assert recs, "expected the exact hook-failure warning with the hook repr"
    assert recs[0].exc_info  # truthy exc tuple — kills exc_info=True -> False/None


# == debug_state: get_recent + indexing ======================================


def test_debug_state_uses_most_recent_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``last_change_seconds_ago`` is ``get_recent(n=1)[0].time_since_last_change``.

    Distinct mocked values pin: ``recent = get_recent(n=1)`` (None ⇒ else 0.0),
    the ``n=1`` argument (n=2 ⇒ recent[0] is the older entry), and ``recent[0]``
    (recent[1] ⇒ IndexError on the single-entry n=1 list).
    """
    engine = DetectionEngine(_ruleset("login"))
    bm = engine._buffer_manager
    s_old = SimpleNamespace(time_since_last_change=11.0)
    s_new = SimpleNamespace(time_since_last_change=22.0)
    monkeypatch.setattr(bm, "get_recent", lambda n=1: [s_new] if n == 1 else [s_old, s_new])
    monkeypatch.setattr(bm, "detect_idle_state", lambda *a, **k: True)

    sb = engine.debug_state()["screen_buffer"]
    assert sb["last_change_seconds_ago"] == 22.0  # most-recent (n=1, index 0)
    assert sb["is_idle"] is True  # recent truthy ⇒ detect_idle_state() taken (not else False)


def test_debug_state_empty_buffer_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty recent list takes the ``else`` defaults without indexing."""
    engine = DetectionEngine(_ruleset("login"))
    monkeypatch.setattr(engine._buffer_manager, "get_recent", lambda n=1: [])
    sb = engine.debug_state()["screen_buffer"]
    assert sb["last_change_seconds_ago"] == 0.0
    assert sb["is_idle"] is False
