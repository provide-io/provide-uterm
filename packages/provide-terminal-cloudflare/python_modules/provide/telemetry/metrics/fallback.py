# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Local fallback metric instrument implementations."""

from __future__ import annotations

import threading
from typing import Any

from provide.telemetry.backpressure import release, try_acquire
from provide.telemetry.cardinality import guard_attributes
from provide.telemetry.health import increment_exemplar_unsupported
from provide.telemetry.sampling import should_sample
from provide.telemetry.tracing.context import get_span_id, get_trace_id

# Lazy re-binding support: when an instrument is created before
# setup_telemetry(), its _otel_* handle is None.  After provider
# setup clears the meter cache, _resolve_otel re-creates the
# OTel instrument from the real provider on first use.
_RESOLVE_LOCK = threading.Lock()


def _exemplar() -> dict[str, str]:
    trace_id = get_trace_id()
    span_id = get_span_id()
    if trace_id is None or span_id is None:
        return {}
    return {"trace_id": trace_id, "span_id": span_id}


class Counter:
    def __init__(self, name: str, otel_counter: Any | None = None) -> None:
        self.name = name
        self._otel_counter = otel_counter
        self._resolved = otel_counter is not None
        self._lock = threading.Lock()
        self.value = 0

    def _resolve_otel(self) -> Any | None:
        if self._resolved:
            return self._otel_counter
        from provide.telemetry.metrics.provider import get_meter

        meter = get_meter()
        if meter is None:
            return None
        with _RESOLVE_LOCK:
            if self._resolved:
                return self._otel_counter
            try:
                self._otel_counter = meter.create_counter(name=self.name)
            except Exception:
                self._otel_counter = None
            self._resolved = True
        return self._otel_counter

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        if not should_sample("metrics", self.name):
            return
        ticket = try_acquire("metrics")
        if ticket is None:
            return
        try:
            with self._lock:
                self.value += amount
            otel_counter = self._resolve_otel()
            if otel_counter is not None:
                attrs = guard_attributes(attributes or {})
                exemplar = _exemplar()
                if exemplar:
                    try:
                        otel_counter.add(amount, attrs, exemplar=exemplar)
                        return
                    except TypeError:
                        increment_exemplar_unsupported()
                otel_counter.add(amount, attrs)
        finally:
            release(ticket)


class Gauge:
    def __init__(self, name: str, otel_gauge: Any | None = None) -> None:
        self.name = name
        self._otel_gauge = otel_gauge
        self._resolved = otel_gauge is not None
        self._lock = threading.Lock()
        self.value = 0

    def _resolve_otel(self) -> Any | None:
        if self._resolved:
            return self._otel_gauge
        from provide.telemetry.metrics.provider import get_meter

        meter = get_meter()
        if meter is None:
            return None
        with _RESOLVE_LOCK:
            if self._resolved:
                return self._otel_gauge
            try:
                self._otel_gauge = meter.create_up_down_counter(name=self.name)
            except Exception:
                self._otel_gauge = None
            self._resolved = True
        return self._otel_gauge

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        if not should_sample("metrics", self.name):
            return
        ticket = try_acquire("metrics")
        if ticket is None:
            return
        try:
            otel_gauge = self._resolve_otel()
            attrs = guard_attributes(attributes or {})
            with self._lock:
                self.value += amount
                if otel_gauge is not None:
                    otel_gauge.add(amount, attrs)
        finally:
            release(ticket)

    def set(self, value: int, attributes: dict[str, str] | None = None) -> None:
        if not should_sample("metrics", self.name):
            return
        ticket = try_acquire("metrics")
        if ticket is None:
            return
        try:
            otel_gauge = self._resolve_otel()
            attrs = guard_attributes(attributes or {})
            with self._lock:
                delta = value - self.value
                self.value = value
                if otel_gauge is not None:
                    otel_gauge.add(delta, attrs)
        finally:
            release(ticket)


class Histogram:
    def __init__(self, name: str, otel_histogram: Any | None = None) -> None:
        self.name = name
        self._otel_histogram = otel_histogram
        self._resolved = otel_histogram is not None
        self._lock = threading.Lock()
        self.count: int = 0
        self.total: float = 0.0
        self.min: float = float("inf")  # pragma: no mutate
        self.max: float = float("-inf")  # pragma: no mutate

    def _resolve_otel(self) -> Any | None:
        if self._resolved:
            return self._otel_histogram
        from provide.telemetry.metrics.provider import get_meter

        meter = get_meter()
        if meter is None:
            return None
        with _RESOLVE_LOCK:
            if self._resolved:
                return self._otel_histogram
            try:
                self._otel_histogram = meter.create_histogram(name=self.name)
            except Exception:
                self._otel_histogram = None
            self._resolved = True
        return self._otel_histogram

    def record(self, value: float, attributes: dict[str, str] | None = None) -> None:
        if not should_sample("metrics", self.name):
            return
        ticket = try_acquire("metrics")
        if ticket is None:
            return
        try:
            with self._lock:
                self.count += 1
                self.total += value
                if value < self.min:  # pragma: no mutate
                    self.min = value
                if value > self.max:  # pragma: no mutate
                    self.max = value
            otel_histogram = self._resolve_otel()
            if otel_histogram is not None:
                attrs = guard_attributes(attributes or {})
                exemplar = _exemplar()
                if exemplar:
                    try:
                        otel_histogram.record(value, attrs, exemplar=exemplar)
                        return
                    except TypeError:
                        increment_exemplar_unsupported()
                otel_histogram.record(value, attrs)
        finally:
            release(ticket)
