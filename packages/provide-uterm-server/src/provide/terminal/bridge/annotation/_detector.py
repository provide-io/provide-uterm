#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PatternDetector — hot-path scanner that matches terminal event text against DetectionRules."""

from __future__ import annotations

from provide.terminal.bridge.annotation._models import Annotation, AnnotationSpan, DetectionRule
from provide.terminal.bridge.annotation._rules import BUILTIN_RULES

_DESCRIPTION_TRUNCATE = 80


class PatternDetector:
    """Scan terminal event text against a set of :class:`DetectionRule` objects.

    Designed for the hot path: returns an empty list immediately when *text* is
    empty and performs no allocations beyond the returned list when no rules
    match.
    """

    def __init__(self, rules: list[DetectionRule] | None = None) -> None:
        """Initialise with *rules*.

        If *rules* is ``None`` the built-in rule set is used.
        """
        self._rules: list[DetectionRule] = BUILTIN_RULES if rules is None else rules

    def detect(self, event_type: str, text: str, seq: int) -> list[Annotation]:
        """Scan *text* against all rules that apply to *event_type*.

        Returns a (possibly empty) list of :class:`Annotation` objects.  At
        most one annotation is returned per *category* — the first rule whose
        pattern matches wins and later rules in that category are skipped.
        """
        if not text:
            return []

        results: list[Annotation] = []
        seen_categories: set[str] = set()

        for rule in self._rules:
            if rule.category in seen_categories:
                continue
            if event_type not in rule.event_types:
                continue
            m = rule.pattern.search(text)
            if m is None:
                continue

            seen_categories.add(rule.category)
            match_text = m.group(0)[:_DESCRIPTION_TRUNCATE]
            try:
                description = rule.description_template.format(match=match_text, event_type=event_type)
            except (KeyError, IndexError):
                description = f"{rule.label}: {match_text}"
            results.append(
                Annotation(
                    label=rule.label,
                    description=description,
                    severity=rule.severity,
                    source="detector",
                    principal="system",
                    span=AnnotationSpan(from_seq=seq, to_seq=seq),
                )
            )

        return results
