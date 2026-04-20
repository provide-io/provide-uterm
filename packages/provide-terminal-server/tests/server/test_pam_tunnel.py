#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.server.pam_tunnel import PamTunnelBridge


def _make_tunnel_mock() -> MagicMock:
    tunnel = MagicMock()
    tunnel.connect = AsyncMock()
    tunnel.open_terminal = AsyncMock()
    tunnel.send_data = AsyncMock()
    tunnel.close = AsyncMock()
    tunnel.recv = AsyncMock()
    return tunnel


def _make_pty_connector(master_fd: int = 99) -> MagicMock:
    conn = MagicMock()
    conn.__class__.__name__ = "PTYConnector"
    type(conn).__name__ = "PTYConnector"
    conn._master_fd = master_fd
    return conn


def _make_capture_connector() -> MagicMock:
    conn = MagicMock()
    conn.__class__.__name__ = "CaptureConnector"
    type(conn).__name__ = "CaptureConnector"
    capture_socket = MagicMock()
    capture_socket.read_frame = AsyncMock()
    conn._capture = capture_socket
    return conn


# ── start / stop lifecycle ────────────────────────────────────────────────────


async def test_bridge_start_connects_tunnel() -> None:
    tunnel = _make_tunnel_mock()
    connector = _make_capture_connector()
    connector._capture.read_frame = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel):
        bridge = PamTunnelBridge("wss://x", "tok", connector)
        await bridge.start()
        await bridge.stop()

    tunnel.connect.assert_awaited_once()
    tunnel.open_terminal.assert_awaited_once_with(cols=80, rows=24)


async def test_bridge_stop_closes_tunnel() -> None:
    tunnel = _make_tunnel_mock()
    connector = _make_capture_connector()
    connector._capture.read_frame = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel):
        bridge = PamTunnelBridge("wss://x", "tok", connector)
        await bridge.start()
        await bridge.stop()

    tunnel.close.assert_awaited_once()


async def test_bridge_stop_cancels_tasks() -> None:
    tunnel = _make_tunnel_mock()
    connector = _make_capture_connector()

    # read_frame blocks forever until cancelled
    async def _block() -> None:
        await asyncio.sleep(3600)

    connector._capture.read_frame = AsyncMock(side_effect=_block)

    with patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel):
        bridge = PamTunnelBridge("wss://x", "tok", connector)
        await bridge.start()
        assert len(bridge._tasks) == 1
        await bridge.stop()
        assert len(bridge._tasks) == 0


async def test_bridge_stop_idempotent() -> None:
    tunnel = _make_tunnel_mock()
    connector = _make_capture_connector()
    connector._capture.read_frame = AsyncMock(side_effect=asyncio.CancelledError)

    with patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel):
        bridge = PamTunnelBridge("wss://x", "tok", connector)
        await bridge.start()
        await bridge.stop()
        await bridge.stop()  # second stop must not raise


# ── capture bridge ────────────────────────────────────────────────────────────


def _fake_capture_module() -> types.ModuleType:
    """Return a minimal stub for provide.terminal.pty.capture."""
    mod = types.ModuleType("provide.terminal.pty.capture")
    mod.CHANNEL_STDOUT = 1  # type: ignore[attr-defined]
    mod.CHANNEL_STDIN = 0  # type: ignore[attr-defined]
    return mod


def _pty_modules_patch() -> dict[str, types.ModuleType]:
    fake_capture = _fake_capture_module()
    fake_pty = types.ModuleType("provide.terminal.pty")
    return {
        "provide.terminal.pty": fake_pty,
        "provide.terminal.pty.capture": fake_capture,
    }


async def test_capture_bridge_sends_stdout_to_tunnel() -> None:
    tunnel = _make_tunnel_mock()
    connector = _make_capture_connector()

    frame = SimpleNamespace(channel=1, data=b"hello world")  # CHANNEL_STDOUT == 1
    call_count = 0

    async def _read_frame() -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return frame
        raise asyncio.CancelledError

    connector._capture.read_frame = AsyncMock(side_effect=_read_frame)

    with (
        patch.dict(sys.modules, _pty_modules_patch()),
        patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel),
    ):
        bridge = PamTunnelBridge("wss://x", "tok", connector)
        await bridge.start()
        await asyncio.sleep(0.05)
        await bridge.stop()

    tunnel.send_data.assert_awaited_with(b"hello world")


async def test_capture_bridge_ignores_non_stdout_frames() -> None:
    tunnel = _make_tunnel_mock()
    connector = _make_capture_connector()

    frame = SimpleNamespace(channel=0, data=b"keystroke")  # CHANNEL_STDIN == 0
    call_count = 0

    async def _read_frame() -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return frame
        raise asyncio.CancelledError

    connector._capture.read_frame = AsyncMock(side_effect=_read_frame)

    with (
        patch.dict(sys.modules, _pty_modules_patch()),
        patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel),
    ):
        bridge = PamTunnelBridge("wss://x", "tok", connector)
        await bridge.start()
        await asyncio.sleep(0.05)
        await bridge.stop()

    tunnel.send_data.assert_not_awaited()


# ── PTY bridge ────────────────────────────────────────────────────────────────


async def test_pty_bridge_reads_from_master_fd() -> None:
    """_start_pty_bridge registers an add_reader callback on master_fd."""
    tunnel = _make_tunnel_mock()

    from provide.terminal.tunnel.protocol import TunnelFrame

    # recv returns an EOF frame immediately
    tunnel.recv = AsyncMock(
        return_value=TunnelFrame(channel=1, flags=0x01, payload=b"")  # is_eof
    )

    connector = _make_pty_connector(master_fd=42)

    added_readers: list[int] = []

    def _add_reader(fd: int, cb: object) -> None:
        added_readers.append(fd)

    with (
        patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel),
        patch("asyncio.get_event_loop") as mock_loop,
    ):
        mock_loop.return_value.add_reader = _add_reader
        mock_loop.return_value.remove_reader = MagicMock()
        bridge = PamTunnelBridge("wss://x", "tok", connector)
        await bridge.start()
        await asyncio.sleep(0.05)
        await bridge.stop()

    assert 42 in added_readers


async def test_pty_bridge_writes_tunnel_data_to_fd() -> None:
    """Tunnel CHANNEL_DATA frames are written to master_fd."""

    from provide.terminal.tunnel.protocol import CHANNEL_DATA, TunnelFrame

    tunnel = _make_tunnel_mock()
    call_count = 0

    async def _recv() -> TunnelFrame:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return TunnelFrame(channel=CHANNEL_DATA, flags=0, payload=b"input from browser")
        raise asyncio.CancelledError

    tunnel.recv = AsyncMock(side_effect=_recv)
    connector = _make_pty_connector(master_fd=55)

    written: list[tuple[int, bytes]] = []

    def _fake_write(fd: int, data: bytes) -> int:
        written.append((fd, data))
        return len(data)

    with (
        patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel),
        patch("asyncio.get_event_loop") as mock_loop,
        patch("os.write", side_effect=_fake_write),
    ):
        mock_loop.return_value.add_reader = MagicMock()
        mock_loop.return_value.remove_reader = MagicMock()
        bridge = PamTunnelBridge("wss://x", "tok", connector)
        await bridge.start()
        await asyncio.sleep(0.05)
        await bridge.stop()

    assert any(data == b"input from browser" for _, data in written)
