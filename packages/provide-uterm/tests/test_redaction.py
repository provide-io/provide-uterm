#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.uterm.redaction import make_redactor, redact_text


def test_make_redactor_masks_pattern_matches() -> None:
    redactor = make_redactor([r"password=\w+", r"token: [A-Z0-9]+"])

    assert redactor("login password=secret token: ABC123") == "login [REDACTED] [REDACTED]"


def test_make_redactor_without_patterns_returns_identity() -> None:
    assert make_redactor(None)("unchanged") == "unchanged"


def test_redact_text_with_none_returns_original() -> None:
    assert redact_text("plain", None) == "plain"


def test_redact_text_calls_redactor() -> None:
    assert redact_text("secret", lambda text: text.replace("secret", "[REDACTED]")) == "[REDACTED]"
