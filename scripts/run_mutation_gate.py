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
import subprocess  # nosec
import tomllib
from pathlib import Path
from typing import Final

BAD_STAT_KEYS: Final[tuple[str, ...]] = (
    "segfault",
    "suspicious",
    "no_tests",
    "check_was_interrupted_by_user",
)
BAD_MUTANT_STATES: Final[tuple[str, ...]] = (
    "not checked",
    "survived",
    "suspicious",
    "timeout",
    "skipped",
)

# Mutant states a documented-equivalent allowlist entry may excuse. These are only
# ever applied to a mutant that is ALSO in the allowlist (see
# _apply_equivalent_allowlist), i.e. one with a written, proven equivalence reason,
# so a non-allowlisted mutant in any of these states still fails the gate.
#
#   - "survived"/"suspicious": the mutated code is behaviorally identical, so no test
#     can kill it — the canonical equivalent-mutant outcome.
#   - "timeout": mutmut flags this purely on wall-clock (it SIGXCPU's a run exceeding
#     (estimated_test_time + 1) * 15s; the estimate is measured single-threaded
#     during stats). For an ALLOWLISTED — therefore proven-unkillable — mutant a
#     timeout is the SAME fact as "survived" (not killed, and cannot be), just
#     surfaced by CI wall-clock noise instead of a clean finish; it cannot be hiding
#     a kill, because a now-killable mutant would FAIL FAST (killed), never time out.
#     A NON-allowlisted timeout is still a hard failure — a real infra/coverage
#     problem to fix, never excused.
#
# "no tests" (a coverage gap) and "skipped" are never excusable in any state.
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


def _uv_mutmut_cmd(python_version: str | None, *args: str) -> list[str]:
    base = ["uv", "run"]
    if python_version:
        base.extend(["--python", python_version])
    return [*base, "mutmut", *args]


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd))
    completed = subprocess.run(cmd, check=False, env=env)
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


def _sanitize_mutants_pyproject(
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
        # Strip uv workspace config — mutants/ doesn't contain workspace members.
        # Do NOT strip from the root pyproject.toml: uv needs workspace/sources
        # to resolve packages like provide-uterm that aren't on PyPI.
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


def _resolve_to_mutmut_path(path: str) -> str | None:
    """Translate a git-diff path to the path mutmut uses.

    mutmut records function hits by import module name, so this repo keeps
    mutation targets on the root ``src/`` symlink tree. Git diff returns real
    package paths, so this inode-based lookup maps changed files back to the
    configured mutmut path without hard-coding package prefixes.

    Strategy: walk paths_to_mutate from the root pyproject.toml and return the
    first entry whose resolved inode matches the changed file's inode, so we
    never hard-code package prefixes.
    """
    try:
        cfg = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        configured = cfg.get("tool", {}).get("mutmut", {}).get("paths_to_mutate", [])
    except Exception:
        return path

    try:
        target_inode = Path(path).stat().st_ino
    except OSError:
        return path

    for entry in configured:
        ep = Path(entry)
        try:
            # Direct match (file entry)
            if ep.is_file() and ep.stat().st_ino == target_inode:
                return str(entry)
            # Directory entry — walk for a matching file
            if ep.is_dir():
                for child in ep.rglob("*.py"):
                    try:
                        if child.stat().st_ino == target_inode:
                            return str(child)
                    except OSError:
                        continue
        except OSError:
            continue

    # Inode lookup failed (e.g. the configured ``src/`` symlink is not resolvable
    # in this checkout). Fall back to a PATH match against the perimeter — but
    # ONLY return a target when the changed file actually corresponds to a
    # configured ``paths_to_mutate`` entry. A changed file OUTSIDE the perimeter
    # (e.g. a connector, deliberately not in ``paths_to_mutate``) must be SKIPPED,
    # not force-mutated: mutmut has no bound test suite for it, so it yields
    # ``total=0`` and spuriously fails the gate (changed-only must never widen the
    # mutation surface beyond the full gate's perimeter).
    marker = "src/provide/uterm/"
    idx = path.find(marker)
    if idx == -1:
        return None
    suffix = path[idx:]
    for entry in configured:
        normalized = str(entry).rstrip("/")
        if suffix == normalized or suffix.startswith(normalized + "/"):
            return str(entry)
    return None


def _prepend_mutant_source_roots(env: dict[str, str], existing_pythonpath: str | None) -> None:
    root_paths = (
        "mutants/src",
        "mutants/packages/provide-uterm/src",
        "mutants/packages/provide-uterm-server/src",
        "mutants/packages/provide-uterm-platform/src",
        "mutants/packages/provide-uterm-client/src",
        "mutants/packages/provide-uterm-cloudflare/src",
    )
    for path in root_paths:
        Path(path).mkdir(parents=True, exist_ok=True)
    source_roots = [str(Path(path).resolve()) for path in root_paths]
    pythonpath_parts = [*source_roots]
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    if pythonpath_parts:
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)


