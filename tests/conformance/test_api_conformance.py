#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Python <-> Go public API conformance: every symbol spec/uterm-api.yaml
marks required: true must exist in both languages' source, named per the
naming_conventions transform. See spec/validate_conformance.py for the
runnable standalone form (``uv run python spec/validate_conformance.py``)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC_DIR = _REPO_ROOT / "spec"


def _load_validator():
    """Import spec/validate_conformance.py by path (spec/ is not a package)."""
    if str(_SPEC_DIR) not in sys.path:
        sys.path.insert(0, str(_SPEC_DIR))
    spec_mod = importlib.util.spec_from_file_location(
        "uterm_validate_conformance", _SPEC_DIR / "validate_conformance.py"
    )
    module = importlib.util.module_from_spec(spec_mod)
    assert spec_mod.loader is not None
    spec_mod.loader.exec_module(module)
    return module


def test_python_go_api_conformance() -> None:
    """spec/uterm-api.yaml's required symbols exist in both Python and Go,
    named per naming_conventions -- fails loudly (with the missing symbol
    list) rather than silently drifting."""
    module = _load_validator()
    assert module.main() == 0
