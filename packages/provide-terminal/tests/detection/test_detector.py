#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.terminal.detection.detector import PromptDetector
from provide.terminal.detection.input_type import auto_detect_input_type


def _make_patterns() -> list[dict]:
    return [
        {"id": "prompt.login", "regex": r"Enter your name:", "input_type": "multi_key", "eol_pattern": "$"},
        {"id": "prompt.password", "regex": r"Password:", "input_type": "multi_key", "eol_pattern": "$"},
    ]


def test_detect_prompt_matches_in_region(snap_factory) -> None:
    d = PromptDetector(_make_patterns())
    match = d.detect_prompt(snap_factory("Welcome\nEnter your name:"))
    assert match is not None
    assert match.prompt_id == "prompt.login"


def test_detect_prompt_returns_none_on_no_match(snap_factory) -> None:
    d = PromptDetector(_make_patterns())
    assert d.detect_prompt(snap_factory("Just some text")) is None


def test_negative_match_excludes_pattern(snap_factory) -> None:
    patterns = [
        {
            "id": "prompt.buy",
            "regex": r"which item",
            "input_type": "single_key",
            "eol_pattern": "$",
            "negative_match": {"pattern": r"stardock", "match_mode": "regex"},
        }
    ]
    d = PromptDetector(patterns)
    assert d.detect_prompt(snap_factory("stardock\nwhich item")) is None
    assert d.detect_prompt(snap_factory("shop\nwhich item")) is not None


def test_negative_match_allows_when_absent(snap_factory) -> None:
    patterns = [
        {
            "id": "prompt.buy",
            "regex": r"which item",
            "input_type": "single_key",
            "eol_pattern": "$",
            "negative_match": {"pattern": r"stardock", "match_mode": "regex"},
        }
    ]
    d = PromptDetector(patterns)
    result = d.detect_prompt(snap_factory("regular store\nwhich item"))
    assert result is not None
    assert result.prompt_id == "prompt.buy"


def test_normalizer_callback_applied(snap_factory) -> None:
    d = PromptDetector(_make_patterns(), normalizer=lambda t: t.replace("name", "NAME"))
    fp1 = d.prompt_fingerprint(snap_factory("Enter your name:"))
    fp2 = d.prompt_fingerprint(snap_factory("Enter your NAME:"))
    assert fp1 == fp2


def test_no_normalizer_preserves_text(snap_factory) -> None:
    d = PromptDetector(_make_patterns())
    fp1 = d.prompt_fingerprint(snap_factory("Enter your name:"))
    fp2 = d.prompt_fingerprint(snap_factory("Enter your NAME:"))
    assert fp1 != fp2


def test_diagnostics_returns_match(snap_factory) -> None:
    d = PromptDetector(_make_patterns())
    diag = d.detect_prompt_with_diagnostics(snap_factory("Enter your name:"))
    assert diag.match is not None
    assert diag.match.prompt_id == "prompt.login"


def test_diagnostics_no_match(snap_factory) -> None:
    d = PromptDetector(_make_patterns())
    diag = d.detect_prompt_with_diagnostics(snap_factory("nothing here"))
    assert diag.match is None


def test_auto_detect_input_type() -> None:
    result = auto_detect_input_type("Press any key to continue")
    assert isinstance(result, str)


def test_reload_patterns(snap_factory) -> None:
    d = PromptDetector([])
    assert d.detect_prompt(snap_factory("Enter your name:")) is None
    d.reload_patterns(_make_patterns())
    assert d.detect_prompt(snap_factory("Enter your name:")) is not None


def test_add_pattern(snap_factory) -> None:
    d = PromptDetector([])
    d.add_pattern({"id": "p.new", "regex": r"New prompt", "input_type": "single_key", "eol_pattern": "$"})
    assert d.detect_prompt(snap_factory("New prompt")) is not None


def test_pattern_count() -> None:
    d = PromptDetector(_make_patterns())
    assert d.pattern_count == 2

    d.add_pattern({"id": "p.x", "regex": "X", "input_type": "single_key", "eol_pattern": "$"})
    assert d.pattern_count == 3


# ---------------------------------------------------------------------------
# Pattern compilation failures (lines 80-109, 114)
# ---------------------------------------------------------------------------


def test_compile_patterns_bad_regex_is_skipped() -> None:
    """A pattern with invalid regex is logged and skipped; others still compile."""
    patterns = [
        {"id": "bad.pattern", "regex": r"[invalid("},
        {"id": "good.pattern", "regex": r"Enter your name:"},
    ]
    d = PromptDetector(patterns)
    assert d.pattern_count == 2  # both in _patterns
    assert len(d._compiled_all) == 1  # only good one compiled
    assert d._compiled_all[0][1]["id"] == "good.pattern"


