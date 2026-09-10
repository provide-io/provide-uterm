#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""provide.uterm.pty must fail fast and clearly on Windows instead of crashing
mid-way through a submodule import (e.g. a bare ``ModuleNotFoundError: fcntl``)."""

from __future__ import annotations

import importlib
import sys

import pytest


def test_import_raises_clear_error_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        # Real Windows: the plain import itself already hits the guard, no
        # simulation needed (and there's no prior successful import to reload).
        with pytest.raises(ImportError, match="not available on Windows"):
            importlib.import_module("provide.uterm.pty")
        return

    module = importlib.import_module("provide.uterm.pty")
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        with pytest.raises(ImportError, match="not available on Windows"):
            importlib.reload(module)
    finally:
        monkeypatch.undo()
        importlib.reload(module)
