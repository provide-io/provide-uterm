#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Data models for session annotation — marking interesting moments in terminal recordings."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import re


def annotation_match_key(rule_id: str, match: str) -> str:
    """Identity of one detector match: ``sha256(rule_id + "\\x00" + match)``, hex.

    *match* is the FULL matched text (``re.Match.group(0)``, not the 80-char
    description cut), encoded as UTF-8 (``surrogatepass``, so a lone surrogate
    hashes instead of raising). The digest names the rule and the exact text it
    matched -- two different AWS keys get two different keys, the same key seen
    twice gets the same one -- without the raw text (often a secret) being kept
    anywhere: a consumer that dedupes on it holds only digests.

    Reference format for the Go/TS/C# ports: lowercase hex SHA-256 of the UTF-8
    bytes of ``rule_id``, one NUL byte, then the UTF-8 bytes of the match.
    """

    return hashlib.sha256(f"{rule_id}\x00{match}".encode("utf-8", "surrogatepass")).hexdigest()


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
    # ``annotation_match_key(rule_id, match)`` for a detector annotation, "" for
    # any other. NOT serialised (``to_dict`` omits it) and excluded from repr and
    # equality: it is an in-process identity for dedupe, not recorded data.
    match_key: str = field(default="", repr=False, compare=False)

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
