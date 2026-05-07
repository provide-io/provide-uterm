#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared return types for ushell command handlers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AnimatedResult:
    """Return type for animated render output — caller handles frame timing."""

    frames: list[str]
    fps: float
    loop: bool
