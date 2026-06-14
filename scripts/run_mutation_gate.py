#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec
import sys
import tomllib
from pathlib import Path

try:
    from scripts.mutation_gate_config import (
        BAD_MUTANT_STATES,
        DEFAULT_EQUIVALENTS_FILE,
        DEFAULT_MUTATION_ROOTS,
        EXCUSABLE_STATES,
        MUTATION_SUPPORT_FILES,
    )
    from scripts.mutation_gate_config import (
        mutation_score as _mutation_score,
    )
    from scripts.mutation_gate_config import (
        sanitize_mutants_pyproject as _sanitize_mutants_pyproject,
    )
    from scripts.mutation_gate_config import (
        seed_mutants_config as _seed_mutants_config,
    )
    from scripts.mutation_gate_config import (
        state_counts_from as _state_counts_from,
    )
    from scripts.mutation_gate_config import (
        uv_mutmut_cmd as _uv_mutmut_cmd,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mutation_gate_config import (  # type: ignore[no-redef]
        BAD_MUTANT_STATES,
        DEFAULT_EQUIVALENTS_FILE,
        DEFAULT_MUTATION_ROOTS,
        EXCUSABLE_STATES,
        MUTATION_SUPPORT_FILES,
    )
    from mutation_gate_config import (
        mutation_score as _mutation_score,
    )
    from mutation_gate_config import (
        sanitize_mutants_pyproject as _sanitize_mutants_pyproject,
    )
    from mutation_gate_config import (
        seed_mutants_config as _seed_mutants_config,
    )
    from mutation_gate_config import (
        state_counts_from as _state_counts_from,
    )
    from mutation_gate_config import (
        uv_mutmut_cmd as _uv_mutmut_cmd,
    )


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


def _changed_paths(base_ref: str, staged_only: bool) -> list[str]:
    diff_cmd = ["git", "diff", "--name-only"]
    if staged_only:
        diff_cmd.append("--cached")
    else:
        diff_cmd.append(base_ref)
    diff_cmd.append("--")
    result = subprocess.run(diff_cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _changed_python_paths(base_ref: str, staged_only: bool, roots: tuple[str, ...]) -> list[str]:
    changed: list[str] = []
    for path in _changed_paths(base_ref, staged_only):
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


def _configured_mutation_tests() -> tuple[str, ...]:
    try:
        cfg = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    except Exception:
        return ()
    tests = cfg.get("tool", {}).get("mutmut", {}).get("tests_dir", [])
    return tuple(str(path) for path in tests)


def _changed_mutation_support_paths(changed_paths: list[str]) -> list[str]:
    support_files = set(MUTATION_SUPPORT_FILES)
    configured_tests = tuple(path.rstrip("/") for path in _configured_mutation_tests())
    support: list[str] = []
    for path in changed_paths:
        if path in support_files:
            support.append(path)
            continue
        if any(path == test_path or path.startswith(test_path + "/") for test_path in configured_tests):
            support.append(path)
    return sorted(set(support))


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

    Returns ``(effective, last_stats)``: the per-mutant ``(name, state)`` list still
    counting toward the gate (allowlisted equivalents removed), and the summary dict.
    """
    per_mutant = _results_per_mutant(python_version, env)
    effective, excused, stale = _apply_equivalent_allowlist(per_mutant, equivalents)
    # Only warn about stale entries whose module was actually mutated this run.
    # On a --changed-only / --paths run the allowlist legitimately carries
    # entries for out-of-scope files (other perimeter modules); those are not
    # stale, just unexercised — warning on them spammed every scoped CI run.
    in_scope = {name.rsplit(".x", 1)[0] for name, _ in per_mutant}
    for name in stale:
        if name.rsplit(".x", 1)[0] in in_scope:
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
    # Surface WHICH mutants survived so a failure is actionable straight from the
    # log (essential for connector.py, which only runs under Linux CI — it
    # fork-hangs on macOS, so the survivors can't be enumerated locally).
    bad = sorted(name for name, state in effective if state in BAD_MUTANT_STATES)
    if bad:
        print(f"surviving mutants ({len(bad)}):")
        for name in bad:
            print(f"  {name}")
    return effective, last_stats


def run_mutation_gate(
    python_version: str | None,
    max_children: int,
    retries: int,
    min_mutation_score: float,
    paths_to_mutate: list[str] | None = None,
    allow_empty: bool = False,
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

        # An explicitly-narrowed target (--paths / --changed-only) with zero mutants
        # is a file with no mutable surface (a Pydantic model, a re-export shim,
        # decorated-only dispatch, constant patterns) — legitimately clean, not a
        # config break. The total>0 guard only matters for the full pyproject
        # perimeter (where total==0 would mean paths_to_mutate is misconfigured).
        if allow_empty and last_stats["total"] == 0 and last_stats["bad_total"] == 0:
            print("mutation gate ok: explicitly-targeted file(s) have no mutable surface (0 mutants)")
            return last_stats

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
    parser.add_argument(
        "--paths",
        default=None,
        help=(
            "Comma-separated explicit subset of [tool.mutmut].paths_to_mutate to mutate "
            "(format matches paths_to_mutate, e.g. 'src/provide/uterm/auth.py'). Used to "
            "chunk a full-perimeter run into small, stable batches — a single mutmut run "
            "over the whole perimeter trips the fork-loop child-reaping crash at scale. "
            "Mutually exclusive with --changed-only."
        ),
    )
    args = parser.parse_args()
    half_cpus = _half_cpu_count()
    requested_children = args.max_children if args.max_children is not None else half_cpus
    max_children = min(max(1, requested_children), half_cpus)

    paths_to_mutate: list[str] | None = None
    changed_support_paths: list[str] = []
    if args.changed_only:
        paths_to_mutate = _changed_python_paths(args.base_ref, args.staged_only, DEFAULT_MUTATION_ROOTS)
        if not paths_to_mutate:
            changed_support_paths = _changed_mutation_support_paths(_changed_paths(args.base_ref, args.staged_only))
            if changed_support_paths:
                print(
                    "mutation gate full-perimeter trigger: changed mutation allowlist/config/tests "
                    f"without changed source mutants: {changed_support_paths}"
                )
                paths_to_mutate = None
            else:
                print("mutation gate skipped: no changed Python files under mutation roots")
                return 0
        else:
            print(f"mutation gate targets ({len(paths_to_mutate)}): {paths_to_mutate}")
    elif args.paths:
        paths_to_mutate = [p.strip() for p in args.paths.split(",") if p.strip()]
        if not paths_to_mutate:
            print("mutation gate skipped: --paths resolved to an empty set")
            return 0
        print(f"mutation gate targets ({len(paths_to_mutate)}): {paths_to_mutate}")

    try:
        run_mutation_gate(
            args.python_version,
            max_children,
            args.retries,
            args.min_mutation_score,
            paths_to_mutate=paths_to_mutate,
            # An explicitly-narrowed target set (--paths / --changed-only) may
            # legitimately contain only no-mutable-surface files → 0 mutants is OK.
            allow_empty=paths_to_mutate is not None and not changed_support_paths,
        )
    except RuntimeError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
