#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Cover _detector.py lines 56-57: description_template.format() raises KeyError/IndexError.

The fallback path must remain privacy-safe: it must NEVER embed the raw matched
text (a potential secret) into the annotation description, since descriptions
flow to telemetry/logs.
"""

from __future__ import annotations

import re

from provide.uterm.annotation._detector import PatternDetector
from provide.uterm.annotation._models import DetectionRule

# The non-leaking placeholder used in place of the raw match in the fallback.
_FALLBACK_PLACEHOLDER = "<unavailable>"


class TestDetectorDescriptionFallback:
    """PatternDetector falls back to a non-leaking description when format() raises."""

    def test_keyerror_in_description_template_uses_safe_fallback(self) -> None:
        """A description_template with an unknown key ({unknown}) causes KeyError;
        the fallback uses the rule label without the raw match text."""
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
        # Non-leaking fallback: label + placeholder, never the raw match.
        assert ann.description == f"bad_key: {_FALLBACK_PLACEHOLDER}"
        assert "TRIGGER" not in ann.description

    def test_index_error_in_description_template_uses_safe_fallback(self) -> None:
        """A description_template with a positional placeholder causes IndexError;
        the fallback uses the rule label without the raw match text."""
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
        # Non-leaking fallback: label + placeholder, never the raw match.
        assert ann.description == f"bad_index: {_FALLBACK_PLACEHOLDER}"
        assert "TRIGGER" not in ann.description

    def test_fallback_never_leaks_secret_like_match(self) -> None:
        """A misconfigured (malformed) template on a secret-matching rule must not
        leak the secret into the description via the fallback path."""
        secret = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret  (test fixture, not a real key)
        leaky_rule = DetectionRule(
            rule_id="bad-cred-rule",
            label="credential_exposure",
            pattern=re.compile(r"AKIA[0-9A-Z]{12}"),
            severity="high",
            # Malformed template forces the except-fallback at runtime.
            description_template="AWS key {does_not_exist}",
            event_types=frozenset({"read"}),
            category="credentials",
        )
        detector = PatternDetector(rules=[leaky_rule])
        results = detector.detect("read", f"export KEY={secret}", seq=3)
        assert len(results) == 1
        description = results[0].description
        # The secret substring must be absent from the description.
        assert secret not in description
        assert description == f"credential_exposure: {_FALLBACK_PLACEHOLDER}"
