#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Root conftest — copied by mutmut to mutants/conftest.py.

When mutmut runs pytest from mutants/, this file ensures that
source imports resolve to the mutated copies rather than the
editable install in .venv.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if os.environ.get("MUTANT_UNDER_TEST"):
    _here = Path(__file__).resolve().parent  # mutants/ when run by mutmut
    _mutated_src = _here / "src"
    if _mutated_src.exists():
        # Prepend mutants/src so mutated copies take priority over the editable install.
        sys.path.insert(0, str(_mutated_src))


# --- Hypothesis: shared profiles + one repo-root example database -----------
# CI and run_all_tests.py run this package a second time via
# ``uv run --directory packages/provide-uterm pytest`` (for its own 100%
# coverage gate), which makes this the rootdir and skips the repo-root
# conftest. Load the same profiles here so both invocations share one corpus
# instead of writing two cwd-relative ones. See hypothesis_profiles.py.
def _activate_hypothesis_profiles(repo_root: Path) -> None:
    import importlib.util

    module_path = repo_root / "hypothesis_profiles.py"
    if not module_path.exists():
        return
    spec = importlib.util.spec_from_file_location("uterm_hypothesis_profiles", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.activate()


_activate_hypothesis_profiles(Path(__file__).resolve().parents[2])
