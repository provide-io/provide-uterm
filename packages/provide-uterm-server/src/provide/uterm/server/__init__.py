#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Standalone hosted server for provide-uterm."""

from __future__ import annotations

from provide.uterm.server.app import create_server_app
from provide.uterm.server.config import config_from_mapping, default_server_config, load_server_config

__all__ = ["config_from_mapping", "create_server_app", "default_server_config", "load_server_config"]
