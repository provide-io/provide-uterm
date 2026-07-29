#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

from fnmatch import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SPDX_OPEN = "#\n"
SPDX_COPYRIGHT = "# SPDX-FileCopyrightText" + ": Copyright (c) 2025-2026 provide.io llc. All rights reserved.\n"
SPDX_LICENSE = "# SPDX-License-Identifier" + ": AGPL-3.0-or-later\n"
SPDX_CLOSE = "#\n"
CANONICAL_BLOCK = (SPDX_OPEN, SPDX_COPYRIGHT, SPDX_LICENSE, SPDX_CLOSE)

EXCLUDED_DIRS = {
    ".git",
    ".worktrees",
    ".claude",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    "mutants",
    "dist",
    "build",
    "__pycache__",
    "node_modules",
    "python_modules",
}


def find_python_files(root: Path, *, skip_globs: tuple[str, ...] = ()) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        path_str = str(path)
        if any(fnmatch(path_str, pattern) for pattern in skip_globs):
            continue
        files.append(path)
    return sorted(files)


def split_shebang(text: str) -> tuple[str, str]:
    if text.startswith("#!"):
        line_end = text.find("\n")
        if line_end == -1:
            return text + "\n", ""
        return text[: line_end + 1], text[line_end + 1 :]
    return "", text


def _is_spdx_line(line: str) -> bool:
    """Whether a line belongs to the header itself rather than to the author."""
    stripped = line.strip()
    return stripped == "#" or stripped.startswith(("# SPDX-FileCopyrightText", "# SPDX-License-Identifier"))


def partition_leading_comments(text: str) -> tuple[list[str], str]:
    """Split the top of a file into the comments worth keeping, and the rest.

    Everything the header itself is made of — the two SPDX lines and the bare
    ``#`` delimiters around them — is dropped, because a canonical block is
    about to be written in its place. Every *other* leading comment is kept.

    That distinction is the whole point. Two comments that live directly under
    the header are read by tooling: ``# uv-package: <name>`` tells the
    golden-corpus drift check which workspace package to run a generator
    under, and ``# Mutation-enforced at killed==100`` records a file's
    mutation perimeter. An earlier version of this stripped every leading
    comment before rewriting the header, which deleted both — silently, since
    the file still had a valid header afterwards.

    Trailing blank lines are handed back with the body rather than swallowed,
    so the separation an author put between the header and their code
    survives being normalized.
    """
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines) and (lines[idx].startswith("#") or lines[idx].strip() == ""):
        idx += 1
    head = lines[:idx]
    while head and head[-1].strip() == "":
        head.pop()
        idx -= 1
    kept = [line for line in head if not _is_spdx_line(line) and line.strip() != ""]
    return kept, "".join(lines[idx:])


def normalize_python_text(text: str) -> str:
    shebang, rest = split_shebang(text)
    kept, body = partition_leading_comments(rest)
    return shebang + "".join(CANONICAL_BLOCK) + "".join(kept) + body


def has_canonical_header(text: str) -> bool:
    _, rest = split_shebang(text)
    lines = rest.splitlines(keepends=True)
    if len(lines) < len(CANONICAL_BLOCK):
        return False
    return tuple(lines[: len(CANONICAL_BLOCK)]) == CANONICAL_BLOCK
