#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineCapabilities:
    """Engine feature flags discovered at bootstrap time."""

    supports_transactions: bool = True
    supports_migrations: bool = True
    supports_retries: bool = True
