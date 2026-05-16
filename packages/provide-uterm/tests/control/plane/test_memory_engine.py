#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import pytest

from provide.uterm.control.plane import ControlPlaneBackend, ControlPlaneConfig, bootstrap_control_plane


@pytest.mark.asyncio
async def test_bootstrap_control_plane_selects_memory_backend() -> None:
    backend: ControlPlaneBackend = "memory"
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend=backend))

    assert plane.__class__.__name__ == "MemoryControlPlane"
