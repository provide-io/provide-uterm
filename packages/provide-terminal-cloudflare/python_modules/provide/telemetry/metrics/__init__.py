# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Metrics facade."""

from provide.telemetry.metrics.api import counter, gauge, histogram
from provide.telemetry.metrics.fallback import Counter, Gauge, Histogram
from provide.telemetry.metrics.provider import get_meter, setup_metrics, shutdown_metrics

__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "counter",
    "gauge",
    "get_meter",
    "histogram",
    "setup_metrics",
    "shutdown_metrics",
]
