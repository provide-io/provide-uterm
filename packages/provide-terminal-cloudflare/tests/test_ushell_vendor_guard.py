#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Guard: verify provide.shell is present in the CF python_modules vendor tree.

If this test fails, run:
  uv pip install --python .venv-workers/pyodide-venv/bin/python --reinstall /path/to/provide-shell
  pywrangler sync --force
from packages/provide-terminal-cloudflare/.
"""

from pathlib import Path


def test_ushell_vendor_tree_exists() -> None:
    """provide/shell must be present in python_modules — absent means a missing vendor sync."""
    vendor_root = Path(__file__).resolve().parents[1] / "python_modules"
    ushell_path = vendor_root / "provide" / "shell"
    assert vendor_root.exists(), (
        "python_modules/ directory not found — run pywrangler sync from packages/provide-terminal-cloudflare/"
    )
    assert ushell_path.exists() and ushell_path.is_dir(), (
        f"provide/shell missing from vendor tree at {ushell_path}. "
        "Run: uv pip install --python .venv-workers/pyodide-venv/bin/python "
        "--reinstall /path/to/provide-shell && pywrangler sync --force"
    )
    py_files = list(ushell_path.rglob("*.py"))
    assert py_files, f"provide/shell vendor tree at {ushell_path} is empty"
