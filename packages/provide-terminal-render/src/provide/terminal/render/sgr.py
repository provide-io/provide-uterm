#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SGR (Select Graphic Rendition) escape sequence emitters.

Provides functions to emit ANSI escape sequences for truecolor (24-bit),
xterm 256-color, and classic 16-color modes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from provide.terminal.render.palette import nearest_16, nearest_256

if TYPE_CHECKING:
    from collections.abc import Callable

ColorMode = Literal["truecolor", "256", "16"]


def sgr_truecolor(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> str:
    """Emit a truecolor (24-bit) SGR sequence for *fg* over *bg*."""
    return f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]};48;2;{bg[0]};{bg[1]};{bg[2]}m"


def sgr_256(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> str:
    """Emit a 256-color SGR sequence for *fg* over *bg*."""
    fi = nearest_256(*fg)
    bi = nearest_256(*bg)
    return f"\x1b[38;5;{fi};48;5;{bi}m"


def sgr_16(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> str:
    """Emit a 16-color SGR sequence for *fg* over *bg*."""
    fg_code, _ = nearest_16(*fg)
    _, bg_code = nearest_16(*bg)
    return f"\x1b[{fg_code};{bg_code}m"


SGR_FUNCTIONS: dict[str, Callable[[tuple[int, int, int], tuple[int, int, int]], str]] = {
    "truecolor": sgr_truecolor,
    "256": sgr_256,
    "16": sgr_16,
}
