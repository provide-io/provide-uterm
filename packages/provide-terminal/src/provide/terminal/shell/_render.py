#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Image-to-ANSI rendering — delegates to provide.terminal.render.

Requires ``provide-terminal[emulator]`` extra.
"""

from __future__ import annotations

try:
    from provide.terminal.render.image import image_to_ansi_frames, render_frame
    from provide.terminal.render.image import render_frame as _render_frame
    from provide.terminal.render.palette import (
        _XTERM256,
        ANSI16_PALETTE,
        _build_xterm256,
        _color_dist_sq,
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
except ImportError:  # pragma: no cover
    raise ImportError(  # pragma: no cover
        "missing dependency — provide-terminal render module\ninstall the images extra: pip install 'provide-terminal[emulator]'"
    ) from None

__all__ = [
    "ANSI16_PALETTE",
    "SGR_FUNCTIONS",
    "_XTERM256",
    "ColorMode",
    "_build_xterm256",
    "_color_dist_sq",
    "_render_frame",
    "image_to_ansi_frames",
    "nearest_16",
    "nearest_256",
    "render_frame",
    "sgr_16",
    "sgr_256",
    "sgr_truecolor",
]

# Backwards-compat aliases for existing tests that use private names
_ANSI16 = ANSI16_PALETTE
_nearest_16 = nearest_16
_nearest_256 = nearest_256
_sgr_truecolor = sgr_truecolor
_sgr_256 = sgr_256
_sgr_16 = sgr_16
_SGR_FN = SGR_FUNCTIONS
