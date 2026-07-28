#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the detection engine.

The engine is what a session actually calls: hand it a screen, get back what
prompt is showing and what was on it. Everything else in detection hangs off
this one method.

**Answers are cached by fingerprint.** A terminal sends the same screen many
times over, and re-running every pattern against each one is the cost this
avoids. The cache key is the detector's fingerprint, which already includes
cursor state — so a screen whose text is identical but whose cursor moved is a
fresh question rather than a stale answer.

**A disabled engine answers nothing at all**, rather than answering stale. An
operator turning detection off wants it off.

**Failures around detection do not stop it.** A screen saver that raises, or a
hook that throws, is logged and stepped over: neither is the reason the
session exists, and taking detection down with them would lose the prompt as
well as the screenshot.

**Reloading rules is transactional.** New rules that will not compile leave
the old ones running, and the cached answer is dropped so the next screen is
re-read rather than answered from the rules that no longer apply.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_engine_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.detection.engine import DetectionEngine

OUT = Path(__file__).with_name("engine_golden.json")

RULES: dict[str, Any] = {
    "game": "tw2002",
    "prompts": [
        {
            "id": "command",
            "input_type": "single_key",
            "match": {"pattern": r"Command \[TL=[\d:]+\]:"},
            "kv_extract": [{"field": "sector", "regex": r"Sector\s+(\d+)", "type": "int"}],
        },
        {"id": "login", "input_type": "multi_key", "match": {"pattern": r"Enter your name:"}},
    ],
}

OTHER_RULES: dict[str, Any] = {
    "game": "tw2002",
    "prompts": [{"id": "different", "match": {"pattern": r"Enter your name:"}}],
}

COMMAND_SCREEN = "Sector  42\nCommand [TL=00:00:00]:? "
LOGIN_SCREEN = "Enter your name: "


def _snapshot(screen: str, **extra: Any) -> dict[str, Any]:
    """A screen snapshot, as a session hands one over."""
    return {"screen": screen, "screen_hash": str(hash(screen)), **extra}


def _detection(result: Any) -> dict[str, Any] | None:
    """What the engine concluded, without the buffer object."""
    if result is None:
        return None
    return {
        "prompt_id": result.prompt_id,
        "input_type": result.input_type,
        "kv_data": result.kv_data,
        "is_idle": result.is_idle,
        "match_prompt_id": result.match.prompt_id if result.match else None,
    }


# (name, screen) — what one screen produces.
SYNC_CASES: list[tuple[str, str]] = [
    ("a command prompt", COMMAND_SCREEN),
    ("a login prompt", LOGIN_SCREEN),
    ("no prompt at all", "just output"),
    ("an empty screen", ""),
    ("a prompt with nothing to extract", LOGIN_SCREEN),
    ("a prompt whose extraction finds nothing", "Command [TL=00:00:00]:? "),
]


def _record_sync() -> list[dict[str, Any]]:
    """One screen at a time, through a fresh engine each time."""
    records = []
    for name, screen in SYNC_CASES:
        engine = DetectionEngine(json.dumps(RULES))
        records.append(
            {"name": name, "screen": screen, "detection": _detection(engine._sync_process_screen(_snapshot(screen)))}
        )
    return records


def _record_cache() -> dict[str, Any]:
    """The same screen twice, and a screen whose cursor moved."""
    engine = DetectionEngine(json.dumps(RULES))
    first = _detection(engine._sync_process_screen(_snapshot(COMMAND_SCREEN)))
    again = _detection(engine._sync_process_screen(_snapshot(COMMAND_SCREEN)))
    moved = _detection(engine._sync_process_screen(_snapshot(COMMAND_SCREEN, cursor={"x": 5, "y": 1})))
    changed = _detection(engine._sync_process_screen(_snapshot(LOGIN_SCREEN)))
    # A screen that matches nothing is cached too, so the miss is not re-run.
    missed = _detection(engine._sync_process_screen(_snapshot("nothing")))
    missed_again = _detection(engine._sync_process_screen(_snapshot("nothing")))
    return {
        "first": first,
        "again": again,
        "cursor_moved": moved,
        "screen_changed": changed,
        "missed": missed,
        "missed_again": missed_again,
    }


