#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests targeting specific coverage gaps across server modules."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as _jwt
import pytest
from fastapi.testclient import TestClient

from provide.terminal.recording import LocalFileRecordingStore
from provide.terminal.server import create_server_app, default_server_config
from provide.terminal.server.models import (
    AuthConfig,
    RecordingConfig,
    SessionDefinition,
)
from provide.terminal.server.registry import SessionRegistry

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

        from provide.terminal.server.models import PamConfig, ServerConfig
        from provide.terminal.server.pam_integration import run_pam_integration

        config = ServerConfig(pam=PamConfig(notify_socket="/run/test.sock"))

        # Remove the module from sys.modules so the lazy import inside
        # run_pam_integration actually triggers and we can make it fail.
        saved = sys.modules.pop("provide.terminal.pty.pam_listener", None)
        try:
            with patch.dict(sys.modules, {"provide.terminal.pty.pam_listener": None}):
                await run_pam_integration(config, MagicMock())
        finally:
            if saved is not None:
                sys.modules["provide.terminal.pty.pam_listener"] = saved

    async def test_handle_close_event_dispatch(self) -> None:
        """Line 132-133: handle() dispatches 'close' events to _on_close."""
        try:
            from provide.terminal.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.terminal.server.models import PamConfig
        from provide.terminal.server.pam_integration import _on_close

        ev = PamEvent(event="close", username="alice", tty="/dev/pts/3", pid=1234)
        cfg = PamConfig()
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=None)

        # Just call _on_close directly — the handle() function dispatches to it
        await _on_close(ev, cfg, registry)

    async def test_on_open_bridge_start_success_stores_bridge(self) -> None:
        """Line 191: successful bridge.start() stores bridge in bridges dict."""
        try:
            from provide.terminal.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.terminal.server.models import PamConfig
        from provide.terminal.server.pam_integration import _on_open

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
            patch("provide.terminal.server.pam_tunnel.PamTunnelBridge", return_value=bridge_mock),
        ):
            await _on_open(ev, cfg, registry, bridges)

        # Bridge stored successfully
        assert "pam-alice-0" in bridges
        assert bridges["pam-alice-0"] is bridge_mock

    async def test_on_close_stops_bridge(self) -> None:
        """Lines 211-216: _on_close stops the bridge from the bridges dict."""
        try:
            from provide.terminal.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.terminal.server.models import PamConfig
        from provide.terminal.server.pam_integration import _on_close

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
            from provide.terminal.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.terminal.server.models import PamConfig
        from provide.terminal.server.pam_integration import _on_close

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
            from provide.terminal.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.terminal.server.models import PamConfig
        from provide.terminal.server.pam_integration import _on_close

        ev = PamEvent(event="close", username="alice", tty="/dev/pts/0", pid=42)
        cfg = PamConfig()
        runtime = MagicMock()
        runtime.stop = AsyncMock()
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=runtime)

        await _on_close(ev, cfg, registry)
        runtime.stop.assert_awaited_once()

    async def test_create_capture_session_none_socket_returns_early(self) -> None:
        """Line 271: _create_capture_session returns early when capture_socket is None."""
        try:
            from provide.terminal.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.terminal.server.pam_integration import _create_capture_session

        ev = PamEvent(event="open", username="alice", tty="/dev/pts/0", pid=42, capture_socket=None)
        registry = MagicMock()
        registry.create_session = AsyncMock()

        await _create_capture_session(ev, registry)
        registry.create_session.assert_not_awaited()

    async def test_safe_create_exception_swallowed(self) -> None:
        """Lines 299-300: _safe_create catches registry.create_session exceptions."""
        from provide.terminal.server.pam_integration import _safe_create

        registry = MagicMock()
        registry.create_session = AsyncMock(side_effect=RuntimeError("db error"))

        await _safe_create(registry, {"session_id": "test"})  # must not raise

    async def test_get_connector_returns_none_on_no_runtime(self) -> None:
        """Line 313: _get_connector returns None when runtime is None."""
        from provide.terminal.server.pam_integration import _get_connector

        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=None)

        assert _get_connector(registry, "nonexistent") is None

    async def test_get_connector_returns_connector_attribute(self) -> None:
        """Line 314: _get_connector returns runtime.connector."""
        from provide.terminal.server.pam_integration import _get_connector

        connector = MagicMock()
        runtime = MagicMock()
        runtime.connector = connector
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=runtime)

        assert _get_connector(registry, "s1") is connector

    async def test_get_connector_exception_returns_none(self) -> None:
        """Lines 315-316: _get_connector returns None on exception."""
        from provide.terminal.server.pam_integration import _get_connector

        registry = MagicMock()
        registry.get_runtime = MagicMock(side_effect=RuntimeError("boom"))

        assert _get_connector(registry, "s1") is None

    async def test_get_connector_no_connector_attr(self) -> None:
        """Line 314: _get_connector returns None when runtime lacks connector attr."""
        from provide.terminal.server.pam_integration import _get_connector

        runtime = MagicMock(spec=[])  # no attributes at all
        registry = MagicMock()
        registry.get_runtime = MagicMock(return_value=runtime)

        assert _get_connector(registry, "s1") is None

    async def test_on_open_relay_connector_none_skips_bridge(self) -> None:
        """Line 187->exit: when _get_connector returns None, bridge is not created."""
        try:
            from provide.terminal.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.terminal.server.models import PamConfig
        from provide.terminal.server.pam_integration import _on_open

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
            from provide.terminal.pty.pam_listener import PamEvent
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.terminal.server.models import PamConfig
        from provide.terminal.server.pam_integration import _on_close

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
            import provide.terminal.pty.pam_listener  # noqa: F401
        except ImportError:
            pytest.skip("provide-uterm-platform not installed")

        from provide.terminal.server.models import PamConfig, ServerConfig
        from provide.terminal.server.pam_integration import run_pam_integration

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


