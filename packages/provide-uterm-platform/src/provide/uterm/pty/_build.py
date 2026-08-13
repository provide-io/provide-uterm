#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Locate the libuterm_capture shared library bundled with this package."""

from __future__ import annotations

import sys
from pathlib import Path

from provide.telemetry import get_logger

logger = get_logger(__name__)

# Sources that must be older than the built library for it to be current. Named
# rather than globbed so a new source file is a deliberate addition here.
_CAPTURE_SOURCES = ("capture.c", "capture_writer.c", "capture_writer.h")


def _source_dir() -> Path:
    """The native sources, when this package is a checkout rather than a wheel."""

    return Path(__file__).resolve().parents[4] / "native" / "capture"


def _warn_if_stale(lib: Path) -> None:
    """Say so when the built library is older than the sources it came from.

    ``_native`` holds a build artifact copied there by hand, and it is ignored by
    git, so a checkout can carry a library built from sources that have since
    changed. Nothing else notices: the stale library loads, connects, and behaves
    like the code someone is reading, so edits appear to do nothing and every
    conclusion drawn from its behaviour is drawn about a different program. CI
    never sees this because it builds fresh every run.
    """

    source_dir = _source_dir()
    if not source_dir.is_dir():
        return  # Installed as a package; there are no sources to be behind.
    try:
        built_at = lib.stat().st_mtime
        newer = [
            name
            for name in _CAPTURE_SOURCES
            if (source := source_dir / name).exists() and source.stat().st_mtime > built_at
        ]
    except OSError:
        return
    if newer:
        logger.warning(
            "capture_library_is_stale",
            library=str(lib),
            newer_sources=newer,
            remedy=f"make -C {source_dir} install",
        )


def get_capture_lib_path() -> Path | None:
    """
    Return the path to libuterm_capture.so/.dylib, or None if not built.

    The library is in _native/, placed there by `make install` at build time.
    """
    native_dir = Path(__file__).parent / "_native"
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    lib = native_dir / f"libuterm_capture{suffix}"
    if not lib.exists():
        return None
    _warn_if_stale(lib)
    return lib
