#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Control-plane backend selection for the hosted terminal server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from provide.uterm.control.plane import ControlPlane as SharedControlPlane
from provide.uterm.control.plane import ControlPlaneConfig as SharedControlPlaneConfig
from provide.uterm.control.plane.memory import MemoryControlPlane
from provide.uterm.control.plane.sqlite import SqliteControlPlane

if TYPE_CHECKING:
    from provide.uterm.server.models import ServerConfig


def _build_control_plane(config: ServerConfig) -> SharedControlPlane:
    shared_config = SharedControlPlaneConfig(
        backend=config.control_plane.backend,
        database_url=config.control_plane.database_url or ":memory:",
    )
    if shared_config.backend == "sqlite":
        return SqliteControlPlane(shared_config)
    return MemoryControlPlane(shared_config)
