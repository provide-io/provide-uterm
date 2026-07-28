#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the screen saver.

Saved screens are what somebody reads back weeks later to work out what a
session was doing, so the file is a record and the header is most of its
value.

**A screen is saved once.** The hash is the identity, and a terminal redraws
the same screen constantly — without the check a session fills a disk with
copies of one screen. The hash is remembered only *after* the write succeeds,
so a failed save is retried rather than silently skipped for ever.

**A forced save never overwrites.** It finds a free name instead, because the
point of forcing is to keep a second copy, not to destroy the first.

**A screen with no content or no hash is not saved.** There is nothing to read
back and nothing to identify it by.

**The header is fixed-width and self-describing.** Everything a reader needs
to place the capture — when, what hash, where the cursor was, how big the
terminal was — with the screen after it, separated by a rule.

Timestamps are recorded under a fixed timezone. The reference formats them in
local time, so the corpus would otherwise depend on where it was generated.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_saver_golden.py
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

os.environ["TZ"] = "UTC"
time.tzset()

from provide.uterm.detection.saver import ScreenSaver  # noqa: E402 - after the timezone is fixed

OUT = Path(__file__).with_name("saver_golden.json")

# A fixed instant, so the recorded names and headers do not move.
CAPTURED_AT = 1_760_000_000.0

BASE: dict[str, Any] = {
    "screen": "Command [TL=00:00:00]:? ",
    "screen_hash": "abcdef0123456789",
    "captured_at": CAPTURED_AT,
}

# (name, snapshot, prompt id) — what the header says.
HEADER_CASES: list[tuple[str, dict[str, Any], str | None]] = [
    ("the least a snapshot can carry", BASE, None),
    ("with a prompt id", BASE, "command"),
    ("with a cursor", {**BASE, "cursor": {"x": 12, "y": 3}}, None),
    ("with a partial cursor", {**BASE, "cursor": {"x": 12}}, None),
    ("with a size", {**BASE, "cols": 132, "rows": 43}, None),
    ("with a terminal type", {**BASE, "term": "xterm-256color"}, None),
    (
        "with a detection",
        {**BASE, "prompt_detected": {"input_type": "single_key", "is_idle": True}},
        "command",
    ),
    ("with an empty detection", {**BASE, "prompt_detected": {}}, None),
    ("with the cursor at the end", {**BASE, "cursor_at_end": True}, None),
    ("with the cursor not at the end", {**BASE, "cursor_at_end": False}, None),
    ("with a time since the last change", {**BASE, "time_since_last_change": 1.5}, None),
    ("with a time that needs rounding", {**BASE, "time_since_last_change": 1.23456}, None),
    ("with a zero time since the last change", {**BASE, "time_since_last_change": 0.0}, None),
    (
        "with everything at once",
        {
            **BASE,
            "cursor": {"x": 1, "y": 2},
            "cols": 100,
            "rows": 30,
            "term": "vt100",
            "cursor_at_end": True,
            "time_since_last_change": 2.0,
            "prompt_detected": {"input_type": "line", "is_idle": False},
        },
        "login",
    ),
    ("with a multi-line screen", {**BASE, "screen": "one\ntwo\nthree"}, None),
    # Midnight, where a twelve-hour formatter reports the hour as 24.
    ("captured at midnight", {**BASE, "captured_at": 1_760_054_400.0}, None),
    ("captured one second before midnight", {**BASE, "captured_at": 1_760_054_399.0}, None),
    ("with an empty prompt id", BASE, ""),
]

# A snapshot with no capture time falls back to the current one, so its
# filename and header cannot be recorded — only that it saved at all.
NO_CAPTURED_AT: dict[str, Any] = {"screen": "x", "screen_hash": "deadbeefcafe"}

# (name, snapshot) — snapshots that are not saved at all.
REFUSED: list[tuple[str, dict[str, Any]]] = [
    ("no screen", {"screen": "", "screen_hash": "abc", "captured_at": CAPTURED_AT}),
    ("no hash", {"screen": "x", "screen_hash": "", "captured_at": CAPTURED_AT}),
    ("neither", {"captured_at": CAPTURED_AT}),
]


def _saver(base: Path, namespace: str | None = None, enabled: bool = True) -> ScreenSaver:
    """A saver rooted somewhere temporary."""
    return ScreenSaver(base, namespace=namespace, enabled=enabled)