def test_compile_patterns_missing_regex_key_is_skipped() -> None:
    """A pattern missing the 'regex' key is logged and skipped."""
    patterns = [
        {"id": "missing.regex"},  # no 'regex' key
        {"id": "good.pattern", "regex": r"Hello"},
    ]
    d = PromptDetector(patterns)
    assert len(d._compiled_all) == 1
    assert d._compiled_all[0][1]["id"] == "good.pattern"


def test_compile_patterns_all_bad_returns_empty() -> None:
    """All bad patterns results in empty compiled list."""
    patterns = [{"id": "bad1", "regex": r"[bad"}, {"id": "bad2", "regex": r"(unclosed"}]
    d = PromptDetector(patterns)
    assert len(d._compiled_all) == 0


# ---------------------------------------------------------------------------
# prompt_region edge cases (lines 137, 143->147, 144->143, 152-153)
# ---------------------------------------------------------------------------


def test_prompt_region_empty_screen() -> None:
    """prompt_region returns ('', False) for empty screen."""
    region, in_region = PromptDetector.prompt_region({"screen": "", "cursor": {}})
    assert region == ""
    assert in_region is False


def test_prompt_region_all_whitespace_lines() -> None:
    """prompt_region handles screen with only whitespace lines (last_idx stays 0)."""
    screen = "   \n   \n   "
    region, in_region = PromptDetector.prompt_region({"screen": screen, "cursor": {"y": 0}})
    # last_idx=0, start_idx=0, region is the first line
    assert isinstance(region, str)


def test_prompt_region_cursor_exception_handled() -> None:
    """prompt_region handles non-numeric cursor y gracefully."""
    snapshot = {"screen": "Hello\nWorld", "cursor": {"y": "not_an_int"}}
    region, in_region = PromptDetector.prompt_region(snapshot)
    assert isinstance(region, str)
    assert isinstance(in_region, bool)


# ---------------------------------------------------------------------------
# normalize_prompt_region with normalizer (line 163)
# ---------------------------------------------------------------------------


def test_normalize_prompt_region_with_normalizer() -> None:
    """normalize_prompt_region calls normalizer when provided."""
    result = PromptDetector.normalize_prompt_region("Hello World", normalizer=lambda t: "NORMALIZED")
    assert result == "NORMALIZED"


def test_normalize_prompt_region_empty_string() -> None:
    """normalize_prompt_region returns '' for empty string regardless of normalizer."""
    result = PromptDetector.normalize_prompt_region("", normalizer=lambda t: "NEVER")
    assert result == ""


# ---------------------------------------------------------------------------
# _resolve_negative_regex variants (lines 197, 203, 205)
# ---------------------------------------------------------------------------


def test_resolve_negative_regex_from_negative_regex_key() -> None:
    """_resolve_negative_regex prefers 'negative_regex' key over 'negative_match'."""
    pattern = {"negative_regex": r"stardock"}
    result = PromptDetector._resolve_negative_regex(pattern)
    assert result == "stardock"


def test_resolve_negative_regex_contains_mode() -> None:
    """_resolve_negative_regex with match_mode 'contains' escapes the pattern."""
    import re

    pattern = {"negative_match": {"pattern": "stardock.station", "match_mode": "contains"}}
    result = PromptDetector._resolve_negative_regex(pattern)
    assert result == re.escape("stardock.station")


def test_resolve_negative_regex_exact_mode() -> None:
    """_resolve_negative_regex with match_mode 'exact' wraps with anchors."""
    import re

    pattern = {"negative_match": {"pattern": "Stardock", "match_mode": "exact"}}
    result = PromptDetector._resolve_negative_regex(pattern)
    assert result == rf"^{re.escape('Stardock')}$"


def test_resolve_negative_regex_regex_mode() -> None:
    """_resolve_negative_regex with match_mode 'regex' returns pattern as-is."""
    pattern = {"negative_match": {"pattern": r"star\w+", "match_mode": "regex"}}
    result = PromptDetector._resolve_negative_regex(pattern)
    assert result == r"star\w+"


def test_resolve_negative_regex_none_when_absent() -> None:
    """_resolve_negative_regex returns None when no negative key present."""
    result = PromptDetector._resolve_negative_regex({"id": "foo", "regex": r"X"})
    assert result is None


# ---------------------------------------------------------------------------
# cursor_miss_candidates / fallback (lines 242-263, 364-369)
# ---------------------------------------------------------------------------


