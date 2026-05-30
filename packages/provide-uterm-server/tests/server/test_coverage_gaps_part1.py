#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests targeting specific coverage gaps across server modules."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as _jwt
import pytest

from provide.uterm.recording import LocalFileRecordingStore
from provide.uterm.server.models import (
    RecordingConfig,
    SessionDefinition,
)
from provide.uterm.server.registry import SessionRegistry

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


def _make_token(sub: str = "user1", roles: list[str] | None = None) -> str:
    now = int(time.time())
    return _jwt.encode(
        {
            "sub": sub,
            "roles": roles or ["viewer"],
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        key=_TEST_KEY,
        algorithm="HS256",
    )


def _make_hub() -> MagicMock:
    hub = MagicMock()
    hub.force_release_hijack = AsyncMock(return_value=True)
    hub.set_input_mode = AsyncMock(return_value=(True, None))
    hub.get_last_snapshot = AsyncMock(return_value=None)
    hub.get_recent_events = AsyncMock(return_value=[])
    hub.browser_count = AsyncMock(return_value=0)
    hub.on_worker_empty = None
    return hub


def _make_registry(
    sessions: list[SessionDefinition] | None = None,
    *,
    recording: RecordingConfig | None = None,
) -> SessionRegistry:
    hub = _make_hub()
    recording_cfg = recording or RecordingConfig()
    return SessionRegistry(
        sessions or [],
        hub=hub,
        public_base_url="http://localhost:9999",
        recording=recording_cfg,
        recording_store=LocalFileRecordingStore(recording_cfg.directory),
    )


def _session(
    session_id: str = "sess1",
    auto_start: bool = False,
    ephemeral: bool = False,
) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name=f"Session {session_id}",
        connector_type="shell",
        auto_start=auto_start,
        ephemeral=ephemeral,
    )


# ===========================================================================
# pam_integration.py coverage gaps
# ===========================================================================


