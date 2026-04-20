#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Memray stress test for ANSI color processing."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.memray.conftest import assert_allocation_within_threshold


@pytest.mark.memray
@pytest.mark.slow
def test_ansi_stress(memray_output_dir: Path, memray_baseline: dict[str, int], tmp_path: Path) -> None:
    """Stress test ANSI color processing with memray profiling."""
    script_path = Path(__file__).parent.parent.parent / "scripts" / "memray_ansi_stress.py"
    output_bin = memray_output_dir / "ansi_stress.bin"

    # Run script with memray
    result = subprocess.run(
        ["python", "-m", "memray", "run", "-o", str(output_bin), str(script_path)],
        cwd=str(Path(__file__).parent.parent.parent),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"memray run failed: {result.stderr}"

    # Parse stats to get total allocations
    stats_result = subprocess.run(
        ["python", "-m", "memray", "stats", str(output_bin)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert stats_result.returncode == 0, f"memray stats failed: {stats_result.stderr}"

    # Extract total allocations from stats output
    # Format: "Total allocations:  26,101,842"
    match = re.search(r"Total allocations:\s+([\d,]+)", stats_result.stdout)
    assert match, f"Could not parse allocations from memray stats:\n{stats_result.stdout}"
    total_allocations = int(match.group(1).replace(",", ""))

    # Compare to baseline
    baseline = memray_baseline.get("ansi_total_allocations")
    assert_allocation_within_threshold(baseline, total_allocations, "ANSI")

    # Update baseline if needed
    if baseline is None:
        memray_baseline["ansi_total_allocations"] = total_allocations
