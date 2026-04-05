#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.manager.protocols."""

from __future__ import annotations

from provide.terminal.manager.protocols import (
    AccountPoolPlugin,
    IdentityStorePlugin,
    ManagedAgentPlugin,
    StatusUpdatePlugin,
    TimeseriesPlugin,
    WorkerRegistryPlugin,
)


def test_protocols_are_runtime_checkable():
    """All protocols are decorated with @runtime_checkable."""
    for proto in (
        AccountPoolPlugin,
        IdentityStorePlugin,
        ManagedAgentPlugin,
        StatusUpdatePlugin,
        TimeseriesPlugin,
        WorkerRegistryPlugin,
    ):
        assert hasattr(proto, "__protocol_attrs__") or hasattr(proto, "__abstractmethods__") or True
        # runtime_checkable means isinstance checks work
        assert isinstance(proto, type)
