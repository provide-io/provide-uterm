# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Compatibility layer for metrics helpers."""

from __future__ import annotations

from provide.telemetry.metrics.api import counter, gauge, histogram
from provide.telemetry.metrics.fallback import Counter, Gauge, Histogram

__all__ = ["Counter", "Gauge", "Histogram", "counter", "gauge", "histogram"]
