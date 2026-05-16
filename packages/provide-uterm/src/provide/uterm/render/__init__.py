#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""provide-uterm render module — ANSI color rendering primitives.

Palette tables, color quantizers, SGR escape emitters, image-to-ANSI
conversion, and pyte-backed terminal buffer rendering.

Zero required dependencies. Optional extras:
- ``[emulator]`` — installs pyte for AnsiBuffer
- ``[images]`` — installs Pillow for image_to_ansi_frames
"""

from __future__ import annotations

from provide.uterm.render.palette import (
    ANSI16_PALETTE,
    nearest_16,
    nearest_256,
)
from provide.uterm.render.sgr import (
    SGR_FUNCTIONS,
    ColorMode,
    sgr_16,
    sgr_256,
    sgr_truecolor,
)

__all__ = [
    "ANSI16_PALETTE",
    "ColorMode",
    "SGR_FUNCTIONS",
    "nearest_16",
    "nearest_256",
    "sgr_16",
    "sgr_256",
    "sgr_truecolor",
]
