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

PROCESS_MANAGER_SOURCE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "src/provide/uterm/manager/process_impl.py",
        "src/provide/uterm/manager/process_impl_spawn.py",
    }
)

PROCESS_MANAGER_MUTATION_TESTS: Final[tuple[str, ...]] = (
    "packages/provide-uterm-platform/tests/manager/manager/test_process_mutation_killing.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_mutation_killing_2.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_mutation_killing_3.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_mutation_killing_4.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_impl_survivors.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part01.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part02.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part03.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part04.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part05.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part06.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part07.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part08.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_kill_part09.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_additional.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_extra.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_process_worker_token_scope.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_coverage_process.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_coverage_process_windows.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_coverage_monitor.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_ext_policy.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_agent_ops_mutation_killing.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_agent_ops_mutation_killing_2.py",
    "packages/provide-uterm-platform/tests/manager/manager/test_coverage_gaps.py",
)

BRIDGE_COORDINATOR_SOURCE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "src/provide/uterm/bridge/coordinator.py",
    }
)

BRIDGE_HUB_SOURCE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "src/provide/uterm/server/bridge/hub/presence.py",
        "src/provide/uterm/server/bridge/hub/store.py",
        "src/provide/uterm/server/bridge/hub/polling_service.py",
        "src/provide/uterm/deckmux/_service.py",
        "src/provide/uterm/bridge/schemas.py",
        *BRIDGE_COORDINATOR_SOURCE_PATHS,
    }
)

# The test selection used when a run is SCOPED to the bridge-hub perimeter
# (--paths / --changed-only). It is a deliberately narrow subset of the root
# pyproject's ``pytest_add_cli_args_test_selection``, not a mirror of it.
#
# That makes it a trap worth stating plainly: wiring a hub kill suite into
# pyproject.toml is NOT enough. A suite missing from this tuple still runs on a
# full-perimeter run and is silently dropped on a scoped one, so its mutants
# come back as phantom survivors on exactly the path a normal push takes. The
# failure is closed, not open — the gate goes red for mutants that are in fact
# covered — which reads as a regression in the source rather than a gap here.
# Seen on 2026-08-15: test_snapshot_diagnostics_kill.py was wired in pyproject
# only, and a scoped polling_service.py run reported 48 survivors against the
# full run's 16.
BRIDGE_HUB_MUTATION_TESTS: Final[tuple[str, ...]] = (
    "packages/provide-uterm/tests/deckmux/test_presence.py",
    "packages/provide-uterm/tests/deckmux/test_hub_mixin.py",
    "packages/provide-uterm/tests/deckmux/test_transfer.py",
    "packages/provide-uterm/tests/deckmux/test_protocol.py",
    "packages/provide-uterm/tests/deckmux/test_names.py",
    "packages/provide-uterm/tests/deckmux/test_edge.py",
    "packages/provide-uterm/tests/deckmux/test_service_mutants.py",
    "packages/provide-uterm-server/tests/bridge/test_presence_protocol.py",
    "packages/provide-uterm/tests/bridge/test_coordinator_units.py",
    "packages/provide-uterm-server/tests/bridge/test_hub_polling_coverage.py",
    "packages/provide-uterm-server/tests/bridge/hub/test_polling_kill.py",
    "packages/provide-uterm-server/tests/bridge/hub/test_store_kill.py",
    "packages/provide-uterm-server/tests/bridge/hub/test_store_policy_kill.py",
    # Covers both halves of the 2163d535 diagnostics: presence.request_snapshot
    # (snapshot_req_undelivered) and polling_service.wait_for_snapshot
    # (snapshot_wait_timeout), so it is in scope for two of the source paths above.
    "packages/provide-uterm-server/tests/bridge/hub/test_snapshot_diagnostics_kill.py",
)


