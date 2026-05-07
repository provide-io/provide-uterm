#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for ``provide.terminal.server.app.connectors``.

Covers the external-plugin loop in ``_register_builtin_connectors`` (factory
lines 60-64): both the success path that imports a real module, and the
``ImportError`` path that logs a load failure for an unresolvable dotted path.
"""

from __future__ import annotations

import logging

import pytest

from provide.terminal.server.app.connectors import _register_builtin_connectors
from provide.terminal.server.config_schema import UtermServerConfig


def _make_config(*external_connectors: str) -> UtermServerConfig:
    """Build a default server config with the supplied external connector paths."""
    config = UtermServerConfig()
    config.governance.external_connectors = list(external_connectors)
    return config


def test_external_connector_loads_real_module(caplog: pytest.LogCaptureFixture) -> None:
    """A guaranteed-to-exist module path runs the success branch and logs ``connector_plugin_loaded``."""
    # ``json`` is in the stdlib and always importable; using it avoids tmp-path
    # plumbing and still exercises the success branch end-to-end.
    config = _make_config("json")
    with caplog.at_level(logging.INFO, logger="provide.terminal.server.app.connectors"):
        _register_builtin_connectors(config)
    assert any("connector_plugin_loaded" in record.getMessage() for record in caplog.records)
    assert any("module=json" in record.getMessage() for record in caplog.records)


def test_external_connector_missing_module_logs_error(caplog: pytest.LogCaptureFixture) -> None:
    """A nonexistent dotted path triggers ``ImportError`` and logs ``connector_plugin_load_failed``."""
    config = _make_config("nonexistent.fake.module")
    with caplog.at_level(logging.ERROR, logger="provide.terminal.server.app.connectors"):
        # Must not raise — the loop swallows ImportError and only logs.
        _register_builtin_connectors(config)
    assert any("connector_plugin_load_failed" in record.getMessage() for record in caplog.records)
    assert any("module=nonexistent.fake.module" in record.getMessage() for record in caplog.records)
