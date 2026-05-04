from __future__ import annotations

from provide.terminal import control_channel_namespace, control_plane_namespace
from provide.terminal.control import channel, plane
from provide.terminal.control.channel import BrowserControlMessage, WorkerControlMessage
from provide.terminal.control.plane import ControlPlane, ControlPlaneConfig, EngineCapabilities


def test_control_import_surface_exposes_channel_and_plane() -> None:
    assert channel is not None
    assert plane is not None
    assert BrowserControlMessage.__name__ == "BrowserControlMessage"
    assert WorkerControlMessage.__name__ == "WorkerControlMessage"
    assert ControlPlane.__name__ == "ControlPlane"
    assert ControlPlaneConfig.__name__ == "ControlPlaneConfig"
    assert EngineCapabilities.__name__ == "EngineCapabilities"


def test_terminal_namespace_reexports_control_namespaces() -> None:
    assert control_channel_namespace is channel
    assert control_plane_namespace is plane