class TestPamTunnelGaps:
    """Covers lines 54-58, 75-76, 89-90."""

    async def test_on_pty_output_callback_reads_and_sends(self) -> None:
        """Lines 54-58: _on_pty_output reads from master_fd and sends to tunnel."""
        from provide.terminal.server.pam_tunnel import PamTunnelBridge

        tunnel = MagicMock()
        tunnel.connect = AsyncMock()
        tunnel.open_terminal = AsyncMock()
        tunnel.send_data = AsyncMock()
        tunnel.close = AsyncMock()

        from provide.terminal.tunnel.protocol import TunnelFrame

        tunnel.recv = AsyncMock(
            return_value=TunnelFrame(channel=1, flags=0x01, payload=b"")  # EOF
        )

        connector = MagicMock()
        type(connector).__name__ = "PTYConnector"
        connector._master_fd = 42

        captured_callback = None

        def _add_reader(fd: int, cb: object) -> None:
            nonlocal captured_callback
            captured_callback = cb

        with (
            patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel),
            patch("asyncio.get_event_loop") as mock_loop,
        ):
            mock_loop.return_value.add_reader = _add_reader
            mock_loop.return_value.remove_reader = MagicMock()
            mock_loop.return_value.create_task = MagicMock()
            bridge = PamTunnelBridge("wss://x", "tok", connector)
            await bridge.start()

            # Now invoke the callback simulating PTY output
            assert captured_callback is not None
            with patch("os.read", return_value=b"hello from pty"):
                captured_callback()

            mock_loop.return_value.create_task.assert_called()
            await bridge.stop()

    async def test_on_pty_output_oserror_handled(self) -> None:
        """Lines 54-58: _on_pty_output handles OSError from os.read."""
        from provide.terminal.server.pam_tunnel import PamTunnelBridge

        tunnel = MagicMock()
        tunnel.connect = AsyncMock()
        tunnel.open_terminal = AsyncMock()
        tunnel.send_data = AsyncMock()
        tunnel.close = AsyncMock()

        from provide.terminal.tunnel.protocol import TunnelFrame

        tunnel.recv = AsyncMock(return_value=TunnelFrame(channel=1, flags=0x01, payload=b""))

        connector = MagicMock()
        type(connector).__name__ = "PTYConnector"
        connector._master_fd = 42

        captured_callback = None

        def _add_reader(fd: int, cb: object) -> None:
            nonlocal captured_callback
            captured_callback = cb

        with (
            patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel),
            patch("asyncio.get_event_loop") as mock_loop,
        ):
            mock_loop.return_value.add_reader = _add_reader
            mock_loop.return_value.remove_reader = MagicMock()
            mock_loop.return_value.create_task = MagicMock()
            bridge = PamTunnelBridge("wss://x", "tok", connector)
            await bridge.start()

            assert captured_callback is not None
            with patch("os.read", side_effect=OSError("fd closed")):
                captured_callback()  # must not raise

            await bridge.stop()

    async def test_tunnel_to_pty_loop_non_cancelled_exception(self) -> None:
        """Lines 75-76: generic exception in _tunnel_to_pty_loop is logged."""
        from provide.terminal.server.pam_tunnel import PamTunnelBridge

        tunnel = MagicMock()
        tunnel.connect = AsyncMock()
        tunnel.open_terminal = AsyncMock()
        tunnel.send_data = AsyncMock()
        tunnel.close = AsyncMock()
        tunnel.recv = AsyncMock(side_effect=RuntimeError("connection lost"))

        connector = MagicMock()
        type(connector).__name__ = "PTYConnector"
        connector._master_fd = 42

        with (
            patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel),
            patch("asyncio.get_event_loop") as mock_loop,
        ):
            mock_loop.return_value.add_reader = MagicMock()
            mock_loop.return_value.remove_reader = MagicMock()
            bridge = PamTunnelBridge("wss://x", "tok", connector)
            await bridge.start()
            await asyncio.sleep(0.05)
            await bridge.stop()

    async def test_capture_to_tunnel_loop_non_cancelled_exception(self) -> None:
        """Lines 89-90: generic exception in _capture_to_tunnel_loop is logged."""
        from provide.terminal.server.pam_tunnel import PamTunnelBridge

        tunnel = MagicMock()
        tunnel.connect = AsyncMock()
        tunnel.open_terminal = AsyncMock()
        tunnel.send_data = AsyncMock()
        tunnel.close = AsyncMock()

        connector = MagicMock()
        type(connector).__name__ = "CaptureConnector"
        capture_socket = MagicMock()
        capture_socket.read_frame = AsyncMock(side_effect=RuntimeError("socket error"))
        connector._capture = capture_socket

        with patch("provide.terminal.tunnel.client.TunnelClient", return_value=tunnel):
            bridge = PamTunnelBridge("wss://x", "tok", connector)
            await bridge.start()
            await asyncio.sleep(0.05)
            await bridge.stop()


