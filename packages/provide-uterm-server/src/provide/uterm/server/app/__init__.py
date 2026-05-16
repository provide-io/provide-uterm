#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI application factory package for the hosted terminal server.

This package re-exports the public surface that historically lived in the
single-file ``provide.uterm.server.app`` module.  The implementation is
split across topical submodules; this ``__init__`` is intentionally
logic-free.
"""

from __future__ import annotations

# ``importlib`` is re-exported so test code that patches
# ``provide.uterm.server.app.importlib.resources.files`` continues to work
# unchanged after the package split.
import importlib.resources

from provide.uterm.server.app.assets import _validate_frontend_assets
from provide.uterm.server.app.auth import _validate_auth_config
from provide.uterm.server.app.connectors import _register_builtin_connectors
from provide.uterm.server.app.control_plane import _build_control_plane
from provide.uterm.server.app.factory import create_server_app

__all__ = [
    "_build_control_plane",
    "_register_builtin_connectors",
    "_validate_auth_config",
    "_validate_frontend_assets",
    "create_server_app",
    "importlib",
]
