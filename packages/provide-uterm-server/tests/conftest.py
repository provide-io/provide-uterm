#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


@pytest.fixture(autouse=True, scope="session")
def _no_ambient_ssh_agent() -> Iterator[None]:
    """Hide the developer's SSH agent from the suite.

    asyncssh's client offers every identity the agent holds before it reaches
    the key a test configured. Against these stub servers that negotiation never
    completes, so the connection sits in auth until the timeout fires. Nine
    tests failed this way -- five in tests/e2e/test_ssh_gateway_start.py, four
    across the two test_app modules -- taking five minutes a run on any machine
    with gnome-keyring or ssh-agent running.

    CI has no agent, so CI never saw it, and the failures read as a local
    environment missing something when what it had was one thing too many.

    Nothing here should consult an agent: every SSH test supplies its own key.
    """

    previous = os.environ.pop("SSH_AUTH_SOCK", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["SSH_AUTH_SOCK"] = previous


def _load_part(name: str) -> None:
    part_path = _HERE / name
    module_name = f"{__package__}.{part_path.stem}" if __package__ else f"{__name__}.{part_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, part_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load split test module: {part_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    for key, value in vars(module).items():
        if key.startswith("__") and key not in {"__doc__"}:
            continue
        globals().setdefault(key, value)


_load_part("conftest_part1.py")
_load_part("conftest_part2.py")


# --- Hypothesis: shared profiles + one repo-root example database -----------
# This package carries its own [tool.pytest.ini_options], so pytest sets rootdir
# to the package directory and never loads the repo-root conftest — hence the
# duplicated hook. Skips itself inside mutmut's mutants/ tree (the profiles
# module is not copied there). See hypothesis_profiles.py for the rationale.
def _activate_hypothesis_profiles(repo_root: Path) -> None:
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


_activate_hypothesis_profiles(_HERE.parents[2])
