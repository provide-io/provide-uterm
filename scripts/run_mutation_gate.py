#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

BAD_STAT_KEYS: Final[tuple[str, ...]] = (
    "segfault",
    "suspicious",
    "no_tests",
    "check_was_interrupted_by_user",
)

CONFIG_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    ".pytest.ini",
    "pytest.ini",
)
MUTMUT_INCOMPATIBLE_PYTEST_ARGS: Final[tuple[str, ...]] = ("--randomly-dont-reorganize",)
DEFAULT_MUTATION_ROOTS: Final[tuple[str, ...]] = (
    "packages/provide-terminal/src/provide/terminal/",
    "packages/provide-terminal-platform/src/provide/terminal/pty/",
)


def _uv_mutmut_cmd(python_version: str | None, *args: str) -> list[str]:
    base = ["uv", "run"]
    if python_version:
        base.extend(["--python", python_version])
    return [*base, "mutmut", *args]


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd))
    completed = subprocess.run(cmd, check=False, env=env)  # noqa: S603
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(cmd)}")


def _seed_mutants_config(paths_to_mutate: list[str] | None = None) -> None:
    mutants = Path("mutants")
    mutants.mkdir(parents=True, exist_ok=True)
    for config_name in CONFIG_FILES:
        src = Path(config_name)
        if src.exists():
            dst = mutants / config_name
            shutil.copy2(src, dst)
    _sanitize_mutants_pyproject(mutants / "pyproject.toml", paths_to_mutate=paths_to_mutate)


def _sanitize_mutants_pyproject(path: Path, *, paths_to_mutate: list[str] | None) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    updated = text
    for arg in MUTMUT_INCOMPATIBLE_PYTEST_ARGS:
        updated = updated.replace(f'"{arg}",\n', "")
        updated = updated.replace(f'"{arg}"', "")
    # Strip uv workspace config — mutants/ doesn't contain workspace members
    updated = re.sub(
        r"^\[tool\.uv\.workspace\]\n(?:.*\n)*?\n",
        "\n",
        updated,
        flags=re.MULTILINE,
    )
    updated = re.sub(
        r"^\[tool\.uv\.sources\]\n(?:.*\n)*?\n",
        "\n",
        updated,
        flags=re.MULTILINE,
    )
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


