#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Which paths in this checkout belong to somebody else.

Shared by the SPDX walker and the docs-accuracy checker, which both need the
same answer and got it wrong in the same way when packages/provide-telemetry
became a submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def submodule_dirs(root: Path) -> set[Path]:
    """Resolved paths of every git submodule declared in ``.gitmodules``.

    Parsed from the file rather than shelled out to ``git submodule``, so the
    answer is the same whether or not the submodules are initialised. CI
    checkouts and fresh clones start with them absent, and a set that changed
    depending on that would make every caller's behaviour depend on how the
    tree happened to be set up.
    """
    gitmodules = root / ".gitmodules"
    if not gitmodules.is_file():
        return set()
    paths: set[Path] = set()
    for line in gitmodules.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("path"):
            continue
        _, _, value = stripped.partition("=")
        if value.strip():
            paths.add((root / value.strip()).resolve())
    return paths


def is_under(path: Path, directories: set[Path]) -> bool:
    """Whether *path* is inside any of *directories*."""
    if not directories:
        return False
    resolved = path.resolve()
    return any(resolved == directory or directory in resolved.parents for directory in directories)