def _changed_python_paths(base_ref: str, staged_only: bool, roots: tuple[str, ...]) -> list[str]:
    diff_cmd = ["git", "diff", "--name-only"]
    if staged_only:
        diff_cmd.append("--cached")
    else:
        diff_cmd.append(base_ref)
    diff_cmd.append("--")
    result = subprocess.run(diff_cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []

    changed: list[str] = []
    for raw in result.stdout.splitlines():
        path = raw.strip()
        if not path.endswith(".py"):
            continue
        if not any(path.startswith(root) for root in roots):
            continue
        if not Path(path).exists():
            continue
        # Translate to the path mutmut actually uses (may differ via symlinks)
        resolved = _resolve_to_mutmut_path(path)
        if resolved:
            changed.append(resolved)
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


def _results_per_mutant(python_version: str | None, env: dict[str, str]) -> list[tuple[str, str]]:
    """Return ``(mutant_name, state)`` for every mutant from ``mutmut results``."""
    cmd = _uv_mutmut_cmd(python_version, "results", "--all", "true")
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)  # nosec
    if completed.returncode != 0:
        raise RuntimeError(f"failed to read mutmut results ({completed.returncode})")
    mutants: list[tuple[str, str]] = []
    for raw in completed.stdout.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        name, state = line.rsplit(":", 1)
        mutants.append((name.strip(), state.strip()))
    return mutants


def _state_counts_from(mutants: list[tuple[str, str]]) -> dict[str, int]:
    """Tally mutant ``(name, state)`` pairs into a ``state -> count`` mapping."""
    counts: dict[str, int] = {}
    for _name, state in mutants:
        counts[state] = counts.get(state, 0) + 1
    return counts


def _load_equivalent_allowlist(path: str | Path = DEFAULT_EQUIVALENTS_FILE) -> dict[str, str]:
    """Load the documented equivalent-mutant allowlist (``mutant id -> reason``).

    Each ``[[equivalent]]`` entry MUST carry a non-empty ``mutant`` id (exactly as
    ``mutmut results`` prints it) and a ``reason`` justifying why the mutation is
    behaviorally equivalent and therefore unkillable. A missing file yields an
    empty allowlist (no exclusions — identical to the historical strict gate).
    """
    file_path = Path(path)
    if not file_path.exists():
        return {}
    data = tomllib.loads(file_path.read_text(encoding="utf-8"))
    allowlist: dict[str, str] = {}
    for entry in data.get("equivalent", []):
        mutant = entry.get("mutant")
        reason = entry.get("reason")
        if not mutant or not reason:
            raise RuntimeError(f"equivalent-mutant entry needs non-empty 'mutant' and 'reason': {entry!r}")
        allowlist[mutant] = reason
    return allowlist


