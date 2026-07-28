#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the prompt detector.

The detector decides whether a terminal is waiting for input. Get it wrong in
one direction and an agent types into a screen that is still drawing; wrong in
the other and it waits forever at a prompt it failed to recognise. Everything
here exists to make one of those two failures less likely.

**Two passes, region first.** Prompts live at the bottom, so the tail of the
screen is searched before the whole of it. That is not only speed: matching
the region first stops a stale prompt still visible in scrollback from firing
ahead of the live one. The region is anchored to the last line with content
rather than to the bottom row, because many UIs leave blank rows below.

**Exclusions are case-insensitive; prompts are not.** Deliberately asymmetric
in the reference: a rule blocking ``stardock`` should block ``STARDOCK`` too,
while a prompt written for ``Command:`` must not fire on ``command:``.

**The cursor test is a heuristic with a fallback.** A pattern may require the
cursor at the end. When that check fails, the match is kept aside rather than
dropped, and is used anyway if the screen has a trailing space — which
correlates with a live input field. Without the fallback a session whose
cursor bookkeeping drifted would hang forever.

**A broken rule is loud or quiet, on purpose.** Non-strict keeps the surviving
patterns and records the failures, so one typo does not take detection
offline; strict raises, so a curated production rule set fails at startup
instead of silently degrading.

**The fingerprint is a cache key that includes cursor state.** Two screens
with identical text but a moved cursor are different questions, so they get
different keys rather than a stale answer.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_detector_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.detection.detector import DetectorPatternCompileError, PromptDetector

OUT = Path(__file__).with_name("detector_golden.json")

SCREEN = "Welcome to the game\nSTARDOCK is closed\n\nCommand [TL=00:00:00]:? "

# A screen long enough that the region genuinely excludes the top of it, so a
# pass that searched the whole screen where it should search the region — or
# an exclusion that searched the region where it should search the screen —
# reaches a different answer.
TALL = "BANNER at the very top\n" + "\n".join(f"filler {n}" for n in range(30)) + "\nCommand [TL=00:00:00]:? "

PATTERNS: list[dict[str, Any]] = [
    {"id": "command", "regex": r"Command \[TL=[\d:]+\]:", "input_type": "single_key"},
    {"id": "yes_no", "regex": r"\(Y/N\)\??", "input_type": "single_key"},
    {"id": "any_key", "regex": r"press any key", "input_type": "single_key", "expect_cursor_at_end": False},
    {"id": "name", "regex": r"Enter your name:", "input_type": "line", "eol_pattern": r"\r"},
]


def _snapshot(screen: str, **extra: Any) -> dict[str, Any]:
    """A screen snapshot, as the emulator hands one over."""
    return {"screen": screen, "screen_hash": "h", **extra}


def _match(detector: PromptDetector, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """What the detector made of one snapshot."""
    result = detector.detect_prompt(snapshot)
    return None if result is None else result.model_dump()


# (name, snapshot) — what fires, and what does not.
DETECT_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a prompt at the bottom", _snapshot(SCREEN)),
    ("no prompt at all", _snapshot("just some output\nnothing to answer")),
    ("an empty screen", _snapshot("")),
    ("a missing screen", {"screen_hash": "h"}),
    ("a null screen", _snapshot(None)),
    ("the cursor is not at the end", _snapshot(SCREEN, cursor_at_end=False)),
    # The cursor heuristic is wrong sometimes; a trailing space says the field
    # is live, so the match is used anyway rather than hanging forever.
    (
        "the cursor is wrong but there is a trailing space",
        _snapshot(SCREEN, cursor_at_end=False, has_trailing_space=True),
    ),
    ("a pattern that does not need the cursor", _snapshot("Loading...\npress any key", cursor_at_end=False)),
    (
        "that same pattern with a trailing space",
        _snapshot("Loading...\npress any key", cursor_at_end=False, has_trailing_space=True),
    ),
    ("a prompt only in scrollback", _snapshot("Command [TL=00:00:00]:?\n" + "\n".join(f"line {n}" for n in range(40)))),
    ("a prompt below blank rows", _snapshot("Command [TL=00:00:00]:? \n\n\n\n")),
    ("the cursor is inside the region", _snapshot(SCREEN, cursor={"x": 5, "y": 3})),
    (
        "the cursor is above the region",
        _snapshot("\n".join(f"line {n}" for n in range(40)) + "\nCommand [TL=00:00:00]:?", cursor={"x": 0, "y": 0}),
    ),
    ("two prompts, the first pattern wins", _snapshot("Enter your name:\nCommand [TL=00:00:00]:? ")),
    ("a line prompt", _snapshot("Enter your name: ")),
    ("a yes/no prompt", _snapshot("Delete it? (Y/N)?")),
    ("a whitespace-only last line", _snapshot("Command [TL=00:00:00]:? \n   \n  ")),
    ("a fractional cursor", _snapshot(SCREEN, cursor={"x": 1.9, "y": 2.9})),
    # Truthy but not a boolean, on the path that can actually reach the
    # fallback — the cursor has to be above the region for a candidate to be
    # recorded at all.
    (
        "a trailing flag that is not a boolean",
        _snapshot(TALL, cursor={"x": 0, "y": 0}, cursor_at_end=False, has_trailing_space="yes"),
    ),
    # Two prompts, one above the region and one inside it. The region pass has
    # to win, or a stale prompt in scrollback answers ahead of the live one.
    (
        "a stale prompt above a live one",
        _snapshot("Enter your name:\n" + "\n".join(f"filler {n}" for n in range(30)) + "\nCommand [TL=00:00:00]:? "),
    ),
    # The cursor-miss fallback only becomes reachable when the cursor is above
    # the region, because that is the only path that runs the full-screen pass
    # — the region pass never records a candidate.
    (
        "the cursor is above the region and wrong, with a trailing space",
        _snapshot(
            "\n".join(f"line {n}" for n in range(40)) + "\nCommand [TL=00:00:00]:? ",
            cursor={"x": 0, "y": 0},
            cursor_at_end=False,
            has_trailing_space=True,
        ),
    ),
    (
        "the same, without the trailing space",
        _snapshot(
            "\n".join(f"line {n}" for n in range(40)) + "\nCommand [TL=00:00:00]:? ",
            cursor={"x": 0, "y": 0},
            cursor_at_end=False,
        ),
    ),
]

