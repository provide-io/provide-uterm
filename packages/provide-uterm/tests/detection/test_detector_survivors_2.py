#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Second wave of surgical kills for ``detection/detector.py`` survivors.

The first wave (``test_detector_survivors.py``) took detector.py from 133
mutmut survivors down to 43.  This module drives the remaining tail to zero
by pairing source restructures (dead-default removal / EQUIV elimination)
with surgical ``caplog`` / return-value assertions.

The log-message mutants in this file are all of the ``"event_name ..." ->
"XXevent_name ...XX"`` family.  A plain substring check (``"event_name" in
msg``) does *not* kill them because the original event name is still a
substring of the XX-wrapped variant.  Every assertion here therefore checks
``msg.startswith("<event_name>")`` so the leading ``XX`` of the mutant is
observable.
"""

from __future__ import annotations

import logging

import pytest

from provide.uterm.detection.detector import PromptDetector

DETECTOR_LOGGER = "provide.uterm.detection.detector"


def _messages(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    """Return rendered messages from ``caplog`` whose text contains ``needle``."""
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


class TestCompileLogEventNames:
    """Kill the XX-wrapped event-name mutants on ``_compile_patterns`` logs.

    Targets ``_compile_patterns`` mutants 7/21/58/76/107/115.
    """

    def test_compile_start_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=DETECTOR_LOGGER):
            PromptDetector([{"id": "p", "regex": "x"}])
        msgs = _messages(caplog, "pattern_compile_start")
        assert msgs
        # XX-wrapping the format string would push the event name to "XX...".
        assert any(m.startswith("pattern_compile_start count=") for m in msgs)
        assert not any(m.startswith("XX") for m in msgs)

    def test_compile_ok_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger=DETECTOR_LOGGER):
            PromptDetector([{"id": "p", "regex": "x"}])
        msgs = _messages(caplog, "pattern_compile_ok")
        assert msgs
        assert msgs[0].startswith("pattern_compile_ok pattern_id=")

    def test_compile_failed_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger=DETECTOR_LOGGER):
            PromptDetector([{"id": "p", "regex": "[bad"}])
        msgs = _messages(caplog, "pattern_compile_failed")
        assert msgs
        assert msgs[0].startswith("pattern_compile_failed pattern_id=")

    def test_compile_invalid_structure_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger=DETECTOR_LOGGER):
            PromptDetector([{"id": "p"}])  # missing regex -> KeyError branch
        msgs = _messages(caplog, "pattern_compile_invalid_structure")
        assert msgs
        assert msgs[0].startswith("pattern_compile_invalid_structure pattern_id=")

    def test_compile_complete_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=DETECTOR_LOGGER):
            PromptDetector([{"id": "p", "regex": "x"}])
        msgs = _messages(caplog, "pattern_compile_complete")
        assert msgs
        assert msgs[0].startswith("pattern_compile_complete succeeded=")

    def test_compile_failures_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger=DETECTOR_LOGGER):
            PromptDetector([{"id": "p", "regex": "[bad"}])
        msgs = _messages(caplog, "pattern_compile_failures")
        assert msgs
        assert msgs[0].startswith("pattern_compile_failures count=")


class TestTwoPassLogEventNames:
    """Kill XX-wrapped event names on the two-pass match-success logs.

    Targets ``_run_two_pass_detection`` mutants 23/45.
    """

    def test_matched_region_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        # Cursor inside the region so only the region pass runs and fires.
        detector = PromptDetector([{"id": "rp", "regex": r"login:", "expect_cursor_at_end": False}])
        snapshot = {
            "screen": "login:",
            "cursor_at_end": False,
            "cursor": {"x": 0, "y": 0},
        }
        with caplog.at_level(logging.INFO, logger=DETECTOR_LOGGER):
            match = detector.detect_prompt(snapshot)
        assert match is not None
        msgs = _messages(caplog, "prompt_detection_matched_region")
        assert msgs
        assert msgs[0].startswith("prompt_detection_matched_region prompt_id=")

    def test_matched_full_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        # PASSWORD sits far above the bottom region (the prompt region only sees
        # the last ~12 non-empty lines), and the cursor is not in that region, so
        # the region pass misses and the full-screen pass fires.
        filler = "\n".join(f"line {i}" for i in range(30))
        detector = PromptDetector([{"id": "fp", "regex": r"PASSWORD", "expect_cursor_at_end": False}])
        snapshot = {
            "screen": "PASSWORD\n" + filler,
            "cursor_at_end": False,
            "cursor": {"x": 0, "y": 0},
        }
        with caplog.at_level(logging.INFO, logger=DETECTOR_LOGGER):
            match = detector.detect_prompt(snapshot)
        assert match is not None
        msgs = _messages(caplog, "prompt_detection_matched_full")
        assert msgs
        assert msgs[0].startswith("prompt_detection_matched_full prompt_id=")


class TestDiagnosticsLogEventNames:
    """Kill XX-wrapped event names on ``detect_prompt_with_diagnostics`` logs.

    Targets mutants 28/36/48/84/112.
    """

    def test_detection_start_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        detector = PromptDetector([{"id": "p", "regex": "x"}])
        with caplog.at_level(logging.DEBUG, logger=DETECTOR_LOGGER):
            detector.detect_prompt_with_diagnostics({"screen": "no match here"})
        msgs = _messages(caplog, "prompt_detection_start")
        assert msgs
        assert msgs[0].startswith("prompt_detection_start pattern_count=")

    def test_detection_cursor_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        detector = PromptDetector([{"id": "p", "regex": "x"}])
        with caplog.at_level(logging.DEBUG, logger=DETECTOR_LOGGER):
            detector.detect_prompt_with_diagnostics({"screen": "nope"})
        msgs = _messages(caplog, "prompt_detection_cursor")
        assert msgs
        assert msgs[0].startswith("prompt_detection_cursor cursor_at_end=")

    def test_detection_region_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        detector = PromptDetector([{"id": "p", "regex": "x"}])
        with caplog.at_level(logging.DEBUG, logger=DETECTOR_LOGGER):
            detector.detect_prompt_with_diagnostics({"screen": "some content"})
        msgs = _messages(caplog, "prompt_detection_region")
        assert msgs
        assert msgs[0].startswith("prompt_detection_region region_len=")

    def test_cursor_heuristic_fallback_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        # Build a snapshot that takes the cursor-heuristic fallback branch:
        # a pattern that requires cursor-at-end, cursor NOT at end, but a
        # trailing-space hint present -> the fallback fires and warns.
        detector = PromptDetector([{"id": "fb", "regex": r"Name\?", "expect_cursor_at_end": True}])
        snapshot = {
            "screen": "Name?",
            "cursor_at_end": False,
            "has_trailing_space": True,
            "cursor": {"x": 0, "y": 50},
        }
        with caplog.at_level(logging.WARNING, logger=DETECTOR_LOGGER):
            diag = detector.detect_prompt_with_diagnostics(snapshot)
        assert diag.match is not None
        msgs = _messages(caplog, "prompt_detection_cursor_heuristic_fallback")
        assert msgs
        assert msgs[0].startswith("prompt_detection_cursor_heuristic_fallback fallback_prompt_id=")

    def test_no_match_event_name_is_exact(self, caplog: pytest.LogCaptureFixture) -> None:
        detector = PromptDetector([{"id": "p", "regex": r"WILLNOTMATCH"}])
        with caplog.at_level(logging.DEBUG, logger=DETECTOR_LOGGER):
            detector.detect_prompt_with_diagnostics({"screen": "totally different"})
        msgs = _messages(caplog, "prompt_detection_no_match")
        assert msgs
        assert msgs[0].startswith("prompt_detection_no_match total_patterns=")


class TestFingerprintEncoding:
    """Lock in the fingerprint encode path.

    ``prompt_fingerprint`` hashes ``norm.encode(errors="replace")``. The
    encoding defaults to UTF-8 (so there is no codec literal to case-fold),
    while ``errors="replace"`` is load-bearing: an invalid handler name would
    raise at encode time. Exercising the path with content guarantees the
    encode call actually runs and returns a stable hex digest.
    """

    def test_fingerprint_is_stable_hex_for_unicode_region(self) -> None:
        detector = PromptDetector([{"id": "p", "regex": "x"}])
        snapshot = {"screen": "café ☕ prompt>", "cursor": {"x": 3, "y": 0}}
        fp1 = detector.prompt_fingerprint(snapshot)
        fp2 = detector.prompt_fingerprint(snapshot)
        # Deterministic and well-formed: "<hexdigest>:<ce>:<trail>:<cx>:<cy>".
        assert fp1 == fp2
        digest = fp1.split(":", 1)[0]
        assert len(digest) == 64  # blake2s default hexdigest length
        assert all(c in "0123456789abcdef" for c in digest)


class TestTrailingSpaceNormalization:
    """Lock in that ``has_trailing_space`` is normalized to a real bool.

    The debug-cursor log renders ``has_trailing_space=%s``; normalizing the
    raw snapshot value to ``bool(...)`` means a missing key logs ``False``
    (never ``None``), which kills the default-literal mutants on that field.
    """

    def test_missing_trailing_space_logs_false_not_none(self, caplog: pytest.LogCaptureFixture) -> None:
        detector = PromptDetector([{"id": "p", "regex": "x"}])
        with caplog.at_level(logging.DEBUG, logger=DETECTOR_LOGGER):
            detector.detect_prompt_with_diagnostics({"screen": "data"})
        msgs = _messages(caplog, "prompt_detection_cursor")
        assert msgs
        assert "has_trailing_space=False" in msgs[0]
        assert "has_trailing_space=None" not in msgs[0]
