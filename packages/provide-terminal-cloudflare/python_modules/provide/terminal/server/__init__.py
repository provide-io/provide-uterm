#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Standalone hosted server for provide-terminal."""

from __future__ import annotations

from provide.terminal.server.app import create_server_app
from provide.terminal.server.config import config_from_mapping, default_server_config, load_server_config

__all__ = ["config_from_mapping", "create_server_app", "default_server_config", "load_server_config"]
