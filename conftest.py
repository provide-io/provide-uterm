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

if not os.environ.get("MUTANT_UNDER_TEST"):
    _ROOT = Path(__file__).resolve().parent
    _PACKAGE_SRCS = [
        _ROOT / "packages" / "provide-terminal" / "src",
        _ROOT / "packages" / "provide-terminal-server" / "src",
        _ROOT / "packages" / "provide-terminal-cloudflare" / "src",
        _ROOT / "packages" / "provide-terminal-platform" / "src",
        _ROOT / "packages" / "provide-terminal-client" / "src",
    ]
    for _src in reversed(_PACKAGE_SRCS):
        _src_str = str(_src)
        if _src_str in sys.path:
            sys.path.remove(_src_str)
        sys.path.insert(0, _src_str)

if os.environ.get("MUTANT_UNDER_TEST"):
    _here = Path(__file__).resolve().parent  # mutants/ when run by mutmut
    # Prepend mutated source copies so they take priority over the editable install.
    _inserted = False
    _src = _here / "src"
    if _src.exists() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
        _inserted = True
    # Flush already-imported provide modules so Python re-imports them from the
    # mutants copies above rather than the editable-install paths cached in
    # sys.modules before this conftest ran.
    if _inserted:
        for _mod in list(sys.modules):
            if _mod == "provide" or _mod.startswith("provide."):
                del sys.modules[_mod]

_here_for_reorder = Path(__file__).resolve().parent
_mutants_src = _here_for_reorder / "src"
if _mutants_src.exists():
    # Ensure mutants/src stays at the front of sys.path even if subsequent
    # conftest runs (e.g. clean-test phase with MUTANT_UNDER_TEST='') re-insert
    # mutants/packages/*/src paths ahead of it.  This guarantees the trampoline
    # module is imported instead of the original copy in mutants/packages/.
    _mutants_src_str = str(_mutants_src)
    if _mutants_src_str in sys.path:
        sys.path.remove(_mutants_src_str)
    sys.path.insert(0, _mutants_src_str)

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

    # On macOS, calling setproctitle() in a forked child causes SIGSEGV.
    # Register an at-fork handler (runs in the child before any user code)
    # to replace the setproctitle binding in mutmut's module namespace with
    # a no-op, so the child survives long enough to run the mutated tests.
    # The guard applies to all mutmut phases: "stats", "fail", and actual mutant names.
    def _noop_setproctitle_in_child() -> None:
        try:
            import mutmut.__main__ as _mm

            _mm.setproctitle = lambda _t: None  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass

    os.register_at_fork(after_in_child=_noop_setproctitle_in_child)
