# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""Pytest helpers for consumers of provide-telemetry.

Activate by adding to your ``conftest.py``::

    pytest_plugins = ["provide.telemetry.testing"]

Or in ``pyproject.toml``::

    [tool.pytest.ini_options]
    plugins = ["provide.telemetry.testing"]

This registers fixtures that make ``caplog`` work correctly with structlog
(which otherwise swallows messages before they reach stdlib's logging).
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
import structlog

from provide.telemetry.logger.core import _reset_logging_for_tests
from provide.telemetry.tracing.context import set_trace_context


def configure_caplog_for_structlog(**overrides: Any) -> None:
    """One-shot structlog configuration for caplog compatibility.

    Call this from a ``conftest.py`` fixture or test setup if you need
    the same caplog-friendly pipeline but **without** using the plugin.

    Parameters
    ----------
    **overrides
        Keyword arguments forwarded to ``structlog.configure()``.
        Defaults are the same as the ``_telemetry_caplog_compat`` fixture.
    """
    defaults: dict[str, Any] = {
        "processors": [
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        "wrapper_class": structlog.make_filtering_bound_logger(0),
        "logger_factory": structlog.stdlib.LoggerFactory(),
        "cache_logger_on_first_use": False,
    }
    defaults.update(overrides)
    structlog.configure(**defaults)


def reset_telemetry_state() -> None:
    """Reset structlog and internal logger state.

    Call this in teardown to ensure no cross-test pollution.
    """
    structlog.reset_defaults()
    _reset_logging_for_tests()


def reset_trace_context() -> None:
    """Reset trace context to prevent cross-test leakage."""
    set_trace_context(None, None)


@pytest.fixture(autouse=True)
def _telemetry_caplog_compat() -> Generator[
    None, None, None
]:  # pragma: no cover — fixture glue, logic tested via public API
    """Configure structlog so captured log records reach ``caplog``."""
    configure_caplog_for_structlog()
    yield
    reset_telemetry_state()


@pytest.fixture(autouse=True)
def _telemetry_reset_trace_context() -> None:  # pragma: no cover — fixture glue, logic tested via public API
    """Reset trace context before each test to prevent cross-test leakage."""
    reset_trace_context()
