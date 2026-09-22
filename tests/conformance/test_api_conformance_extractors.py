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
    get_csharp_exports,
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


def test_transport_close_exports_in_every_language() -> None:
    """The typed-close surface (issue #102) is found where each port declares it."""
    python = get_python_exports("transport_close", repo_root=_REPO_ROOT)
    assert {"CloseInitiator", "TransportClose", "TransportClosedError", "summary", "close_info", "last_close"} <= python
    go = get_go_exports("transport_close", repo_root=_REPO_ROOT)
    assert {"CloseInitiator", "TransportClose", "TransportClosedError", "Summary", "CloseInfo", "LastClose"} <= go
    csharp = get_csharp_exports("transport_close", repo_root=_REPO_ROOT)
    assert {
        "CloseInitiator",
        "TransportClose",
        "TransportClosedException",
        "Summary",
        "CloseInfo",
        "ReconnectingTransport",
        "LastClose",
    } <= csharp


def _csharp_session_tree(tmp_path: Path, source: str) -> Path:
    directory = tmp_path / "packages" / "provide-uterm-csharp" / "src" / "Provide.Uterm" / "TermSession"
    directory.mkdir(parents=True)
    (directory / "TransportSession.cs").write_text(source, encoding="utf-8")
    return tmp_path


def test_csharp_properties_count_and_keyword_lines_do_not(tmp_path: Path) -> None:
    """A C# property is how a Python @property is spelled, so it satisfies the spec name."""
    root = _csharp_session_tree(
        tmp_path,
        "public sealed class TransportSession\n{\n"
        "    public TransportClose? CloseInfo\n    {\n        get { return null; }\n    }\n"
        "    public int Rows => 25;\n"
        '    public const string Kind = "x";\n'
        "    public void Close() { }\n}\n",
    )
    names = get_csharp_exports("session", repo_root=root)
    assert {"TransportSession", "CloseInfo", "Rows", "Close"} <= names
    assert "Kind" not in names


def test_a_csharp_record_or_enum_is_a_type(tmp_path: Path) -> None:
    directory = tmp_path / "packages" / "provide-uterm-csharp" / "src" / "Provide.Uterm" / "Transports"
    directory.mkdir(parents=True)
    (directory / "TransportClose.cs").write_text(
        "public enum CloseInitiator { Local }\n"
        "public sealed record TransportClose(CloseInitiator Initiator)\n{\n"
        '    public string Summary() => "";\n}\n',
        encoding="utf-8",
    )
    names = get_csharp_exports("transport_close", repo_root=tmp_path)
    assert {"CloseInitiator", "TransportClose", "Summary"} <= names
    # Nothing declares the exception type in this tree, so it must be missing.
    assert "TransportClosedException" not in names


def test_a_csharp_session_without_close_info_is_missing_it(tmp_path: Path) -> None:
    root = _csharp_session_tree(tmp_path, "public sealed class TransportSession\n{\n    public void Close() { }\n}\n")
    assert "CloseInfo" not in get_csharp_exports("transport_close", repo_root=root)
