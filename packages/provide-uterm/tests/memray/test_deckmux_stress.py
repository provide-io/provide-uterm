#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Memray stress test for DeckMux per-connection presence cycle."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from .conftest import assert_allocation_within_threshold


@pytest.mark.memray
@pytest.mark.slow
def test_deckmux_stress(memray_output_dir: Path, memray_baseline: dict[str, int]) -> None:
    """Stress test DeckMux presence: 1k connections, full identity + presence cycle."""
    script_path = Path(__file__).parent.parent.parent / "scripts" / "memray_deckmux_stress.py"
    output_bin = memray_output_dir / "deckmux_stress.bin"

    result = subprocess.run(
        ["python", "-m", "memray", "run", "--force", "-o", str(output_bin), str(script_path)],
        cwd=str(Path(__file__).parent.parent.parent),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"memray run failed: {result.stderr}"

    stats_result = subprocess.run(
        ["python", "-m", "memray", "stats", str(output_bin)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert stats_result.returncode == 0, f"memray stats failed: {stats_result.stderr}"

    match = re.search(r"Total allocations:\s+([\d,]+)", stats_result.stdout)
    assert match, f"Could not parse allocations from memray stats:\n{stats_result.stdout}"
    total_allocations = int(match.group(1).replace(",", ""))

    baseline = memray_baseline.get("deckmux_total_allocations")
    assert_allocation_within_threshold(baseline, total_allocations, "DeckMux")

    if baseline is None:
        memray_baseline["deckmux_total_allocations"] = total_allocations
