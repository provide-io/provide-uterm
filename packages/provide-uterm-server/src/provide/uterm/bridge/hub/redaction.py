#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import bisect
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from provide.uterm.bridge.hub.ext import RedactionRule


class StreamRedactor:
    """High-performance regex-based stream redactor."""

    def __init__(self, rules: Iterable[RedactionRule] | None = None):
        """
        Initialize with a list of RedactionRule objects, combining them into a single regex.
        """
        self._pattern = None
        self._rule_start_indices = []
        self._replacements = []
        self._single_replacement = None

        if rules:
            patterns = []
            current_index = 1
            for rule in rules:
                try:
                    compiled = re.compile(rule.pattern)
                    patterns.append(f"({rule.pattern})")
                    self._rule_start_indices.append(current_index)
                    self._replacements.append(rule.replacement)
                    current_index += 1 + compiled.groups
                except re.error:
                    # Ignore invalid regex patterns
                    continue

            if patterns:
                self._pattern = re.compile("|".join(patterns))
                if len(set(self._replacements)) == 1:
                    self._single_replacement = self._replacements[0]

    def redact(self, data: str) -> str:
        """Apply all redaction rules to the input string in a single pass."""
        if not self._pattern:
            return data

        if self._single_replacement is not None:
            # Optimized path for when all rules use the same replacement string.
            # Capture the str in a local so mypy can narrow it past the
            # ``is not None`` check — the lambda closes over the local,
            # not the optional instance attribute.
            single: str = self._single_replacement
            return self._pattern.sub(lambda _match: single, data)

        return self._pattern.sub(self._replace_match, data)

    def _replace_match(self, match: re.Match[str]) -> str:
        """Find which rule matched by looking at the last matched group index."""
        # ``match.lastindex`` is the 1-based index of the highest-numbered
        # capturing group that matched. It is ``None`` only when no
        # capturing group matched at all — which can't happen here because
        # every rule pattern is wrapped in a top-level group during
        # ``__init__`` (see ``patterns.append(f"({rule.pattern})")``). The
        # ``or 0`` keeps mypy happy without a runtime cost; bisect_right
        # with a 0 sentinel returns 0 and ``self._replacements[-1]`` is
        # still a valid (deterministic) replacement.
        last = match.lastindex or 0
        idx = bisect.bisect_right(self._rule_start_indices, last) - 1
        return self._replacements[idx]