def test_cursor_miss_candidate_saved_when_cursor_not_at_end() -> None:
    """When cursor_at_end=False, regex matches full-screen, cursor miss candidate is recorded.

    Setup: prompt at top (row 0), 25 filler lines, cursor_y=0.
    Region is rows 14-25. cursor_y=0 is NOT in region → full-screen pass fires with compiled_all.
    compiled_fast (no-cursor-end-req) is empty → region pass finds nothing.
    Full-screen pass with compiled_all finds the prompt but cursor_at_end=False → miss candidate.
    """
    filler = "\n".join(["line"] * 25)
    screen = "Enter your name:\n" + filler
    patterns = [
        {
            "id": "prompt.login",
            "regex": r"Enter your name:",
            "input_type": "multi_key",
            "eol_pattern": "$",
            "expect_cursor_at_end": True,
        }
    ]
    d = PromptDetector(patterns)
    snapshot = {
        "screen": screen,
        "screen_hash": "abc",
        "cursor_at_end": False,
        "has_trailing_space": False,
        "cursor": {"y": 0, "x": 0},  # cursor at top (row 0), outside region rows 14-25
    }
    diag = d.detect_prompt_with_diagnostics(snapshot)
    # Without trailing space, fallback is NOT used — result is None
    assert diag.match is None
    # But partial match was recorded (cursor_position reason)
    assert any(r["reason"] == "cursor_position" for r in diag.regex_matched_but_failed)


def test_cursor_miss_fallback_used_with_trailing_space() -> None:
    """When cursor_at_end=False + has_trailing_space=True, fallback prompt is returned.

    Same layout: prompt at top, cursor at row 0 (outside region rows 14-25).
    Full-screen pass records miss candidate; trailing space triggers fallback return.
    """
    filler = "\n".join(["line"] * 25)
    screen = "Enter your name:\n" + filler
    patterns = [
        {
            "id": "prompt.login",
            "regex": r"Enter your name:",
            "input_type": "multi_key",
            "eol_pattern": "$",
            "expect_cursor_at_end": True,
        }
    ]
    d = PromptDetector(patterns)
    snapshot = {
        "screen": screen,
        "screen_hash": "abc",
        "cursor_at_end": False,
        "has_trailing_space": True,
        "cursor": {"y": 0, "x": 0},
    }
    result = d.detect_prompt(snapshot)
    assert result is not None
    assert result.prompt_id == "prompt.login"


# ---------------------------------------------------------------------------
# Full-screen fallback when cursor not in region (lines 327->344, 345-359)
# ---------------------------------------------------------------------------


def test_full_screen_fallback_when_cursor_not_in_region() -> None:
    """When pattern matches full screen but not region, full-screen scan is used.

    Setup: prompt at top (line 0), filler lines after it, cursor_y=0 (above region start).
    With 25 filler lines: last_idx=25, start_idx=max(0,25-12+1)=14.
    cursor_y=0 < start_idx=14 → cursor NOT in region → full-screen fallback fires.
    """
    filler = "\n".join(["line"] * 25)
    tall_screen = "Enter your name:\n" + filler
    patterns = [
        {
            "id": "prompt.login",
            "regex": r"Enter your name:",
            "input_type": "multi_key",
            "eol_pattern": "$",
            "expect_cursor_at_end": True,
        }
    ]
    d = PromptDetector(patterns)
    # cursor_y=0: the prompt is at row 0, which is ABOVE the region (rows 14-25)
    snapshot = {
        "screen": tall_screen,
        "screen_hash": "x",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "cursor": {"y": 0, "x": 0},
    }
    result = d.detect_prompt(snapshot)
    assert result is not None
    assert result.prompt_id == "prompt.login"


# ---------------------------------------------------------------------------
# detect_prompt_with_diagnostics: empty screen branch (line 310->320)
# ---------------------------------------------------------------------------


def test_detect_with_diagnostics_empty_screen() -> None:
    """detect_prompt_with_diagnostics handles empty screen gracefully."""
    d = PromptDetector(_make_patterns())
    diag = d.detect_prompt_with_diagnostics({"screen": "", "cursor_at_end": True})
    assert diag.match is None


# ---------------------------------------------------------------------------
# auto_detect_input_type: all branches (lines 415-450)
# ---------------------------------------------------------------------------


def test_auto_detect_input_type_any_key() -> None:
    assert auto_detect_input_type("Press any key to continue") == "any_key"
    assert auto_detect_input_type("Press a key now") == "any_key"
    assert auto_detect_input_type("Hit any key") == "any_key"
    assert auto_detect_input_type("Strike any key") == "any_key"
    assert auto_detect_input_type("<more> text") == "any_key"
    assert auto_detect_input_type("[more] pages") == "any_key"
    assert auto_detect_input_type("-- more --") == "any_key"


