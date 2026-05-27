#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for ``uterm share`` CLI subcommand."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.cli.share import (
    _cmd_share,
    _display_name,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TUNNEL_RESPONSE: dict[str, Any] = {
    "tunnel_id": "tun-abc123",
    "share_url": "https://warp.example.com/view/tun-abc123",
    "control_url": "https://warp.example.com/control/tun-abc123",
    "ws_endpoint": "wss://warp.example.com/ws/tunnel/tun-abc123",
    "worker_token": "tok-worker-secret",
}


def _make_args(**overrides: Any) -> Any:
    """Build a minimal argparse.Namespace for _cmd_share."""
    defaults: dict[str, Any] = {
        "server": "https://warp.example.com",
        "cmd": None,
        "token": None,
        "token_file": "/nonexistent/.uterm/session_token",
        "attach": False,
        "display_name": None,
    }
    defaults.update(overrides)
    ns = MagicMock()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestRunShare:
    @pytest.mark.asyncio
    async def test_missing_websockets_dependency(self) -> None:
        """If websockets not installed, exit with error."""
        from provide.uterm.cli.share import _run_share

        mock_pty = MagicMock()

        # Patch the import inside _run_share to raise ImportError
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def _fake_import(name: str, *a: Any, **kw: Any) -> Any:
            if name == "websockets":
                raise ImportError("no websockets")
            return real_import(name, *a, **kw)

        with (
            patch("builtins.__import__", side_effect=_fake_import),
            pytest.raises(SystemExit),
        ):
            await _run_share(mock_pty, "wss://x.com/ws", "tok")

    @pytest.mark.asyncio
    async def test_bridge_loop_pty_to_ws(self) -> None:
        """Data flows from PTY read → ws_send."""
        from provide.uterm.cli.share import _bridge_loop

        mock_pty = AsyncMock()
        # read returns data once, then empty bytes to signal EOF
        mock_pty.read = AsyncMock(side_effect=[b"hello", b""])

        sent: list[bytes] = []

        async def ws_send(data: bytes) -> None:
            sent.append(data)

        async def ws_recv() -> bytes:
            return b""

        await _bridge_loop(mock_pty, ws_send, ws_recv, is_attach=False)

        assert len(sent) == 1
        # Frame: channel=0x01, flags=0x00, payload=b"hello"
        assert sent[0] == bytes([0x01, 0x00]) + b"hello"

    @pytest.mark.asyncio
    async def test_bridge_loop_ws_to_pty_write(self) -> None:
        """Data flows from ws_recv → PTY write (spawn mode)."""
        from provide.uterm.cli.share import _bridge_loop

        mock_pty = AsyncMock()
        mock_pty.read = AsyncMock(return_value=b"")
        mock_pty.write = AsyncMock()

        recv_data = [b"world", b""]
        idx = 0

        async def ws_recv() -> bytes:
            nonlocal idx
            val = recv_data[idx]
            idx += 1
            return val

        async def ws_send(data: bytes) -> None:
            pass

        await _bridge_loop(mock_pty, ws_send, ws_recv, is_attach=False)
        mock_pty.write.assert_called_once_with(b"world")

    @pytest.mark.asyncio
    async def test_bridge_loop_attach_writes_local(self) -> None:
        """In attach mode, ws_recv data goes to write_local, not write."""
        from provide.uterm.cli.share import _bridge_loop

        mock_pty = AsyncMock()
        mock_pty.read = AsyncMock(return_value=b"")
        mock_pty.write_local = AsyncMock()

        recv_data = [b"output", b""]
        idx = 0

        async def ws_recv() -> bytes:
            nonlocal idx
            val = recv_data[idx]
            idx += 1
            return val

        async def ws_send(data: bytes) -> None:
            pass

        await _bridge_loop(mock_pty, ws_send, ws_recv, is_attach=True)
        mock_pty.write_local.assert_called_once_with(b"output")


class TestCmdShareRelativeEndpoint:
    def test_relative_ws_endpoint_resolved(self) -> None:
        """Line 192-193: relative /tunnel/... resolved to full wss:// URL."""
        resp = {**_TUNNEL_RESPONSE, "ws_endpoint": "/tunnel/tun-abc123"}
        mock_pty = MagicMock()
        with (
            patch("provide.uterm.cli.share._create_tunnel", return_value=resp),
            patch("provide.uterm.cli.share.spawn_pty", return_value=mock_pty),
            patch("provide.uterm.cli.share.asyncio.run") as mock_run,
        ):
            _cmd_share(_make_args())
        mock_pty.close.assert_called_once()
        # asyncio.run was called with _run_share coroutine
        mock_run.assert_called_once()


class TestDisplayNameEdgeCases:
    def test_getpass_exception_falls_back(self) -> None:
        """Line 110-111: getpass.getuser() raises → user='unknown'."""
        with patch("provide.uterm.cli.share.getpass.getuser", side_effect=KeyError("no user")):
            name = _display_name(_make_args())
        assert name.startswith("unknown@")


class TestBridgeLoopExceptions:
    @pytest.mark.asyncio
    async def test_pty_read_oserror(self) -> None:
        """Line 145-146: OSError in pty_to_ws is caught."""
        from provide.uterm.cli.share import _bridge_loop

        mock_pty = AsyncMock()
        mock_pty.read = AsyncMock(side_effect=OSError("fd closed"))

        async def ws_send(data: bytes) -> None:
            pass

        async def ws_recv() -> bytes:
            return b""

        await _bridge_loop(mock_pty, ws_send, ws_recv)  # no raise

    @pytest.mark.asyncio
    async def test_ws_recv_oserror(self) -> None:
        """Line 158-159: OSError in ws_to_pty is caught."""
        from provide.uterm.cli.share import _bridge_loop

        mock_pty = AsyncMock()
        mock_pty.read = AsyncMock(return_value=b"")
        mock_pty.write = AsyncMock()

        async def ws_send(data: bytes) -> None:
            pass

        async def ws_recv() -> bytes:
            raise OSError("broken pipe")

        await _bridge_loop(mock_pty, ws_send, ws_recv)  # no raise


class TestCmdShareCleanup:
    def test_pty_close_called_on_normal_exit(self) -> None:
        """Line 192-193: pty_source.close() called in finally."""
        mock_pty = MagicMock()
        with (
            patch("provide.uterm.cli.share._create_tunnel", return_value=_TUNNEL_RESPONSE),
            patch("provide.uterm.cli.share.spawn_pty", return_value=mock_pty),
            patch("provide.uterm.cli.share.asyncio.run"),
        ):
            _cmd_share(_make_args())
        mock_pty.close.assert_called_once()
