# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Tracer setup and acquisition."""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from typing import Any, Protocol, cast

from provide.telemetry import _otel
from provide.telemetry.config import TelemetryConfig
from provide.telemetry.resilience import run_with_resilience
from provide.telemetry.tracing.context import get_trace_context, set_trace_context


def _has_otel() -> bool:
    return _otel.has_otel()


_HAS_OTEL = _has_otel()
_provider_configured: bool = False
_provider_lock = threading.Lock()
_provider_ref: Any | None = None
_otel_global_set: bool = False  # True once we called set_tracer_provider()
_setup_generation: int = 0

# Baseline captured inside setup_tracing() (not at module load) so that
# external providers installed before import are not mistaken for the default.
_baseline_tracer_provider: Any | None = None
_baseline_captured: bool = False


class _NoopSpan(AbstractContextManager["_NoopSpan"]):
    NOOP_TRACE_ID = "0" * 32
    NOOP_SPAN_ID = "0" * 16

    def __init__(self, name: str) -> None:
        self.name = name
        self.trace_id = self.NOOP_TRACE_ID
        self.span_id = self.NOOP_SPAN_ID
        self._prev_trace_id: str | None = None
        self._prev_span_id: str | None = None

    def __enter__(self) -> _NoopSpan:
        prev = get_trace_context()
        self._prev_trace_id = prev["trace_id"]
        self._prev_span_id = prev["span_id"]
        set_trace_context(self.trace_id, self.span_id)
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        set_trace_context(self._prev_trace_id, self._prev_span_id)


class _NoopTracer:
    def start_as_current_span(self, name: str, **_: object) -> _NoopSpan:
        return _NoopSpan(name)


def _refresh_otel_tracing() -> None:
    global _HAS_OTEL
    _HAS_OTEL = _has_otel()


def _load_otel_trace_api() -> Any | None:
    if not _HAS_OTEL:
        return None
    return _otel.load_otel_trace_api()


def _load_otel_tracing_components() -> tuple[Any, Any, Any, Any] | None:
    if not _HAS_OTEL:
        return None
    return _otel.load_otel_tracing_components()


def _has_tracing_provider() -> bool:
    """Return True if a tracing provider is installed or was ever installed (thread-safe)."""
    with _provider_lock:
        return _provider_ref is not None or _otel_global_set


def setup_tracing(config: TelemetryConfig) -> None:
    global _provider_configured, _provider_ref, _otel_global_set
    global _baseline_tracer_provider, _baseline_captured
    if not config.tracing.enabled or not _HAS_OTEL:
        return

    with _provider_lock:
        if _provider_configured:
            return
        # Capture the baseline provider before we install ours so that
        # _has_real_tracer_provider() can distinguish external providers
        # regardless of import order.
        if not _baseline_captured:  # pragma: no mutate
            otel_trace_api = _load_otel_trace_api()  # pragma: no mutate
            if otel_trace_api is not None:
                _baseline_tracer_provider = otel_trace_api.get_tracer_provider()  # pragma: no mutate
            _baseline_captured = True  # pragma: no mutate
        gen = _setup_generation  # snapshot before releasing the lock

    # Build provider/exporter outside the lock to avoid blocking
    # concurrent get_tracer()/shutdown_tracing() during slow network I/O.
    components = _load_otel_tracing_components()
    otel_trace = _load_otel_trace_api()
    if components is None or otel_trace is None:
        return

    resource_cls, provider_cls, processor_cls, exporter_cls = components
    resource = resource_cls.create({"service.name": config.service_name, "service.version": config.version})
    provider = provider_cls(resource=resource)
    if config.tracing.otlp_endpoint:
        exporter = run_with_resilience(
            "traces",
            lambda: exporter_cls(
                endpoint=config.tracing.otlp_endpoint,
                headers=config.tracing.otlp_headers,
                timeout=config.exporter.traces_timeout_seconds,
            ),
        )
        if exporter is not None:
            provider.add_span_processor(processor_cls(exporter))

    with _provider_lock:
        if _provider_configured or _setup_generation != gen:
            # Another thread won the race OR shutdown happened mid-build — discard ours.
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()
            return
        otel_trace.set_tracer_provider(provider)
        _provider_ref = provider
        _provider_configured = True
        _otel_global_set = True


def shutdown_tracing() -> None:
    global _provider_ref, _provider_configured, _setup_generation
    with _provider_lock:
        _setup_generation += 1
        provider = _provider_ref
        if provider is None:
            _provider_configured = False
            return
        try:
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()
        finally:
            _provider_ref = None
            _provider_configured = False


def _reset_tracing_for_tests() -> None:
    global _provider_configured, _provider_ref, _otel_global_set, _setup_generation
    global _baseline_tracer_provider, _baseline_captured
    _provider_configured = False
    _provider_ref = None
    _otel_global_set = False
    _setup_generation = 0
    _baseline_tracer_provider = None
    _baseline_captured = False


class _TracerLike(Protocol):
    def start_as_current_span(self, name: str, **kwargs: object) -> AbstractContextManager[object]: ...


def _has_real_tracer_provider(otel_trace: Any) -> bool:
    """Return True if a usable (non-placeholder) OTel tracer provider is globally available."""
    if _provider_configured:
        return True
    if _otel_global_set:
        # We installed a provider but it was shut down; don't use the stale global.
        return False
    provider = otel_trace.get_tracer_provider()
    if not _baseline_captured:
        # setup_tracing() hasn't been called yet — no baseline to compare against.
        # Use class-name heuristic: the OTel API default is ProxyTracerProvider.
        return "Proxy" not in type(provider).__name__
    # Identity comparison against the baseline captured inside setup_tracing().
    return provider is not _baseline_tracer_provider


def get_tracer(name: str | None = None) -> _TracerLike:
    otel_trace = _load_otel_trace_api()
    if otel_trace is None:
        return _NoopTracer()
    if not _has_real_tracer_provider(otel_trace):
        return _NoopTracer()
    tracer_name = "provide.telemetry" if name is None else name
    return cast(_TracerLike, otel_trace.get_tracer(tracer_name))  # pragma: no mutate


def _sync_otel_trace_context() -> None:
    """Sync the active OTel span's trace/span IDs into our contextvars."""
    otel_trace = _load_otel_trace_api()
    if otel_trace is None:
        return
    if not _has_real_tracer_provider(otel_trace):
        return
    span = otel_trace.get_current_span()
    ctx = span.get_span_context()
    if ctx is not None and ctx.trace_id != 0 and ctx.span_id != 0:
        set_trace_context(format(ctx.trace_id, "032x"), format(ctx.span_id, "016x"))


class _LazyTracer:
    """Defers tracer resolution to call time so setup() takes effect."""

    def start_as_current_span(self, name: str, **kwargs: object) -> AbstractContextManager[object]:
        return get_tracer().start_as_current_span(name, **kwargs)


tracer: _TracerLike = _LazyTracer()
