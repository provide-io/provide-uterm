#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Build (or verify) the workspace-root ``src/provide/uterm`` mutation symlink tree.

Why this exists
---------------
mutmut derives a mutant's *name* from the path passed in ``paths_to_mutate``:
it takes ``str(path).replace(os.sep, ".")`` and strips a leading ``src.``
prefix (see ``get_mutant_name`` in ``mutmut/__main__.py``). The trampoline that
records which test exercises which mutant keys coverage on the *imported*
module's ``__module__`` attribute.

For the core package the two agree because the configured path is
``src/provide/uterm/...`` (-> ``provide.uterm....``) and the import name is
``provide.uterm...``. For the other workspace members the source lives at
``packages/<pkg>/src/provide/uterm/...``; mutating *that* path yields the name
``packages.<pkg>.src.provide.uterm...`` which never matches the
``provide.uterm...`` import name, so every such mutant reports ``no_tests``
regardless of how good the tests are.

The fix is to expose every mutated namespace under one canonical
``src/provide/uterm`` root so the derived name always equals the import name.
``src/provide/uterm`` is therefore a *real* directory of symlinks: the core
package's children plus one symlink per cross-package namespace
(``server``, ``tunnel``, ``pty``, ``manager``).

This script is the single source of truth for that tree. Run it after adding a
new top-level core module or a new cross-package mutation namespace, and commit
the resulting symlinks. ``--check`` verifies the on-disk tree matches what this
script would build (suitable for CI / pre-commit).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UTERM_ROOT = REPO_ROOT / "src" / "provide" / "uterm"
CORE_UTERM = REPO_ROOT / "packages" / "provide-uterm" / "src" / "provide" / "uterm"

# Cross-package namespaces that live under ``provide.uterm`` but are owned by a
# workspace member other than the core ``provide-uterm`` package. Each maps the
# child name under ``src/provide/uterm`` to the owning package's source subtree.
CROSS_PACKAGE_NAMESPACES: dict[str, str] = {
    "server": "provide-uterm-server/src/provide/uterm/server",
    "tunnel": "provide-uterm-server/src/provide/uterm/tunnel",
    "pty": "provide-uterm-platform/src/provide/uterm/pty",
    "manager": "provide-uterm-platform/src/provide/uterm/manager",
}


def _is_ignored(name: str) -> bool:
    """Skip non-source cruft that must never be symlinked into the tree.

    ``__pycache__`` appears whenever bytecode is written under the core
    package; ``*.egg-info`` is editable-install metadata. Neither is a
    mutation target, and symlinking them would make ``--check`` flap.
    """
    return name == "__pycache__" or name.endswith(".egg-info")


def _planned_links() -> dict[str, Path]:
    """Return ``{child_name: absolute_target}`` for every symlink in the tree."""
    links: dict[str, Path] = {}
    # Core package children (modules + subpackages) come first.
    for child in sorted(CORE_UTERM.iterdir()):
        if _is_ignored(child.name):
            continue
        links[child.name] = CORE_UTERM / child.name
    # Cross-package namespaces override / extend the core view.
    for name, rel in CROSS_PACKAGE_NAMESPACES.items():
        links[name] = REPO_ROOT / "packages" / rel
    return links


def _relative_target(child: str, target: Path) -> str:
    """Compute the symlink body (relative to ``src/provide/uterm/<child>``)."""
    return os.path.relpath(target, UTERM_ROOT)


def build() -> None:
    if UTERM_ROOT.is_symlink() or UTERM_ROOT.exists():
        if UTERM_ROOT.is_symlink() or UTERM_ROOT.is_file():
            UTERM_ROOT.unlink()
        else:
            for entry in UTERM_ROOT.iterdir():
                entry.unlink()
            UTERM_ROOT.rmdir()
    UTERM_ROOT.mkdir(parents=True)
    for child, target in _planned_links().items():
        (UTERM_ROOT / child).symlink_to(_relative_target(child, target))
    print(f"Built {len(_planned_links())} symlinks under {UTERM_ROOT}")


def check() -> int:
    if not UTERM_ROOT.is_dir() or UTERM_ROOT.is_symlink():
        print(f"ERROR: {UTERM_ROOT} must be a real directory of symlinks, not a symlink/file")
        return 1
    planned = _planned_links()
    actual = {p.name for p in UTERM_ROOT.iterdir() if not _is_ignored(p.name)}
    missing = set(planned) - actual
    extra = actual - set(planned)
    problems: list[str] = []
    if missing:
        problems.append(f"missing symlinks: {sorted(missing)}")
    if extra:
        problems.append(f"unexpected entries: {sorted(extra)}")
    for child, target in planned.items():
        link = UTERM_ROOT / child
        if not link.is_symlink():
            problems.append(f"{child}: not a symlink")
            continue
        if link.resolve() != target.resolve():
            problems.append(f"{child}: points to {link.resolve()}, expected {target.resolve()}")
    if problems:
        print("Mutation src tree is stale; run `python scripts/build_mutation_src_tree.py`:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("Mutation src tree is up to date.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the tree matches what would be built (non-zero exit on drift).",
    )
    args = parser.parse_args()
    if args.check:
        return check()
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
