#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for spec/_conformance_extractors.py -- exercised directly (not
just through the end-to-end validate_conformance run) so a broken extractor
shows up as a specific, localized failure."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC_DIR = _REPO_ROOT / "spec"
if str(_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(_SPEC_DIR))

from _conformance_extractors import (
    get_go_exports,
    get_python_exports,
    to_pascal_case,
)


def test_to_pascal_case_plain_words() -> None:
    assert to_pascal_case("connect_telnet") == "ConnectTelnet"
    assert to_pascal_case("wait_for_screen_change") == "WaitForScreenChange"


def test_to_pascal_case_known_acronyms() -> None:
    assert to_pascal_case("ansi_screen") == "ANSIScreen"


def test_get_python_exports_session_includes_methods_and_functions() -> None:
    names = get_python_exports("session", repo_root=_REPO_ROOT)
    # Method on TransportSession, module function from telnet_session.py.
    assert "wait_for_screen_change" in names
    assert "connect_telnet" in names
    # Private helpers must not leak in.
    assert not any(n.startswith("_") for n in names)


def test_get_python_exports_unknown_category_is_empty() -> None:
    assert get_python_exports("does_not_exist", repo_root=_REPO_ROOT) == set()


def test_get_go_exports_session_includes_type_methods_and_functions() -> None:
    names = get_go_exports("session", repo_root=_REPO_ROOT)
    assert "TransportSession" in names
    assert "WaitForScreenChange" in names
    assert "ConnectTelnet" in names


def test_get_go_exports_missing_type_is_absent_not_crashing() -> None:
    """A nonexistent Go dir/type must yield an empty set, not raise."""
    assert get_go_exports("session", repo_root=_REPO_ROOT / "nonexistent-dir-xyz") == set()
