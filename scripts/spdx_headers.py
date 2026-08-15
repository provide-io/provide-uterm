#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import subprocess
from fnmatch import fnmatch
from pathlib import Path

from repo_paths import is_under, submodule_dirs

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


def git_ignored(root: Path, paths: list[Path]) -> set[Path]:
    """The subset of *paths* that git ignores.

    Returns an EMPTY set whenever git cannot answer -- no git binary, not a
    work tree, any failure at all. That direction is deliberate: an empty set
    filters nothing, so the caller falls back to considering everything and at
    worst reports files it could have skipped. The opposite default would
    silently skip files whenever git hiccuped, and a header checker that
    quietly stops checking is far worse than a noisy one.
    """
    if not paths:
        return set()
    try:
        # Fixed argv, shell=False, and the paths go over stdin rather than the
        # command line -- which is also what keeps this to one process for a
        # tree of any size.
        completed = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin", "-z"],
            input="\0".join(str(path) for path in paths),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return set()
    # 0 = at least one path is ignored, 1 = none are. Anything else (128 for
    # "not a git repository", or a usage error) means git did not answer the
    # question, so treat it as "cannot tell" rather than "nothing is ignored".
    if completed.returncode not in (0, 1):
        return set()
    return {Path(entry) for entry in completed.stdout.split("\0") if entry}


def find_python_files(root: Path, *, skip_globs: tuple[str, ...] = (), respect_gitignore: bool = True) -> list[Path]:
    """Every Python file under *root* that should carry an SPDX header.

    Anything git ignores is skipped. EXCLUDED_DIRS and ``skip_globs`` still
    cover what git does not ignore -- vendored trees, generated packages, and
    scripts/ itself -- but they are no longer the only thing standing between
    this walk and a virtualenv, which is a job they kept losing: ``.venv`` was
    in EXCLUDED_DIRS, ``.venv-workers`` was in the caller's skip globs, and
    ``.venv-goldens`` (created on demand by .ci/check_goldens.sh) was in
    neither. It contributed 10,487 "noncompliant" files to a local run.

    CI never saw that, because runners start clean -- which is what makes the
    failure mode expensive rather than merely untidy. It fires only on a
    developer's machine, and it fires loudly enough to bury a real violation
    among ten thousand false ones.

    The stakes are higher for the other caller. normalize_spdx_headers.py
    walks with this same function and REWRITES what it finds, so an unignored
    virtualenv there was not noise -- it was 10,487 rewritten files inside
    site-packages.
    """
    submodules = submodule_dirs(root)
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        # Submodules are another repository's files, with their own headers and
        # their own checker. Dropping them here is also what keeps the git call
        # below working at all: `git check-ignore` aborts the WHOLE batch with
        # exit 128 the moment one path is inside a submodule ("fatal: Pathspec
        # ... is in submodule"), so a single such path would take the answer
        # for all fifteen thousand others down with it -- and the caller would
        # fall back to reporting everything, virtualenvs included.
        if is_under(path, submodules):
            continue
        path_str = str(path)
        if any(fnmatch(path_str, pattern) for pattern in skip_globs):
            continue
        files.append(path)
    if respect_gitignore:
        ignored = git_ignored(root, files)
        files = [path for path in files if path not in ignored]
    return sorted(files)


def split_shebang(text: str) -> tuple[str, str]:
    if text.startswith("#!"):
        line_end = text.find("\n")
        if line_end == -1:
            return text + "\n", ""
        return text[: line_end + 1], text[line_end + 1 :]
    return "", text


def _is_spdx_text(line: str) -> bool:
    """Whether a line is one of the two SPDX lines themselves."""
    return line.strip().startswith(("# SPDX-FileCopyrightText", "# SPDX-License-Identifier"))


def _header_line_indices(head: list[str]) -> set[int]:
    """Indices in *head* that belong to the SPDX block rather than the author.

    The two SPDX lines, plus the bare ``#`` delimiters IMMEDIATELY around them.

    Adjacency is the whole point. This used to treat every bare ``#`` anywhere
    in the leading comments as header material, which silently deleted the
    paragraph breaks authors use inside a long file comment -- one bare ``#``
    between two paragraphs of a mutation-perimeter note, gone on the next
    normalize, with the file still carrying a valid header afterwards so
    nothing looked wrong. That is the same silent-deletion failure this
    module's tests were written for after it ate `# uv-package:` markers.
    """
    spdx = {index for index, line in enumerate(head) if _is_spdx_text(line)}
    drop = set(spdx)
    for index in spdx:
        for step in (-1, 1):
            cursor = index + step
            while 0 <= cursor < len(head) and head[cursor].strip() == "#":
                drop.add(cursor)
                cursor += step
    return drop


def partition_leading_comments(text: str) -> tuple[list[str], str]:
    """Split the top of a file into the comments worth keeping, and the rest.

    Everything the header itself is made of — the two SPDX lines and the bare
    ``#`` delimiters IMMEDIATELY around them — is dropped, because a canonical
    block is about to be written in its place. Every *other* leading comment is
    kept, including a bare ``#`` an author used to break a long comment into
    paragraphs further down (see ``_header_line_indices``).

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
    header = _header_line_indices(head)
    kept = [line for index, line in enumerate(head) if index not in header and line.strip() != ""]
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