def _half_cpu_count() -> int:
    count = os.cpu_count() or 1
    return max(1, count // 2)


def _changed_python_paths(base_ref: str, staged_only: bool, roots: tuple[str, ...]) -> list[str]:
    diff_cmd = ["git", "diff", "--name-only"]
    if staged_only:
        diff_cmd.append("--cached")
    else:
        diff_cmd.append(base_ref)
    diff_cmd.append("--")
    result = subprocess.run(diff_cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        return []

    changed: list[str] = []
    for raw in result.stdout.splitlines():
        path = raw.strip()
        if not path.endswith(".py"):
            continue
        if not any(path.startswith(root) for root in roots):
            continue
        if Path(path).exists():
            changed.append(path)
    return sorted(set(changed))


def _read_stats(path: Path) -> dict[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {k: int(v) for k, v in payload.items()}


def _is_clean(stats: dict[str, int]) -> bool:
    if int(stats.get("total", 0)) <= 0:
        return False
    return all(int(stats.get(key, 0)) == 0 for key in BAD_STAT_KEYS)


def _mutation_score(stats: dict[str, int]) -> float:
    total = int(stats.get("total", 0))
    if total <= 0:
        return 0.0
    killed = int(stats.get("killed", 0))
    return (killed / total) * 100.0


def run_mutation_gate(
    python_version: str | None,
    max_children: int,
    retries: int,
    min_mutation_score: float,
    paths_to_mutate: list[str] | None = None,
) -> dict[str, int]:
    attempts = retries + 1
    stats_path = Path("mutants/mutmut-cicd-stats.json")
    last_stats: dict[str, int] = {}
    mutation_env = dict(os.environ)

    # mutmut reads paths_to_mutate from the ROOT pyproject.toml (not mutants/).
    # When --changed-only narrows the targets, rewrite the root config temporarily.
    root_pyproject = Path("pyproject.toml")
    root_original = root_pyproject.read_text(encoding="utf-8") if root_pyproject.exists() else None

    for attempt in range(1, attempts + 1):
        mutants_dir = Path("mutants")
        if mutants_dir.exists():
            shutil.rmtree(mutants_dir)
        _seed_mutants_config(paths_to_mutate=paths_to_mutate)

        # Also rewrite the root pyproject.toml so mutmut sees the narrowed targets
        if paths_to_mutate and root_original is not None:
            _sanitize_mutants_pyproject(root_pyproject, paths_to_mutate=paths_to_mutate)

        children = max_children if attempt == 1 else 1
        print(f"Running mutation attempt {attempt}/{attempts} with max-children={children}")

        # mutmut returns 0 = all killed, 1 = survivors exist, 2+ = error.
        # Survivors are expected (equivalent mutants); only fail on real errors.
        cmd = _uv_mutmut_cmd(python_version, "run", "--max-children", str(children))
        print("+", " ".join(cmd))
        try:
            mutmut_result = subprocess.run(cmd, check=False, env=mutation_env)  # noqa: S603
        finally:
            # Restore root pyproject.toml immediately
            if root_original is not None:
                root_pyproject.write_text(root_original, encoding="utf-8")
        if mutmut_result.returncode > 1:
            raise RuntimeError(f"mutmut crashed (exit {mutmut_result.returncode})")
        _run(_uv_mutmut_cmd(python_version, "export-cicd-stats"), env=mutation_env)
        last_stats = _read_stats(stats_path)
        score = _mutation_score(last_stats)
        print(f"mutation_score={score:.2f}")
        print(json.dumps(last_stats, indent=2, sort_keys=True))

        if _is_clean(last_stats) and score >= min_mutation_score:
            return last_stats
        if attempt < attempts:
            print("Mutation gate not clean; retrying in single-worker mode.")

    score = _mutation_score(last_stats)
    raise RuntimeError(
        "mutation gate failed: "
        f"score={score:.2f} min_required={min_mutation_score:.2f} "
        f"stats={json.dumps(last_stats, sort_keys=True)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run strict mutmut gate with retries.")
    parser.add_argument("--python-version", default="3.11", help="Python version passed to `uv run --python`.")
    parser.add_argument(
        "--max-children",
        type=int,
        default=None,
        help="Initial mutmut worker count (defaults to half CPU count).",
    )
    parser.add_argument("--retries", type=int, default=1, help="Number of retries after initial failure.")
    parser.add_argument(
        "--min-mutation-score",
        type=float,
        default=100.0,
        help="Minimum mutation score required to pass (killed/total * 100).",
    )
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="Mutate only changed Python files under configured mutation roots.",
    )
    parser.add_argument(
        "--base-ref",
        default="HEAD",
        help="Git base ref used for --changed-only (default: HEAD).",
    )
    parser.add_argument(
        "--staged-only",
        action="store_true",
        help="With --changed-only, consider only staged changes.",
    )
    args = parser.parse_args()
    half_cpus = _half_cpu_count()
    requested_children = args.max_children if args.max_children is not None else half_cpus
    max_children = min(max(1, requested_children), half_cpus)

    paths_to_mutate: list[str] | None = None
    if args.changed_only:
        paths_to_mutate = _changed_python_paths(args.base_ref, args.staged_only, DEFAULT_MUTATION_ROOTS)
        if not paths_to_mutate:
            print("mutation gate skipped: no changed Python files under mutation roots")
            return 0
        print(f"mutation gate targets ({len(paths_to_mutate)}): {paths_to_mutate}")

    try:
        run_mutation_gate(
            args.python_version,
            max_children,
            args.retries,
            args.min_mutation_score,
            paths_to_mutate=paths_to_mutate,
        )
    except RuntimeError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