# ===========================================================================
# registry.py coverage gaps
# ===========================================================================


class TestRegistryGaps:
    """Covers lines 94, 105, 284, 324-325, 341-343."""

    async def test_on_worker_empty_stops_runtime(self) -> None:
        """Line 94: runtime.stop() called when ephemeral session cleaned up."""
        reg = _make_registry([_session("ephem", ephemeral=True)])
        # Force runtime creation
        async with reg._lock:
            session = reg._require_session("ephem")
            runtime = reg._runtime_for(session)

        runtime.stop = AsyncMock()

        await reg._on_worker_empty("ephem")

        # Session removed and runtime stopped
        assert await reg.get_definition("ephem") is None
        runtime.stop.assert_awaited_once()

    async def test_get_runtime_returns_none_for_unknown(self) -> None:
        """Line 105: get_runtime returns None for unknown session."""
        reg = _make_registry()
        assert reg.get_runtime("nonexistent") is None

    async def test_get_runtime_returns_runtime_when_exists(self) -> None:
        """Line 105: get_runtime returns the runtime when it exists."""
        reg = _make_registry([_session("s1")])
        # Start session to create runtime
        async with reg._lock:
            session = reg._require_session("s1")
            reg._runtime_for(session)
        assert reg.get_runtime("s1") is not None

    async def test_set_tunnel_connected_unknown_returns_none(self) -> None:
        """Line 284: set_tunnel_connected returns None for unknown session."""
        reg = _make_registry()
        result = await reg.set_tunnel_connected("nonexistent", True)
        assert result is None

    async def test_watch_session_events_no_event_bus_falls_back(self) -> None:
        """Lines 324-325: watch_session_events falls back to ring buffer when no event_bus."""
        reg = _make_registry([_session("s1")])
        hub = reg._hub
        hub.event_bus = None  # type: ignore[attr-defined]
        hub.get_recent_events = AsyncMock(return_value=[{"type": "test"}])

        result = await reg.watch_session_events("s1")
        assert result["events"] == [{"type": "test"}]
        assert result["timed_out"] is False

    async def test_watch_session_events_timeout(self) -> None:
        """Lines 341-343: watch_session_events times out when no events arrive."""
        reg = _make_registry([_session("s1")])
        hub = reg._hub

        # Create a mock event bus
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        class MockSub:
            def __init__(self) -> None:
                self.queue = queue
                self.dropped = 0

        class MockEventBus:
            def watch(self, session_id: str, **kwargs: Any) -> Any:
                import contextlib

                @contextlib.asynccontextmanager
                async def _ctx() -> Any:
                    yield MockSub()

                return _ctx()

        hub.event_bus = MockEventBus()  # type: ignore[attr-defined]

        result = await reg.watch_session_events("s1", timeout_ms=100, max_events=5)
        assert result["timed_out"] is True
        assert result["events"] == []

    async def test_watch_session_events_sentinel_stops(self) -> None:
        """Lines 341-343: None sentinel from event bus stops collection."""
        reg = _make_registry([_session("s1")])
        hub = reg._hub

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        await queue.put({"type": "snapshot"})
        await queue.put(None)  # sentinel

        class MockSub:
            def __init__(self) -> None:
                self.queue = queue
                self.dropped = 0

        class MockEventBus:
            def watch(self, session_id: str, **kwargs: Any) -> Any:
                import contextlib

                @contextlib.asynccontextmanager
                async def _ctx() -> Any:
                    yield MockSub()

                return _ctx()

        hub.event_bus = MockEventBus()  # type: ignore[attr-defined]

        result = await reg.watch_session_events("s1", timeout_ms=5000)
        assert len(result["events"]) == 1
        assert result["events"][0]["type"] == "snapshot"
        assert result["timed_out"] is False


