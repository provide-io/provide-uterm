#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Emit ``[tool.mutmut].paths_to_mutate`` as a JSON array for the full-gate CI matrix.

The full-perimeter mutation gate fans out one matrix job per perimeter file: a
single ``mutmut run`` over the whole ~6k-mutant perimeter trips the fork-loop
child-reaping crash on small (2-worker) runners and mass-reports ``not checked``
(observed dying at ~1540/6227), whereas per-file runs (<=~700 mutants each) are
stable — exactly the batches the ``--changed-only`` gate already runs cleanly.

Reads the perimeter from ``pyproject.toml`` so the file list has one source of
truth. Prints a compact JSON array to stdout, e.g. ``["src/.../auth.py", ...]``,
for consumption via ``fromJson`` in a GitHub Actions ``strategy.matrix``.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

# Perimeter files that cannot be mutated under mutmut on a low-core CI runner and
# are therefore omitted from the SCHEDULED matrix (they still run locally in a
# full-perimeter pass, and via --changed-only on a many-core host when touched).
#
#   pty/connector.py — its PTY tests (test_connector*/test_capture_connector*)
#   spawn subprocesses that leak into mutmut's fork-loop os.wait() and mass-report
#   "not checked" (reproduced deterministically across sharded runs; passes only
#   on many-core local hosts). manager/process_impl survives the same hazard
#   because the manager-dir conftest mocks subprocess during mutation — pty has no
#   such mock yet. FOLLOW-UP: add a pty-test subprocess mock keyed on
#   MUTANT_UNDER_TEST (mirroring the manager conftest), then drop this exclusion.
_CI_EXCLUDE: frozenset[str] = frozenset(
    {
        "src/provide/uterm/pty/connector.py",
    }
)


def main() -> int:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    paths = [p for p in data["tool"]["mutmut"]["paths_to_mutate"] if p not in _CI_EXCLUDE]
    print(json.dumps(paths, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
