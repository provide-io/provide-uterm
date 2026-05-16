#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Text-level color downgrade functions.

These are the downgrade counterparts to ``upgrade_to_256`` /
``upgrade_to_truecolor`` in :mod:`provide.uterm.ansi`. Both operate on
``str`` input / output; the unified ``str | bytes`` dispatcher lives in
:mod:`.mode`.
"""

from __future__ import annotations

from provide.uterm.colors.sgr import SGR_RE, rewrite_params


def downgrade_to_256(text: str) -> str:
    """Downgrade truecolor SGR sequences in text to xterm-256 cube codes.

    Replaces ``\\x1b[38;2;R;G;Bm`` and ``\\x1b[48;2;R;G;Bm`` runs within
    SGR parameter lists with their nearest xterm-256 palette index
    equivalents (``38;5;N`` / ``48;5;N``). Non-truecolor SGR and other
    escape sequences pass through unchanged. Idempotent on content that
    contains no truecolor.

    Args:
        text: ANSI text.

    Returns:
        Text with truecolor SGR codes replaced by their xterm-256 equivalents.
    """
    return SGR_RE.sub(lambda m: rewrite_params(m.group(1), "256"), text)


def downgrade_to_16(text: str) -> str:
    """Downgrade truecolor SGR sequences in text to base 16-color codes.

    Uses Euclidean-nearest matching over the canonical BBS 16-color
    palette. Non-truecolor SGR passes through unchanged.

    Args:
        text: ANSI text.

    Returns:
        Text with truecolor SGR codes replaced by 16-color equivalents.
    """
    return SGR_RE.sub(lambda m: rewrite_params(m.group(1), "16"), text)


__all__ = ["downgrade_to_16", "downgrade_to_256"]
