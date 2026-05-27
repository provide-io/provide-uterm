#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests targeting specific coverage gaps across server modules."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as _jwt
from fastapi.testclient import TestClient

from provide.uterm.recording import LocalFileRecordingStore
from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.models import (
    AuthConfig,
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


class TestPamTunnelGaps:
    """Covers lines 54-58, 75-76, 89-90."""

    async def test_on_pty_output_callback_reads_and_sends(self) -> None:
        """Lines 54-58: _on_pty_output reads from master_fd and sends to tunnel."""
        from provide.uterm.server.pam_tunnel import PamTunnelBridge

        tunnel = MagicMock()
        tunnel.connect = AsyncMock()
        tunnel.open_terminal = AsyncMock()
        tunnel.send_data = AsyncMock()
        tunnel.close = AsyncMock()

        from provide.uterm.tunnel.protocol import TunnelFrame

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
            patch("provide.uterm.tunnel.client.TunnelClient", return_value=tunnel),
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
        from provide.uterm.server.pam_tunnel import PamTunnelBridge

        tunnel = MagicMock()
        tunnel.connect = AsyncMock()
        tunnel.open_terminal = AsyncMock()
        tunnel.send_data = AsyncMock()
        tunnel.close = AsyncMock()

        from provide.uterm.tunnel.protocol import TunnelFrame

        tunnel.recv = AsyncMock(return_value=TunnelFrame(channel=1, flags=0x01, payload=b""))

        connector = MagicMock()
        type(connector).__name__ = "PTYConnector"
        connector._master_fd = 42

        captured_callback = None

        def _add_reader(fd: int, cb: object) -> None:
            nonlocal captured_callback
            captured_callback = cb

        with (
            patch("provide.uterm.tunnel.client.TunnelClient", return_value=tunnel),
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
        from provide.uterm.server.pam_tunnel import PamTunnelBridge

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
            patch("provide.uterm.tunnel.client.TunnelClient", return_value=tunnel),
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
        from provide.uterm.server.pam_tunnel import PamTunnelBridge

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

        with patch("provide.uterm.tunnel.client.TunnelClient", return_value=tunnel):
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
        cfg.auth.mode = "header"
        cfg.auth.header_mode_acknowledged = True
        cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
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
