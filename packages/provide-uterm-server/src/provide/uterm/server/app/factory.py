#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Public exports for the split FastAPI application factory."""

from __future__ import annotations

from provide.uterm.server.app.factory_impl import _detect_multi_replica_environment, create_server_app

__all__ = ["_detect_multi_replica_environment", "create_server_app"]