# (name, patterns, screen) — exclusions.
NEGATIVE_CASES: list[tuple[str, list[dict[str, Any]], str]] = [
    (
        "a negative regex blocks it",
        [{"id": "cmd", "regex": r"Command", "negative_regex": r"STARDOCK"}],
        SCREEN,
    ),
    (
        "the negative regex is not on screen",
        [{"id": "cmd", "regex": r"Command", "negative_regex": r"NOWHERE"}],
        SCREEN,
    ),
    (
        "a negative regex is case insensitive",
        [{"id": "cmd", "regex": r"Command", "negative_regex": r"stardock"}],
        SCREEN,
    ),
    (
        "the positive pattern is not",
        [{"id": "cmd", "regex": r"command"}],
        SCREEN,
    ),
    (
        "a contains-mode negative match",
        [{"id": "cmd", "regex": r"Command", "negative_match": {"pattern": "STARDOCK is", "match_mode": "contains"}}],
        SCREEN,
    ),
    (
        "a contains-mode match with regex characters in it",
        [{"id": "cmd", "regex": r"Command", "negative_match": {"pattern": "[TL=", "match_mode": "contains"}}],
        SCREEN,
    ),
    (
        "an exact-mode negative match",
        [
            {
                "id": "cmd",
                "regex": r"Command",
                "negative_match": {"pattern": "STARDOCK is closed", "match_mode": "exact"},
            }
        ],
        SCREEN,
    ),
    (
        "an exact-mode match that is only a substring",
        [{"id": "cmd", "regex": r"Command", "negative_match": {"pattern": "STARDOCK", "match_mode": "exact"}}],
        SCREEN,
    ),
    (
        "a negative match with no mode is a regex",
        [{"id": "cmd", "regex": r"Command", "negative_match": {"pattern": r"STAR\w+"}}],
        SCREEN,
    ),
    (
        "an empty negative match dict",
        [{"id": "cmd", "regex": r"Command", "negative_match": {}}],
        SCREEN,
    ),
    (
        "a negative match that is not a dict",
        [{"id": "cmd", "regex": r"Command", "negative_match": "STARDOCK"}],
        SCREEN,
    ),
    (
        "a null negative match",
        [{"id": "cmd", "regex": r"Command", "negative_match": None}],
        SCREEN,
    ),
    (
        "negative_regex wins over negative_match",
        [
            {
                "id": "cmd",
                "regex": r"Command",
                "negative_regex": r"NOWHERE",
                "negative_match": {"pattern": "STARDOCK", "match_mode": "contains"},
            }
        ],
        SCREEN,
    ),
    (
        "the exclusion only looks at the whole screen",
        # BANNER is far above the prompt region, and still blocks the match.
        [{"id": "cmd", "regex": r"Command", "negative_regex": r"BANNER"}],
        TALL,
    ),
    (
        "an empty negative regex blocks nothing",
        # Falsy in the reference, so it is no exclusion at all rather than one
        # that matches everywhere.
        [{"id": "cmd", "regex": r"Command", "negative_regex": ""}],
        SCREEN,
    ),
    (
        "a null negative regex",
        [{"id": "cmd", "regex": r"Command", "negative_regex": None}],
        SCREEN,
    ),
    (
        "an empty pattern in a contains-mode match",
        [{"id": "cmd", "regex": r"Command", "negative_match": {"match_mode": "contains"}}],
        SCREEN,
    ),
]

