# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locate the libuterm_capture shared library bundled with this package."""

from __future__ import annotations

import sys
from pathlib import Path


def get_capture_lib_path() -> Path | None:
    """
    Return the path to libuterm_capture.so/.dylib, or None if not built.

    The library is in _native/, placed there by `make && cp` at build time.
    """
    native_dir = Path(__file__).parent / "_native"
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    lib = native_dir / f"libuterm_capture{suffix}"
    return lib if lib.exists() else None
