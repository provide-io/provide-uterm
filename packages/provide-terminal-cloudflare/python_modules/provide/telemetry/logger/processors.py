# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Structlog processors."""

from __future__ import annotations

from typing import Any

import structlog

from provide.telemetry.config import TelemetryConfig
from provide.telemetry.logger.context import get_context
from provide.telemetry.pii import sanitize_payload
from provide.telemetry.sampling import should_sample
from provide.telemetry.schema.events import validate_event_name, validate_required_keys
from provide.telemetry.tracing.context import get_trace_context


def merge_runtime_context(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict.update(get_context())
    trace_ctx = get_trace_context()
    trace_id = trace_ctx.get("trace_id")
    span_id = trace_ctx.get("span_id")
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
