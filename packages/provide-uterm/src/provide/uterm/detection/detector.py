#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Prompt detection with cursor-aware pattern matching.

End-state goals:
- Avoid full-screen regex scans on every frame (most prompts are near the bottom).
- Reduce false positives from stale/header content by prioritizing the prompt region.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any

from provide.uterm.detection.detector_compile import (
    DetectorPatternCompileError,
    compile_patterns,
    swap_patterns,
)
from provide.uterm.detection.models import PromptDetectionDiagnostics, PromptMatch

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Re-exported so ``from provide.uterm.detection.detector import
# DetectorPatternCompileError`` keeps working after the compile/reload
# machinery moved to ``detector_compile``.
__all__ = ["DetectorPatternCompileError", "PromptDetector"]

_DEFAULT_PROMPT_REGION_TAIL_LINES = 12


class PromptDetector:
    """Intelligent prompt detection with cursor-awareness."""

    def __init__(
        self,
        patterns: list[dict[str, Any]],
        *,
        normalizer: Callable[[str], str] | None = None,
        strict: bool = False,  # pragma: no mutate  # trampoline-masked default (see docs/mutmut-survivors-triage.md): mutmut's wrapper passes the original default positionally, so strict=False->True is unkillable.
    ) -> None:
        """Initialize prompt detector.

        Args:
            patterns: List of prompt pattern dictionaries from JSON.
            normalizer: Optional callback to normalize prompt region text
                for fingerprinting.
            strict: When ``True``, any pattern that fails to compile (bad
                regex syntax, missing required keys) raises
                :class:`DetectorPatternCompileError` instead of silently
                being skipped. Use this in production where a malformed
                rule should be loud, not silently degrade coverage.
        """
        self._normalizer = normalizer
        self._patterns = patterns
        self._strict = bool(strict)
        self._compile_failures: list[dict[str, Any]] = []
        self._compiled_all = self._compile_patterns()
        # Optimization only: patterns that *don't* require cursor-at-end.
        # IMPORTANT: do not treat cursor_at_end as authoritative; if the heuristic is wrong
        # and we skip "expect_cursor_at_end=true" patterns entirely, prompt detection can fail.
        self._compiled_no_cursor_end_req = [
            (regex, pattern)
            for (regex, pattern) in self._compiled_all
            if not bool(pattern.get("expect_cursor_at_end", True))
        ]
        # Backward compatibility for legacy debug helpers that still reference `_compiled`.
        self._compiled = self._compiled_all

    @property
    def pattern_count(self) -> int:
        """Return the number of compiled patterns."""
        return len(self._patterns)

    @property
    def compile_failures(self) -> tuple[dict[str, Any], ...]:
        """Return immutable view of pattern compile failures.

        Each entry is ``{"id": str, "regex": str | None, "error": str}``.
        In ``strict=True`` mode this tuple is always empty (the
        constructor would have raised before returning).
        """
        return tuple(self._compile_failures)

    def _compile_patterns(self) -> list[tuple[re.Pattern[str], dict[str, Any]]]:
        """Compile regex patterns for efficient matching.

        Thin wrapper over :func:`detector_compile.compile_patterns` (the
        body lives there to keep this module under its LOC budget).
        """
        return compile_patterns(self)

    @staticmethod
    def prompt_region(
        snapshot: dict[str, Any],
        *,
        tail_lines: int = _DEFAULT_PROMPT_REGION_TAIL_LINES,
    ) -> tuple[str, bool]:
        """Extract a bottom-of-content region likely to contain prompts.

        Returns (region_text, cursor_in_region).

        We anchor to the last non-empty line of the screen, not the bottom row,
        because many UIs leave blank rows below the last content.
        """
        # ``or ""`` collapses a missing/None screen to "", so a ``.get`` default
        # would be behaviourally inert — omit it to leave no dead literal.
        screen = snapshot.get("screen") or ""
        if not screen:
            return ("", False)

        # Preserve empty trailing lines if present.
        lines = screen.split("\n")
        # Find the last line with any non-whitespace content.
        last_idx = 0
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].rstrip():
                last_idx = i
                break
        start_idx = max(0, last_idx - max(1, int(tail_lines)) + 1)

        cursor = snapshot.get("cursor") or {}
        # ``or 0`` collapses None/falsy to 0, so a ``.get`` default would be inert.
        try:
            cursor_y = int(cursor.get("y") or 0)
        except Exception:
            cursor_y = 0
        cursor_in_region = start_idx <= cursor_y <= last_idx

        region_text = "\n".join(lines[start_idx : last_idx + 1])
        return (region_text, cursor_in_region)

    @staticmethod
    def normalize_prompt_region(region_text: str, normalizer: Callable[[str], str] | None = None) -> str:
        """Normalize volatile prompt-region fields for stable fingerprinting."""
        if not region_text:
            return ""
        if normalizer is not None:
            return normalizer(region_text)
        return region_text

    def prompt_fingerprint(
        self,
        snapshot: dict[str, Any],
        *,
        tail_lines: int = _DEFAULT_PROMPT_REGION_TAIL_LINES,
    ) -> str:
        """Compute a stable fingerprint for prompt-detection caching."""
        region, _cursor_above = PromptDetector.prompt_region(snapshot, tail_lines=tail_lines)
        norm = PromptDetector.normalize_prompt_region(region, self._normalizer)
        # ``str.encode`` defaults to UTF-8; spelling it out would only add an
        # encoding literal whose case-fold ("utf-8" <-> "UTF-8") is normalized by
        # the codec registry (a behaviourally-inert mutant). Relying on the
        # default leaves no such literal to mutate. ``errors="replace"`` is load
        # bearing — an invalid handler name raises at encode time, so any
        # fingerprint test exercises it.
        h = hashlib.blake2s(norm.encode(errors="replace")).hexdigest()
        cursor_at_end = int(bool(snapshot.get("cursor_at_end", True)))
        # No ``.get`` default: a missing/None/False value all collapse to 0 via
        # ``int(bool(...))``, so any default literal here would be inert.
        trailing = int(bool(snapshot.get("has_trailing_space")))

        cursor = snapshot.get("cursor") or {}
        # ``.get`` without a default returns None when absent; the ``or 0`` then
        # collapses None/"" /0 to 0.  Supplying a ``.get`` default here would be
        # behaviourally inert (the ``or 0`` overrides it), so it is omitted to
        # leave no dead literal for mutation.
        try:
            cx = int(cursor.get("x") or 0)
            cy = int(cursor.get("y") or 0)
        except (ValueError, TypeError):
            cx = 0
            cy = 0

        # cursor_at_end and trailing are included in the fingerprint so that a
        # screen whose cursor position oscillates (e.g. mid-burst telnet frames)
        # is re-evaluated rather than served stale from cache.  The trade-off is
        # that cache hits are missed on cursor-only changes between otherwise
        # identical screens.  A future optimisation could fingerprint content
        # only and use the flags purely as detection inputs, not cache keys.
        return f"{h}:{cursor_at_end}:{trailing}:{cx}:{cy}"

    @staticmethod
    def _resolve_negative_regex(pattern: dict[str, Any]) -> str | None:
        """Extract a negative match regex string from a pattern dict.

        Supports two formats:
        - ``negative_regex``: a plain regex string (from to_prompt_patterns())
        - ``negative_match``: a RegexRule-style dict with ``pattern`` key
        """
        if "negative_regex" in pattern:
            return str(pattern["negative_regex"])
        nm = pattern.get("negative_match")
        if nm and isinstance(nm, dict):
            sub_pattern = str(nm.get("pattern", ""))
            match_mode = str(nm.get("match_mode", "regex"))
            if match_mode == "contains":
                return re.escape(sub_pattern)
            if match_mode == "exact":
                return rf"^{re.escape(sub_pattern)}$"
            return sub_pattern
        return None

    def _detect_in_text(
        self,
        *,
        text: str,
        full_screen: str,
        cursor_at_end: bool,
        compiled: list[tuple[re.Pattern[str], dict[str, Any]]],
        regex_matched_but_failed: list[dict[str, Any]],
        cursor_miss_candidates: list[PromptMatch] | None = None,
    ) -> PromptMatch | None:
        for regex, pattern in compiled:
            match = regex.search(text)
            if not match:
                continue

            negative = self._resolve_negative_regex(pattern)
            # NOTE: negative_match is intentionally case-insensitive (re.IGNORECASE) so
            # that exclusion rules like "stardock" block "STARDOCK", "Stardock", etc.
            # Positive patterns (compiled above) are case-sensitive by design — prompt
            # authors rely on exact case to distinguish prompts.  This asymmetry is
            # deliberate: exclusions are broad guards; positive matches are precise.
            if negative and re.search(negative, full_screen, re.MULTILINE | re.IGNORECASE):
                regex_matched_but_failed.append(
                    {
                        "pattern_id": pattern["id"],
                        "reason": "negative_match",
                        "negative_pattern": negative,
                    }
                )
                continue

            expect_cursor_at_end = pattern.get("expect_cursor_at_end", True)
            if expect_cursor_at_end and not cursor_at_end:
                regex_matched_but_failed.append(
                    {
                        "pattern_id": pattern["id"],
                        "reason": "cursor_position",
                        "expected_cursor_at_end": expect_cursor_at_end,
                        "actual_cursor_at_end": cursor_at_end,
                    }
                )
                # Cursor-at-end is a heuristic; on some screens (or some telnet bursts)
                # pyte cursor bookkeeping can be off. Preserve a fallback candidate so
                # callers can still make progress instead of timing out forever.
                if cursor_miss_candidates is not None:
                    cursor_miss_candidates.append(
                        PromptMatch(
                            prompt_id=pattern["id"],
                            pattern=pattern,
                            input_type=pattern.get("input_type", "multi_key"),
                            eol_pattern=pattern.get("eol_pattern", r"[\r\n]+"),
                            kv_extract=pattern.get("kv_extract"),
                        )
                    )
                continue

            return PromptMatch(
                prompt_id=pattern["id"],
                pattern=pattern,
                input_type=pattern.get("input_type", "multi_key"),
                eol_pattern=pattern.get("eol_pattern", r"[\r\n]+"),
                kv_extract=pattern.get("kv_extract"),
            )
        return None

    def detect_prompt(self, snapshot: dict[str, Any]) -> PromptMatch | None:
        """Detect if snapshot contains a prompt waiting for input.

        This method keeps the legacy API and returns only the match.
        Use `detect_prompt_with_diagnostics()` to also get partial-match reasons.

        Args:
            snapshot: Screen snapshot with timing and cursor metadata

        Returns:
            PromptMatch if a prompt pattern matches, None otherwise
        """
        return self.detect_prompt_with_diagnostics(snapshot).match

    def _run_two_pass_detection(
        self,
        snapshot: dict[str, Any],
        screen: str,
        cursor_at_end: bool,
        compiled_fast: list[tuple[re.Pattern[str], dict[str, Any]]],
        compiled_all: list[tuple[re.Pattern[str], dict[str, Any]]],
        regex_matched_but_failed: list[dict[str, Any]],
    ) -> tuple[PromptMatch | None, list[PromptMatch]]:
        """Run two-pass prompt detection: prompt region first, then full screen.

        Returns (match, cursor_miss_candidates).  match is None if nothing fired.
        """
        cursor_miss_candidates: list[PromptMatch] = []
        region_text, cursor_in_region = self.prompt_region(snapshot)
        if region_text:
            # The region pass uses ``compiled_fast``, which never reaches the
            # cursor-miss branch (it is either the no-cursor-required subset, or
            # the full set under ``cursor_at_end=True`` where the miss branch is
            # gated off).  So no candidate is ever appended here — we deliberately
            # omit ``cursor_miss_candidates`` (defaults to None) rather than thread
            # a list that can never be written, leaving no dead kwarg to mutate.
            match = self._detect_in_text(
                text=region_text,
                full_screen=screen,
                cursor_at_end=cursor_at_end,
                compiled=compiled_fast,
                regex_matched_but_failed=regex_matched_but_failed,
            )
            if match:
                logger.info(
                    "prompt_detection_matched_region prompt_id=%s input_type=%s",
                    match.prompt_id,
                    match.input_type,
                )
                return match, cursor_miss_candidates

        if not cursor_in_region:
            match = self._detect_in_text(
                text=screen,
                full_screen=screen,
                cursor_at_end=cursor_at_end,
                compiled=compiled_all,
                regex_matched_but_failed=regex_matched_but_failed,
                cursor_miss_candidates=cursor_miss_candidates,
            )
            if match:
                logger.info(
                    "prompt_detection_matched_full prompt_id=%s input_type=%s",
                    match.prompt_id,
                    match.input_type,
                )
                return match, cursor_miss_candidates

        return None, cursor_miss_candidates

    def detect_prompt_with_diagnostics(self, snapshot: dict[str, Any]) -> PromptDetectionDiagnostics:
        """Detect prompt and include partial-match diagnostics.

        Args:
            snapshot: Screen snapshot with timing and cursor metadata

        Returns:
            PromptDetectionDiagnostics containing both match and partial-match failures
        """
        # ``or ""`` collapses a missing/None screen to "", so a ``.get`` default
        # would be behaviourally inert — omit it to leave no dead literal.
        screen = snapshot.get("screen") or ""
        # Most callers supply cursor metadata; tests/legacy callers may not.
        # Defaulting to True keeps prompt detection working for minimal snapshots.
        cursor_at_end = snapshot.get("cursor_at_end", True)
        # Normalize to a real bool at the boundary: ``has_trailing_space`` is only
        # ever consumed as a truthiness gate below, so collapsing here removes any
        # behaviourally-inert default literal (missing → False) and makes the
        # logged value deterministic.
        has_trailing_space = bool(snapshot.get("has_trailing_space"))

        # Track patterns that partially matched (for diagnostics)
        regex_matched_but_failed: list[dict[str, Any]] = []

        logger.debug("prompt_detection_start pattern_count=%d", len(self._compiled_all))
        logger.debug(
            "prompt_detection_cursor cursor_at_end=%s has_trailing_space=%s", cursor_at_end, has_trailing_space
        )
        if screen:
            region_text, cursor_in_region = self.prompt_region(snapshot)
            logger.debug(
                "prompt_detection_region region_len=%d cursor_in_region=%s region_tail=%s",
                len(region_text),
                cursor_in_region,
                region_text[-200:],
            )

        # Candidate pattern set: always allow all patterns; cursor constraints are checked per-pattern.
        compiled_all = self._compiled_all
        compiled_fast = self._compiled_no_cursor_end_req if not cursor_at_end else self._compiled_all

        match, cursor_miss_candidates = self._run_two_pass_detection(
            snapshot,
            screen,
            bool(cursor_at_end),
            compiled_fast,
            compiled_all,
            regex_matched_but_failed,
        )
        if match:
            return PromptDetectionDiagnostics(match=match, regex_matched_but_failed=regex_matched_but_failed)

        # Fallback: if we matched prompt regexes but the cursor heuristic disagreed, prefer progress.
        # Gate this on "trailing space" which strongly correlates with an active input field.
        # ``cursor_miss_candidates`` is only ever populated when the per-pattern
        # cursor check saw ``not cursor_at_end`` (same ``bool(cursor_at_end)`` we
        # passed into the two-pass run), so a redundant ``not cursor_at_end`` guard
        # here would be dead — the non-empty candidate list already implies it.
        # ``has_trailing_space`` is already a bool (normalized above).
        if cursor_miss_candidates and has_trailing_space:
            cand = cursor_miss_candidates[0]
            logger.warning(
                "prompt_detection_cursor_heuristic_fallback fallback_prompt_id=%s",
                cand.prompt_id,
            )
            return PromptDetectionDiagnostics(match=cand, regex_matched_but_failed=regex_matched_but_failed)

        # NO PATTERNS MATCHED - Emit diagnostic
        if regex_matched_but_failed:
            logger.error(
                "prompt_detection_failed partial_matches=%d failures=%s",
                len(regex_matched_but_failed),
                [{"pattern_id": p["pattern_id"], "reason": p["reason"]} for p in regex_matched_but_failed],
            )
        else:
            # No patterns matched at all - this might be okay (e.g., data display)
            logger.debug(
                "prompt_detection_no_match total_patterns=%d screen_preview=%s",
                len(self._compiled_all),
                screen[-150:],
            )

        # No explicit ``match=`` kwarg: the model defaults ``match`` to None, so
        # passing it would be a behaviourally-inert literal with nothing to mutate.
        return PromptDetectionDiagnostics(regex_matched_but_failed=regex_matched_but_failed)

    def add_pattern(self, pattern: dict[str, Any]) -> None:
        """Add a new pattern to the detector.

        Args:
            pattern: Pattern dictionary to add
        """
        self._swap_patterns([*self._patterns, pattern])

    def reload_patterns(self, patterns: list[dict[str, Any]]) -> None:
        """Replace all patterns with new set.

        Args:
            patterns: New list of pattern dictionaries
        """
        self._swap_patterns(list(patterns))

    def _swap_patterns(self, candidate: list[dict[str, Any]]) -> None:
        """Atomically replace ``self._patterns`` with ``candidate``.

        Thin wrapper over :func:`detector_compile.swap_patterns` (the body
        lives there to keep this module under its LOC budget).
        """
        swap_patterns(self, candidate)
