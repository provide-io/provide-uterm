#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Memray stress test for ControlChannel gateway processing."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.memray.conftest import assert_allocation_within_threshold


@pytest.mark.memray
@pytest.mark.slow
def test_gateway_stress(memray_output_dir: Path, memray_baseline: dict[str, int]) -> None:
    """Stress test ControlChannel encode/decode with memray profiling."""
    script_path = Path(__file__).parent.parent.parent / "scripts" / "memray_gateway_stress.py"
    output_bin = memray_output_dir / "gateway_stress.bin"

    # Run script with memray
    result = subprocess.run(
        ["python", "-m", "memray", "run", "--force", "-o", str(output_bin), str(script_path)],
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
    match = re.search(r"Total allocations:\s+([\d,]+)", stats_result.stdout)
    assert match, f"Could not parse allocations from memray stats:\n{stats_result.stdout}"
    total_allocations = int(match.group(1).replace(",", ""))

    # Compare to baseline
    baseline = memray_baseline.get("controlstream_total_allocations")
    assert_allocation_within_threshold(baseline, total_allocations, "ControlChannel")

    # Update baseline if needed
    if baseline is None:
        memray_baseline["controlstream_total_allocations"] = total_allocations
