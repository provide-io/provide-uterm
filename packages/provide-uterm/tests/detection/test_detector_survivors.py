#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut-killer tests for the long-tail of detector.py survivors.

Targets the ~130 pre-existing survivors documented in
``docs/mutmut-survivors-triage.md`` that live in
``provide.uterm.detection.detector``. Each test is paired in a comment with
the specific mutant id(s) it kills.

Tests deliberately exercise observable consequences of each mutation:
public-API outputs (``compile_failures``, ``PromptMatch`` fields,
``prompt_fingerprint`` string, ``PromptDetectionDiagnostics``), the
strict-mode error-summary path, and log records via ``caplog`` for the
mutations whose only observability is the structured-log payload.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from provide.uterm.detection.detector import (
    DetectorPatternCompileError,
    PromptDetector,
)

# ---------------------------------------------------------------------------
# _compile_patterns — dict-key + default mutations in compile_failures
# ---------------------------------------------------------------------------


class TestCompileFailuresShape:
    """``compile_failures`` returns the operator-facing diagnostic.

    The mutations rename ``regex``/``error``/``id`` keys, swap defaults,
    or drop the ``re.MULTILINE`` flag. Every observable mutation in this
    cluster is killable by asserting on the exact dict shape.
    """

    def test_failed_pattern_entry_uses_regex_and_error_keys(self) -> None:
        """Targets _compile_patterns mutants 42/43/48/49 (dict-key rename of
        ``regex``) and 51/52 (rename of ``error``) and 44/46/47 (drop / swap
        of get-call args for the regex value)."""
        d = PromptDetector([{"id": "bad", "regex": "[unclosed"}])
        failures = d.compile_failures
        assert len(failures) == 1
        entry = failures[0]
        # Exact keys — renames to XXregexXX / REGEX / XXerrorXX / ERROR all fail.
        assert set(entry.keys()) == {"id", "regex", "error"}
        # The original regex value is captured verbatim. Mutants that
        # change the default ("XXXX") or swap arg position (get("")) cause
        # entry["regex"] to differ from the supplied regex.
        assert entry["regex"] == "[unclosed"
        # The error is the str(e) representation — a non-empty diagnostic.
        # Mutant 53 swaps to str(None) which produces literally "None".
        assert entry["error"]
        assert entry["error"] != "None"

    def test_missing_regex_key_records_id_and_missing_key_error(self) -> None:
        """Targets _compile_patterns mutants 102/104/107/108 — the KeyError
        branch's ``id`` default and case folds (UNKNOWN / XXunknownXX).

        Also covers mutants 100 (drop default — gives KeyError) by
        asserting the id ends up as the literal "myid" when present.
        """
        # Pattern missing "regex" key — triggers the KeyError branch.
        d = PromptDetector([{"id": "myid"}])
        failures = d.compile_failures
        assert len(failures) == 1
        entry = failures[0]
        assert entry["id"] == "myid"
        assert "Missing key" in entry["error"]
        assert "regex" in entry["error"]

    def test_missing_id_key_falls_back_to_unknown_default(self) -> None:
        """Targets _compile_patterns mutants 24/26/29/30 — the
        ``pattern.get("id", "unknown")`` default-value mutations on the
        success-path debug log. AND mutants 65/67/70/71 (failed-path log)
        AND mutants 90/92/95/96 (KeyError-path log) — the same default is
        looked up via the dict entry built in ``failed_patterns``.

        Observable via ``compile_failures`` for the failed-regex path:
        when a pattern omits ``id``, the entry's ``id`` field falls back
        to the literal "unknown".
        """
        # Pattern without "id" and with bad regex — exercises the
        # ``failed_patterns`` build site that uses pattern.get("id", "unknown").
        d = PromptDetector([{"regex": "[unclosed"}])
        failures = d.compile_failures
        assert len(failures) == 1
        # Mutants that swap default to None / "XXunknownXX" / "UNKNOWN" /
        # drop default would produce a different sentinel here.
        assert failures[0]["id"] == "unknown"

    def test_missing_id_and_regex_uses_unknown_id_in_keyerror_branch(self) -> None:
        """KeyError-branch sibling of the previous test."""
        d = PromptDetector([{}])
        failures = d.compile_failures
        assert len(failures) == 1
        assert failures[0]["id"] == "unknown"