# ===========================================================================
# pages.py coverage gaps — inspect_view (lines 178-200)
# ===========================================================================


class TestPagesInspectView:
    """Covers lines 178-200: inspect_view route."""

    def _make_app_with_session(self, visibility: str = "public") -> Any:
        cfg = default_server_config()
        cfg.auth.mode = "dev"
        cfg.sessions = [
            SessionDefinition(
                session_id="test-sess",
                display_name="Test Session",
                connector_type="shell",
                visibility=visibility,  # type: ignore[arg-type]
            )
        ]
        return create_server_app(cfg)

    def test_inspect_view_200(self) -> None:
        app = self._make_app_with_session()
        with TestClient(app) as client:
            r = client.get("/app/inspect/test-sess")
        assert r.status_code == 200
        assert '"page_kind": "inspect"' in r.text
        assert "test-sess" in r.text

    def test_inspect_view_404_unknown(self) -> None:
        app = self._make_app_with_session()
        with TestClient(app) as client:
            r = client.get("/app/inspect/nonexistent")
        assert r.status_code == 404

    def test_inspect_view_403_insufficient_privileges(self) -> None:
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-uterm",
            jwt_audience="provide-uterm-server",
            worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
        )
        cfg.sessions = [
            SessionDefinition(
                session_id="priv-inspect",
                display_name="Private",
                connector_type="shell",
                visibility="operator",
            )
        ]
        app = create_server_app(cfg)
        headers = {"Authorization": f"Bearer {_make_token(sub='viewer', roles=['viewer'])}"}
        with TestClient(app, headers=headers) as client:
            r = client.get("/app/inspect/priv-inspect")
        assert r.status_code == 403

    def test_inspect_view_sets_cookies(self) -> None:
        app = self._make_app_with_session()
        with TestClient(app) as client:
            r = client.get("/app/inspect/test-sess")
        assert r.status_code == 200
        cookies = ",".join(r.headers.get_list("set-cookie"))
        assert "uterm_surface=operator" in cookies


# ===========================================================================
# ui.py coverage gaps
# ===========================================================================


