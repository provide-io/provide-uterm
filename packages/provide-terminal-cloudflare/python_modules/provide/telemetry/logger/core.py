# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Logging setup and accessors."""

from __future__ import annotations

import logging
import sys
import threading
import warnings
from typing import Any, Protocol

import structlog

from provide.telemetry import _otel
from provide.telemetry.config import TelemetryConfig
from provide.telemetry.logger.pretty import PrettyRenderer
from provide.telemetry.logger.processors import (
    add_standard_fields,
    apply_sampling,
    enforce_event_schema,
    make_level_filter,
    merge_runtime_context,
    sanitize_sensitive_fields,
)
from provide.telemetry.resilience import run_with_resilience

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

_LEVEL_NAME_TO_NUMERIC: dict[str, int] = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "TRACE": TRACE,
}


def _get_level(level: str) -> int:
    if level == "TRACE":  # pragma: no mutate
        return TRACE
    mapped = logging.getLevelName(level)
    if isinstance(mapped, int):
        return mapped
    return logging.INFO


def _make_filtering_bound_logger(level: int) -> type:
    """Create a FilteringBoundLogger with zero-cost level guards and .trace().

    Extends structlog's FilteringBoundLogger with:
    - ``.trace()`` — routes through ``.debug(_trace=True)`` when TRACE is active
    - ``.is_debug_enabled()`` / ``.is_trace_enabled()`` — O(1) level checks
      for guarding expensive argument construction
    - Permissive no-op — accepts ``log.debug(key=val)`` without event string
    """
    structlog_level = max(level, logging.DEBUG)
    cls = structlog.make_filtering_bound_logger(structlog_level)

    # Permissive no-op for filtered methods (accepts any args/kwargs)
    _standard_levels = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,  # pragma: no mutate
    }

    def _permissive_nop(*_args: Any, **_kw: Any) -> None:
        return None

    for method_name, method_level in _standard_levels.items():
        if method_level < structlog_level:
            setattr(cls, method_name, _permissive_nop)

    # .trace() — forwards through debug() with _trace marker when TRACE active
    if level <= TRACE:

        def _trace(self: Any, event: str, **kw: Any) -> None:
            self.debug(event, _trace=True, **kw)
    else:
        _trace = _permissive_nop
    cls.trace = _trace  # type: ignore[attr-defined]

    # .is_debug_enabled() / .is_trace_enabled() — baked in at class creation
    _debug_ok = level <= logging.DEBUG
    _trace_ok = level <= TRACE
    cls.is_debug_enabled = lambda _self: _debug_ok  # type: ignore[attr-defined]
    cls.is_trace_enabled = lambda _self: _trace_ok  # type: ignore[attr-defined]

    return cls


_configured = False
_lock = threading.Lock()
_active_config: TelemetryConfig | None = None
_otel_log_provider: object | None = None
_otel_log_global_set: bool = False  # True once we called set_logger_provider()


def _has_otel_logs() -> bool:
    return _otel.has_otel()


class _InstrumentationLoggingHandlerFactory(Protocol):
    def __call__(
        self,
        level: int,
        logger_provider: object | None,
        log_code_attributes: bool,
        **kwargs: object,
    ) -> logging.Handler: ...


def _load_otel_logs_components() -> tuple[Any, Any, Any, Any, Any] | None:
    if not _has_otel_logs():
        return None
    return _otel.load_otel_logs_components()


def _load_instrumentation_logging_handler() -> _InstrumentationLoggingHandlerFactory | None:
    return _otel.load_instrumentation_logging_handler()


def _build_handlers(config: TelemetryConfig, level: int) -> list[logging.Handler]:
    global _otel_log_provider, _otel_log_global_set
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]  # pragma: no mutate
    _otel_log_provider = None

    if not config.logging.otlp_endpoint:
        return handlers

    components = _load_otel_logs_components()
    if components is None:
        return handlers

    logs_api_mod, sdk_logs_mod, sdk_logs_export_mod, resource_cls, otlp_exporter_cls = components
    resource = resource_cls.create({"service.name": config.service_name, "service.version": config.version})
    provider = sdk_logs_mod.LoggerProvider(resource=resource)
    exporter = run_with_resilience(
        "logs",
        lambda: otlp_exporter_cls(
            endpoint=config.logging.otlp_endpoint,
            headers=config.logging.otlp_headers,
            timeout=config.exporter.logs_timeout_seconds,
        ),
    )
    if exporter is None:
        return handlers
    provider.add_log_record_processor(sdk_logs_export_mod.BatchLogRecordProcessor(exporter))
    logs_api_mod.set_logger_provider(provider)
    _otel_log_global_set = True  # pragma: no mutate
    instrumentation_handler_cls = _load_instrumentation_logging_handler()
    if instrumentation_handler_cls is not None:
        handlers.append(
            instrumentation_handler_cls(
                level=level,
                logger_provider=provider,
                log_code_attributes=config.logging.log_code_attributes,
            )
        )
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            handlers.append(sdk_logs_mod.LoggingHandler(level=level, logger_provider=provider))
    _otel_log_provider = provider
    return handlers


