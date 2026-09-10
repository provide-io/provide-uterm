#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""File I/O helpers for loading BBS screen files and color palettes."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from provide.telemetry import get_logger

from provide.uterm.ansi import DEFAULT_PALETTE

if TYPE_CHECKING:
    from io import TextIOWrapper

logger = get_logger(__name__)

_warned_no_symlink_guard = False


def _ensure_owner_only_dir(directory: Path, *, mode: int) -> None:
    directory.mkdir(mode=mode, parents=True, exist_ok=True)
    directory.chmod(mode)


def try_fchmod(fd: int, mode: int) -> None:
    """Best-effort ``os.fchmod`` — a silent no-op where it doesn't exist (Windows)."""
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)


def secure_create(path: Path | str, *, mode: int = 0o600, dir_mode: int = 0o700) -> int:
    """Create/open *path* for append with owner-only permissions and no symlink following.

    ``O_NOFOLLOW`` and ``fchmod`` have no Windows equivalent (Windows lacks both
    symlink-following flags and POSIX permission bits on file descriptors), so
    both are applied only when the platform's ``os`` module exposes them. On
    Windows the owner-only-mode guarantee is UNAVAILABLE, and the symlink
    refusal below is a pre-open check rather than ``O_NOFOLLOW``'s atomic
    kernel-level one — it closes the common case (a symlink planted before
    this call) but not a race where one is swapped in between the check and
    the open. A warning is logged once per process so the gap is visible at
    runtime rather than only in this docstring.
    """
    global _warned_no_symlink_guard
    target = Path(path)
    if not hasattr(os, "O_NOFOLLOW"):
        if not _warned_no_symlink_guard:
            _warned_no_symlink_guard = True
            logger.warning("secure_create_symlink_guard_unavailable_on_windows")
        if target.is_symlink():
            raise OSError(f"Refusing to open symlink as a recording sink: {target}")
    _ensure_owner_only_dir(target.parent, mode=dir_mode)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, mode)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"Refusing to open non-regular recording sink: {target}")
        try_fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


def secure_open_append(path: Path | str, *, mode: int = 0o600, dir_mode: int = 0o700) -> TextIOWrapper:
    """Open *path* for append using :func:`secure_create`."""
    fd = secure_create(path, mode=mode, dir_mode=dir_mode)
    return os.fdopen(fd, "a", encoding="utf-8")


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
