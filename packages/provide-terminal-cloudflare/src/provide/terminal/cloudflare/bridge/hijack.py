# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hijack coordinator — re-exported from shared provide.terminal.bridge.coordinator."""

try:
    from provide.terminal.bridge.coordinator import (
        AcquireResult,
        HijackCoordinator,
        HijackSession,
    )
except ImportError:  # pragma: no cover — Pyodide flat-path fallback
    from hijack.coordinator import (  # type: ignore[import-not-found,no-redef]
        AcquireResult,
        HijackCoordinator,
        HijackSession,
    )

__all__ = ["AcquireResult", "HijackCoordinator", "HijackSession"]