# (name, region tail lines, screen)
REGION_CASES: list[tuple[str, int, str]] = [
    ("the default tail", 12, SCREEN),
    ("one line", 1, SCREEN),
    ("more lines than there are", 100, SCREEN),
    ("zero lines is treated as one", 0, SCREEN),
    ("a negative tail is treated as one", -5, SCREEN),
    ("trailing blank rows are ignored", 12, "one\ntwo\n\n\n\n"),
    ("an all-blank screen", 12, "\n\n\n"),
    ("a single line", 12, "only this"),
    ("an empty screen", 12, ""),
    ("a long screen", 3, "\n".join(f"line {n}" for n in range(20))),
    ("a whitespace-only last line", 12, "content\n   \n\t\n"),
    ("a tall screen at the default tail", 12, TALL),
]

# (name, snapshot) — fingerprints, which are cache keys.
FINGERPRINT_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a plain screen", _snapshot(SCREEN)),
    ("the same screen again", _snapshot(SCREEN)),
    ("a different screen", _snapshot("something else")),
    ("the cursor moved", _snapshot(SCREEN, cursor={"x": 1, "y": 2})),
    ("the cursor moved again", _snapshot(SCREEN, cursor={"x": 2, "y": 2})),
    ("the cursor is not at the end", _snapshot(SCREEN, cursor_at_end=False)),
    ("there is a trailing space", _snapshot(SCREEN, has_trailing_space=True)),
    ("an empty screen", _snapshot("")),
    ("a cursor that is not a number", _snapshot(SCREEN, cursor={"x": "nonsense", "y": 0})),
    ("a null cursor", _snapshot(SCREEN, cursor=None)),
    ("a fractional cursor", _snapshot(SCREEN, cursor={"x": 1.9, "y": 2.9})),
    # Same region, different text above it: the fingerprint covers the region
    # only, so these two must collide rather than being told apart.
    ("a tall screen", _snapshot(TALL)),
    ("the same tail, a different banner", _snapshot(TALL.replace("BANNER", "OTHER!"))),
    ("a cursor with no coordinates", _snapshot(SCREEN, cursor={})),
]


def _record_detect() -> list[dict[str, Any]]:
    """What fires for each snapshot."""
    detector = PromptDetector(PATTERNS)
    records = []
    for name, snapshot in DETECT_CASES:
        diagnostics = detector.detect_prompt_with_diagnostics(snapshot)
        records.append(
            {
                "name": name,
                "snapshot": snapshot,
                "match": _match(detector, snapshot),
                "failures": diagnostics.regex_matched_but_failed,
            }
        )
    return records


def _record_negative() -> list[dict[str, Any]]:
    """Which exclusions block a match."""
    records = []
    for name, patterns, screen in NEGATIVE_CASES:
        detector = PromptDetector(patterns)
        snapshot = _snapshot(screen)
        diagnostics = detector.detect_prompt_with_diagnostics(snapshot)
        records.append(
            {
                "name": name,
                "patterns": patterns,
                "screen": screen,
                "matched": diagnostics.match is not None,
                "prompt_id": diagnostics.match.prompt_id if diagnostics.match else None,
                "failures": diagnostics.regex_matched_but_failed,
            }
        )
    return records


def _record_region() -> list[dict[str, Any]]:
    """The slice of screen a prompt is looked for in."""
    records = []
    for name, tail, screen in REGION_CASES:
        for cursor_y in (0, 3, 100):
            region, in_region = PromptDetector.prompt_region(
                _snapshot(screen, cursor={"x": 0, "y": cursor_y}), tail_lines=tail
            )
            records.append(
                {
                    "name": f"{name} (cursor y={cursor_y})",
                    "screen": screen,
                    "tail_lines": tail,
                    "cursor_y": cursor_y,
                    "region": region,
                    "cursor_in_region": in_region,
                }
            )
    return records


