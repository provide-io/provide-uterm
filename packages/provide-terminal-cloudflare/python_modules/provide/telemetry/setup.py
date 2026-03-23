# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Telemetry setup coordinator."""

from __future__ import annotations

__all__ = [
    "setup_telemetry",
    "shutdown_telemetry",
]

import logging
import threading

from provide.telemetry.backpressure import reset_queues_for_tests as _reset_queues
from provide.telemetry.cardinality import clear_cardinality_limits as _reset_cardinality
from provide.telemetry.config import TelemetryConfig
from provide.telemetry.health import reset_health_for_tests as _reset_health
from provide.telemetry.logger.core import _reset_logging_for_tests as _reset_logging
from provide.telemetry.logger.core import configure_logging, shutdown_logging
from provide.telemetry.metrics.provider import _refresh_otel_metrics, setup_metrics, shutdown_metrics
from provide.telemetry.metrics.provider import _set_meter_for_test as _reset_metrics
from provide.telemetry.pii import reset_pii_rules_for_tests as _reset_pii
from provide.telemetry.resilience import reset_resilience_for_tests as _reset_resilience
from provide.telemetry.runtime import apply_runtime_config
from provide.telemetry.runtime import reset_runtime_for_tests as _reset_runtime
from provide.telemetry.sampling import reset_sampling_for_tests as _reset_sampling
from provide.telemetry.tracing.provider import _refresh_otel_tracing, setup_tracing, shutdown_tracing
from provide.telemetry.tracing.provider import _reset_tracing_for_tests as _reset_tracing

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_setup_done = False


def _rollback(completed: list[str]) -> None:
    teardowns = {
        "configure_logging": shutdown_logging,
        "setup_tracing": shutdown_tracing,
        "setup_metrics": shutdown_metrics,
    }
    for step in reversed(completed):
        try:
            teardowns[step]()
        except Exception:
            _logger.warning("setup.rollback.step_failed", exc_info=True)  # pragma: no mutate


def _quiet_otel_sdk_loggers() -> None:
    """Suppress OTel SDK export noise that the resilience layer already handles."""
    for name in ("opentelemetry.exporter", "opentelemetry.sdk"):  # pragma: no mutate
        logging.getLogger(name).setLevel(logging.CRITICAL)  # pragma: no mutate


def setup_telemetry(config: TelemetryConfig | None = None) -> TelemetryConfig:
    from provide.telemetry.slo import _rebind_slo_instruments, record_red_metrics, record_use_metrics

    global _setup_done
    cfg = config or TelemetryConfig.from_env()
    with _lock:
        if not _setup_done:
            _quiet_otel_sdk_loggers()
            apply_runtime_config(cfg)
            completed: list[str] = []
            try:
                configure_logging(cfg, force=True)
                completed.append("configure_logging")
                _refresh_otel_tracing()
                _refresh_otel_metrics()
                setup_tracing(cfg)
                completed.append("setup_tracing")
                setup_metrics(cfg)
                completed.append("setup_metrics")
                _rebind_slo_instruments()
            except Exception:
                _rollback(completed)
                raise
            _setup_done = True
            if cfg.slo.enable_red_metrics:
                record_red_metrics("startup", "INIT", 200, 0.0)
            if cfg.slo.enable_use_metrics:
                record_use_metrics("startup", 0)
    return cfg


def _reset_setup_state_for_tests() -> None:
    global _setup_done
    with _lock:
        _setup_done = False


def _reset_all_for_tests() -> None:
    from provide.telemetry.slo import _reset_slo_for_tests as _reset_slo

    global _setup_done
    with _lock:
        _setup_done = False
    _reset_logging()
    _reset_tracing()
    _reset_metrics(None)
    _reset_slo()
    _reset_resilience()
    _reset_health()
    _reset_queues()
    _reset_pii()
    _reset_cardinality()
    _reset_sampling()
    _reset_runtime()


def shutdown_telemetry() -> None:
    """Flush and tear down telemetry providers and reset runtime policies."""
    global _setup_done
    with _lock:
        _setup_done = False
        shutdown_tracing()
        shutdown_metrics()
        shutdown_logging()
        _reset_runtime()
