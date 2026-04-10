#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Data models for session annotation — marking interesting moments in terminal recordings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import re


@dataclass(slots=True)
class AnnotationSpan:
    """A contiguous range of recording event sequence numbers."""

    from_seq: int
    to_seq: int


@dataclass(slots=True)
class Annotation:
    """A single annotation marking an interesting moment (or range) in a session recording."""

    label: str
    description: str
    severity: str
    source: str
    principal: str
    span: AnnotationSpan | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain :class:`dict`, with ``span`` as a nested dict if present."""
        result: dict[str, Any] = {
            "label": self.label,
            "description": self.description,
            "severity": self.severity,
            "source": self.source,
            "principal": self.principal,
            "span": None,
        }
        if self.span is not None:
            result["span"] = {"from_seq": self.span.from_seq, "to_seq": self.span.to_seq}
        return result


@dataclass(slots=True)
class DetectionRule:
    """A compiled regex rule used to detect annotation-worthy events in terminal output."""

    rule_id: str
    label: str
    pattern: re.Pattern[str]
    severity: str
    description_template: str
    event_types: frozenset[str]
    category: str
