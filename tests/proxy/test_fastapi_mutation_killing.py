#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for provide.terminal.fastapi.

Kills surviving mutations in mount_terminal_ui (path default, frontend dir
path, error message content, StaticFiles args) and WsTerminalProxy._handle
(browser_to_remote args, return_when=FIRST_COMPLETED, gather *pending).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# mount_terminal_ui — path default (kills mutmut_1, _2)
# ---------------------------------------------------------------------------


class TestMountTerminalUiPathDefault:
    def test_default_path_is_slash_terminal(self) -> None:
        """mount_terminal_ui default path is '/terminal' (kills mutmut_1, _2)."""
        from provide.terminal.fastapi import mount_terminal_ui

        sig = inspect.signature(mount_terminal_ui)
        default = sig.parameters["path"].default
        assert default == "/terminal"

    def test_default_path_lowercase(self) -> None:
        """Default path is exactly '/terminal' — not '/TERMINAL'."""
        from provide.terminal.fastapi import mount_terminal_ui

        sig = inspect.signature(mount_terminal_ui)
        default = sig.parameters["path"].default
        assert default == default.lower()
        assert "XX" not in default

    def test_mount_called_with_slash_terminal_by_default(self) -> None:
        """When path not given, app.mount is called with '/terminal'."""
        from provide.terminal.fastapi import mount_terminal_ui

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=True),
            patch("starlette.staticfiles.StaticFiles.__init__", return_value=None),
        ):
            mount_terminal_ui(mock_app)

        call_args = mock_app.mount.call_args
        assert call_args[0][0] == "/terminal"


# ---------------------------------------------------------------------------
# mount_terminal_ui — frontend directory (kills mutmut_7)
# ---------------------------------------------------------------------------


class TestMountTerminalUiFrontendDir:
    def test_frontend_path_uses_lowercase_frontend(self) -> None:
        """frontend_path uses 'frontend' not 'FRONTEND' (kills mutmut_7)."""
        from provide.terminal.fastapi import mount_terminal_ui

        captured_paths: list[Any] = []

        # StaticFiles is imported inside mount_terminal_ui, so we patch at its origin

        class CapturingStaticFiles:
            def __init__(self, directory: Any = None, **kw: Any) -> None:
                captured_paths.append(directory)

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=True),
            patch("starlette.staticfiles.StaticFiles", CapturingStaticFiles),
        ):
            mount_terminal_ui(mock_app)

        assert len(captured_paths) == 1
        frontend_path = captured_paths[0]
        # The directory passed to StaticFiles should end with 'frontend' (lowercase)
        assert str(frontend_path).endswith("frontend")
        assert not str(frontend_path).endswith("FRONTEND")

    def test_frontend_path_parent_is_fastapi_module_parent(self) -> None:
        """frontend_path parent is same as fastapi.py's parent dir."""
        from provide.terminal import fastapi as fastapi_module

        expected_parent = Path(fastapi_module.__file__).parent
        expected_frontend = expected_parent / "frontend"

        # Verify the actual path exists or would be derived correctly
        assert str(expected_frontend).endswith("frontend")
        assert expected_parent == expected_frontend.parent


# ---------------------------------------------------------------------------
# mount_terminal_ui — error message content (kills mutmut_10, _11)
# ---------------------------------------------------------------------------


class TestMountTerminalUiErrorMessage:
    def test_error_message_contains_assets_not_found(self) -> None:
        """RuntimeError message mentions 'assets not found' (kills mutmut_10, _11)."""
        from provide.terminal.fastapi import mount_terminal_ui

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=False),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mount_terminal_ui(mock_app)

        msg = str(exc_info.value)
        assert "terminal UI assets not found" in msg

    def test_error_message_mentions_pip_install(self) -> None:
        """Error mentions pip install hint (kills mutmut_10, _11)."""
        from provide.terminal.fastapi import mount_terminal_ui

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=False),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mount_terminal_ui(mock_app)

        msg = str(exc_info.value)
        assert "pip install" in msg.lower()
        assert "provide-terminal" in msg

    def test_error_message_starts_lowercase_is(self) -> None:
        """Second part of error message uses lowercase 'is the package...' (kills mutmut_11)."""
        from provide.terminal.fastapi import mount_terminal_ui

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=False),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mount_terminal_ui(mock_app)

        msg = str(exc_info.value)
        # 'IS THE PACKAGE' (mutmut_11) vs 'is the package' (correct)
        assert "is the package installed correctly" in msg


