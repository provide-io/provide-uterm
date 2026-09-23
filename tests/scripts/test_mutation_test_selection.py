#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Every scoped mutation test list must be a subset of the root selection.

``scoped_test_selection`` only returns a scoped list when *every* selected
source path belongs to that one group. A changed-only run that mixes groups --
say ``rest_gui.py`` (bridge hub) with ``connector.py`` (not in any group) --
gets ``None`` and falls back to root ``[tool.mutmut]
pytest_add_cli_args_test_selection``. A suite listed only in the scoped tuple
is then silently dropped, and its file's mutants come back as ``no tests``.

That was live: the four ``rest_gui`` suites were added to
``BRIDGE_HUB_MUTATION_TESTS`` in 285f3b23 but never to the root list. Every
single-file leg (and every ``--paths`` run of that file alone) was green, while
a five-file changed-only run scored 93.77 with all 97 ``rest_gui`` mutants
untested. ``no tests`` is not in the gate's stats dict, so the failure printed
``survived: 0`` and an empty survivor list.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import mutation_gate_config

_SCOPED_LISTS = ("BRIDGE_HUB_MUTATION_TESTS", "PROCESS_MANAGER_MUTATION_TESTS")


def _root_selection() -> set[str]:
    config = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {str(entry) for entry in config["tool"]["mutmut"]["pytest_add_cli_args_test_selection"]}


@pytest.mark.parametrize("name", _SCOPED_LISTS)
def test_scoped_list_is_subset_of_root_selection(name: str) -> None:
    missing = sorted(set(getattr(mutation_gate_config, name)) - _root_selection())
    assert missing == [], f"{name} suites absent from root pytest_add_cli_args_test_selection: {missing}"
