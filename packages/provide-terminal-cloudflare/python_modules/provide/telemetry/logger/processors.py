# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Structlog processors."""

from __future__ import annotations

import logging
from typing import Any

import structlog

from provide.telemetry.config import TelemetryConfig
from provide.telemetry.logger.context import get_context
from provide.telemetry.pii import sanitize_payload
from provide.telemetry.sampling import should_sample
from provide.telemetry.schema.events import validate_event_name, validate_required_keys
from provide.telemetry.tracing.context import get_span_id, get_trace_id

TRACE_LEVEL = 5

# Fast lowercase level → numeric lookup (avoids normalize + getLevelName per message)
_FAST_LEVEL_LOOKUP: dict[str, int] = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": TRACE_LEVEL,
}


def merge_runtime_context(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict.update(get_context())
    trace_id = get_trace_id()
    span_id = get_span_id()
    if trace_id is not None:
        event_dict["trace_id"] = trace_id
    if span_id is not None:
        event_dict["span_id"] = span_id
    return event_dict


def add_standard_fields(config: TelemetryConfig) -> Any:
    def _processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict.setdefault("service", config.service_name)
        event_dict.setdefault("env", config.environment)
        event_dict.setdefault("version", config.version)
        if config.slo.include_error_taxonomy and "error_type" not in event_dict and "exc_name" in event_dict:
            from provide.telemetry.slo import classify_error  # lazy: avoid loading metrics at logging config time

            status_code = event_dict.get("status_code")
            typed_status = status_code if isinstance(status_code, int) else None
            event_dict.update(classify_error(str(event_dict["exc_name"]), typed_status))
        return event_dict

    return _processor


def apply_sampling(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_name = str(event_dict.get("event", ""))  # pragma: no mutate
    if should_sample("logs", event_name):
        return event_dict
    raise structlog.DropEvent()


def enforce_event_schema(config: TelemetryConfig) -> Any:
    # strict_schema is authoritative: strict mode always enforces both checks.
    # compat mode keeps event-name policy configurable and skips required-key hard failures.
    strict_event_name = True if config.strict_schema else config.event_schema.strict_event_name
    required_keys = config.event_schema.required_keys

    def _processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event = str(event_dict.get("event", ""))
        validate_event_name(event, strict_event_name=strict_event_name)
        validate_required_keys(event_dict, required_keys)
        return event_dict

    return _processor


def sanitize_sensitive_fields(enabled: bool) -> Any:
    def _processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return sanitize_payload(event_dict, enabled)

    return _processor


class _LevelFilter:
    """Per-module log level filter.

    FilteringBoundLogger handles the default level at zero cost.  This
    processor handles **module-level overrides** — e.g. ``asyncio=WARNING``
    while the default is ``INFO``.  It drops events whose level is below
    the threshold for their module (matched by longest-prefix).

    Placed late in the processor chain so enrichment processors run first.
    """

    __slots__ = ("_default_numeric", "_module_numerics", "_sorted_prefixes")

    def __init__(self, default_level: str, module_levels: dict[str, str]) -> None:
        self._default_numeric = _FAST_LEVEL_LOOKUP.get(default_level.lower(), logging.INFO)
        self._module_numerics: dict[str, int] = {
            module: _FAST_LEVEL_LOOKUP.get(lvl.lower(), logging.INFO) for module, lvl in module_levels.items()
        }
        # Longest prefix first for correct matching
        self._sorted_prefixes = sorted(self._module_numerics, key=len, reverse=True)

    def __call__(self, _: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        logger_name: str = event_dict.get("logger_name", event_dict.get("logger", ""))
        event_level = _FAST_LEVEL_LOOKUP.get(event_dict.get("level", method_name).lower(), logging.INFO)

        threshold = self._default_numeric
        for prefix in self._sorted_prefixes:
            if logger_name.startswith(prefix):
                threshold = self._module_numerics[prefix]
                break

        if event_level < threshold:
            raise structlog.DropEvent()
        return event_dict


def make_level_filter(default_level: str, module_levels: dict[str, str]) -> _LevelFilter:
    """Create a _LevelFilter for per-module log level overrides."""
    return _LevelFilter(default_level, module_levels)
