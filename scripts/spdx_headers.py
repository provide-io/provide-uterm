#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

from fnmatch import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SPDX_OPEN = "#\n"
SPDX_COPYRIGHT = "# SPDX-FileCopyrightText" + ": Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.\n"
SPDX_LICENSE = "# SPDX-License-Identifier" + ": AGPL-3.0-or-later\n"
SPDX_CLOSE = "#\n"
CANONICAL_BLOCK = (SPDX_OPEN, SPDX_COPYRIGHT, SPDX_LICENSE, SPDX_CLOSE)

EXCLUDED_DIRS = {
    ".git",
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


def strip_leading_comment_block(text: str) -> str:
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("#") or line.strip() == "":
            idx += 1
            continue
        break
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    return "".join(lines[idx:])


def normalize_python_text(text: str) -> str:
    shebang, rest = split_shebang(text)
    body = strip_leading_comment_block(rest)
    return shebang + "".join(CANONICAL_BLOCK) + body


def has_canonical_header(text: str) -> bool:
    _, rest = split_shebang(text)
    lines = rest.splitlines(keepends=True)
    if len(lines) < len(CANONICAL_BLOCK):
        return False
    return tuple(lines[: len(CANONICAL_BLOCK)]) == CANONICAL_BLOCK
