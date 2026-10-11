#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the wire-contract drift gate.

A drift gate is worth exactly what it catches, and this one has a specific way
of becoming worthless: every check returns early on an empty input, so a
mistyped path or a renamed constant turns the gate into a no-op that still
prints "passed". These exercise the failing side of each check, plus the
vacuity guard -- a declaration site whose constants can no longer be found must
be an error, not a silent skip.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCRIPT_PATH = _ROOT / "scripts" / "check_protocol_drift.py"
_spec = importlib.util.spec_from_file_location("check_protocol_drift", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_protocol_drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_protocol_drift)


def test_repository_is_currently_consistent() -> None:
    """The committed tree must pass, or every other test here is untrustworthy."""
    assert check_protocol_drift.check_versions() == []
    assert check_protocol_drift.check_twinned_fixtures() == []


def test_every_declaration_site_is_real() -> None:
    """Each configured site must actually yield three bounds.

    Without this, renaming a constant would make its site silently contribute
    nothing and the agreement check would still pass on the survivors.
    """
    for label, relative, patterns in check_protocol_drift.VERSION_SITES:
        path = check_protocol_drift.ROOT / relative
        assert path.is_file(), f"{label}: {relative} does not exist"
        text = path.read_text(encoding="utf-8")
        for bound, pattern in patterns.items():
            assert re.search(pattern, text, re.MULTILINE) is not None, f"{label}: {bound} pattern matched nothing"


def test_version_disagreement_is_reported(monkeypatch, tmp_path: Path) -> None:
    """One port bumping only half of its declarations must fail."""
    agreeing = tmp_path / "agree.txt"
    agreeing.write_text("MIN = 1\nMAX = 1\nPREF = 1\n", encoding="utf-8")
    drifted = tmp_path / "drift.txt"
    drifted.write_text("MIN = 1\nMAX = 2\nPREF = 1\n", encoding="utf-8")

    patterns = {"min": r"MIN = (\d+)", "max": r"MAX = (\d+)", "preferred": r"PREF = (\d+)"}
    monkeypatch.setattr(check_protocol_drift, "ROOT", tmp_path)
    monkeypatch.setattr(
        check_protocol_drift,
        "VERSION_SITES",
        (("first", "agree.txt", patterns), ("second", "drift.txt", patterns)),
    )

    errors = check_protocol_drift.check_versions()

    assert len(errors) == 1
    assert "protocol-version drift" in errors[0]
    assert "second" in errors[0]


def test_missing_constant_is_reported(monkeypatch, tmp_path: Path) -> None:
    """A renamed constant fails rather than silently dropping its site."""
    renamed = tmp_path / "renamed.txt"
    renamed.write_text("MINIMUM = 1\nMAX = 1\nPREF = 1\n", encoding="utf-8")

    monkeypatch.setattr(check_protocol_drift, "ROOT", tmp_path)
    monkeypatch.setattr(
        check_protocol_drift,
        "VERSION_SITES",
        (("only", "renamed.txt", {"min": r"MIN = (\d+)", "max": r"MAX = (\d+)", "preferred": r"PREF = (\d+)"}),),
    )

    errors = check_protocol_drift.check_versions()

    assert any("no min protocol-version declaration" in error for error in errors)


def test_twinned_fixture_drift_is_reported(monkeypatch, tmp_path: Path) -> None:
    """Two committed copies of one corpus must stay byte-identical."""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text('{"x": 1}', encoding="utf-8")
    second.write_text('{"x": 1} ', encoding="utf-8")

    monkeypatch.setattr(check_protocol_drift, "ROOT", tmp_path)
    monkeypatch.setattr(check_protocol_drift, "TWINNED_FIXTURES", (("a.json", "b.json"),))

    errors = check_protocol_drift.check_twinned_fixtures()

    assert len(errors) == 1
    assert "twinned fixture drift" in errors[0]


