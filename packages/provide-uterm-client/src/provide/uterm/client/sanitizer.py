#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Compatibility exports for shared keystroke sanitization."""

from __future__ import annotations

from provide.uterm.sanitizer import (
    prepare_keystrokes,
    sanitize_keystrokes,
    unescape_keys,
)

__all__ = ["prepare_keystrokes", "sanitize_keystrokes", "unescape_keys"]
