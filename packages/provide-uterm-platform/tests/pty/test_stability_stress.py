import psutil
import pytest

from provide.terminal.pty.capture_connector import CaptureConnector
from provide.terminal.pty.connector import PTYConnector


@pytest.mark.asyncio
async def test_pty_connector_rapid_lifecycle_no_fd_leak():
    proc = psutil.Process()
    initial_fds = proc.num_fds()

    for _i in range(50):
        connector = PTYConnector("sess", "Sess", {"command": "/bin/echo", "args": ["hello"]})
        await connector.start()
        await connector.stop()

    final_fds = proc.num_fds()
    # Allow some overhead but should not grow linearly
    assert final_fds <= initial_fds + 5

@pytest.mark.asyncio
async def test_capture_connector_with_complex_binary():
    # Use a dummy path for testing
    connector = CaptureConnector("c1", "Capture", {"socket_path": "/tmp/test.sock"})
    await connector.start()

    # CaptureConnector uses is_connected()
    assert connector.is_connected()

    await connector.stop()
    assert not connector.is_connected()
