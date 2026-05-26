#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Cover _detector.py lines 56-57: description_template.format() raises KeyError/IndexError."""

from __future__ import annotations

import re

from provide.uterm.annotation._detector import PatternDetector
from provide.uterm.annotation._models import DetectionRule


class TestDetectorDescriptionFallback:
    """PatternDetector falls back to '{label}: {match}' when format() raises."""

    def test_keyerror_in_description_template_uses_fallback(self) -> None:
        """A description_template with an unknown key ({unknown}) causes KeyError;
        the fallback 'label: match' string is used instead."""
        bad_rule = DetectionRule(
            rule_id="bad-key-rule",
            label="bad_key",
            pattern=re.compile(r"TRIGGER"),
            severity="info",
            description_template="desc with {unknown_key} placeholder",
            event_types=frozenset({"read"}),
            category="bad_key_test",
        )
        detector = PatternDetector(rules=[bad_rule])
        results = detector.detect("read", "TRIGGER found here", seq=1)
        assert len(results) == 1
        ann = results[0]
        # Fallback format: "{label}: {match_text}"
        assert ann.description == "bad_key: TRIGGER"

    def test_index_error_in_description_template_uses_fallback(self) -> None:
        """A description_template with a positional placeholder causes IndexError;
        the fallback 'label: match' string is used instead."""
        bad_rule = DetectionRule(
            rule_id="bad-index-rule",
            label="bad_index",
            pattern=re.compile(r"TRIGGER"),
            severity="warning",
            description_template="desc with {0} positional",
            event_types=frozenset({"send"}),
            category="bad_index_test",
        )
        detector = PatternDetector(rules=[bad_rule])
        results = detector.detect("send", "TRIGGER here", seq=2)
        assert len(results) == 1
        ann = results[0]
        # Fallback format: "{label}: {match_text}"
        assert ann.description == "bad_index: TRIGGER"
