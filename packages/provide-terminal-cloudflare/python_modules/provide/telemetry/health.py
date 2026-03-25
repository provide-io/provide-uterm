# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Internal self-observability counters and snapshot API."""

from __future__ import annotations

__all__ = [
    "HealthSnapshot",
    "get_health_snapshot",
    "increment_async_blocking_risk",
    "increment_dropped",
    "increment_exemplar_unsupported",
    "increment_retries",
    "record_export_failure",
    "record_export_success",
    "set_queue_depth",
]

import threading
import time
from dataclasses import dataclass

Signal = str


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    queue_depth_logs: int
    queue_depth_traces: int
    queue_depth_metrics: int
    dropped_logs: int
    dropped_traces: int
    dropped_metrics: int
    retries_logs: int
    retries_traces: int
    retries_metrics: int
    async_blocking_risk_logs: int
    async_blocking_risk_traces: int
    async_blocking_risk_metrics: int
    export_failures_logs: int
    export_failures_traces: int
    export_failures_metrics: int
    exemplar_unsupported_total: int
    last_error_logs: str | None
    last_error_traces: str | None
    last_error_metrics: str | None
    last_successful_export_logs: float | None
    last_successful_export_traces: float | None
    last_successful_export_metrics: float | None
    export_latency_ms_logs: float
    export_latency_ms_traces: float
    export_latency_ms_metrics: float


_lock = threading.Lock()
_queue_depth: dict[Signal, int] = {"logs": 0, "traces": 0, "metrics": 0}
_dropped: dict[Signal, int] = {"logs": 0, "traces": 0, "metrics": 0}
_retries: dict[Signal, int] = {"logs": 0, "traces": 0, "metrics": 0}
_async_blocking_risk: dict[Signal, int] = {"logs": 0, "traces": 0, "metrics": 0}
_export_failures: dict[Signal, int] = {"logs": 0, "traces": 0, "metrics": 0}
_last_error: dict[Signal, str | None] = {"logs": None, "traces": None, "metrics": None}
_last_success: dict[Signal, float | None] = {"logs": None, "traces": None, "metrics": None}
_export_latency_ms: dict[Signal, float] = {"logs": 0.0, "traces": 0.0, "metrics": 0.0}
_exemplar_unsupported_total = 0


_VALID_SIGNALS_HEALTH = frozenset({"logs", "traces", "metrics"})


def _known_signal(signal: Signal) -> Signal:
    if signal in _VALID_SIGNALS_HEALTH:  # pragma: no mutate
        return signal
    raise ValueError(f"unknown signal {signal!r}, expected one of {sorted(_VALID_SIGNALS_HEALTH)}")


def set_queue_depth(signal: Signal, depth: int) -> None:
    sig = _known_signal(signal)
    with _lock:
        _queue_depth[sig] = max(0, depth)


def increment_dropped(signal: Signal, amount: int = 1) -> None:  # pragma: no mutate
    sig = _known_signal(signal)
    with _lock:
        _dropped[sig] += max(0, amount)


def increment_retries(signal: Signal, amount: int = 1) -> None:  # pragma: no mutate
    sig = _known_signal(signal)
    with _lock:
        _retries[sig] += max(0, amount)


def increment_async_blocking_risk(signal: Signal, amount: int = 1) -> None:  # pragma: no mutate
    sig = _known_signal(signal)
    with _lock:
        _async_blocking_risk[sig] += max(0, amount)


def record_export_failure(signal: Signal, exc: Exception) -> None:
    sig = _known_signal(signal)
    with _lock:
        _export_failures[sig] += 1
        _last_error[sig] = str(exc)


def record_export_success(signal: Signal, latency_ms: float = 0.0) -> None:  # pragma: no mutate
    sig = _known_signal(signal)
    with _lock:
        _last_success[sig] = time.time()
        _export_latency_ms[sig] = max(0.0, latency_ms)
        _last_error[sig] = None


def increment_exemplar_unsupported(amount: int = 1) -> None:  # pragma: no mutate
    global _exemplar_unsupported_total
    with _lock:
        _exemplar_unsupported_total += max(0, amount)


def get_health_snapshot() -> HealthSnapshot:
    with _lock:
        return HealthSnapshot(
            queue_depth_logs=_queue_depth["logs"],
            queue_depth_traces=_queue_depth["traces"],
            queue_depth_metrics=_queue_depth["metrics"],
            dropped_logs=_dropped["logs"],
            dropped_traces=_dropped["traces"],
            dropped_metrics=_dropped["metrics"],
            retries_logs=_retries["logs"],
            retries_traces=_retries["traces"],
            retries_metrics=_retries["metrics"],
            async_blocking_risk_logs=_async_blocking_risk["logs"],
            async_blocking_risk_traces=_async_blocking_risk["traces"],
            async_blocking_risk_metrics=_async_blocking_risk["metrics"],
            export_failures_logs=_export_failures["logs"],
            export_failures_traces=_export_failures["traces"],
            export_failures_metrics=_export_failures["metrics"],
            exemplar_unsupported_total=_exemplar_unsupported_total,
            last_error_logs=_last_error["logs"],
            last_error_traces=_last_error["traces"],
            last_error_metrics=_last_error["metrics"],
            last_successful_export_logs=_last_success["logs"],
            last_successful_export_traces=_last_success["traces"],
            last_successful_export_metrics=_last_success["metrics"],
            export_latency_ms_logs=_export_latency_ms["logs"],
            export_latency_ms_traces=_export_latency_ms["traces"],
            export_latency_ms_metrics=_export_latency_ms["metrics"],
        )


def reset_health_for_tests() -> None:
    global _exemplar_unsupported_total
    with _lock:
        for signal in ("logs", "traces", "metrics"):
            _queue_depth[signal] = 0
            _dropped[signal] = 0
            _retries[signal] = 0
            _async_blocking_risk[signal] = 0
            _export_failures[signal] = 0
            _last_error[signal] = None
            _last_success[signal] = None
            _export_latency_ms[signal] = 0.0
        _exemplar_unsupported_total = 0
