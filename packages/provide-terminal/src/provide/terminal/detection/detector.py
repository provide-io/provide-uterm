#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
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

from provide.terminal.detection.models import PromptDetectionDiagnostics, PromptMatch

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT_REGION_TAIL_LINES = 12


class PromptDetector:
    """Intelligent prompt detection with cursor-awareness."""

    def __init__(
        self,
        patterns: list[dict[str, Any]],
        *,
        normalizer: Callable[[str], str] | None = None,
    ) -> None:
        """Initialize prompt detector.

        Args:
            patterns: List of prompt pattern dictionaries from JSON
            normalizer: Optional callback to normalize prompt region text for fingerprinting
        """
        self._normalizer = normalizer
        self._patterns = patterns
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

    def _compile_patterns(self) -> list[tuple[re.Pattern[str], dict[str, Any]]]:
        """Compile regex patterns for efficient matching.

        Returns:
            List of (compiled_regex, pattern_dict) tuples
        """
        compiled = []
        failed_patterns = []

        logger.info("pattern_compile_start count=%d", len(self._patterns))

        for pattern in self._patterns:
            try:
                regex = re.compile(pattern["regex"], re.MULTILINE)
                compiled.append((regex, pattern))
                logger.debug("pattern_compile_ok pattern_id=%s", pattern.get("id", "unknown"))
            except re.error as e:
                # Pattern compilation failed - emit diagnostic
                failed_patterns.append(
                    {
                        "id": pattern.get("id", "unknown"),
                        "regex": pattern.get("regex", ""),
                        "error": str(e),
                    }
                )
                logger.exception(
                    "pattern_compile_failed pattern_id=%s regex=%s error=%s",
                    pattern.get("id", "unknown"),
                    pattern.get("regex", ""),
                    str(e),
                )
                continue
            except KeyError as e:
                # Pattern missing required 'regex' key
                logger.exception(
                    "pattern_compile_invalid_structure pattern_id=%s missing_key=%s",
                    pattern.get("id", "unknown"),
                    str(e),
                )
                failed_patterns.append(
                    {
                        "id": pattern.get("id", "unknown"),
                        "error": f"Missing key: {e}",
                    }
                )
                continue

        logger.info("pattern_compile_complete succeeded=%d failed=%d", len(compiled), len(failed_patterns))

        if failed_patterns:
            logger.error(
                "pattern_compile_failures count=%d failed=%s",
                len(failed_patterns),
                [{"id": p["id"], "error": p.get("error", "unknown error")} for p in failed_patterns],
            )

        return compiled

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
        screen = snapshot.get("screen", "") or ""
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
        try:
            cursor_y = int(cursor.get("y", 0) or 0)
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
        h = hashlib.blake2s(norm.encode("utf-8", errors="replace")).hexdigest()
        cursor_at_end = int(bool(snapshot.get("cursor_at_end", True)))
        trailing = int(bool(snapshot.get("has_trailing_space", False)))
        # cursor_at_end and trailing are included in the fingerprint so that a
        # screen whose cursor position oscillates (e.g. mid-burst telnet frames)
        # is re-evaluated rather than served stale from cache.  The trade-off is
        # that cache hits are missed on cursor-only changes between otherwise
        # identical screens.  A future optimisation could fingerprint content
        # only and use the flags purely as detection inputs, not cache keys.
        return f"{h}:{cursor_at_end}:{trailing}"

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
            match = self._detect_in_text(
                text=region_text,
                full_screen=screen,
                cursor_at_end=cursor_at_end,
                compiled=compiled_fast,
                regex_matched_but_failed=regex_matched_but_failed,
                cursor_miss_candidates=cursor_miss_candidates,
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
        screen = snapshot.get("screen", "") or ""
        # Most callers supply cursor metadata; tests/legacy callers may not.
        # Defaulting to True keeps prompt detection working for minimal snapshots.
        cursor_at_end = snapshot.get("cursor_at_end", True)
        has_trailing_space = snapshot.get("has_trailing_space", False)

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
        if cursor_miss_candidates and not bool(cursor_at_end) and bool(has_trailing_space):
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

        return PromptDetectionDiagnostics(match=None, regex_matched_but_failed=regex_matched_but_failed)

    def auto_detect_input_type(self, screen: str) -> str:
        """Heuristically detect input type from prompt text.

        Args:
            screen: Screen text to analyze

        Returns:
            "any_key", "single_key", or "multi_key"
        """
        screen_lower = screen.lower()

        # "Press any key" type prompts
        if any(
            phrase in screen_lower
            for phrase in [
                "press any key",
                "press a key",
                "hit any key",
                "strike any key",
                "<more>",
                "[more]",
                "-- more --",
            ]
        ):
            return "any_key"

        # Single key choice prompts (Y/N, menu selections)
        if any(
            phrase in screen_lower
            for phrase in [
                "(y/n)",
                "(yes/no)",
                "continue?",
                "quit?",
                "abort?",
                "retry?",
                "[y/n]",
                "(q)uit",
                "(a)bort",
            ]
        ):
            return "single_key"

        # Multi-key field input prompts
        if any(
            phrase in screen_lower
            for phrase in [
                "enter",
                "type",
                "input",
                "name:",
                "password:",
                "username:",
                "choose:",
                "select:",
                "command:",
                "search:",
            ]
        ):
            return "multi_key"

        # Default to multi_key (safest, waits for EOL)
        return "multi_key"

    def add_pattern(self, pattern: dict[str, Any]) -> None:
        """Add a new pattern to the detector.

        Args:
            pattern: Pattern dictionary to add
        """
        self._patterns.append(pattern)
        # Recompile patterns
        self._compiled_all = self._compile_patterns()
        self._compiled_no_cursor_end_req = [
            (regex, pat) for (regex, pat) in self._compiled_all if not bool(pat.get("expect_cursor_at_end", True))
        ]
        self._compiled = self._compiled_all

    def reload_patterns(self, patterns: list[dict[str, Any]]) -> None:
        """Replace all patterns with new set.

        Args:
            patterns: New list of pattern dictionaries
        """
        self._patterns = patterns
        self._compiled_all = self._compile_patterns()
        self._compiled_no_cursor_end_req = [
            (regex, pat) for (regex, pat) in self._compiled_all if not bool(pat.get("expect_cursor_at_end", True))
        ]
        self._compiled = self._compiled_all