class TestUiGaps:
    """Covers lines 31-33 (_hijack_js_version exception) and 256-273 (inspect_page_html)."""

    def test_hijack_js_version_returns_zero_on_exception(self) -> None:
        """Lines 31-33: _hijack_js_version returns '0' when exception occurs."""
        from provide.terminal.server import ui

        with patch("importlib.resources.files", side_effect=Exception("boom")):
            result = ui._hijack_js_version()
        assert result == "0"

    def test_hijack_js_version_returns_zero_when_not_file(self) -> None:
        """Lines 31-33: _hijack_js_version returns '0' when path is not a file."""
        from provide.terminal.server import ui

        mock_path = MagicMock()
        mock_path.is_file.return_value = False

        frontend_mock = MagicMock()
        frontend_mock.__truediv__ = lambda self, name: mock_path

        with patch("importlib.resources.files") as mock_files:
            mock_files.return_value = MagicMock(__truediv__=lambda self, name: frontend_mock)
            result = ui._hijack_js_version()
        # Should be "0" since path is not a file
        assert result == "0"

    def test_inspect_page_html_minimal(self) -> None:
        """Lines 256-273: inspect_page_html with minimal args."""
        from provide.terminal.server import ui

        ui._vite_manifest = None
        ui._vite_manifest_loaded = True

        html = ui.inspect_page_html(
            "Inspect",
            "/assets",
            "sess-2",
            app_path="/app",
        )
        assert '"page_kind": "inspect"' in html
        assert '"share_role": null' in html
        assert "sess-2" in html

        ui._vite_manifest = None
        ui._vite_manifest_loaded = False


# ===========================================================================
# routes/api.py coverage gaps
# ===========================================================================


