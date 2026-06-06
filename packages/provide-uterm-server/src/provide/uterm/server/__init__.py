#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Standalone hosted server for provide-uterm.

The public names are resolved lazily via ``__getattr__`` so that importing this
package does not eagerly pull in :mod:`provide.uterm.server.app` (the FastAPI
app factory). The Cloudflare Worker imports lightweight server submodules (e.g.
``provide.uterm.server.bridge.rest_helpers``) under Pyodide, where FastAPI is
not importable — an eager factory import there would fail the whole subtree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.config import config_from_mapping, default_server_config, load_server_config

__all__ = ["config_from_mapping", "create_server_app", "default_server_config", "load_server_config"]


def __getattr__(name: str) -> object:
    if name == "create_server_app":
        from provide.uterm.server.app import create_server_app

        return create_server_app
    if name in {"config_from_mapping", "default_server_config", "load_server_config"}:
        from provide.uterm.server import config

        return getattr(config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
