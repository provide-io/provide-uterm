#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.uterm.detection.detector import PromptDetector
from provide.uterm.detection.input_type import auto_detect_input_type


def _make_patterns() -> list[dict]:
    return [
        {"id": "prompt.login", "regex": r"Enter your name:", "input_type": "multi_key", "eol_pattern": "$"},
        {"id": "prompt.password", "regex": r"Password:", "input_type": "multi_key", "eol_pattern": "$"},
    ]


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


def test_prompt_fingerprint_includes_cursor_coords(snap_factory) -> None:
    d = PromptDetector([])
    snap = snap_factory("some text")
    snap["cursor"] = {"x": 10, "y": 20}
    fp = d.prompt_fingerprint(snap)
    assert fp.endswith(":10:20")


def test_prompt_fingerprint_cursor_exception_handled(snap_factory) -> None:
    d = PromptDetector([])
    snap = snap_factory("some text")
    snap["cursor"] = {"x": "bad", "y": None}
    fp = d.prompt_fingerprint(snap)
    # Should default to 0:0
    assert fp.endswith(":0:0")