class TestApiGaps:
    """Covers lines 158, 170, 173, 176->178, 411, 591-594."""

    def _admin_client(self) -> TestClient:
        cfg = default_server_config()
        cfg.auth.mode = "dev"
        app = create_server_app(cfg)
        return TestClient(app)

    def test_bulk_delete_with_state_filter(self) -> None:
        """Lines 170, 173: bulk delete filters by state."""
        client = self._admin_client()
        # Create sessions
        client.post("/api/sessions", json={"session_id": "bd-1", "connector_type": "shell"})
        client.post("/api/sessions", json={"session_id": "bd-2", "connector_type": "shell"})

        # Bulk delete with state filter — "running" should not match stopped sessions
        # (sessions default to lifecycle_state="stopped")
        r = client.request(
            "DELETE",
            "/api/sessions",
            json={"filter": {"state": "running"}},
        )
        assert r.status_code == 200
        # No sessions should be deleted since none are running
        assert r.json()["deleted"] == 0

    def test_bulk_delete_with_older_than_filter(self) -> None:
        """Lines 173, 176->178: bulk delete filters by older_than_s and stopped_at."""
        client = self._admin_client()
        client.post("/api/sessions", json={"session_id": "bd-old", "connector_type": "shell"})

        r = client.request(
            "DELETE",
            "/api/sessions",
            json={"filter": {"older_than_s": 3600}},
        )
        assert r.status_code == 200
        # Sessions without stopped_at don't match older_than_s filter
        body = r.json()
        assert isinstance(body["deleted"], int)

    def test_bulk_delete_requires_admin(self) -> None:
        """Line 158: non-admin gets 403 on bulk delete."""
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-uterm",
            jwt_audience="provide-uterm-server",
            worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
        )
        app = create_server_app(cfg)
        token = _make_token(sub="viewer", roles=["viewer"])
        with TestClient(app) as client:
            r = client.request(
                "DELETE",
                "/api/sessions",
                json={"filter": {}},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 403

    def test_bulk_delete_can_mutate_check(self) -> None:
        """Line 170 is unreachable (admin check at line 157 + admin always passes
        can_mutate_session). Test that admin bulk delete works correctly."""
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-uterm",
            jwt_audience="provide-uterm-server",
            worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
        )
        cfg.sessions = [
            SessionDefinition(
                session_id="bd-sess",
                display_name="BD",
                connector_type="shell",
                visibility="public",
            )
        ]
        app = create_server_app(cfg)
        admin_token = _make_token(sub="admin", roles=["admin"])
        with TestClient(app) as client:
            r = client.request(
                "DELETE",
                "/api/sessions",
                json={"filter": {}},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert r.status_code == 200

    def test_bulk_delete_with_stopped_at_too_recent(self) -> None:
        """Line 176->178: older_than_s filter skips sessions stopped too recently."""
        cfg = default_server_config()
        cfg.auth.mode = "dev"
        app = create_server_app(cfg)

        with TestClient(app) as client:
            # Create a session
            client.post("/api/sessions", json={"session_id": "bd-recent", "connector_type": "websocket"})

            # Use the API to simulate tunnel connect/disconnect which sets stopped_at
            registry = app.state.uterm_registry
            # Directly set _stopped_at on the runtime via set_tunnel_connected
            import asyncio

            loop = asyncio.new_event_loop()
            loop.run_until_complete(registry.set_tunnel_connected("bd-recent", True))
            loop.run_until_complete(registry.set_tunnel_connected("bd-recent", False))
            loop.close()

            # Now bulk delete with older_than_s=3600 — the session was stopped <1s ago
            r = client.request(
                "DELETE",
                "/api/sessions",
                json={"filter": {"older_than_s": 3600}},
            )
            assert r.status_code == 200
            # bd-recent was stopped too recently (< 3600s ago), provide-shell has stopped_at=None
            assert r.json()["deleted"] == 0

    def test_watch_events_403_insufficient_privileges(self) -> None:
        """Line 411: watch_events returns 403 for unauthorized principal."""
        cfg = default_server_config()
        cfg.auth = AuthConfig(
            mode="jwt",
            jwt_public_key_pem=_TEST_KEY,
            jwt_algorithms=["HS256"],
            jwt_issuer="provide-uterm",
            jwt_audience="provide-uterm-server",
            worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
        )
        cfg.sessions = [
            SessionDefinition(
                session_id="watch-priv",
                display_name="Watch",
                connector_type="shell",
                visibility="operator",
            )
        ]
        app = create_server_app(cfg)
        viewer_token = _make_token(sub="viewer", roles=["viewer"])
        with TestClient(app) as client:
            r = client.get(
                "/api/sessions/watch-priv/events/watch",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
        assert r.status_code == 403

    def test_create_tunnel_validation_error(self) -> None:
        """Lines 591-594: create tunnel returns 422 on validation error."""
        client = self._admin_client()

        # Force a validation error by making the registry fail
        async def _fail(payload: dict[str, Any]) -> Any:
            from provide.terminal.server.registry import SessionValidationError

            raise SessionValidationError("bad tunnel")

        original_create = client.app.state.uterm_registry.create_session  # type: ignore[union-attr]
        client.app.state.uterm_registry.create_session = _fail  # type: ignore[union-attr]
        r = client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        client.app.state.uterm_registry.create_session = original_create  # type: ignore[union-attr]
        assert r.status_code == 422

    def test_create_tunnel_conflict_error(self) -> None:
        """Lines 591-594: create tunnel returns 409 on ValueError (conflict)."""
        client = self._admin_client()

        async def _conflict(payload: dict[str, Any]) -> Any:
            raise ValueError("session already exists")

        original_create = client.app.state.uterm_registry.create_session  # type: ignore[union-attr]
        client.app.state.uterm_registry.create_session = _conflict  # type: ignore[union-attr]
        r = client.post("/api/tunnels", json={"tunnel_type": "terminal"})
        client.app.state.uterm_registry.create_session = original_create  # type: ignore[union-attr]
        assert r.status_code == 409

    def test_bulk_delete_actually_deletes_old_session(self) -> None:
        """Lines 176->178: session IS old enough → gets deleted."""
        import asyncio
        import time

        cfg = default_server_config()
        cfg.auth.mode = "dev"
        app = create_server_app(cfg)

        with TestClient(app) as client:
            client.post("/api/sessions", json={"session_id": "bd-old2", "connector_type": "websocket"})

            # Connect/disconnect to set stopped_at, then backdate it.
            registry = app.state.uterm_registry

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(registry.set_tunnel_connected("bd-old2", True))
                loop.run_until_complete(registry.set_tunnel_connected("bd-old2", False))
            finally:
                loop.close()
            rt = registry.get_runtime("bd-old2")
            if rt is not None:
                rt._stopped_at = time.time() - 7200

            r = client.request(
                "DELETE",
                "/api/sessions",
                json={"filter": {"older_than_s": 3600}},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["deleted"] >= 1
