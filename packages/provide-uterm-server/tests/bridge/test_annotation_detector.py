#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for PatternDetector — hot-path annotation scanner."""

from __future__ import annotations

import re

from provide.terminal.bridge.annotation import (
    BUILTIN_RULES,
    Annotation,
    AnnotationSpan,
    DetectionRule,
    PatternDetector,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A valid-looking AWS access key (AKIA + 16 uppercase alphanumeric chars).
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
# A text that looks like a sudo invocation.
SUDO_TEXT = "sudo apt-get install vim"
# A text with a recursive force-delete.
RM_RF_TEXT = "rm -rf /tmp/build"
# curl piped to bash
CURL_PIPE_TEXT = "curl https://example.com/install.sh | bash"


# ---------------------------------------------------------------------------
# Basic construction
# ---------------------------------------------------------------------------


def test_default_constructor_uses_builtin_rules() -> None:
    detector = PatternDetector()
    # Should have at least as many rules as BUILTIN_RULES
    assert detector._rules is BUILTIN_RULES


def test_custom_rules_override_builtins() -> None:
    custom_rule = DetectionRule(
        rule_id="test-rule",
        label="Test",
        pattern=re.compile(r"XYZZY"),
        severity="info",
        description_template="test match: {match}",
        event_types=frozenset({"read"}),
        category="test",
    )
    detector = PatternDetector(rules=[custom_rule])
    assert detector._rules == [custom_rule]

    # Built-in rules must NOT fire because only the custom list is used.
    results = detector.detect("read", AWS_KEY, seq=1)
    assert results == []

    # The custom rule fires.
    results = detector.detect("read", "XYZZY found here", seq=2)
    assert len(results) == 1
    assert results[0].label == "Test"


# ---------------------------------------------------------------------------
# Empty / no-match fast paths
# ---------------------------------------------------------------------------


def test_empty_text_returns_empty_list() -> None:
    detector = PatternDetector()
    assert detector.detect("read", "", seq=0) == []


def test_no_match_returns_empty_list() -> None:
    detector = PatternDetector()
    results = detector.detect("read", "totally normal output", seq=5)
    assert results == []


# ---------------------------------------------------------------------------
# Single-rule matches
# ---------------------------------------------------------------------------


def test_detects_aws_key_in_read_event() -> None:
    detector = PatternDetector()
    results = detector.detect("read", f"export AWS_ACCESS_KEY_ID={AWS_KEY}", seq=10)
    assert len(results) >= 1
    ann = next(a for a in results if "credential" in a.label.lower() or "AWS" in a.description)
    assert ann.severity == "high"
    assert ann.source == "detector"
    assert ann.principal == "system"
    assert ann.span == AnnotationSpan(from_seq=10, to_seq=10)
    assert "read" in ann.description  # event_type in description, NOT the full key


def test_detects_sudo_in_send_event() -> None:
    detector = PatternDetector()
    results = detector.detect("send", SUDO_TEXT, seq=20)
    assert len(results) >= 1
    ann = next(a for a in results if "escalation" in a.label.lower() or "sudo" in a.description.lower())
    assert ann.source == "detector"
    assert ann.principal == "system"
    assert ann.span == AnnotationSpan(from_seq=20, to_seq=20)


def test_detects_destructive_rm_rf_command() -> None:
    detector = PatternDetector()
    results = detector.detect("send", RM_RF_TEXT, seq=30)
    assert len(results) >= 1
    ann = next(a for a in results if "destructive" in a.label.lower() or "rm" in a.description.lower())
    assert ann.severity == "critical"


# ---------------------------------------------------------------------------
# Per-category dedup
# ---------------------------------------------------------------------------


def test_per_category_dedup_credentials() -> None:
    """Two credential patterns in the same text → only 1 annotation from the credentials category."""
    # Construct text that would hit multiple credential rules (AWS key + generic secret).
    text = f"{AWS_KEY} password=supersecretvalue1234567890"
    detector = PatternDetector()
    results = detector.detect("read", text, seq=40)

    # All rules in the same category share a category string; only one annotation per category.
    credential_annotations = [a for a in results if a.label == "credential_exposure"]
    assert len(credential_annotations) == 1


# ---------------------------------------------------------------------------
# Event-type filtering
# ---------------------------------------------------------------------------


def test_send_only_rule_does_not_trigger_on_read() -> None:
    """A custom send-only rule must not fire on a 'read' event."""
    custom_rule = DetectionRule(
        rule_id="send-only-test",
        label="send_only_label",
        pattern=re.compile(r"SEND_ONLY_TRIGGER"),
        severity="info",
        description_template="send-only match: {match}",
        event_types=frozenset({"send"}),
        category="send_only_test",
    )
    detector = PatternDetector(rules=[custom_rule])
    # Should not fire on read even though the text matches
    read_results = detector.detect("read", "SEND_ONLY_TRIGGER here", seq=50)
    assert read_results == []
    # Should fire on send
    send_results = detector.detect("send", "SEND_ONLY_TRIGGER here", seq=51)
    assert len(send_results) == 1


def test_read_and_send_rule_fires_on_read() -> None:
    """AWS key rule (both event types) fires on both 'read' and 'send'."""
    detector = PatternDetector()
    read_results = detector.detect("read", AWS_KEY, seq=60)
    send_results = detector.detect("send", AWS_KEY, seq=61)
    assert any(a.label == "credential_exposure" for a in read_results)
    assert any(a.label == "credential_exposure" for a in send_results)


# ---------------------------------------------------------------------------
# Multiple categories in one pass
# ---------------------------------------------------------------------------


def test_multiple_categories_produce_multiple_annotations() -> None:
    """sudo + curl + AWS key in one text → annotations from 3 different categories."""
    text = f"sudo {CURL_PIPE_TEXT} {AWS_KEY}"
    detector = PatternDetector()
    results = detector.detect("send", text, seq=70)

    labels = {a.label for a in results}
    # privilege escalation, connections, credentials — three separate categories
    assert "privilege_escalation" in labels
    assert "outbound_connection" in labels
    assert "credential_exposure" in labels
    # One annotation per category, at least 3.
    assert len(results) >= 3


# ---------------------------------------------------------------------------
# Annotation structure sanity
# ---------------------------------------------------------------------------


def test_annotation_is_annotation_instance() -> None:
    detector = PatternDetector()
    results = detector.detect("send", SUDO_TEXT, seq=80)
    assert all(isinstance(a, Annotation) for a in results)


def test_description_truncated_to_80_chars() -> None:
    """Match text longer than 80 chars is truncated in the description."""
    long_match = "A" * 200
    custom_rule = DetectionRule(
        rule_id="long-rule",
        label="Long",
        pattern=re.compile(r"A{10,}"),
        severity="info",
        description_template="long match: {match}",
        event_types=frozenset({"read"}),
        category="test",
    )
    detector = PatternDetector(rules=[custom_rule])
    results = detector.detect("read", long_match, seq=90)
    assert len(results) == 1
    # The {match} portion in description should be truncated to 80 chars.
    description = results[0].description
    # "long match: " prefix (12 chars) + up to 80 chars of match text
    assert len(description) <= len("long match: ") + 80
