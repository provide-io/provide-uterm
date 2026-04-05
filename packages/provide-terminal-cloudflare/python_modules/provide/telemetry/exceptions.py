# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Exception hierarchy for provide telemetry."""

from __future__ import annotations

__all__ = [
    "ConfigurationError",
    "TelemetryError",
]


class TelemetryError(Exception):
    """Base exception for all provide telemetry errors."""


class ConfigurationError(TelemetryError, ValueError):
    """Raised when telemetry configuration is invalid.

    Inherits from both TelemetryError and ValueError for
    backwards compatibility with code catching ValueError.
    """