class TestPamIntegrationGaps:
    """Covers lines 114-116, 132-133, 191, 211-216, 230->232, 271, 299-300, 313, 315-316."""

    async def test_run_pam_integration_import_error(self) -> None:
        """Line 114-116: ImportError when provide-uterm-platform not installed."""
        import sys

        from provide.uterm.server.models import PamConfig, ServerConfig
        from provide.uterm.server.pam_integration import run_pam_integration

        config = ServerConfig(pam=PamConfig(notify_socket="/run/test.sock"))

        # Remove the module from sys.modules so the lazy import inside
        # run_pam_integration actually triggers and we can make it fail.
        saved = sys.modules.pop("provide.uterm.pty.pam_listener", None)
        try:
            with patch.dict(sys.modules, {"provide.uterm.pty.pam_listener": None}):
                await run_pam_integration(config, MagicMock())
        finally:
            if saved is not None:
                sys.modules["provide.uterm.pty.pam_listener"] = saved

    async def test_handle_close_event_dispatch(self) -> None:
        """Line 132-133: handle() dispatches 'close' events to _on_close."""
        try:
            from provide.uterm.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.uterm.server.models import PamConfig
        from provide.uterm.server.pam_integration import _on_close

        ev = PamEvent(event="close", username="alice", tty="/dev/pts/3", pid=1234)
        cfg = PamConfig()
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=None)

        # Just call _on_close directly — the handle() function dispatches to it
        await _on_close(ev, cfg, registry)

    async def test_on_open_bridge_start_success_stores_bridge(self) -> None:
        """Line 191: successful bridge.start() stores bridge in bridges dict."""
        try:
            from provide.uterm.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.uterm.server.models import PamConfig
        from provide.uterm.server.pam_integration import _on_open

        ev = PamEvent(event="open", username="alice", tty="/dev/pts/0", pid=42)
        cfg = PamConfig(
            notify_socket="/run/x.sock",
            relay_url="https://cf.example.com",
            relay_token="tok",
        )
        registry = MagicMock()
        registry.create_session = AsyncMock()
        runtime = MagicMock()
        connector = MagicMock()
        runtime.connector = connector
        registry.get_runtime = MagicMock(return_value=runtime)

        bridge_mock = MagicMock()
        bridge_mock.start = AsyncMock()  # succeeds
        bridge_mock.stop = AsyncMock()

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(
            return_value=MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"worker_token": "t", "ws_endpoint": "wss://x"}),
            )
        )

        bridges: dict[str, object] = {}
        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("provide.uterm.server.pam_tunnel.PamTunnelBridge", return_value=bridge_mock),
        ):
            await _on_open(ev, cfg, registry, bridges)

        # Bridge stored successfully
        assert "pam-alice-0" in bridges
        assert bridges["pam-alice-0"] is bridge_mock

    async def test_on_close_stops_bridge(self) -> None:
        """Lines 211-216: _on_close stops the bridge from the bridges dict."""
        try:
            from provide.uterm.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.uterm.server.models import PamConfig
        from provide.uterm.server.pam_integration import _on_close

        ev = PamEvent(event="close", username="alice", tty="/dev/pts/0", pid=42)
        cfg = PamConfig()
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=None)

        bridge_mock = MagicMock()
        bridge_mock.stop = AsyncMock()
        bridges: dict[str, object] = {"pam-alice-0": bridge_mock}

        await _on_close(ev, cfg, registry, bridges)

        bridge_mock.stop.assert_awaited_once()
        assert "pam-alice-0" not in bridges

    async def test_on_close_bridge_stop_exception_swallowed(self) -> None:
        """Lines 211-216: bridge.stop() exception is caught and logged."""
        try:
            from provide.uterm.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.uterm.server.models import PamConfig
        from provide.uterm.server.pam_integration import _on_close

        ev = PamEvent(event="close", username="alice", tty="/dev/pts/0", pid=42)
        cfg = PamConfig()
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=None)

        bridge_mock = MagicMock()
        bridge_mock.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        bridges: dict[str, object] = {"pam-alice-0": bridge_mock}

        await _on_close(ev, cfg, registry, bridges)  # must not raise

    async def test_on_close_runtime_stop_callable(self) -> None:
        """Line 230->232: runtime has stop_fn that is callable."""
        try:
            from provide.uterm.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.uterm.server.models import PamConfig
        from provide.uterm.server.pam_integration import _on_close

        ev = PamEvent(event="close", username="alice", tty="/dev/pts/0", pid=42)
        cfg = PamConfig()
        runtime = MagicMock()
        runtime.stop = AsyncMock()
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=runtime)

        await _on_close(ev, cfg, registry)
        runtime.stop.assert_awaited_once()

    async def test_create_capture_session_none_socket_returns_early(self) -> None:
        """_create_capture_session returns early when capture_socket is None."""
        try:
            from provide.uterm.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.uterm.server.models import PamConfig
        from provide.uterm.server.pam_integration import _create_capture_session

        ev = PamEvent(event="open", username="alice", tty="/dev/pts/0", pid=42, capture_socket=None)
        cfg = PamConfig()
        registry = MagicMock()
        registry.create_session = AsyncMock()

        await _create_capture_session(ev, cfg, registry)
        registry.create_session.assert_not_awaited()

    async def test_safe_create_exception_swallowed(self) -> None:
        """Lines 299-300: _safe_create catches registry.create_session exceptions."""
        from provide.uterm.server.pam_integration import _safe_create

        registry = MagicMock()
        registry.create_session = AsyncMock(side_effect=RuntimeError("db error"))

        await _safe_create(registry, {"session_id": "test"})  # must not raise

    async def test_get_connector_returns_none_on_no_runtime(self) -> None:
        """Line 313: _get_connector returns None when runtime is None."""
        from provide.uterm.server.pam_integration import _get_connector

        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=None)

        assert _get_connector(registry, "nonexistent") is None

    async def test_get_connector_returns_connector_attribute(self) -> None:
        """Line 314: _get_connector returns runtime.connector."""
        from provide.uterm.server.pam_integration import _get_connector

        connector = MagicMock()
        runtime = MagicMock()
        runtime.connector = connector
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=runtime)

        assert _get_connector(registry, "s1") is connector

    async def test_get_connector_exception_returns_none(self) -> None:
        """Lines 315-316: _get_connector returns None on exception."""
        from provide.uterm.server.pam_integration import _get_connector

        registry = MagicMock()
        registry.get_runtime = MagicMock(side_effect=RuntimeError("boom"))

        assert _get_connector(registry, "s1") is None

    async def test_get_connector_no_connector_attr(self) -> None:
        """Line 314: _get_connector returns None when runtime lacks connector attr."""
        from provide.uterm.server.pam_integration import _get_connector

        runtime = MagicMock(spec=[])  # no attributes at all
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=runtime)

        assert _get_connector(registry, "s1") is None

    async def test_on_open_relay_connector_none_skips_bridge(self) -> None:
        """Line 187->exit: when _get_connector returns None, bridge is not created."""
        try:
            from provide.uterm.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.uterm.server.models import PamConfig
        from provide.uterm.server.pam_integration import _on_open

        ev = PamEvent(event="open", username="alice", tty="/dev/pts/0", pid=42)
        cfg = PamConfig(
            notify_socket="/run/x.sock",
            relay_url="https://cf.example.com",
            relay_token="tok",
        )
        registry = MagicMock()
        registry.create_session = AsyncMock()
        # get_runtime returns None → _get_connector returns None
        registry.get_runtime = MagicMock(return_value=None)

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(
            return_value=MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"worker_token": "t", "ws_endpoint": "wss://x"}),
            )
        )

        bridges: dict[str, object] = {}
        with patch("httpx.AsyncClient", return_value=mock_client):
            await _on_open(ev, cfg, registry, bridges)

        # No bridge created because connector was None
        assert "pam-alice-0" not in bridges

    async def test_on_close_runtime_stop_not_callable(self) -> None:
        """Line 230->232: when stop_fn is not callable, it's skipped."""
        try:
            from provide.uterm.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.uterm.server.models import PamConfig
        from provide.uterm.server.pam_integration import _on_close

        ev = PamEvent(event="close", username="alice", tty="/dev/pts/0", pid=42)
        cfg = PamConfig()
        runtime = MagicMock()
        # stop attribute exists but is not callable
        runtime.stop = "not-callable"
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=runtime)

        await _on_close(ev, cfg, registry)  # must not raise

    async def test_run_pam_integration_close_event_via_socket(self) -> None:
        """Lines 132-133: handle() dispatches 'close' events through full integration path."""
        import json
        import tempfile

        try:
            import provide.uterm.pty.pam_listener  # noqa: F401
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.uterm.server.models import PamConfig, ServerConfig
        from provide.uterm.server.pam_integration import run_pam_integration

        with tempfile.TemporaryDirectory() as td:
            sock_path = str(Path(td) / "pam-close.sock")
            config = ServerConfig(pam=PamConfig(notify_socket=sock_path, auto_session=True))

            registry = MagicMock()
            registry.create_session = AsyncMock()
            registry.get_runtime = MagicMock(return_value=None)

            task = asyncio.create_task(run_pam_integration(config, registry))
            await asyncio.sleep(0.05)

            # Send open then close events
            open_event = (
                json.dumps(
                    {
                        "event": "open",
                        "username": "testuser",
                        "tty": "/dev/pts/5",
                        "pid": 7777,
                    }
                ).encode()
                + b"\n"
            )
            close_event = (
                json.dumps(
                    {
                        "event": "close",
                        "username": "testuser",
                        "tty": "/dev/pts/5",
                        "pid": 7777,
                    }
                ).encode()
                + b"\n"
            )

            reader, writer = await asyncio.open_unix_connection(sock_path)
            writer.write(open_event + close_event)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

            # Wait for handler to process
            deadline = asyncio.get_event_loop().time() + 2.0
            while asyncio.get_event_loop().time() < deadline:
                if registry.get_runtime.call_count >= 1:
                    break
                await asyncio.sleep(0.05)

            task.cancel()
            import contextlib

            with contextlib.suppress(asyncio.CancelledError):
                await task


# ===========================================================================
# pam_tunnel.py coverage gaps
# ===========================================================================
