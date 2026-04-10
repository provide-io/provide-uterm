#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for session annotation data models."""

from __future__ import annotations

import re

from provide.terminal.bridge.annotation import Annotation, AnnotationSpan, DetectionRule


class TestAnnotationDefaults:
    def test_span_defaults_to_none(self) -> None:
        a = Annotation(
            label="cred-exposure",
            description="Password visible in output",
            severity="high",
            source="detector",
            principal="user1",
        )
        assert a.span is None

    def test_required_fields_stored(self) -> None:
        a = Annotation(
            label="priv-esc",
            description="sudo used",
            severity="critical",
            source="rule-engine",
            principal="admin",
        )
        assert a.label == "priv-esc"
        assert a.description == "sudo used"
        assert a.severity == "critical"
        assert a.source == "rule-engine"
        assert a.principal == "admin"


class TestAnnotationWithSpan:
    def test_span_stored(self) -> None:
        span = AnnotationSpan(from_seq=10, to_seq=20)
        a = Annotation(
            label="data-exfil",
            description="Large data transfer",
            severity="medium",
            source="heuristic",
            principal="user2",
            span=span,
        )
        assert a.span is not None
        assert a.span.from_seq == 10
        assert a.span.to_seq == 20

    def test_annotation_span_fields(self) -> None:
        span = AnnotationSpan(from_seq=0, to_seq=999)
        assert span.from_seq == 0
        assert span.to_seq == 999

    def test_slots_prevent_arbitrary_attributes_on_span(self) -> None:
        span = AnnotationSpan(from_seq=1, to_seq=5)
        try:
            span.extra = "nope"  # type: ignore[attr-defined]
            assert False, "Should have raised AttributeError"  # pragma: no cover
        except AttributeError:
            pass


class TestAnnotationToDict:
    def test_to_dict_without_span(self) -> None:
        a = Annotation(
            label="cred-exposure",
            description="Password in output",
            severity="high",
            source="detector",
            principal="svc-account",
        )
        result = a.to_dict()
        assert result == {
            "label": "cred-exposure",
            "description": "Password in output",
            "severity": "high",
            "source": "detector",
            "principal": "svc-account",
            "span": None,
        }

    def test_to_dict_with_span(self) -> None:
        span = AnnotationSpan(from_seq=5, to_seq=15)
        a = Annotation(
            label="priv-esc",
            description="sudo invoked",
            severity="critical",
            source="rule-engine",
            principal="deploy-bot",
            span=span,
        )
        result = a.to_dict()
        assert result["span"] == {"from_seq": 5, "to_seq": 15}

    def test_to_dict_span_is_nested_dict_not_dataclass(self) -> None:
        span = AnnotationSpan(from_seq=1, to_seq=3)
        a = Annotation(
            label="x",
            description="y",
            severity="low",
            source="s",
            principal="p",
            span=span,
        )
        result = a.to_dict()
        assert isinstance(result["span"], dict)
        assert not isinstance(result["span"], AnnotationSpan)

    def test_to_dict_returns_plain_dict(self) -> None:
        a = Annotation(
            label="x",
            description="y",
            severity="low",
            source="s",
            principal="p",
        )
        result = a.to_dict()
        assert isinstance(result, dict)


class TestDetectionRule:
    def test_field_access(self) -> None:
        pattern = re.compile(r"password\s*=\s*\S+", re.IGNORECASE)
        rule = DetectionRule(
            rule_id="CRED-001",
            label="credential-exposure",
            pattern=pattern,
            severity="high",
            description_template="Credential exposed: {match}",
            event_types=frozenset({"output", "screen"}),
            category="credential",
        )
        assert rule.rule_id == "CRED-001"
        assert rule.label == "credential-exposure"
        assert rule.pattern is pattern
        assert rule.severity == "high"
        assert rule.description_template == "Credential exposed: {match}"
        assert rule.event_types == frozenset({"output", "screen"})
        assert rule.category == "credential"

    def test_pattern_matches(self) -> None:
        pattern = re.compile(r"sudo\s+\w+")
        rule = DetectionRule(
            rule_id="PRIV-001",
            label="priv-esc",
            pattern=pattern,
            severity="critical",
            description_template="Privilege escalation: {match}",
            event_types=frozenset({"output"}),
            category="privilege",
        )
        assert rule.pattern.search("sudo rm -rf /") is not None
        assert rule.pattern.search("ls -la") is None

    def test_slots_prevent_arbitrary_attributes(self) -> None:
        pattern = re.compile(r"test")
        rule = DetectionRule(
            rule_id="R1",
            label="test",
            pattern=pattern,
            severity="low",
            description_template="test",
            event_types=frozenset({"output"}),
            category="test",
        )
        try:
            rule.extra = "nope"  # type: ignore[attr-defined]
            assert False, "Should have raised AttributeError"  # pragma: no cover
        except AttributeError:
            pass

    def test_event_types_is_frozenset(self) -> None:
        pattern = re.compile(r"x")
        rule = DetectionRule(
            rule_id="R2",
            label="x",
            pattern=pattern,
            severity="low",
            description_template="x",
            event_types=frozenset({"output", "keystroke"}),
            category="x",
        )
        assert isinstance(rule.event_types, frozenset)
