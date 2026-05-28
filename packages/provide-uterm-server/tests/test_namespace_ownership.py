#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import importlib

import pytest


def test_core_bridge_facade_owns_user_facing_primitives() -> None:
    bridge = importlib.import_module("provide.uterm.bridge")

    assert "packages/provide-uterm/src/provide/uterm/bridge/__init__.py" in str(bridge.__file__)
    assert hasattr(bridge, "HijackCoordinator")
    assert hasattr(bridge, "HijackableMixin")


def test_server_runtime_bridge_imports_from_server_namespace() -> None:
    bridge_hub = importlib.import_module("provide.uterm.server.bridge.hub")

    assert hasattr(bridge_hub, "TermHub")


def test_old_runtime_bridge_namespace_is_not_available() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("provide.uterm.bridge.hub")
