#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""GIF-to-ANSI converter for Playwright tests — delegates to provide.terminal.render."""

from __future__ import annotations

from pathlib import Path

from provide.terminal.render.image import image_to_ansi_frames as _image_to_ansi_frames
from provide.terminal.render.sgr import ColorMode


def gif_to_ansi_frames(
    gif_path: str | Path,
    cols: int = 80,
    rows: int = 40,
    mode: ColorMode = "truecolor",
) -> tuple[list[str], float]:
    """Convert a GIF file to ANSI frames."""
    data = Path(gif_path).read_bytes()
    return _image_to_ansi_frames(data, cols=cols, rows=rows, mode=mode)
