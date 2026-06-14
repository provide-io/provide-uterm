#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Two-pass and diagnostics survivor tests for detector.py."""

from __future__ import annotations

import pytest

from provide.uterm.detection.detector import PromptDetector


class TestRunTwoPassDetection:
    """Kwarg-pass-through mutations in the two-pass orchestrator."""

    def test_cursor_miss_candidates_collected_from_full_screen_pass(self) -> None:
        """Targets _run_two_pass_detection mutant 10 (kwarg → None on
        region pass) and 23 (XX-wrapped log) — verifies the full-screen
        pass appends cursor-miss candidates which the fallback then
        returns.

        We use the full-screen pass (long screen, cursor at y=0 →
        cursor_in_region False) because the region pass with
        cursor_at_end=False uses compiled_fast which filters out
        expect_cursor_at_end=True patterns.
        """
        d = PromptDetector([{"id": "p", "regex": r"user\$\s*$", "expect_cursor_at_end": True}])
        screen = "\n".join(f"line{i}" for i in range(30)) + "\nuser$ "
        diag = d.detect_prompt_with_diagnostics(
            {
                "screen": screen,
                "cursor_at_end": False,
                "has_trailing_space": True,
                "cursor": {"x": 0, "y": 0},
            }
        )
        # Under original: full-screen pass appends candidate → fallback returns it.
        # Under mutant: candidate not appended → fallback can't fire → no match.
        assert diag.match is not None
        assert diag.match.prompt_id == "p"

    def test_full_screen_kwarg_passes_screen_value(self) -> None:
        """Targets _run_two_pass_detection mutant 28 — ``full_screen=screen``
        becomes ``full_screen=None`` for the FULL-SCREEN pass call.

        When a pattern has a negative_regex, the full_screen value is the
        haystack for the exclusion check. Passing None would TypeError
        out of re.search.

        Force the full-screen-only path: the unique regex token lives at
        line 0 (outside the tail-12 region), so the region pass returns
        no match; cursor at y=0 → cursor_in_region=False → full-screen
        pass runs and exercises the ``full_screen=screen`` kwarg.
        """
        # Token at line 0 — NOT inside the tail-12 region.
        screen = "uniquetoken\n" + "\n".join(f"line{i}" for i in range(30))
        d = PromptDetector(
            [
                {
                    "id": "p",
                    "regex": r"uniquetoken",
                    "expect_cursor_at_end": True,
                    "negative_regex": r"banner-that-does-not-appear",
                }
            ]
        )
        diag = d.detect_prompt_with_diagnostics(
            {
                "screen": screen,
                "cursor_at_end": True,
                "cursor": {"x": 0, "y": 0},
            }
        )
        # Under the mutant (full_screen=None), re.search raises TypeError
        # and the test fails. Under the original, the negative_regex
        # doesn't match → positive match returned via full-screen pass.
        assert diag.match is not None
        assert diag.match.prompt_id == "p"


# ---------------------------------------------------------------------------
# detect_prompt_with_diagnostics — snapshot.get + kwarg-pass mutations
# ---------------------------------------------------------------------------


class TestDiagnosticsSnapshotDefaults:
    """Default-value mutations on the snapshot.get(...) lookups."""

    def test_missing_screen_key_treated_as_empty_string(self) -> None:
        """Targets dpwd_9/10 — ``snapshot.get("screen", "")`` /
        ``or ""`` becomes ``"XXXX"``.

        When the snapshot lacks ``screen``, the function falls back to
        "" and short-circuits (no logs, no match). Mutant 9 (default
        "XXXX") and mutant 10 (or "XXXX") would make ``screen`` non-empty
        — and the region log fires.
        """
        d = PromptDetector([{"id": "p", "regex": r"."}])
        diag = d.detect_prompt_with_diagnostics({"cursor": {"x": 0, "y": 0}})
        assert diag.match is None
        # No partial-match entries — the function never enters the
        # detection loop because screen is empty.
        assert diag.regex_matched_but_failed == []

    def test_missing_cursor_at_end_defaults_to_true(self) -> None:
        """Targets dpwd_13 (default None), dpwd_15 (drop kwarg), dpwd_18
        (default False) — ``cursor_at_end`` lookup default.

        Pattern requires expect_cursor_at_end=True. Snapshot omits
        cursor_at_end. Under original (default True), pattern matches.
        Under mutants (None/False), the cursor check fails and the
        pattern is recorded as a partial match with cursor_position.
        """
        d = PromptDetector([{"id": "p", "regex": r"user", "expect_cursor_at_end": True}])
        diag = d.detect_prompt_with_diagnostics({"screen": "user", "cursor": {"x": 0, "y": 0}})
        # Original: cursor_at_end defaults to True → match.
        # Mutant: defaults to None/False → no match, cursor_position diagnostic.
        assert diag.match is not None
        assert diag.match.prompt_id == "p"

    def test_missing_has_trailing_space_defaults_to_false(self) -> None:
        """Targets dpwd_26 (default True).

        With pattern requiring expect_cursor_at_end and cursor_at_end=False
        on snapshot, the cursor-miss candidate is recorded (via the full-
        screen pass — region pass with cursor_at_end=False uses compiled_fast
        which excludes cursor-required patterns). The fallback path is
        gated on ``bool(has_trailing_space)``. With has_trailing_space
        omitted and default=False → no fallback → no match. With mutant
        default=True → fallback fires → match returned.
        """
        d = PromptDetector([{"id": "p", "regex": r"user", "expect_cursor_at_end": True}])
        # Long screen, cursor at y=0 → cursor_in_region=False → full-screen
        # pass runs, evaluates the strict pattern, appends candidate.
        screen = "user\n" + "\n".join(f"line{i}" for i in range(30))
        diag = d.detect_prompt_with_diagnostics(
            {
                "screen": screen,
                "cursor_at_end": False,
                "cursor": {"x": 0, "y": 0},
            }
        )
        # Original: no fallback → no match.
        # Mutant 26 (True): fallback fires → match present.
        assert diag.match is None

    def test_fallback_uses_cursor_at_end_via_bool_not_none(self) -> None:
        """Targets dpwd_80 — ``not bool(cursor_at_end)`` becomes ``not bool(None)``.

        ``bool(None)`` is False → ``not bool(None)`` is True. The fallback
        path's first condition is then unconditionally True (regardless
        of cursor_at_end). With cursor_at_end=True the fallback should
        NOT fire; the mutant makes it fire.
        """
        # cursor_at_end=True → no cursor_miss candidate is recorded at
        # all (the check passes). So the fallback list is empty and the
        # mutation is unobservable on this snapshot.
        # Construct a snapshot where:
        #  - cursor_at_end=True (so the fallback's not bool(True) is False)
        #  - has_trailing_space=True
        #  - AND a cursor_miss candidate is somehow recorded
        # We can't get a cursor_miss with cursor_at_end=True. So mutant
        # dpwd_80 fires only when the candidate list is non-empty AND
        # cursor_at_end is truthy. Construct via a two-pattern snapshot
        # where the first pass fails cursor and the second succeeds…
        # Actually the candidate is only added when the check fails. So
        # dpwd_80 is only observable when (a) candidates exist and (b)
        # cursor_at_end is truthy. Those two are mutually exclusive in
        # the current code path. Mark dpwd_80 as documented EQUIV here.
        pytest.skip("dpwd_80 is unreachable on the natural data flow — EQUIV")


