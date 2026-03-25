# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Runtime config/policy update API.

Hot-reconfigurable: sampling policies, backpressure queue limits, exporter retry/timeout policies.
NOT hot-reconfigurable: log handlers, tracer providers, meter providers (require full restart).
Use ``reconfigure_telemetry()`` for a full shutdown+setup cycle when providers must change.
"""

from __future__ import annotations

__all__ = [
    "get_runtime_config",
    "reconfigure_telemetry",
    "reload_runtime_from_env",
    "update_runtime_config",
]

import copy
import threading

from provide.telemetry.backpressure import QueuePolicy, set_queue_policy
from provide.telemetry.config import TelemetryConfig
from provide.telemetry.resilience import ExporterPolicy, set_exporter_policy
from provide.telemetry.sampling import SamplingPolicy, set_sampling_policy

_lock = threading.Lock()
_active_config: TelemetryConfig | None = None


def apply_runtime_config(config: TelemetryConfig) -> None:
    """Apply a config snapshot to runtime signal policies."""
    global _active_config
    with _lock:
        snapshot = copy.deepcopy(config)
        _active_config = snapshot
        set_sampling_policy("logs", SamplingPolicy(default_rate=snapshot.sampling.logs_rate))  # pragma: no mutate
        set_sampling_policy("traces", SamplingPolicy(default_rate=snapshot.sampling.traces_rate))
        set_sampling_policy("metrics", SamplingPolicy(default_rate=snapshot.sampling.metrics_rate))
        set_queue_policy(
            QueuePolicy(
                logs_maxsize=snapshot.backpressure.logs_maxsize,
                traces_maxsize=snapshot.backpressure.traces_maxsize,
                metrics_maxsize=snapshot.backpressure.metrics_maxsize,
            )
        )
        set_exporter_policy(
            "logs",
            ExporterPolicy(
                retries=snapshot.exporter.logs_retries,
                backoff_seconds=snapshot.exporter.logs_backoff_seconds,
                timeout_seconds=snapshot.exporter.logs_timeout_seconds,
                fail_open=snapshot.exporter.logs_fail_open,
                allow_blocking_in_event_loop=snapshot.exporter.logs_allow_blocking_in_event_loop,
            ),
        )
        set_exporter_policy(
            "traces",
            ExporterPolicy(
                retries=snapshot.exporter.traces_retries,
                backoff_seconds=snapshot.exporter.traces_backoff_seconds,
                timeout_seconds=snapshot.exporter.traces_timeout_seconds,
                fail_open=snapshot.exporter.traces_fail_open,
                allow_blocking_in_event_loop=snapshot.exporter.traces_allow_blocking_in_event_loop,
            ),
        )
        set_exporter_policy(
            "metrics",
            ExporterPolicy(
                retries=snapshot.exporter.metrics_retries,
                backoff_seconds=snapshot.exporter.metrics_backoff_seconds,
                timeout_seconds=snapshot.exporter.metrics_timeout_seconds,
                fail_open=snapshot.exporter.metrics_fail_open,
                allow_blocking_in_event_loop=snapshot.exporter.metrics_allow_blocking_in_event_loop,
            ),
        )


def update_runtime_config(config: TelemetryConfig) -> TelemetryConfig:
    """Apply config and return the active runtime snapshot."""
    apply_runtime_config(config)
    return get_runtime_config()


def reload_runtime_from_env() -> TelemetryConfig:
    """Reload environment config, apply it, and return the active snapshot."""
    cfg = TelemetryConfig.from_env()
    apply_runtime_config(cfg)
    return get_runtime_config()


def reconfigure_telemetry(config: TelemetryConfig | None = None) -> TelemetryConfig:
    """Apply hot runtime updates or fail fast when provider replacement would be required."""
    from provide.telemetry.logger import core as logger_core
    from provide.telemetry.metrics import provider as metrics_provider
    from provide.telemetry.setup import setup_telemetry, shutdown_telemetry
    from provide.telemetry.tracing import provider as tracing_provider

    target = config or TelemetryConfig.from_env()
    current = get_runtime_config()
    if _provider_config_changed(current, target):
        if (
            logger_core._has_otel_log_provider()
            or tracing_provider._has_tracing_provider()
            or metrics_provider._has_meter_provider()
        ):
            raise RuntimeError(
                "provider-changing reconfiguration is unsupported after OpenTelemetry providers are installed; "
                "restart the process and call setup_telemetry() with the new config"
            )
        shutdown_telemetry()
        return setup_telemetry(target)
    return update_runtime_config(target)


_COLD_KEYS = frozenset(
    {
        "service_name",
        "environment",
        "version",
        "strict_schema",
        "tracing",
        "metrics",
        "event_schema",
        "slo",
    }
)


def _provider_config_changed(current: TelemetryConfig, target: TelemetryConfig) -> bool:
    return any(getattr(current, k) != getattr(target, k) for k in _COLD_KEYS)


def get_runtime_config() -> TelemetryConfig:
    """Return a defensive copy of the active runtime config snapshot."""
    with _lock:
        if _active_config is None:
            return TelemetryConfig.from_env()
        return copy.deepcopy(_active_config)


def _is_strict_event_name() -> bool:
    """Check strict event-name mode without deepcopy (hot-path optimised)."""
    with _lock:
        if _active_config is None:
            return False
        return _active_config.strict_schema or _active_config.event_schema.strict_event_name


def reset_runtime_for_tests() -> None:
    """Clear the cached runtime config snapshot."""
    global _active_config
    with _lock:
        _active_config = None