def configure_logging(config: TelemetryConfig, *, force: bool = False) -> None:  # pragma: no mutate
    global _configured, _active_config
    with _lock:
        if _configured and not force and _active_config == config:
            return

        level = _get_level(config.logging.level)
        handlers = _build_handlers(config, level)
        logging.basicConfig(level=level, handlers=handlers, format="%(message)s", force=True)

        # Compute effective level for FilteringBoundLogger: min of default
        # and all module-level overrides so overridden modules reach the
        # _LevelFilter processor which evaluates per-module thresholds.
        effective_level = level
        for module_level_str in config.logging.module_levels.values():
            module_numeric = _LEVEL_NAME_TO_NUMERIC.get(module_level_str, logging.INFO)  # pragma: no mutate
            if module_numeric < effective_level:  # pragma: no mutate
                effective_level = module_numeric

        processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            merge_runtime_context,
            structlog.processors.add_log_level,
        ]
        if config.logging.include_timestamp:
            processors.append(structlog.processors.TimeStamper(fmt="iso"))

        processors.extend(
            [
                add_standard_fields(config),
                apply_sampling,
                enforce_event_schema(config),
                sanitize_sensitive_fields(config.logging.sanitize),
            ]
        )

        # Per-module level filter — placed late so enrichment processors
        # run first.  Only added when module_levels are configured.
        if config.logging.module_levels:
            processors.append(make_level_filter(config.logging.level, config.logging.module_levels))

        if config.logging.include_caller:
            processors.append(
                structlog.processors.CallsiteParameterAdder(
                    parameters=[
                        structlog.processors.CallsiteParameter.FILENAME,
                        structlog.processors.CallsiteParameter.LINENO,
                    ]
                )
            )

        renderer: Any
        if config.logging.fmt == "json":
            renderer = structlog.processors.JSONRenderer()
        elif config.logging.fmt == "pretty":
            from provide.telemetry.logger.pretty import resolve_color

            renderer = PrettyRenderer(  # pragma: no mutate
                colors=sys.stderr.isatty(),
                key_color=resolve_color(config.logging.pretty_key_color),  # pragma: no mutate
                value_color=resolve_color(config.logging.pretty_value_color),  # pragma: no mutate
                fields=config.logging.pretty_fields,  # pragma: no mutate
            )
        else:
            renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

        processors.append(renderer)

        structlog.configure(
            processors=processors,
            wrapper_class=_make_filtering_bound_logger(effective_level),
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=False,
        )
        _active_config = config
        _configured = True


def shutdown_logging() -> None:
    global _configured, _active_config, _otel_log_provider
    with _lock:
        provider = _otel_log_provider
        if provider is None:
            _configured = False
            _active_config = None
            return
        try:
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()
        finally:
            _otel_log_provider = None
            _active_config = None
            _configured = False


def _reset_logging_for_tests() -> None:
    global _configured, _active_config, _otel_log_provider, _otel_log_global_set
    with _lock:
        _configured = False
        _active_config = None
        _otel_log_provider = None
        _otel_log_global_set = False


def _has_otel_log_provider() -> bool:
    """Return True if an OTel log provider is installed or was ever installed (thread-safe)."""
    with _lock:
        return _otel_log_provider is not None or _otel_log_global_set


def get_logger(name: str | None = None) -> _TraceWrapper:
    if not _configured:
        from provide.telemetry.config import TelemetryConfig

        configure_logging(TelemetryConfig.from_env())
    return _TraceWrapper(structlog.get_logger(name or "provide"))


def is_debug_enabled() -> bool:
    """Standalone check if debug-level logging is enabled.

    Use to guard expensive argument construction::

        from provide.telemetry.logger import is_debug_enabled
        if is_debug_enabled():
            logger.debug("result", payload=model.model_dump_json())
    """
    active = _active_config
    if active is None:
        return True  # unconfigured — let everything through
    return _get_level(active.logging.level) <= logging.DEBUG


def is_trace_enabled() -> bool:
    """Standalone check if trace-level logging is enabled."""
    active = _active_config
    if active is None:
        return True
    return _get_level(active.logging.level) <= TRACE


class _TraceWrapper:
    """Thin wrapper that forwards to the structlog bound logger.

    The custom FilteringBoundLogger (from ``_make_filtering_bound_logger``)
    already provides ``.trace()``, ``.is_debug_enabled()``, and
    ``.is_trace_enabled()`` — this wrapper just preserves the return type
    on ``.bind()``.
    """

    __slots__ = ("_logger",)

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def __getattr__(self, item: str) -> Any:
        return getattr(self._logger, item)

    def trace(self, event: str, **kwargs: Any) -> None:
        self._logger.trace(event, **kwargs)

    def is_debug_enabled(self) -> bool:
        return bool(self._logger.is_debug_enabled())

    def is_trace_enabled(self) -> bool:
        return bool(self._logger.is_trace_enabled())

    def bind(self, **kwargs: Any) -> _TraceWrapper:
        return _TraceWrapper(self._logger.bind(**kwargs))


class _LazyLogger:
    def _resolve(self) -> _TraceWrapper:
        return get_logger()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._resolve(), item)

    def trace(self, event: str, **kwargs: Any) -> None:
        self._resolve().trace(event, **kwargs)

    def is_debug_enabled(self) -> bool:
        return self._resolve().is_debug_enabled()

    def is_trace_enabled(self) -> bool:
        return self._resolve().is_trace_enabled()

    def bind(self, **kwargs: Any) -> _TraceWrapper:
        return self._resolve().bind(**kwargs)


logger = _LazyLogger()