class TestDiagnosticsRegexMatchedButFailedPassthrough:
    """Kwarg-pass-through mutations on the diagnostics return value."""

    def test_match_path_preserves_regex_matched_but_failed(self) -> None:
        """Targets dpwd_76 — the success-return drops the
        ``regex_matched_but_failed`` kwarg → defaults to empty list →
        diagnostic content lost.

        Construct a snapshot where two patterns are evaluated by the
        full-screen pass (cursor outside region):
          (a) first pattern requires cursor → cursor_position diagnostic
          (b) second pattern is lenient and matches positively
        """
        d = PromptDetector(
            [
                {
                    "id": "needs_cursor",
                    "regex": r"user",
                    "expect_cursor_at_end": True,
                },
                {
                    "id": "lenient",
                    "regex": r"user",
                    "expect_cursor_at_end": False,
                },
            ]
        )
        # cursor_at_end=False → compiled_fast filters out needs_cursor.
        # We need both patterns to be evaluated in the full-screen pass.
        # Put "user" at line 0 (outside the tail-12 region) so neither
        # pattern matches in the region pass. cursor at y=0 → cursor_in_region
        # is False → full-screen pass runs with compiled_all (both patterns).
        # In full-screen: strict matches regex → cursor check fails →
        # cursor_position diagnostic; lenient matches → returns.
        screen = "user\n" + "\n".join(f"line{i}" for i in range(30))
        diag = d.detect_prompt_with_diagnostics(
            {
                "screen": screen,
                "cursor_at_end": False,
                "cursor": {"x": 0, "y": 0},
            }
        )
        assert diag.match is not None
        assert diag.match.prompt_id == "lenient"
        # Under original: diagnostic carried through.
        # Under mutant dpwd_76: diagnostic dropped → empty list.
        cursor_pos = [e for e in diag.regex_matched_but_failed if e.get("reason") == "cursor_position"]
        assert len(cursor_pos) >= 1

    def test_fallback_path_preserves_regex_matched_but_failed(self) -> None:
        """Targets dpwd_93 — the cursor-miss-fallback return drops the
        ``regex_matched_but_failed`` kwarg.

        Use a long screen so the full-screen pass runs (cursor outside
        region), which evaluates the strict pattern → records the
        cursor_position diagnostic AND appends a cursor_miss candidate.
        With has_trailing_space=True the fallback returns the candidate.
        """
        d = PromptDetector([{"id": "p", "regex": r"user", "expect_cursor_at_end": True}])
        screen = "\n".join(f"line{i}" for i in range(30)) + "\nuser"
        diag = d.detect_prompt_with_diagnostics(
            {
                "screen": screen,
                "cursor_at_end": False,
                "has_trailing_space": True,
                "cursor": {"x": 0, "y": 0},
            }
        )
        # Fallback path returns the cand match.
        assert diag.match is not None
        assert diag.match.prompt_id == "p"
        # The cursor_position partial-match diagnostic must be carried
        # through. dpwd_93 would drop it → empty list.
        cursor_pos = [e for e in diag.regex_matched_but_failed if e.get("reason") == "cursor_position"]
        assert len(cursor_pos) >= 1
