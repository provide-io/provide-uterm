#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
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

    # mutmut calls set_start_method('fork') in the parent process; the forked
    # pytest worker inherits the already-set context, so the trampoline's second
    # call to set_start_method('fork') raises RuntimeError.  Suppress it.
    # multiprocessing.set_start_method is a module-level bound method captured
    # at import time, so we must patch the module attribute directly.
    import multiprocessing as _mp

    _orig_set_start = _mp.set_start_method

    def _safe_set_start(method: str, force: bool = False) -> None:
        import contextlib

        with contextlib.suppress(RuntimeError):
            _orig_set_start(method, force=force)

    _mp.set_start_method = _safe_set_start  # type: ignore[attr-defined]
