#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regenerate behavior vectors (alias for generate_behavior_vectors).

Kept so existing docs that name generate_parity_tests.py still work. Real
consumers load spec/behavior_vectors.json and call shipped policy evaluators —
they are not placeholder string compares.
"""

from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("generate_behavior_vectors.py")), run_name="__main__")