def test_identical_twins_pass(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text('{"x": 1}', encoding="utf-8")
    second.write_text('{"x": 1}', encoding="utf-8")

    monkeypatch.setattr(check_protocol_drift, "ROOT", tmp_path)
    monkeypatch.setattr(check_protocol_drift, "TWINNED_FIXTURES", (("a.json", "b.json"),))

    assert check_protocol_drift.check_twinned_fixtures() == []


def test_csharp_copies_of_typescript_goldens_are_derived() -> None:
    """The derived pairs must be real, or the check is vacuous.

    The six named here drifted while nothing compared them; stream_redaction was
    the one pair listed by hand before the pairs were derived.
    """
    pairs = {Path(copy).name: (source, copy) for source, copy in check_protocol_drift.csharp_copies_of_ts_goldens()}
    assert len(pairs) > 100
    for name in (
        "chaos_golden.json",
        "guiroutes_golden.json",
        "serverjwt_golden.json",
        "session_logger_golden.json",
        "telnet_transport_golden.json",
        "ws_transport_golden.json",
        "stream_redaction_golden.json",
        "annotation_golden.json",
    ):
        source, copy = pairs[name]
        assert source == f"{check_protocol_drift.TS_TESTDATA}/{name}"
        assert copy == f"{check_protocol_drift.CSHARP_TESTDATA}/{name}"
    # A C# corpus with no TypeScript source is not a copy.
    assert "read_path_golden.json" not in pairs


def test_drift_in_a_derived_csharp_copy_is_reported(monkeypatch, tmp_path: Path) -> None:
    ts_dir = tmp_path / check_protocol_drift.TS_TESTDATA
    cs_dir = tmp_path / check_protocol_drift.CSHARP_TESTDATA
    ts_dir.mkdir(parents=True)
    cs_dir.mkdir(parents=True)
    (ts_dir / "x_golden.json").write_text('{"x": 2}', encoding="utf-8")
    (cs_dir / "x_golden.json").write_text('{"x": 1}', encoding="utf-8")
    (cs_dir / "only_here_golden.json").write_text("{}", encoding="utf-8")
    (cs_dir / "x.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(check_protocol_drift, "ROOT", tmp_path)
    monkeypatch.setattr(check_protocol_drift, "TWINNED_FIXTURES", ())

    assert check_protocol_drift.csharp_copies_of_ts_goldens() == (
        (f"{check_protocol_drift.TS_TESTDATA}/x_golden.json", f"{check_protocol_drift.CSHARP_TESTDATA}/x_golden.json"),
    )
    errors = check_protocol_drift.check_twinned_fixtures()
    assert len(errors) == 1
    assert "twinned fixture drift" in errors[0]
    assert "x_golden.json" in errors[0]

    (cs_dir / "x_golden.json").write_text('{"x": 2}', encoding="utf-8")
    assert check_protocol_drift.check_twinned_fixtures() == []


def test_protocol_change_without_doc_is_reported(monkeypatch) -> None:
    """The half of the gate that a passing local run cannot demonstrate."""
    monkeypatch.setattr(
        check_protocol_drift,
        "_changed_files",
        lambda ref: ["spec/behavior.json", "packages/provide-uterm-go/bridge/contracts.go"],
    )

    errors = check_protocol_drift.check_docs_followed("origin/main")

    assert len(errors) == 1
    assert "protocol sources changed without the protocol matrix" in errors[0]
    assert "spec/behavior.json" in errors[0]


def test_protocol_change_with_doc_passes(monkeypatch) -> None:
    monkeypatch.setattr(
        check_protocol_drift,
        "_changed_files",
        lambda ref: ["spec/behavior.json", "docs/protocol-matrix.md"],
    )

    assert check_protocol_drift.check_docs_followed("origin/main") == []


def test_unrelated_change_needs_no_doc(monkeypatch) -> None:
    """The gate must not fire on a diff that touches no wire contract."""
    monkeypatch.setattr(check_protocol_drift, "_changed_files", lambda ref: ["README.md"])

    assert check_protocol_drift.check_docs_followed("origin/main") == []


def test_undiffable_ref_is_an_error_not_a_pass(monkeypatch) -> None:
    """A shallow clone with no base ref must fail loudly, not skip the check."""

    def _raise(ref: str) -> list[str]:
        raise RuntimeError("unknown revision")

    monkeypatch.setattr(check_protocol_drift, "_changed_files", _raise)

    errors = check_protocol_drift.check_docs_followed("origin/main")

    assert len(errors) == 1
    assert "could not diff" in errors[0]