# Perimeter files that legitimately generate ZERO mutants, and why.
#
# A `--paths` run over a file with no mutable surface exits 0 with score 0.00.
# That is correct for a re-export shim, but it means a perimeter entry can sit
# in source_paths enforcing nothing while its leg reports success -- the file
# reads as covered by the strongest gate in the repo when it is covered by
# none of it. Blanket-accepting every empty target hides that; requiring the
# file to be named here turns a silent hole into a stated decision.
#
# The gate FAILS on an undeclared zero-mutant target. If a file lands here,
# check first whether it just became invisible: mutmut skips any decorated
# CLASS outright (mutation/file_mutation.py -- "if isinstance(node,
# cst.ClassDef) and len(node.decorators): return True"), so putting
# @dataclass on a class removes every one of its methods from the gate
# without changing a line of their logic.
KNOWN_ZERO_MUTANT_PATHS: Final[dict[str, str]] = {
    "src/provide/uterm/server/app/factory.py": "11-line re-export shim; no statements to mutate.",
    "src/provide/uterm/server/bridge/hub/router.py": "11-line re-export shim; no statements to mutate.",
    "src/provide/uterm/bridge/schemas.py": (
        "35 Pydantic model declarations and no function bodies -- the same 0-mutant shape as "
        "manager/config.py. Wire-format drift is caught by codegen_frames.py --check instead."
    ),
    "src/provide/uterm/control_channel_patterns.py": (
        "Both classes are @dataclass, and mutmut skips decorated classes entirely, so all seven "
        "methods are invisible to it -- including LinkPattern.__post_init__, which is the check "
        "that stops a link pattern declaring an action outside _VALID_ACTIONS. NOT equivalent to "
        "being safe: that logic is enforced by test_control_channel_patterns.py alone, with no "
        "mutation backstop. Kept in the perimeter deliberately so this entry has to be read."
    ),
}


def undeclared_zero_mutant_paths(source_paths: list[str] | None) -> list[str]:
    """Return targets that produced no mutants without being declared above."""
    if not source_paths:
        return []
    return [path for path in source_paths if path not in KNOWN_ZERO_MUTANT_PATHS]


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


def scoped_test_selection(source_paths: list[str] | None) -> tuple[str, ...] | None:
    if not source_paths:
        return None
    selected = set(source_paths)
    if selected and selected <= PROCESS_MANAGER_SOURCE_PATHS:
        return PROCESS_MANAGER_MUTATION_TESTS
    if selected and selected <= BRIDGE_HUB_SOURCE_PATHS:
        return BRIDGE_HUB_MUTATION_TESTS
    return None


def seed_mutants_config(
    source_paths: list[str] | None = None,
    test_selection: tuple[str, ...] | None = None,
) -> None:
    mutants = Path("mutants")
    mutants.mkdir(parents=True, exist_ok=True)
    for config_name in CONFIG_FILES:
        src = Path(config_name)
        if src.exists():
            shutil.copy2(src, mutants / config_name)
    sanitize_mutants_pyproject(mutants / "pyproject.toml", source_paths=source_paths, test_selection=test_selection)


def sanitize_mutants_pyproject(
    path: Path,
    *,
    source_paths: list[str] | None,
    test_selection: tuple[str, ...] | None = None,
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
    if source_paths:
        encoded = ", ".join(f'"{item}"' for item in source_paths)
        updated, count = re.subn(
            r"^source_paths\s*=\s*\[[\s\S]*?\]",
            f"source_paths = [{encoded}]",
            updated,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise RuntimeError("failed to rewrite source_paths in mutants/pyproject.toml")
    if test_selection:
        encoded_tests = ",\n".join(f'    "{item}"' for item in test_selection)
        updated, count = re.subn(
            r"^pytest_add_cli_args_test_selection\s*=\s*\[[\s\S]*?^\]",
            f"pytest_add_cli_args_test_selection = [\n{encoded_tests}\n]",
            updated,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise RuntimeError("failed to rewrite pytest_add_cli_args_test_selection in mutants/pyproject.toml")
    if updated != text:
        path.write_text(updated, encoding="utf-8")
