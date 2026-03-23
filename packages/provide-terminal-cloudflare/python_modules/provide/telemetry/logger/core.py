# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
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
    merge_runtime_context,
    sanitize_sensitive_fields,
)
from provide.telemetry.resilience import run_with_resilience

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _get_level(level: str) -> int:
    if level == "TRACE":  # pragma: no mutate
        return TRACE
    mapped = logging.getLevelName(level)
    if isinstance(mapped, int):
        return mapped
    return logging.INFO


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
        structlog_level = max(level, logging.DEBUG)
        handlers = _build_handlers(config, level)
        logging.basicConfig(level=level, handlers=handlers, format="%(message)s", force=True)

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
            wrapper_class=structlog.make_filtering_bound_logger(structlog_level),
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


class _TraceWrapper:
    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def __getattr__(self, item: str) -> Any:
        return getattr(self._logger, item)

    def trace(self, event: str, **kwargs: Any) -> None:
        active = _active_config  # atomic ref read under GIL; no lock needed
        if active is not None and active.logging.level == "TRACE":
            self._logger.debug(event, _trace=True, **kwargs)

    def bind(self, **kwargs: Any) -> _TraceWrapper:
        return _TraceWrapper(self._logger.bind(**kwargs))


class _LazyLogger:
    def _resolve(self) -> _TraceWrapper:
        return get_logger()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._resolve(), item)

    def trace(self, event: str, **kwargs: Any) -> None:
        self._resolve().trace(event, **kwargs)

    def bind(self, **kwargs: Any) -> _TraceWrapper:
        return self._resolve().bind(**kwargs)


logger = _LazyLogger()