def _apply_equivalent_allowlist(
    mutants: list[tuple[str, str]], allowlist: dict[str, str]
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Partition mutants against the equivalent-mutant allowlist.

    Returns ``(effective, excused, stale)``:
      - ``effective``: mutants that still count toward the gate (allowlisted
        survivors/suspicious removed from the denominator).
      - ``excused``: allowlisted mutant ids that genuinely survived this run.
      - ``stale``: allowlisted ids that did NOT match a surviving mutant this run
        (killed since, or renumbered by a source edit) — reported so the
        allowlist stays honest. A renumbered equivalent simply reappears as a new
        unexcused survivor and fails the gate, so stale entries never weaken it.
    """
    excused: list[str] = []
    effective: list[tuple[str, str]] = []
    for name, state in mutants:
        if name in allowlist and state in EXCUSABLE_STATES:
            excused.append(name)
        else:
            effective.append((name, state))
    stale = sorted(set(allowlist) - set(excused))
    return effective, excused, stale


def _collect_stats(
    python_version: str | None,
    env: dict[str, str],
    equivalents: dict[str, str],
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """Read ``mutmut results``, apply the equivalent allowlist, and tally stats.

    Returns ``(effective, last_stats)`` where ``effective`` is the per-mutant
    ``(name, state)`` list that still counts toward the gate (allowlisted
    equivalents removed) and ``last_stats`` is the score-bearing summary dict.
    """
    per_mutant = _results_per_mutant(python_version, env)
    effective, excused, stale = _apply_equivalent_allowlist(per_mutant, equivalents)
    for name in stale:
        print(f"WARNING: stale equivalent-allowlist entry (no surviving mutant matched): {name}")
    if excused:
        print(f"excused {len(excused)} documented-equivalent mutant(s) via {DEFAULT_EQUIVALENTS_FILE}")
    state_counts = _state_counts_from(effective)
    total = sum(state_counts.values())
    killed = int(state_counts.get("killed", 0))
    score = (killed / total * 100.0) if total > 0 else 0.0
    last_stats = {
        "total": total,
        "killed": killed,
        "survived": int(state_counts.get("survived", 0)),
        "suspicious": int(state_counts.get("suspicious", 0)),
        "timeout": int(state_counts.get("timeout", 0)),
        "skipped": int(state_counts.get("skipped", 0)),
        "not_checked": int(state_counts.get("not checked", 0)),
        "bad_total": sum(int(state_counts.get(state, 0)) for state in BAD_MUTANT_STATES),
    }
    print(f"mutation_score={score:.2f}")
    print(json.dumps(state_counts, indent=2, sort_keys=True))
    return effective, last_stats


def run_mutation_gate(
    python_version: str | None,
    max_children: int,
    retries: int,
    min_mutation_score: float,
    paths_to_mutate: list[str] | None = None,
) -> dict[str, int]:
    attempts = retries + 1
    last_stats: dict[str, int] = {}
    mutation_env = dict(os.environ)
    existing_pythonpath = mutation_env.get("PYTHONPATH")
    _prepend_mutant_source_roots(mutation_env, existing_pythonpath)
    equivalents = _load_equivalent_allowlist()

    # mutmut reads paths_to_mutate from the ROOT pyproject.toml (not mutants/).
    # When --changed-only narrows the targets, rewrite the root config temporarily.
    root_pyproject = Path("pyproject.toml")
    root_original = root_pyproject.read_text(encoding="utf-8") if root_pyproject.exists() else None

    for attempt in range(1, attempts + 1):
        mutants_dir = Path("mutants")
        if mutants_dir.exists():
            shutil.rmtree(mutants_dir)
        _seed_mutants_config(paths_to_mutate=paths_to_mutate)
        _prepend_mutant_source_roots(mutation_env, existing_pythonpath)

        # mutmut reads config from the root pyproject.toml. Normalize it for
        # mutation runs (strip incompatible pytest args and optionally narrow paths).
        if root_original is not None:
            _sanitize_mutants_pyproject(root_pyproject, paths_to_mutate=paths_to_mutate, strip_workspace=False)

        children = max_children if attempt == 1 else 1
        print(f"Running mutation attempt {attempt}/{attempts} with max-children={children}")

        # mutmut returns 0 = all killed, 1 = survivors exist, 2+ = error.
        # Survivors are expected (equivalent mutants); only fail on real errors.
        cmd = _uv_mutmut_cmd(python_version, "run", "--max-children", str(children))
        print("+", " ".join(cmd))
        try:
            mutmut_result = subprocess.run(cmd, check=False, env=mutation_env)
        finally:
            # Restore root pyproject.toml (even on error so we never leave it modified)
            if root_original is not None:
                root_pyproject.write_text(root_original, encoding="utf-8")
        if mutmut_result.returncode > 1:
            raise RuntimeError(f"mutmut crashed (exit {mutmut_result.returncode})")

        _effective, last_stats = _collect_stats(python_version, mutation_env, equivalents)

        score = _mutation_score(last_stats)
        if last_stats["total"] > 0 and score >= min_mutation_score and last_stats["bad_total"] == 0:
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