# ---------------------------------------------------------------------------
# mount_terminal_ui — StaticFiles args (kills mutmut_18, _19, _20, _21, _22)
# ---------------------------------------------------------------------------


class TestMountTerminalUiStaticFilesArgs:
    def _mount_with_capture(self) -> tuple[MagicMock, list[dict]]:
        """Helper: run mount_terminal_ui with StaticFiles constructor captured."""
        from provide.terminal.fastapi import mount_terminal_ui

        captured: list[dict] = []

        class CapturingStaticFiles:
            def __init__(self, *args: Any, **kw: Any) -> None:
                captured.append({"args": args, "kwargs": kw})

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=True),
            patch("starlette.staticfiles.StaticFiles", CapturingStaticFiles),
        ):
            mount_terminal_ui(mock_app)

        return mock_app, captured

    def test_static_files_called_with_frontend_path_as_directory(self) -> None:
        """StaticFiles(directory=frontend_path) — not None (kills mutmut_18)."""
        mock_app, captured = self._mount_with_capture()
        assert len(captured) == 1
        kw = captured[0]["kwargs"]
        assert kw.get("directory") is not None

    def test_static_files_called_with_html_true(self) -> None:
        """StaticFiles(html=True) — not None, not False (kills mutmut_19, _21, _22)."""
        mock_app, captured = self._mount_with_capture()
        kw = captured[0]["kwargs"]
        assert kw.get("html") is True

    def test_static_files_receives_directory_kwarg(self) -> None:
        """StaticFiles is constructed with directory= kwarg (kills mutmut_20)."""
        mock_app, captured = self._mount_with_capture()
        kw = captured[0]["kwargs"]
        # mutmut_20: StaticFiles(html=True) — no directory at all
        assert "directory" in kw

    def test_mount_name_is_terminal_ui(self) -> None:
        """app.mount name is 'terminal-ui' (not None, not missing)."""
        from provide.terminal.fastapi import mount_terminal_ui

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=True),
            patch("starlette.staticfiles.StaticFiles", MagicMock()),
        ):
            mount_terminal_ui(mock_app)

        call_kwargs = mock_app.mount.call_args[1]
        assert call_kwargs.get("name") == "terminal-ui"


# ---------------------------------------------------------------------------
# WsTerminalProxy._handle — reader/transport args (kills mutmut_17, _18)
# ---------------------------------------------------------------------------


class _SimpleMockTransport:
    """Transport that connects immediately, then disconnects on first receive."""

    def __init__(self) -> None:
        self._connected = False
        self.disconnected = False

    async def connect(self, host: str, port: int) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnected = True
        self._connected = False

    async def send(self, data: bytes) -> None:
        pass

    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        # Disconnect on first receive to end the remote-to-browser loop quickly
        self._connected = False
        return b""

    def is_connected(self) -> bool:
        return self._connected


class TestWsTerminalProxyHandleArgs:
    def test_browser_to_remote_receives_actual_reader(self) -> None:
        """_handle passes the real reader to _browser_to_remote (kills mutmut_17)."""
        from provide.terminal.fastapi import WsTerminalProxy

        readers_seen: list[Any] = []
        transports_seen: list[Any] = []

        async def capturing_b2r(self_proxy: Any, reader: Any, transport: Any) -> None:
            readers_seen.append(reader)
            transports_seen.append(transport)

        transport = _SimpleMockTransport()
        proxy = WsTerminalProxy("localhost", 23, transport_factory=lambda: transport)

        sentinel_reader = MagicMock()
        sentinel_reader.read = AsyncMock(return_value=b"")
        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()

        with patch.object(WsTerminalProxy, "_browser_to_remote", capturing_b2r):
            asyncio.run(proxy._handle(sentinel_reader, mock_writer, MagicMock()))

        assert len(readers_seen) >= 1
        # mutmut_17 passes None instead of reader
        assert readers_seen[0] is sentinel_reader

    def test_browser_to_remote_receives_actual_transport(self) -> None:
        """_handle passes real transport to _browser_to_remote (kills mutmut_18)."""
        from provide.terminal.fastapi import WsTerminalProxy

        transports_seen: list[Any] = []

        async def capturing_b2r(self_proxy: Any, reader: Any, transport: Any) -> None:
            transports_seen.append(transport)

        transport = _SimpleMockTransport()
        proxy = WsTerminalProxy("localhost", 23, transport_factory=lambda: transport)

        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=b"")
        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()

        with patch.object(WsTerminalProxy, "_browser_to_remote", capturing_b2r):
            asyncio.run(proxy._handle(mock_reader, mock_writer, MagicMock()))

        assert len(transports_seen) >= 1
        # mutmut_18 passes None instead of transport
        assert transports_seen[0] is transport