def _record_disabled() -> dict[str, Any]:
    """What a disabled engine says."""
    engine = DetectionEngine(json.dumps(RULES))
    before = _detection(engine._sync_process_screen(_snapshot(COMMAND_SCREEN)))
    engine.enabled = False
    while_off = _detection(engine._sync_process_screen(_snapshot(COMMAND_SCREEN)))
    engine.enabled = True
    after = _detection(engine._sync_process_screen(_snapshot(COMMAND_SCREEN)))
    return {
        "before": before,
        "while_off": while_off,
        "after": after,
        "default_enabled": DetectionEngine(json.dumps(RULES)).enabled,
    }


def _record_reload() -> dict[str, Any]:
    """Hot reloading, successful and not."""
    engine = DetectionEngine(json.dumps(RULES))
    before = _detection(engine._sync_process_screen(_snapshot(LOGIN_SCREEN)))
    before_count = engine.pattern_count

    engine.reload_rules(json.dumps(OTHER_RULES))
    after = _detection(engine._sync_process_screen(_snapshot(LOGIN_SCREEN)))
    after_count = engine.pattern_count

    failed_error: str | None = None
    try:
        engine.reload_rules("{not json")
    except ValueError as exc:
        failed_error = str(exc)
    survived = _detection(engine._sync_process_screen(_snapshot(LOGIN_SCREEN)))

    # The cached answer must go with the rules that produced it.
    cached = DetectionEngine(json.dumps(RULES))
    cached._sync_process_screen(_snapshot(LOGIN_SCREEN))
    cached.reload_rules(json.dumps(OTHER_RULES))
    after_cached = _detection(cached._sync_process_screen(_snapshot(LOGIN_SCREEN)))

    return {
        "before": before,
        "before_count": before_count,
        "after": after,
        "after_count": after_count,
        "failed_error": failed_error,
        "survived": survived,
        "after_cached": after_cached,
    }


async def _record_async() -> dict[str, Any]:
    """Buffering, hooks, and a saver that fails."""
    engine = DetectionEngine(json.dumps(RULES), idle_threshold_s=0.0)
    seen: list[Any] = []

    async def hook(snapshot: Any, detection: Any, buffer: Any, is_idle: bool) -> None:
        seen.append({"prompt_id": detection.prompt_id if detection else None, "is_idle": is_idle})

    async def failing_hook(*_args: Any) -> None:
        raise RuntimeError("hook exploded")

    engine.add_hook(failing_hook)
    engine.add_hook(hook)

    matched = _detection(await engine.process_screen(_snapshot(COMMAND_SCREEN)))
    unmatched = _detection(await engine.process_screen(_snapshot("nothing at all")))

    class _Saver:
        """A saver that always fails."""

        _enabled = True
        _namespace = None

        def save_screen(self, *_args: Any, **_kwargs: Any) -> None:
            raise OSError("disk is full")

        def set_namespace(self, ns: Any) -> None:
            self._namespace = ns

        def get_screens_dir(self) -> str:
            return "/tmp/screens"

        def get_saved_count(self) -> int:
            return 0

        def set_enabled(self, enabled: bool) -> None:
            self._enabled = enabled

    with_saver = DetectionEngine(json.dumps(RULES), screen_saver=_Saver())
    survived_saver = _detection(await with_saver.process_screen(_snapshot(COMMAND_SCREEN)))

    return {
        "matched": matched,
        "unmatched": unmatched,
        "hook_calls": seen,
        "survived_a_failing_saver": survived_saver,
        "saver_status_without_one": DetectionEngine(json.dumps(RULES)).get_screen_saver_status(),
        "saver_status_with_one": with_saver.get_screen_saver_status(),
    }


def _record_namespace() -> dict[str, Any]:
    """The namespace, which is passed along to a saver."""
    engine = DetectionEngine(json.dumps(RULES), namespace="tw2002")
    started = engine.namespace
    engine.set_namespace("other")
    changed = engine.namespace
    default = DetectionEngine(json.dumps(RULES)).namespace
    return {"started": started, "changed": changed, "default": default}


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "rules": RULES,
        "other_rules": OTHER_RULES,
        "command_screen": COMMAND_SCREEN,
        "login_screen": LOGIN_SCREEN,
        "sync": _record_sync(),
        "cache": _record_cache(),
        "disabled": _record_disabled(),
        "reload": _record_reload(),
        "asynchronous": asyncio.run(_record_async()),
        "namespace": _record_namespace(),
        "pattern_count": DetectionEngine(json.dumps(RULES)).pattern_count,
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(SYNC_CASES)} sync cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
