#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PatternDetector — hot-path scanner that matches terminal event text against DetectionRules."""

from __future__ import annotations

from provide.uterm.annotation._models import Annotation, AnnotationSpan, DetectionRule
from provide.uterm.annotation._rules import BUILTIN_RULES

_DESCRIPTION_TRUNCATE = 80
# Placeholder used in the fallback description so the raw match (a potential
# secret) is never embedded when a description_template fails to format.
_FALLBACK_PLACEHOLDER = "<unavailable>"


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
        return self.scan(event_type, text, seq)[0]

    def scan(self, event_type: str, text: str, seq: int) -> tuple[list[Annotation], int]:
        """Like :meth:`detect`, but also return the end offset of the furthest
        match in *text* (0 when nothing matches).

        :class:`StreamingDetector` uses the offset to carry only the window tail
        *after* the matched region — bridging a second secret that straddles the
        boundary without re-reporting a match that already completed.
        """
        if not text:
            return [], 0

        results: list[Annotation] = []
        seen_categories: set[str] = set()
        max_end = 0

        for rule in self._rules:
            if rule.category in seen_categories:
                continue
            if event_type not in rule.event_types:
                continue
            m = rule.pattern.search(text)
            if m is None:
                continue

            seen_categories.add(rule.category)
            max_end = max(max_end, m.end())
            match_text = m.group(0)[:_DESCRIPTION_TRUNCATE]
            try:
                description = rule.description_template.format(match=match_text, event_type=event_type)
            except (KeyError, IndexError):
                # A malformed template must not leak the raw match (a potential
                # secret) into the description, which flows to telemetry/logs.
                description = f"{rule.label}: {_FALLBACK_PLACEHOLDER}"
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

        return results, max_end
