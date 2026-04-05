#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ANSI rendering — re-exports from provide.terminal.render.buffer.

This module is kept for backwards compatibility. New code should import
directly from ``provide.terminal.render``.
"""

from __future__ import annotations

__all__ = [
    "ANSI16_PALETTE",
    "ANSI_ALT_SCREEN",
    "ANSI_EXIT_ALT",
    "ANSI_HIDE_CURSOR",
    "ANSI_RESET",
    "ANSI_SHOW_CURSOR",
    "BG_CODES",
    "FG_CODES",
    "SGR_FUNCTIONS",
    "AnsiBuffer",
    "ColorMode",
    "clear_screen",
    "move_to",
    "nearest_16",
    "nearest_256",
    "sgr_16",
    "sgr_256",
    "sgr_truecolor",
    "style_to_sgr",
]

# Re-export everything from the render package for backwards compat
from provide.terminal.render.buffer import (  # noqa: F401
    ANSI_ALT_SCREEN,
    ANSI_EXIT_ALT,
    ANSI_HIDE_CURSOR,
    ANSI_RESET,
    ANSI_SHOW_CURSOR,
    BG_CODES,
    FG_CODES,
    AnsiBuffer,
    _attr_codes,
    _color_sgr,
    _hex_to_rgb,
    _is_hex_color,
    clear_screen,
    move_to,
    style_to_sgr,
)
from provide.terminal.render.palette import (
    ANSI16_PALETTE,
    nearest_16,
    nearest_256,
)
from provide.terminal.render.sgr import (
    SGR_FUNCTIONS,
    ColorMode,
    sgr_16,
    sgr_256,
    sgr_truecolor,
)
