#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Final

BAD_MUTANT_STATES: Final[tuple[str, ...]] = (
    "not checked",
    "survived",
    "suspicious",
    "timeout",
    "skipped",
)

# Mutant states a documented-equivalent allowlist entry may excuse. Only ever
# applied to a mutant that is ALSO allowlisted (see _apply_equivalent_allowlist),
# i.e. one with a written, proven equivalence reason. A non-allowlisted mutant
# in any of these states still fails the gate.
EXCUSABLE_STATES: Final[tuple[str, ...]] = (
    "survived",
    "suspicious",
    "timeout",
)

# Allowlist of documented equivalent mutants (mutant id -> justification),
# excluded from the killed==N denominator so a file with genuinely-unkillable
# equivalent mutants can still be enforced. See _load_equivalent_allowlist.
DEFAULT_EQUIVALENTS_FILE: Final[str] = "mutation_equivalents.toml"

CONFIG_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    ".pytest.ini",
    "pytest.ini",
)
MUTMUT_INCOMPATIBLE_PYTEST_ARGS: Final[tuple[str, ...]] = (
    "--randomly-dont-reorganize",
    "-x",
)
DEFAULT_MUTATION_ROOTS: Final[tuple[str, ...]] = (
    "packages/provide-uterm/src/provide/uterm/",
    "packages/provide-uterm-platform/src/provide/uterm/pty/",
    "packages/provide-uterm-platform/src/provide/uterm/manager/",
    "packages/provide-uterm-server/src/provide/uterm/",
    "src/provide/uterm/",
)
MUTATION_SUPPORT_FILES: Final[tuple[str, ...]] = (
    DEFAULT_EQUIVALENTS_FILE,
    "pyproject.toml",
    "ci/prepare_mutation_args.sh",
    "scripts/run_mutation_gate.py",
    "scripts/mutation_gate_config.py",
)


def uv_mutmut_cmd(python_version: str | None, *args: str) -> list[str]:
    base = ["uv", "run"]
    if python_version:
        base.extend(["--python", python_version])
    return [*base, "mutmut", *args]


def state_counts_from(mutants: list[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _name, state in mutants:
        counts[state] = counts.get(state, 0) + 1
    return counts


def mutation_score(stats: dict[str, int]) -> float:
    total = int(stats.get("total", 0))
    if total <= 0:
        return 0.0
    return (int(stats.get("killed", 0)) / total) * 100.0


def seed_mutants_config(paths_to_mutate: list[str] | None = None) -> None:
    mutants = Path("mutants")
    mutants.mkdir(parents=True, exist_ok=True)
    for config_name in CONFIG_FILES:
        src = Path(config_name)
        if src.exists():
            shutil.copy2(src, mutants / config_name)
    sanitize_mutants_pyproject(mutants / "pyproject.toml", paths_to_mutate=paths_to_mutate)


def sanitize_mutants_pyproject(
    path: Path,
    *,
    paths_to_mutate: list[str] | None,
    strip_workspace: bool = True,
) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    updated = text
    for arg in MUTMUT_INCOMPATIBLE_PYTEST_ARGS:
        updated = updated.replace(f'"{arg}",\n', "")
        updated = updated.replace(f'"{arg}"', "")
    if strip_workspace:
        updated = re.sub(r"^\[tool\.uv\.workspace\]\n(?:.*\n)*?\n", "\n", updated, flags=re.MULTILINE)
        updated = re.sub(r"^\[tool\.uv\.sources\]\n(?:.*\n)*?\n", "\n", updated, flags=re.MULTILINE)
    if paths_to_mutate:
        encoded = ", ".join(f'"{item}"' for item in paths_to_mutate)
        updated, count = re.subn(
            r"^paths_to_mutate\s*=\s*\[[\s\S]*?\]",
            f"paths_to_mutate = [{encoded}]",
            updated,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise RuntimeError("failed to rewrite paths_to_mutate in mutants/pyproject.toml")
    if updated != text:
        path.write_text(updated, encoding="utf-8")
