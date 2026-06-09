#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Reusable redaction helpers for terminal logs and captures."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def make_redactor(patterns: Sequence[str] | None = None) -> Callable[[str], str]:
    """Build a text redactor from regex patterns."""
    compiled = [re.compile(pattern) for pattern in patterns or ()]
    if not compiled:
        return lambda text: text

    def _redact(text: str) -> str:
        result = text
        for pattern in compiled:
            result = pattern.sub("[REDACTED]", result)
        return result

    return _redact


def redact_text(text: str, redactor: Callable[[str], str] | None) -> str:
    """Apply *redactor* to *text*, preserving identity when no redactor is configured."""
    if redactor is None:
        return text
    return redactor(text)
