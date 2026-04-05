# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Public API for provide telemetry."""

from importlib.metadata import PackageNotFoundError, version

from provide.telemetry.asgi import TelemetryMiddleware, bind_websocket_context, clear_websocket_context
from provide.telemetry.backpressure import QueuePolicy, get_queue_policy, set_queue_policy
from provide.telemetry.cardinality import (
    CardinalityLimit,
    clear_cardinality_limits,
    get_cardinality_limits,
    register_cardinality_limit,
)
from provide.telemetry.exceptions import ConfigurationError, TelemetryError
from provide.telemetry.health import HealthSnapshot, get_health_snapshot
from provide.telemetry.logger import bind_context, clear_context, get_logger, logger, unbind_context
from provide.telemetry.metrics import counter, gauge, get_meter, histogram
from provide.telemetry.pii import PIIRule, get_pii_rules, register_pii_rule, replace_pii_rules
from provide.telemetry.propagation import bind_propagation_context, extract_w3c_context
from provide.telemetry.resilience import ExporterPolicy, get_exporter_policy, set_exporter_policy
from provide.telemetry.runtime import (
    get_runtime_config,
    reconfigure_telemetry,
    reload_runtime_from_env,
    update_runtime_config,
)
from provide.telemetry.sampling import SamplingPolicy, get_sampling_policy, set_sampling_policy, should_sample
from provide.telemetry.schema.events import EventSchemaError, event_name
from provide.telemetry.setup import setup_telemetry, shutdown_telemetry
from provide.telemetry.tracing import get_trace_context, get_tracer, set_trace_context, trace, tracer

try:
    __version__ = version("provide-telemetry")
except (PackageNotFoundError, TypeError):
    __version__ = "0.0.0"

# Lazy-load slo functions to avoid pulling in slo/metrics at import time.
_SLO_NAMES = frozenset({"classify_error", "record_red_metrics", "record_use_metrics"})


def __getattr__(name: str) -> object:
    if name in _SLO_NAMES:
        from provide.telemetry import slo

        return getattr(slo, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CardinalityLimit",
    "ConfigurationError",
    "EventSchemaError",
    "ExporterPolicy",
    "HealthSnapshot",
    "PIIRule",
    "QueuePolicy",
    "SamplingPolicy",
    "TelemetryError",
    "TelemetryMiddleware",
    "__version__",
    "bind_context",
    "bind_propagation_context",
    "bind_websocket_context",
    "classify_error",
    "clear_cardinality_limits",
    "clear_context",
    "clear_websocket_context",
    "counter",
    "event_name",
    "extract_w3c_context",
    "gauge",
    "get_cardinality_limits",
    "get_exporter_policy",
    "get_health_snapshot",
    "get_logger",
    "get_meter",
    "get_pii_rules",
    "get_queue_policy",
    "get_runtime_config",
    "get_sampling_policy",
    "get_trace_context",
    "get_tracer",
    "histogram",
    "logger",
    "reconfigure_telemetry",
    "record_red_metrics",
    "record_use_metrics",
    "register_cardinality_limit",
    "register_pii_rule",
    "reload_runtime_from_env",
    "replace_pii_rules",
    "set_exporter_policy",
    "set_queue_policy",
    "set_sampling_policy",
    "set_trace_context",
    "setup_telemetry",
    "should_sample",
    "shutdown_telemetry",
    "trace",
    "tracer",
    "unbind_context",
    "update_runtime_config",
]
