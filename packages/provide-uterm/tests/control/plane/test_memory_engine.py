from __future__ import annotations

import pytest

from provide.uterm.control.plane import ControlPlaneBackend, ControlPlaneConfig, bootstrap_control_plane


@pytest.mark.asyncio
async def test_bootstrap_control_plane_selects_memory_backend() -> None:
    backend: ControlPlaneBackend = "memory"
    plane = await bootstrap_control_plane(ControlPlaneConfig(backend=backend))

    assert plane.__class__.__name__ == "MemoryControlPlane"