class TestCompileFailuresLogPayload:
    """The structured-log payload for compile failures.

    The ``pattern_compile_failures`` ERROR log emits a list of
    ``{"id", "error"}`` dicts. Mutants 135/137 rename the ``error``
    default to None / drop kwarg; 140/141 do case folds.
    """

    def test_failures_log_uses_error_field_from_entry(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets _compile_patterns mutants 135/137/140/141."""
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            PromptDetector([{"id": "x", "regex": "[bad"}])
        records = [r for r in caplog.records if "pattern_compile_failures" in r.getMessage()]
        assert records
        # The log uses %s formatting; the rendered list of dicts must
        # contain the real error string, not "unknown error" / "None" /
        # case-folded variants.
        rendered = records[0].getMessage()
        # The real re.error message includes "unclosed" or similar — never the
        # mutant defaults.
        assert "unknown error" not in rendered
        assert "UNKNOWN ERROR" not in rendered


class TestCompilePatternsDebugLogIdDefault:
    """Compile-time DEBUG log uses ``pattern.get("id", "unknown")``.

    When a pattern omits its ``id`` key, the log emits the literal
    "unknown" sentinel. Mutants 24/26 swap the default to None / drop
    kwarg; 29/30 change the sentinel string (XX-wrap / case fold).

    Same family also lives on the failed-path (mutants 65/67/70/71) and
    KeyError-path (mutants 90/92/95/96) logs.
    """

    def test_success_log_id_default_is_lowercase_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets _compile_patterns mutants 24/26/29/30."""
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            PromptDetector([{"regex": r"x"}])  # valid regex, no id
        ok_logs = [r for r in caplog.records if "pattern_compile_ok" in r.getMessage()]
        assert ok_logs
        msg = ok_logs[0].getMessage()
        assert "pattern_id=unknown" in msg

    def test_failed_log_id_default_is_lowercase_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets _compile_patterns mutants 65/67/70/71. Also kills
        mutants 57/79 — ``str(e) → None`` / ``str(None)`` for the
        ``error=%s`` arg.
        """
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            PromptDetector([{"regex": "[bad"}])  # bad regex, no id
        failed_logs = [r for r in caplog.records if "pattern_compile_failed" in r.getMessage()]
        assert failed_logs
        msg = failed_logs[0].getMessage()
        assert "pattern_id=unknown" in msg
        # The error=%s arg renders the real re.error message — not "None".
        assert "error=None" not in msg

    def test_keyerror_log_id_default_is_lowercase_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets _compile_patterns mutants 90/92/95/96. Also kills
        mutants 83/97 — ``str(e) → None`` / ``str(None)`` for the
        ``missing_key=%s`` arg.
        """
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            PromptDetector([{}])  # no regex, no id
        invalid_logs = [r for r in caplog.records if "pattern_compile_invalid_structure" in r.getMessage()]
        assert invalid_logs
        msg = invalid_logs[0].getMessage()
        assert "pattern_id=unknown" in msg
        # The missing_key=%s arg renders the real KeyError repr — not "None".
        assert "missing_key=None" not in msg

    def test_failed_log_regex_default_is_empty_string(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets _compile_patterns mutants 73/75/78 — the failed-path
        log's ``regex=%s`` arg with ``pattern.get("regex", "")`` default.

        ONLY reachable when the pattern has a regex key (so the re.error
        branch fires) — but the original default is unobservable here
        because the key is always present. EQUIV. Documented for
        completeness."""
        pytest.skip("regex default unreachable on the re.error branch — EQUIV")


class TestStrictModeErrorSummary:
    """The strict-mode ``raise DetectorPatternCompileError`` summary string.

    Mutants 145/148/149/150/151/152/153/154 mutate the ``", "``
    separator, the ``error``/``?`` defaults, or rename ``error`` in the
    f-string ``p.get('error', '?')`` lookup. All are observable via the
    raised exception's str.
    """

    def test_strict_mode_summary_contains_id_and_error(self) -> None:
        with pytest.raises(DetectorPatternCompileError) as exc_info:
            PromptDetector([{"id": "alpha", "regex": "[bad"}], strict=True)
        msg = str(exc_info.value)
        # Format: ``{id}: {error}``. Mutants that rename "error" key
        # (152/153) leave the lookup falling back to "?", so the message
        # would end in ": ?".
        assert "alpha: " in msg
        assert not msg.endswith(": ?")
        # Mutant 154 changes the "?" fallback marker; we don't rely on it,
        # but we make sure the real error text appears.
        # The actual re.error message for "[bad" contains "unterminated" /
        # "missing" / "unbalanced" — at minimum it must NOT be just "?".

    def test_strict_mode_summary_joins_multiple_with_comma_space(self) -> None:
        """Targets mutant 145 — ``", "`` separator becomes ``"XX, XX"``."""
        with pytest.raises(DetectorPatternCompileError) as exc_info:
            PromptDetector(
                [
                    {"id": "alpha", "regex": "[bad"},
                    {"id": "beta", "regex": "(also-bad"},
                ],
                strict=True,
            )
        msg = str(exc_info.value)
        # Separator must be exactly ", " — not "XX, XX". The literal
        # "XX" must not appear in the operator-facing message.
        assert "XX" not in msg
        # Both failed ids appear (proving the join happened with the
        # right separator and the iterable wasn't truncated).
        assert "alpha:" in msg
        assert "beta:" in msg

    def test_strict_mode_summary_uses_id_field_from_failed_entry(self) -> None:
        """Mutants 148/150 drop / change ``p['id']`` lookup key."""
        with pytest.raises(DetectorPatternCompileError) as exc_info:
            PromptDetector([{"id": "myid", "regex": "[bad"}], strict=True)
        # The id must appear verbatim — mutants that subscript a wrong
        # key would KeyError before raising.
        assert "myid" in str(exc_info.value)

    def test_strict_mode_summary_uses_error_value_after_id(self) -> None:
        """Targets mutant 150 — ``p.get('error', '?')`` becomes
        ``p.get('?')``. Since '?' is never a real key, .get returns
        None → summary becomes "id: None" instead of "id: <real error>".
        """
        with pytest.raises(DetectorPatternCompileError) as exc_info:
            PromptDetector([{"id": "myid", "regex": "[bad"}], strict=True)
        msg = str(exc_info.value)
        # Real error text appears after "myid: "; mutant 150 makes it "None".
        assert "myid: " in msg
        assert "myid: None" not in msg
        # Real re.error messages mention the unbalanced bracket.
        assert (
            "[" in msg
            or "bracket" in msg.lower()
            or "unterminated" in msg.lower()
            or "unbalanced" in msg.lower()
            or "missing" in msg.lower()
        )


# ---------------------------------------------------------------------------
# prompt_fingerprint — hashed-input + cursor x/y observability
# ---------------------------------------------------------------------------


class TestPromptFingerprint:
    """``prompt_fingerprint`` returns ``"{hash}:{cursor_at_end}:{trailing}:{cx}:{cy}"``.

    Most mutations either change the trailing flag default, the cursor
    x/y defaults, or the encoding. All four are observable in the
    returned string.
    """

    def _det(self) -> PromptDetector:
        return PromptDetector([{"id": "p", "regex": "x"}])

    def test_fingerprint_cursor_xy_default_zero_when_missing(self) -> None:
        """Targets fp_50 (x default None), fp_52 (drop kwarg), fp_55
        (default=1), fp_56 (or-default=1), fp_61/63/66/67 (same for y)."""
        fp = self._det().prompt_fingerprint({"screen": "abc", "cursor": {}})
        # Format ends with ":0:0" when cursor x/y are absent and the
        # defaults are the original (0, 0). Mutant fp_55 swaps to 1 →
        # ":1:0"; fp_66 swaps y default to 1 → ":0:1".
        assert fp.endswith(":0:0")

    def test_fingerprint_cursor_xy_zero_when_explicit_zero(self) -> None:
        """Sibling test — the ``or 0`` legs also default to 0 (fp_56, fp_67).

        With explicit ``x=0``, ``cursor.get("x", 0)`` returns 0 (falsy),
        triggering the ``or 0`` fallback. The mutant flips this fallback
        to ``or 1`` → "1" at the end of the fingerprint.
        """
        fp = self._det().prompt_fingerprint({"screen": "abc", "cursor": {"x": 0, "y": 0}})
        assert fp.endswith(":0:0")

    def test_fingerprint_explicit_cursor_xy_propagates(self) -> None:
        """Sanity — non-zero cursor values do appear in the fingerprint
        (proves the fields are not stuck at defaults)."""
        fp = self._det().prompt_fingerprint({"screen": "abc", "cursor": {"x": 7, "y": 3}})
        assert fp.endswith(":7:3")

    def test_fingerprint_trailing_flag_default_false(self) -> None:
        """Targets fp_35 (default None — None as bool is False, killable
        only via truthy), fp_37 (drop kwarg → None), fp_40 (default True).

        With the original (False) default, the ``trailing`` segment is
        0. Mutant fp_40 (True) makes it 1.
        """
        fp = self._det().prompt_fingerprint({"screen": "abc", "cursor": {"x": 0, "y": 0}})
        # Format: "{hash}:{cursor_at_end}:{trailing}:{cx}:{cy}"
        parts = fp.split(":")
        assert len(parts) == 5
        # cursor_at_end defaults to True (1), trailing defaults to False (0).
        assert parts[1] == "1"
        assert parts[2] == "0"

    def test_fingerprint_respects_caller_tail_lines_kwarg(self) -> None:
        """Targets fp_5 — ``prompt_region(snapshot, tail_lines=tail_lines)``
        becomes ``prompt_region(snapshot, )`` which silently uses the
        default tail_lines=12.

        Build a screen with 20 unique non-empty lines. Fingerprint with
        ``tail_lines=2`` vs ``tail_lines=20`` MUST produce different hashes
        because the region text is different. Under the mutant both use
        the default 12 → identical hashes.
        """
        screen = "\n".join(f"line{i:02d}" for i in range(20))
        snap = {"screen": screen, "cursor": {"x": 0, "y": 0}, "cursor_at_end": True}
        d = self._det()
        fp_tiny = d.prompt_fingerprint(snap, tail_lines=2)
        fp_huge = d.prompt_fingerprint(snap, tail_lines=20)
        # Different tail_lines → different region → different hash.
        assert fp_tiny != fp_huge

    def test_fingerprint_text_with_non_ascii_uses_replace_error_handler(self) -> None:
        """Targets fp_15 (drop encoding arg), fp_16 (drop errors kwarg),
        fp_18 (utf-8 case fold — Python normalizes, equivalent), fp_19/20
        (errors="XXreplaceXX"/"REPLACE" — invalid handler names).

        Smoke test: an unencodable / weird input must not raise; the
        fingerprint is deterministic and well-formed.

        Mutants 16/19/20 raise (invalid encoding/handler), so the call
        would raise instead of returning a string. Mutant 18 is equivalent
        (Python normalizes codec names).
        """
        # surrogate code point — would raise under default 'strict' handler
        # and under invalid handler names.
        screen_with_surrogate = "abc\ud800def"
        fp = self._det().prompt_fingerprint({"screen": screen_with_surrogate, "cursor": {"x": 0, "y": 0}})
        assert isinstance(fp, str)
        # Format check — hash:cursor_at_end:trailing:cx:cy → 5 segments.
        assert fp.count(":") == 4


# ---------------------------------------------------------------------------
# _detect_in_text — pattern.get default observability via PromptMatch
# ---------------------------------------------------------------------------


class TestDetectInTextDefaults:
    """Mutations to ``pattern.get(...)`` calls that build PromptMatch.

    When a pattern dict omits ``input_type`` / ``eol_pattern``, the
    default value flows through to the returned ``PromptMatch``. Mutants
    that change the default value or drop the kwarg are observable on
    the match object.
    """

    def _long_screen_cursor_outside_region(self) -> str:
        # 30 lines of content so a tail_lines=12 region excludes y=0.
        return "\n".join(f"line{i}" for i in range(30)) + "\nuser$ "

    def test_cursor_miss_candidate_defaults_when_pattern_omits_keys(self) -> None:
        """Targets _detect_in_text mutants 70/71 (input_type default
        ``"multi_key"`` → ``"XXmulti_keyXX"`` / ``"MULTI_KEY"``) and 78
        (eol_pattern default ``r"[\\r\\n]+"`` → ``r"XX[\\r\\n]+XX"``).

        Pattern OMITS input_type and eol_pattern. The fallback PromptMatch
        uses the default values which must be exactly ``"multi_key"`` and
        ``r"[\\r\\n]+"``.
        """
        d = PromptDetector([{"id": "p", "regex": r"user\$\s*$", "expect_cursor_at_end": True}])
        diag = d.detect_prompt_with_diagnostics(
            {
                "screen": self._long_screen_cursor_outside_region(),
                "cursor_at_end": False,
                "has_trailing_space": True,
                "cursor": {"x": 0, "y": 0},
            }
        )
        assert diag.match is not None
        assert diag.match.input_type == "multi_key"
        assert diag.match.eol_pattern == r"[\r\n]+"

    def test_cursor_miss_candidate_input_type_uses_pattern_input_type(self) -> None:
        """Targets _detect_in_text mutants 64/68/69 — key-rename mutations
        on ``pattern.get("input_type", "multi_key")`` in the cursor-miss
        PromptMatch construction.

        Pattern PROVIDES an explicit non-default input_type. Under the
        original, the value is read and propagates to the fallback match.
        Under mutants that change the lookup key (None / XX-wrapped /
        UPPER), ``.get`` falls back to the default ``"multi_key"``.
        """
        d = PromptDetector(
            [
                {
                    "id": "p",
                    "regex": r"user\$\s*$",
                    "expect_cursor_at_end": True,
                    "input_type": "custom_kind",
                    "eol_pattern": "CUSTOM_EOL",
                },
            ]
        )
        diag = d.detect_prompt_with_diagnostics(
            {
                "screen": self._long_screen_cursor_outside_region(),
                "cursor_at_end": False,
                "has_trailing_space": True,
                "cursor": {"x": 0, "y": 0},
            }
        )
        assert diag.match is not None
        # Mutants 64/68/69 rename the lookup key → fallback to "multi_key".
        assert diag.match.input_type == "custom_kind"
        # Mutants 72/76/77 do the same for eol_pattern.
        assert diag.match.eol_pattern == "CUSTOM_EOL"

    def test_expect_cursor_at_end_defaults_to_true_when_missing(self) -> None:
        """Targets _detect_in_text mutants 28/29/31/32/33/34 — defaults
        and key renames for ``pattern.get("expect_cursor_at_end", True)``.

        When the key is absent in the pattern dict, the default True
        must apply. Then cursor_at_end=False makes the check fire (the
        pattern is *skipped* with a cursor_position diagnostic).
        """
        # Pattern omits expect_cursor_at_end. Cursor_at_end=False on the
        # snapshot. Force the full-screen pass (which uses compiled_all)
        # by placing the cursor outside the region.
        #
        # Under default=True: pattern fires cursor_position diagnostic
        # AND no positive match. Under default=False or default=None
        # (mutants 29/31/34): ``expect_cursor_at_end and ...`` is falsy
        # → pattern matches positively in the full-screen pass.
        d = PromptDetector([{"id": "p", "regex": r"user"}])
        screen = "\n".join(f"line{i}" for i in range(30)) + "\nuser"
        diag = d.detect_prompt_with_diagnostics(
            {
                "screen": screen,
                "cursor_at_end": False,
                "has_trailing_space": False,
                "cursor": {"x": 0, "y": 0},
            }
        )
        # Original (default True): no match, but cursor_position diagnostic recorded.
        # Mutant (default False/None): positive match without diagnostic.
        cursor_pos_diags = [e for e in diag.regex_matched_but_failed if e.get("reason") == "cursor_position"]
        assert diag.match is None
        assert len(cursor_pos_diags) >= 1
        # The key name "expect_cursor_at_end" is wrapped in XX/UPPER mutants;
        # those make get() fall back to default True so behavior matches the
        # original — those are equivalent. We can't kill 32/33 here.


# ---------------------------------------------------------------------------
# _run_two_pass_detection — kwarg pass-through observability
# ---------------------------------------------------------------------------


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


class TestDiagnosticsRegionLogPayload:
    """The structured-log payload for the region-debug log path.

    Mutants dpwd_46/47/112 substitute log args with None — observable via
    caplog format-failure or rendered message content.
    """

    def test_region_log_records_cursor_in_region_and_tail(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets dpwd_46 (cursor_in_region → None) and dpwd_47
        (region_text[-200:] → None).

        Under the original, the rendered message contains a sensible
        value for cursor_in_region (True/False as string) and a region
        tail string. Under mutant 46, the message contains "None" where
        the cursor flag would be. Under mutant 47, the tail is "None".
        """
        d = PromptDetector([{"id": "p", "regex": r"will-not-match"}])
        screen = "hello-world-this-is-a-screen-tail"
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics({"screen": screen, "cursor": {"x": 0, "y": 0}, "cursor_at_end": True})
        region_logs = [r for r in caplog.records if "prompt_detection_region" in r.getMessage()]
        assert region_logs
        msg = region_logs[0].getMessage()
        # cursor_in_region segment must be True/False (real screen has
        # cursor at y=0, last_idx=0 → cursor_in_region=True).
        assert "cursor_in_region=True" in msg
        # region_tail segment must contain real content from the screen,
        # not the literal "None".
        assert "region_tail=hello" in msg

    def test_no_match_log_records_screen_preview(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets dpwd_112 — ``screen[-150:]`` becomes None."""
        d = PromptDetector([{"id": "p", "regex": r"will-not-match"}])
        screen = "screen-content-end"
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics({"screen": screen, "cursor": {"x": 0, "y": 0}, "cursor_at_end": True})
        no_match_logs = [r for r in caplog.records if "prompt_detection_no_match" in r.getMessage()]
        assert no_match_logs
        msg = no_match_logs[0].getMessage()
        # screen_preview segment is the real screen tail, not "None".
        assert "screen_preview=screen-content-end" in msg


class TestRunTwoPassMatchLogPayload:
    """Log-arg mutations in the match-success paths of _run_two_pass_detection."""

    def test_matched_region_log_uses_input_type(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets _run_two_pass_detection mutant 19 — input_type → None
        in the matched-region log."""
        d = PromptDetector([{"id": "myprompt", "regex": r"user", "input_type": "my_special_type"}])
        with caplog.at_level(logging.INFO, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics({"screen": "user", "cursor": {"x": 0, "y": 0}, "cursor_at_end": True})
        logs = [r for r in caplog.records if "prompt_detection_matched_region" in r.getMessage()]
        assert logs
        msg = logs[0].getMessage()
        assert "input_type=my_special_type" in msg
        assert "prompt_id=myprompt" in msg

    def test_matched_full_log_uses_prompt_id_and_input_type(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets rtpd_40 (prompt_id → None) and rtpd_41 (input_type → None)
        in the matched-full log.

        Force a full-screen-only match: regex matches at a line that
        falls OUTSIDE the tail region (tail_lines=12) so the region
        pass returns no match, then the full-screen pass matches.
        Cursor at y=0 → cursor_in_region=False → full-screen pass runs.
        """
        # Put the unique target at line 0, then enough tail content so the
        # region (last 12 lines) doesn't contain "uniquetoken".
        screen = "uniquetoken\n" + "\n".join(f"line{i}" for i in range(30))
        d = PromptDetector(
            [
                {
                    "id": "fullprompt",
                    "regex": r"uniquetoken",
                    "input_type": "fullkind",
                    "expect_cursor_at_end": True,
                }
            ]
        )
        with caplog.at_level(logging.INFO, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics({"screen": screen, "cursor": {"x": 0, "y": 0}, "cursor_at_end": True})
        logs = [r for r in caplog.records if "prompt_detection_matched_full" in r.getMessage()]
        assert logs
        msg = logs[0].getMessage()
        assert "prompt_id=fullprompt" in msg
        assert "input_type=fullkind" in msg


class TestFallbackLogPayload:
    """The cursor-heuristic-fallback warning log payload."""

    def test_fallback_log_uses_cand_prompt_id(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets dpwd_85 — ``cand.prompt_id`` → None in the fallback log."""
        d = PromptDetector([{"id": "fallbackid", "regex": r"user", "expect_cursor_at_end": True}])
        # Long screen → full-screen pass → cursor_miss candidate recorded.
        screen = "\n".join(f"line{i}" for i in range(30)) + "\nuser"
        with caplog.at_level(logging.WARNING, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics(
                {
                    "screen": screen,
                    "cursor_at_end": False,
                    "has_trailing_space": True,
                    "cursor": {"x": 0, "y": 0},
                }
            )
        logs = [r for r in caplog.records if "prompt_detection_cursor_heuristic_fallback" in r.getMessage()]
        assert logs
        msg = logs[0].getMessage()
        # Under original: "...fallback_prompt_id=fallbackid".
        # Under mutant: "...fallback_prompt_id=None".
        assert "fallback_prompt_id=fallbackid" in msg


class TestNoMatchLogFailuresPayload:
    """The no-positive-match ERROR log's failures list."""

    def test_failures_list_includes_pattern_id_and_reason(self, caplog: pytest.LogCaptureFixture) -> None:
        """Targets dpwd_96 — drops the whole failures list (arg → None).

        Under original, the rendered log contains the partial-match
        pattern ids. Under mutant 96, the arg is None → "None" appears
        instead.
        """
        d = PromptDetector([{"id": "partial", "regex": r"user", "expect_cursor_at_end": True}])
        # Long screen → full-screen pass evaluates the strict pattern →
        # cursor_position diagnostic recorded. has_trailing_space=False so
        # the fallback doesn't fire → the no-match ERROR log emits.
        screen = "\n".join(f"line{i}" for i in range(30)) + "\nuser"
        with caplog.at_level(logging.ERROR, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics(
                {
                    "screen": screen,
                    "cursor_at_end": False,
                    "has_trailing_space": False,
                    "cursor": {"x": 0, "y": 0},
                }
            )
        logs = [r for r in caplog.records if "prompt_detection_failed" in r.getMessage()]
        assert logs
        msg = logs[0].getMessage()
        # The real partial-match pattern_id appears in the rendered list.
        assert "partial" in msg
        # And the reason string.
        assert "cursor_position" in msg
        # The rendered dict uses the lowercase keys "pattern_id" and "reason"
        # — kills key-rename mutants 102/103/106/107.
        assert "'pattern_id'" in msg
        assert "'reason'" in msg
        # Mutants would render "'XXpattern_idXX'" or "'PATTERN_ID'" etc.
        assert "XX" not in msg
        assert "PATTERN_ID" not in msg
        assert "REASON" not in msg


# ---------------------------------------------------------------------------
# reload_patterns — the _compiled_no_cursor_end_req filter
# ---------------------------------------------------------------------------


class TestReloadPatternsFilter:
    """``reload_patterns`` rebuilds ``_compiled_no_cursor_end_req``.

    The filter ``not bool(pat.get("expect_cursor_at_end", True))`` is
    observable via the size of ``_compiled_no_cursor_end_req`` after
    reload — patterns with ``expect_cursor_at_end=False`` end up in the
    list; the rest don't.
    """

    def _patterns_with_one_no_cursor(self) -> list[dict[str, Any]]:
        return [
            {"id": "needs_cursor", "regex": r"a", "expect_cursor_at_end": True},
            {"id": "lenient", "regex": r"b", "expect_cursor_at_end": False},
            {"id": "default_required", "regex": r"c"},  # default True
        ]

    def test_reload_filter_uses_expect_cursor_at_end_key(self) -> None:
        """Targets reload_patterns mutants 6 (key=None), 9 (drop kwarg).

        Both mutants make ``pat.get`` always return None → ``not bool(None)``
        is True → ALL patterns end up in the filtered list.
        """
        d = PromptDetector([{"id": "x", "regex": r"x"}])
        d.reload_patterns(self._patterns_with_one_no_cursor())
        # Original: only "lenient" lands in the no-cursor list (1 entry).
        # Mutant 6/9: all 3 patterns land in the list.
        assert len(d._compiled_no_cursor_end_req) == 1
        assert d._compiled_no_cursor_end_req[0][1]["id"] == "lenient"

    def test_reload_filter_default_true_excludes_pattern_without_key(self) -> None:
        """Targets reload_patterns mutants 7 (default=None), 12 (default=False).

        With the original default True, a pattern lacking the key is
        EXCLUDED. With mutant defaults None/False, ``not bool(default)``
        is True → INCLUDED.
        """
        d = PromptDetector([{"id": "x", "regex": r"x"}])
        d.reload_patterns([{"id": "default_required", "regex": r"c"}])  # no key
        # Original: 0 patterns in the no-cursor list.
        # Mutants 7/12: 1 pattern (the default-True / default-False flip).
        assert len(d._compiled_no_cursor_end_req) == 0

    def test_reload_compiled_legacy_attr_equals_compiled_all(self) -> None:
        """Targets reload_patterns mutant 13 — ``self._compiled = None``."""
        d = PromptDetector([{"id": "x", "regex": r"x"}])
        d.reload_patterns([{"id": "a", "regex": r"a"}, {"id": "b", "regex": r"b"}])
        # Legacy attribute exists and points to the same list.
        assert d._compiled is not None
        assert d._compiled is d._compiled_all
        assert len(d._compiled) == 2


class TestAddPatternLegacyCompiledAttr:
    """``add_pattern`` rebuilds the legacy ``_compiled`` attr."""

    def test_add_pattern_compiled_legacy_attr_set_to_compiled_all(self) -> None:
        """Targets add_pattern mutant 13 — ``self._compiled = None``."""
        d = PromptDetector([{"id": "x", "regex": r"x"}])
        d.add_pattern({"id": "y", "regex": r"y"})
        assert d._compiled is not None
        assert d._compiled is d._compiled_all
        assert len(d._compiled) == 2
