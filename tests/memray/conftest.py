#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fixtures for memray memory profiling tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

_baseline_updates: dict[str, int] = {}


@pytest.fixture
def memray_output_dir() -> Path:
    """Return path to memray output directory, creating it if needed."""
    output_dir = Path(__file__).parent.parent.parent / "memray-output"
    output_dir.mkdir(exist_ok=True)
    return output_dir


@pytest.fixture
def memray_baseline() -> dict[str, int]:
    """Load baseline allocation counts from baselines.json, or return empty dict if not found."""
    baseline_path = Path(__file__).parent / "baselines.json"
    if baseline_path.exists():
        with baseline_path.open() as f:
            return json.load(f)
    return {}


@pytest.fixture(autouse=True)
def _save_memray_baseline_updates(request: Any) -> None:
    """Save baseline updates to baselines.json if MEMRAY_UPDATE_BASELINE is set."""
    yield
    # After test completes, save updates if requested
    if os.getenv("MEMRAY_UPDATE_BASELINE") and _baseline_updates:
        baseline_path = Path(__file__).parent / "baselines.json"
        with baseline_path.open("w") as f:
            json.dump(_baseline_updates, f, indent=2)


def assert_allocation_within_threshold(baseline: int | None, current: int, name: str, tolerance: float = 0.15) -> None:
    """Assert that current allocation is within tolerance of baseline.

    Args:
        baseline: Baseline allocation count (or None if first run)
        current: Current allocation count
        name: Test name for error message
        tolerance: Allowed deviation as fraction (default 15%)

    Raises:
        AssertionError: If current exceeds baseline by >tolerance%
    """
    if baseline is None:
        # First run: record this value and skip assertion
        # Determine key from name
        key = f"{name.lower().replace(' ', '_')}_total_allocations"
        _baseline_updates[key] = current
        return
    max_allowed = baseline * (1 + tolerance)
    if current > max_allowed:
        percent_over = ((current - baseline) / baseline) * 100
        raise AssertionError(
            f"{name} allocation {current} exceeds baseline {baseline} by {percent_over:.1f}% "
            f"(tolerance: {tolerance * 100:.0f}%)"
        )
