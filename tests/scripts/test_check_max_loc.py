#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the max-LOC gate.

The gate is only useful if it fails on the things it claims to catch, so these
exercise the three behaviours that matter: a new oversized file in each covered
language, a baselined file that grows, and the exclusions.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCRIPT_PATH = _ROOT / "scripts" / "check_max_loc.py"
_spec = importlib.util.spec_from_file_location("check_max_loc", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_max_loc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_max_loc)


def _write(path: Path, lines: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// x\n" * lines, encoding="utf-8")
    return path


def test_scans_python_csharp_and_go(tmp_path: Path) -> None:
    """All three covered languages are measured by the same cap."""
    _write(tmp_path / "big.py", 800)
    _write(tmp_path / "big.cs", 800)
    _write(tmp_path / "big.go", 800)
    _write(tmp_path / "small.cs", 10)

    offenders = check_max_loc.find_loc_offenders([tmp_path], max_lines=777)
    names = sorted(p.name for p, _ in offenders)
    assert names == ["big.cs", "big.go", "big.py"]


def test_ignores_unlisted_suffixes(tmp_path: Path) -> None:
    """A language outside the configured suffixes is not measured."""
    _write(tmp_path / "big.ts", 800)
    assert check_max_loc.find_loc_offenders([tmp_path], max_lines=777) == []


def test_excludes_build_output_and_vendored_trees(tmp_path: Path) -> None:
    """Generated and third-party trees are skipped: a cap on them is noise."""
    for part in ("bin", "obj", "vendor", "node_modules", ".venv"):
        _write(tmp_path / part / "big.cs", 800)

    assert check_max_loc.find_loc_offenders([tmp_path], max_lines=777) == []


def test_reports_the_actual_line_count(tmp_path: Path) -> None:
    _write(tmp_path / "big.go", 900)
    ((path, lines),) = check_max_loc.find_loc_offenders([tmp_path], max_lines=777)
    assert path.name == "big.go"
    assert lines == 900


def test_missing_root_is_not_an_error(tmp_path: Path) -> None:
    assert check_max_loc.find_loc_offenders([tmp_path / "absent"], max_lines=777) == []


def test_baseline_permits_recorded_size_but_not_growth(tmp_path: Path) -> None:
    """The ratchet is the point: listed files may shrink, never grow."""
    target = _write(tmp_path / "legacy.cs", 800)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"allow_over_limit": {str(target): 800}}), encoding="utf-8")

    loaded = check_max_loc._load_baseline(baseline)
    assert loaded[str(target)] == 800

    # At the recorded size the file is tolerated...
    offenders = check_max_loc.find_loc_offenders([tmp_path], max_lines=777)
    assert [p for p, _ in offenders] == [target]
    assert all(lines <= loaded[str(p)] for p, lines in offenders)

    # ...but one line more is a new offence.
    _write(target, 801)
    grown = check_max_loc.find_loc_offenders([tmp_path], max_lines=777)
    assert any(lines > loaded[str(p)] for p, lines in grown)


def test_baseline_absent_or_malformed_yields_no_allowances(tmp_path: Path) -> None:
    assert check_max_loc._load_baseline(tmp_path / "missing.json") == {}

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"allow_over_limit": "not-a-mapping"}), encoding="utf-8")
    assert check_max_loc._load_baseline(bad) == {}

    wrong_types = tmp_path / "wrong.json"
    wrong_types.write_text(json.dumps({"allow_over_limit": {"a.cs": "800", "b.cs": 800}}), encoding="utf-8")
    assert check_max_loc._load_baseline(wrong_types) == {"b.cs": 800}


def test_repo_baseline_entries_still_exist(tmp_path: Path) -> None:
    """A stale waiver is worse than none: it hides that the debt was paid.

    Every baselined path must still be present, so deleting or renaming a file
    forces its entry to be cleaned up too.
    """
    baseline = check_max_loc._load_baseline(_ROOT / ".ci" / "max-loc-baseline.json")
    assert baseline, "expected the repo baseline to be populated"
    missing = [key for key in baseline if not (_ROOT / key).exists()]
    assert missing == [], f"baseline lists files that no longer exist: {missing}"