def _record_headers() -> list[dict[str, Any]]:
    """What each snapshot's file contains."""
    records = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, snapshot, prompt_id in HEADER_CASES:
            saver = _saver(Path(tmp))
            path = saver.save_screen(snapshot, prompt_id=prompt_id)
            assert path is not None, name
            records.append(
                {
                    "name": name,
                    "snapshot": snapshot,
                    "prompt_id": prompt_id,
                    "filename": path.name,
                    "content": path.read_text(),
                }
            )
    return records


def _record_behaviour() -> dict[str, Any]:
    """Saving twice, forcing, disabling, and where files land."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        once = _saver(base)
        first = once.save_screen(BASE)
        second = once.save_screen(BASE)
        forced = once.save_screen(BASE, force=True)
        forced_again = once.save_screen(BASE, force=True)

        # A different screen is a different hash, so it saves.
        other = once.save_screen({**BASE, "screen": "other", "screen_hash": "9999"})

        disabled = _saver(base, enabled=False)
        while_off = disabled.save_screen(BASE)

        cleared = _saver(base / "cleared")
        cleared.save_screen(BASE)
        count_before_clear = cleared.get_saved_count()
        cleared.clear_saved_hashes()
        count_after_clear = cleared.get_saved_count()
        after_clear = cleared.save_screen(BASE)

        # Recorded as path segments relative to the saver's own base. An
        # absolute temporary path differs every run — and on macOS the
        # resolved form gains a /private prefix the base does not have — so
        # neither survives a drift check.
        namespaced = _saver(base / "ns", namespace="tw2002")
        namespaced.save_screen(BASE)
        namespaced_tail = namespaced.get_screens_dir().relative_to(base / "ns").parts

        renamed = _saver(base / "renamed")
        shared_tail = renamed.get_screens_dir().relative_to(base / "renamed").parts
        renamed.set_namespace("other")
        renamed_tail = renamed.get_screens_dir().relative_to(base / "renamed").parts

        # Saved, forgotten, then saved again: the second write finds its own
        # file already there, without having been forced.
        resaved = _saver(base / "resaved")
        resaved.save_screen(BASE)
        resaved.clear_saved_hashes()
        resaved_again = resaved.save_screen(BASE)

        empty_named = _saver(base / "empty", namespace="")
        empty_named_tail = empty_named.get_screens_dir().relative_to(base / "empty").parts

        toggled = _saver(base / "toggled", enabled=False)
        off_result = toggled.save_screen(BASE)
        toggled.set_enabled(True)
        on_result = toggled.save_screen(BASE)

        return {
            "first_filename": first.name if first else None,
            "saved_twice": second,
            "forced_filename": forced.name if forced else None,
            "forced_again_filename": forced_again.name if forced_again else None,
            "other_filename": other.name if other else None,
            "count_after_three": once.get_saved_count(),
            "disabled": while_off,
            "count_before_clear": count_before_clear,
            "count_after_clear": count_after_clear,
            "saves_again_after_clear": after_clear is not None,
            "namespaced_dir_tail": list(namespaced_tail),
            "shared_dir_tail": list(shared_tail),
            "namespaced_dir_tail_after_rename": list(renamed_tail),
            "resaved_filename": resaved_again.name if resaved_again else None,
            "empty_namespace_tail": list(empty_named_tail),
            "off_result": off_result,
            "on_result_saved": on_result is not None,
        }


def _record_refused() -> list[dict[str, Any]]:
    """Snapshots that produce no file at all."""
    records = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, snapshot in REFUSED:
            saver = _saver(Path(tmp))
            records.append({"name": name, "snapshot": snapshot, "saved": saver.save_screen(snapshot) is not None})
    return records


def _saves_without_a_time() -> bool:
    """A snapshot with no capture time still saves, timed from now."""
    with tempfile.TemporaryDirectory() as tmp:
        return _saver(Path(tmp)).save_screen(NO_CAPTURED_AT) is not None


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "captured_at": CAPTURED_AT,
        "base": BASE,
        "headers": _record_headers(),
        "behaviour": _record_behaviour(),
        "refused": _record_refused(),
        "no_captured_at_saves": _saves_without_a_time(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(HEADER_CASES)} headers, {len(REFUSED)} refused)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
