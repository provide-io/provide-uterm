# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""SLO-oriented telemetry helpers (RED/USE baseline)."""

from __future__ import annotations

__all__ = [
    "classify_error",
    "record_red_metrics",
    "record_use_metrics",
]

import threading

from provide.telemetry.metrics import counter, gauge, histogram
from provide.telemetry.metrics.instruments import Counter, Gauge, Histogram

_lock = threading.Lock()
_counters: dict[str, Counter] = {}
_histograms: dict[str, Histogram] = {}
_gauges: dict[str, Gauge] = {}


def _rebind_slo_instruments() -> None:
    """Clear cached instruments so they rebind to current providers on next use."""
    with _lock:
        _counters.clear()
        _histograms.clear()
        _gauges.clear()


def _lazy_counter(name: str, description: str) -> Counter:
    with _lock:
        if name not in _counters:
            _counters[name] = counter(name, description)
        return _counters[name]


def _lazy_histogram(name: str, description: str, unit: str) -> Histogram:
    with _lock:
        if name not in _histograms:
            _histograms[name] = histogram(name, description, unit)
        return _histograms[name]


def _lazy_gauge(name: str, description: str, unit: str) -> Gauge:
    with _lock:
        if name not in _gauges:
            _gauges[name] = gauge(name, description, unit)
        return _gauges[name]


def record_red_metrics(route: str, method: str, status_code: int, duration_ms: float) -> None:
    attrs = {"route": route, "method": method, "status_code": str(status_code)}
    _lazy_counter("http.requests.total", "Total HTTP requests").add(1, attrs)
    if method != "WS" and status_code >= 500:
        _lazy_counter("http.errors.total", "Total HTTP errors").add(1, attrs)
    _lazy_histogram("http.request.duration_ms", "HTTP request latency", "ms").record(duration_ms, attrs)


def record_use_metrics(resource: str, utilization_percent: int) -> None:
    _lazy_gauge("resource.utilization.percent", "Resource utilization", "%").set(
        utilization_percent, {"resource": resource}
    )


def classify_error(exc_name: str, status_code: int | None = None) -> dict[str, str]:
    if status_code is not None and status_code >= 500:
        return {"error_type": "server", "error_code": str(status_code), "error_name": exc_name}
    if status_code is not None and status_code >= 400:
        return {"error_type": "client", "error_code": str(status_code), "error_name": exc_name}
    return {"error_type": "internal", "error_code": "0", "error_name": exc_name}


def _reset_slo_for_tests() -> None:
    with _lock:
        _counters.clear()
        _histograms.clear()
        _gauges.clear()