def test_auto_detect_input_type_single_key() -> None:
    assert auto_detect_input_type("Continue? (y/n)") == "single_key"
    assert auto_detect_input_type("Proceed (yes/no)") == "single_key"
    assert auto_detect_input_type("Are you sure? Continue?") == "single_key"
    assert auto_detect_input_type("Quit?") == "single_key"
    assert auto_detect_input_type("Abort?") == "single_key"
    assert auto_detect_input_type("Retry?") == "single_key"
    assert auto_detect_input_type("Delete [y/n]") == "single_key"
    assert auto_detect_input_type("(q)uit") == "single_key"
    assert auto_detect_input_type("(a)bort") == "single_key"


def test_auto_detect_input_type_multi_key_keywords() -> None:
    assert auto_detect_input_type("Please enter your choice") == "multi_key"
    assert auto_detect_input_type("Type your message here") == "multi_key"
    assert auto_detect_input_type("Input required") == "multi_key"
    assert auto_detect_input_type("Name: ") == "multi_key"
    assert auto_detect_input_type("Password: ") == "multi_key"
    assert auto_detect_input_type("Username: ") == "multi_key"
    assert auto_detect_input_type("Choose: ") == "multi_key"
    assert auto_detect_input_type("Select: ") == "multi_key"
    assert auto_detect_input_type("Command: ") == "multi_key"
    assert auto_detect_input_type("Search: ") == "multi_key"


def test_auto_detect_input_type_default_multi_key() -> None:
    """Default fallback returns multi_key."""
    assert auto_detect_input_type("Some random text with no known prompt phrases") == "multi_key"


# ---------------------------------------------------------------------------
# diagnostics: partial match logging when no match but regex_matched_but_failed
# ---------------------------------------------------------------------------


def test_diagnostics_logs_partial_failures() -> None:
    """When regex matched but cursor check failed (and no fallback), failed list is populated."""
    # Use same tall-screen layout so full-screen pass fires and records the miss.
    filler = "\n".join(["line"] * 25)
    screen = "Enter your name:\n" + filler
    patterns = [
        {
            "id": "prompt.login",
            "regex": r"Enter your name:",
            "input_type": "multi_key",
            "expect_cursor_at_end": True,
        }
    ]
    d = PromptDetector(patterns)
    snapshot = {
        "screen": screen,
        "screen_hash": "abc",
        "cursor_at_end": False,
        "has_trailing_space": False,
        "cursor": {"y": 0, "x": 0},
    }
    diag = d.detect_prompt_with_diagnostics(snapshot)
    assert diag.match is None
    assert len(diag.regex_matched_but_failed) > 0


# ---------------------------------------------------------------------------
# kv_extract field on PromptMatch (lines 242-263 cursor_miss_candidates with kv)
# ---------------------------------------------------------------------------


def test_detect_in_text_with_none_cursor_miss_candidates() -> None:
    """_detect_in_text skips appending when cursor_miss_candidates is None."""
    patterns = [
        {
            "id": "prompt.login",
            "regex": r"Enter your name:",
            "input_type": "multi_key",
            "eol_pattern": "$",
            "expect_cursor_at_end": True,
        }
    ]
    d = PromptDetector(patterns)
    result = d._detect_in_text(
        text="Enter your name:",
        full_screen="Enter your name:",
        cursor_at_end=False,  # triggers cursor check failure
        compiled=d._compiled_all,
        regex_matched_but_failed=[],
        cursor_miss_candidates=None,  # None: branch 253->263 is taken
    )
    assert result is None  # no match returned


def test_cursor_miss_candidate_includes_kv_extract() -> None:
    """Cursor-miss candidate PromptMatch includes kv_extract when pattern specifies it."""
    kv_cfg = [{"field": "score", "regex": r"Score:\s+(\d+)", "type": "int"}]
    # Tall screen so cursor at row 0 is outside region rows 14-25
    filler = "\n".join(["line"] * 25)
    screen = "Score: 100\n" + filler
    patterns = [
        {
            "id": "prompt.score",
            "regex": r"Score:",
            "input_type": "multi_key",
            "expect_cursor_at_end": True,
            "kv_extract": kv_cfg,
        }
    ]
    d = PromptDetector(patterns)
    snapshot = {
        "screen": screen,
        "screen_hash": "abc",
        "cursor_at_end": False,
        "has_trailing_space": True,
        "cursor": {"y": 0, "x": 0},
    }
    result = d.detect_prompt(snapshot)
    assert result is not None
    assert result.kv_extract == kv_cfg