def _record_compile() -> dict[str, Any]:
    """What a broken rule does, loudly and quietly."""
    broken = [
        {"id": "good", "regex": r"Command"},
        {"id": "bad", "regex": r"unclosed ("},
        {"id": "missing", "input_type": "line"},
        {"regex": r"[unclosed"},
    ]
    lenient = PromptDetector(broken)
    strict_error: str | None = None
    try:
        PromptDetector(broken, strict=True)
    except DetectorPatternCompileError as exc:
        strict_error = str(exc)

    # A bad reload must not poison the detector: the previous patterns stay.
    survivor = PromptDetector([{"id": "good", "regex": r"Command"}], strict=True)
    reload_error: str | None = None
    try:
        survivor.reload_patterns([{"id": "bad", "regex": r"("}])
    except DetectorPatternCompileError as exc:
        reload_error = str(exc)

    grower = PromptDetector([{"id": "good", "regex": r"Command"}])
    grower.add_pattern({"id": "second", "regex": r"Enter"})

    # Both rules match the same screen, so which one fires says where the
    # added pattern was put in the order.
    ordered = PromptDetector([{"id": "first", "regex": r"Command"}])
    ordered.add_pattern({"id": "appended", "regex": r"Command \["})

    # A rule set that fails twice must not accumulate its failures.
    twice = PromptDetector([{"id": "bad", "regex": r"("}])
    twice.reload_patterns([{"id": "bad2", "regex": r"["}])

    # A rule whose regex is not a string. The reference does not survive it:
    # re.compile raises TypeError, which compile_patterns does not catch, so
    # the whole detector fails to construct even in lenient mode — the one
    # mode whose entire purpose is to keep going. Recorded as the refusal it
    # is; the TypeScript port treats it as a compile failure instead. See the
    # roadmap's cross-port misalignments.
    non_string_error: str | None = None
    try:
        PromptDetector([{"id": "numeric", "regex": 123}, {"id": "good", "regex": r"Command"}])
    except TypeError as exc:
        non_string_error = str(exc)

    replacer = PromptDetector([{"id": "good", "regex": r"Command"}])
    replacer.reload_patterns([{"id": "other", "regex": r"Enter"}])

    return {
        "lenient_pattern_count": lenient.pattern_count,
        "lenient_failures": list(lenient.compile_failures),
        "lenient_still_detects": _match(lenient, _snapshot(SCREEN)),
        "strict_error": strict_error,
        "reload_error": reload_error,
        "survivor_pattern_count": survivor.pattern_count,
        "survivor_still_detects": _match(survivor, _snapshot(SCREEN)),
        "grown_count": grower.pattern_count,
        "grown_detects_new": _match(grower, _snapshot("Enter your name: ")),
        "grown_detects_old": _match(grower, _snapshot(SCREEN)),
        "replaced_count": replacer.pattern_count,
        "replaced_detects_new": _match(replacer, _snapshot("Enter your name: ")),
        "replaced_detects_old": _match(replacer, _snapshot(SCREEN)),
        "ordered_detects": _match(ordered, _snapshot(SCREEN)),
        "twice_failures": list(twice.compile_failures),
        "python_non_string_regex_error": non_string_error,
    }


def _input_type_refusal() -> str | None:
    """What the reference does with a non-string ``input_type``."""
    try:
        PromptDetector([{"id": "d", "regex": "x", "input_type": 7}]).detect_prompt(_snapshot("x"))
    except Exception as exc:
        return type(exc).__name__
    return None


def main() -> int:
    """Write the golden corpus and report the case count."""
    detector = PromptDetector(PATTERNS)
    corpus = {
        "patterns": PATTERNS,
        "screen": SCREEN,
        "detect": _record_detect(),
        "negative": _record_negative(),
        "region": _record_region(),
        "fingerprints": [
            {"name": name, "snapshot": snapshot, "fingerprint": detector.prompt_fingerprint(snapshot)}
            for name, snapshot in FINGERPRINT_CASES
        ],
        # A lone surrogate cannot travel through JSON, so only the answer is
        # recorded and the test rebuilds the screen. The encode uses
        # errors="replace", so an undecodable screen still fingerprints rather
        # than raising mid-detection.
        "surrogate_fingerprint": detector.prompt_fingerprint(_snapshot("bad \ud800 char")),
        "compile": _record_compile(),
        # A rule that carries extraction instructions for whatever reads the
        # reply. They travel on the match untouched.
        "rich_pattern": _match(
            PromptDetector([{"id": "rich", "regex": "x", "kv_extract": [{"key": "a", "regex": "(.*)"}]}]),
            _snapshot("x"),
        ),
        # A non-string input_type is refused by the reference's own model
        # rather than defaulted. Recorded so the TypeScript port's fallback is
        # visibly a decision about input the reference rejects, not a silent
        # difference on input it accepts.
        "python_non_string_input_type_error": _input_type_refusal(),
        "tall": TALL,
        "defaults": {
            "input_type": PromptDetector([{"id": "d", "regex": "x"}]).detect_prompt(_snapshot("x")).input_type,
            "eol_pattern": PromptDetector([{"id": "d", "regex": "x"}]).detect_prompt(_snapshot("x")).eol_pattern,
            "kv_extract": PromptDetector([{"id": "d", "regex": "x"}]).detect_prompt(_snapshot("x")).kv_extract,
        },
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(DETECT_CASES)} detect, {len(NEGATIVE_CASES)} negative, {len(REGION_CASES)} region)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