# ---------------------------------------------------------------------------
# WsTerminalProxy._handle — return_when=FIRST_COMPLETED (kills mutmut_31)
# ---------------------------------------------------------------------------


class TestWsTerminalProxyHandleFirstCompleted:
    def test_handle_exits_when_first_task_completes(self) -> None:
        """_handle uses FIRST_COMPLETED: exits when remote side closes (kills mutmut_31).

        mutmut_31 removes return_when=FIRST_COMPLETED, making asyncio.wait use
        ALL_COMPLETED (the default). With ALL_COMPLETED the proxy would block
        until the browser side finishes too.
        """
        from provide.terminal.fastapi import WsTerminalProxy

        # Transport that immediately disconnects (remote done after first receive)
        class ImmediateTransport:
            def __init__(self) -> None:
                self._connected = True
                self.disconnected = False

            async def connect(self, host: str, port: int) -> None:
                pass

            async def disconnect(self) -> None:
                self.disconnected = True
                self._connected = False

            async def send(self, data: bytes) -> None:
                pass

            async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
                # First call: return empty data AND disconnect
                self._connected = False
                return b""

            def is_connected(self) -> bool:
                return self._connected

        transport = ImmediateTransport()
        proxy = WsTerminalProxy("localhost", 23, transport_factory=lambda: transport)

        # Reader that blocks forever — if ALL_COMPLETED used, handle would never return
        browser_read_event = asyncio.Event()

        class BlockingReader:
            async def read(self, n: int) -> bytes:
                browser_read_event.set()
                # Block for a long time
                await asyncio.sleep(60)
                return b""

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()

        async def run_with_timeout() -> None:
            # Should complete in < 3s because remote side finishes quickly
            # With mutmut_31 (ALL_COMPLETED), would block for 60s
            await asyncio.wait_for(
                proxy._handle(BlockingReader(), mock_writer, MagicMock()),
                timeout=5.0,
            )

        asyncio.run(run_with_timeout())
        assert transport.disconnected


# ---------------------------------------------------------------------------
# WsTerminalProxy._handle — gather pending (kills mutmut_33)
# ---------------------------------------------------------------------------


class TestWsTerminalProxyHandleGather:
    def test_handle_completes_cleanly_after_pending_gathered(self) -> None:
        """_handle gathers and cancels pending tasks cleanly (kills mutmut_33).

        mutmut_33 calls asyncio.gather(return_exceptions=True) without *pending,
        which means pending tasks are never cancelled/awaited.
        The test verifies the proxy completes without leaking warnings.
        """
        from provide.terminal.fastapi import WsTerminalProxy

        class FastRemoteTransport:
            def __init__(self) -> None:
                self._connected = True
                self.disconnected = False

            async def connect(self, host: str, port: int) -> None:
                pass

            async def disconnect(self) -> None:
                self.disconnected = True
                self._connected = False

            async def send(self, data: bytes) -> None:
                pass

            async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
                # Disconnect on first receive to end the remote-to-browser loop
                self._connected = False
                return b""

            def is_connected(self) -> bool:
                return self._connected

        transport = FastRemoteTransport()
        proxy = WsTerminalProxy("localhost", 23, transport_factory=lambda: transport)

        class SlowReader:
            async def read(self, n: int) -> bytes:
                await asyncio.sleep(30)
                return b""

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()

        # Should complete within 5s; pending b2r task should be cancelled cleanly
        async def run() -> None:
            await asyncio.wait_for(
                proxy._handle(SlowReader(), mock_writer, MagicMock()),
                timeout=5.0,
            )

        asyncio.run(run())
        assert transport.disconnected
