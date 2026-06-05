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
# are therefore omitted from the SCHEDULED matrix. Currently empty: pty/connector.py
# used to crash here (its real-fork integration tests leak children into mutmut's
# fork-loop os.wait()), but the pty conftest now SKIPS those real-fork tests during a
# mutation run and test_connector_mutation_mocked.py reproduces their mutant-killing
# coverage fork-free, so connector is gated normally again.
_CI_EXCLUDE: frozenset[str] = frozenset()


def main() -> int:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    paths = [p for p in data["tool"]["mutmut"]["paths_to_mutate"] if p not in _CI_EXCLUDE]
    print(json.dumps(paths, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
