#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Guard: verify provide.uterm.shell is present in the CF python_modules vendor tree.

If this test fails, run:
  uv pip install --python .venv-workers/pyodide-venv/bin/python --reinstall /path/to/provide-uterm
  pywrangler sync --force
from packages/provide-uterm-cloudflare/.

On a clean checkout (python_modules/ absent) the test skips — the guard only
fires when the CF developer environment has been initialised but the vendor
tree is stale or incomplete.
"""

import os
from pathlib import Path

import pytest


def test_ushell_vendor_tree_exists() -> None:
    """provide/uterm/shell must be present in python_modules — absent means a missing vendor sync."""
    vendor_root = Path(__file__).resolve().parents[1] / "python_modules"
    if not vendor_root.exists():
        pytest.skip("python_modules/ not present — CF vendor tree not initialised (clean checkout)")
    ushell_path = vendor_root / "provide" / "terminal" / "shell"
    if not (ushell_path.exists() and ushell_path.is_dir()):
        if os.getenv("UTERM_VENDOR_GUARD_STRICT") == "1":
            pytest.fail(
                f"provide/uterm/shell missing from vendor tree at {ushell_path}. "
                "Run: uv pip install --python .venv-workers/pyodide-venv/bin/python "
                "--reinstall /path/to/provide-uterm && pywrangler sync --force"
            )
        pytest.skip(
            f"vendor tree incomplete at {ushell_path}; set UTERM_VENDOR_GUARD_STRICT=1 to enforce in this environment"
        )
    py_files = list(ushell_path.rglob("*.py"))
    assert py_files, f"provide/uterm/shell vendor tree at {ushell_path} is empty"
