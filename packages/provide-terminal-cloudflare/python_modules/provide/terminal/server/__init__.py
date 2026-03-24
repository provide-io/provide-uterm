#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Standalone hosted server for provide-terminal."""

from __future__ import annotations

__all__ = ["config_from_mapping", "create_server_app", "default_server_config", "load_server_config"]

_CONFIG_NAMES = frozenset({"config_from_mapping", "default_server_config", "load_server_config"})


def __getattr__(name: str) -> object:
    if name == "create_server_app":
        from provide.terminal.server.app import create_server_app  # noqa: PLC0415

        return create_server_app
    if name in _CONFIG_NAMES:
        import provide.terminal.server.config as _cfg  # noqa: PLC0415

        return getattr(_cfg, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
