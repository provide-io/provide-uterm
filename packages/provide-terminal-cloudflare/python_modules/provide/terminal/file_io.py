#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""File I/O helpers for loading BBS screen files and color palettes."""

from __future__ import annotations

import json
from pathlib import Path

from provide.terminal.ansi import DEFAULT_PALETTE


def load_ans(path: Path | str, encoding: str = "latin-1") -> str:
    """Load a .ans file (BBS ANSI art).

    Args:
        path: Path to the .ans file.
        encoding: Character encoding.  Default is ``latin-1``, the standard
            encoding for BBS ANSI art files.

    Returns:
        File contents as a string.
    """
    return Path(path).read_bytes().decode(encoding)


def load_txt(path: Path | str, encoding: str = "utf-8") -> str:
    """Load a plain .txt file.

    Args:
        path: Path to the text file.
        encoding: Character encoding.  Default is ``utf-8``.

    Returns:
        File contents as a string.
    """
    return Path(path).read_text(encoding=encoding)


def load_palette(path: Path | str | None) -> list[int]:
    """Load a JSON 256-color palette (list of 16 ints 0-255).

    Args:
        path: Path to a JSON file containing a list of 16 integers (0-255),
            or ``None`` to use the default palette.

    Returns:
        A copy of the palette as a list of 16 integers.

    Raises:
        ValueError: If the file does not contain a list of exactly 16 integers
            in the range 0-255.
    """
    if path is None:
        return DEFAULT_PALETTE[:]
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) != 16:
        raise ValueError("palette map must be a JSON list of 16 integers")
    out: list[int] = []
    for v in data:
        if not isinstance(v, int) or not (0 <= v <= 255):
            raise ValueError("palette map values must be integers in 0..255")
        out.append(v)
    return out
