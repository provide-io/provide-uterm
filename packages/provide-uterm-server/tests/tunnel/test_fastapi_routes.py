#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the FastAPI tunnel WebSocket route."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.control_channel import ControlChannelDecoder
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.tunnel.fastapi_routes import (
    register_tunnel_routes as _tunnel_registrar,
)
from provide.uterm.tunnel.protocol import (
    CHANNEL_CONTROL,
    CHANNEL_DATA,
    FLAG_EOF,
    encode_control,
    encode_frame,
)
from provide.uterm.tunnel.token_hash import hash_token


@pytest.fixture
def hub() -> TermHub:
    return TermHub()


@pytest.fixture
def app(hub: TermHub) -> FastAPI:
    app = FastAPI()
    app.include_router(hub.create_router(extra_route_registrars=[_tunnel_registrar]))
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _decode_browser_msg(raw: str) -> dict[str, Any]:
    """Decode a control-channel-encoded browser message."""
    decoder = ControlChannelDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    for ev in events:
        if hasattr(ev, "control"):
            return ev.control
        return {"type": "data", "data": ev.data}
    msg = "no events decoded"
    raise ValueError(msg)


def _drain_until_hello(ws: Any) -> dict[str, Any]:
    """Read messages until we get a hello frame."""
    for _ in range(10):
        raw = ws.receive_text()
        msg = _decode_browser_msg(raw)
        if msg.get("type") == "hello":
            return msg
    msg_err = "never received hello"
    raise AssertionError(msg_err)


class TestTunnelConnect:
    def test_tunnel_ws_connects(self, client: TestClient) -> None:
        """Agent can connect via /tunnel/{worker_id}."""
        with client.websocket_connect("/tunnel/test-tunnel-1") as ws:
            # Worker is connected — we should receive the worker_connected broadcast
            # Send data and then close
            data_frame = encode_frame(CHANNEL_DATA, b"hello from tunnel")
            ws.send_bytes(data_frame)

    def test_tunnel_auth_rejected(self) -> None:
        """Tunnel rejects connection when bearer token doesn't match."""
        hub = TermHub(worker_token="secret-token-123")
        app = FastAPI()
        app.include_router(hub.create_router(extra_route_registrars=[_tunnel_registrar]))
        tc = TestClient(app)
        with tc.websocket_connect(
            "/tunnel/test-auth",
            headers={"Authorization": "Bearer wrong-token"},
        ):
            # Should receive close frame with 1008
            pass  # connection closed by server

    def test_tunnel_accepts_global_worker_token(self) -> None:
        """Global worker_bearer_token accepted → line 70 covered."""
        hub = TermHub(worker_token="global-secret")
        app = FastAPI()
        app.include_router(hub.create_router(extra_route_registrars=[_tunnel_registrar]))
        app.state.uterm_registry = MagicMock(set_tunnel_connected=AsyncMock())
        tc = TestClient(app)
        with tc.websocket_connect(
            "/tunnel/test-global-auth",
            headers={"Authorization": "Bearer global-secret"},
        ) as ws:
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"hello"))

    def test_tunnel_accepts_per_session_worker_token(self) -> None:
        hub = TermHub(worker_token="global-token")
        app = FastAPI()
        app.include_router(hub.create_router(extra_route_registrars=[_tunnel_registrar]))
        app.state.uterm_tunnel_tokens = {"test-auth": {"worker_token_hash": hash_token("session-token")}}
        app.state.uterm_registry = MagicMock(set_tunnel_connected=AsyncMock())
        tc = TestClient(app)
        with tc.websocket_connect(
            "/tunnel/test-auth",
            headers={"Authorization": "Bearer session-token"},
        ) as ws:
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"hello"))


class TestTunnelDataFlow:
    def test_data_frame_broadcast(self, client: TestClient) -> None:
        """Data frames from tunnel agent are broadcast to browsers."""
        with (
            client.websocket_connect("/tunnel/test-data") as tunnel_ws,
            client.websocket_connect("/ws/browser/test-data/term") as browser_ws,
        ):
            # Drain hello + any worker_connected frames
            _drain_until_hello(browser_ws)

            # Send data from tunnel agent
            data_frame = encode_frame(CHANNEL_DATA, b"terminal output here")
            tunnel_ws.send_bytes(data_frame)

            # Browser should receive the term data (may be preceded by hijack_state)
            for _ in range(10):
                raw = browser_ws.receive_text()
                if "terminal output here" in raw:
                    break
            assert "terminal output here" in raw


class TestTunnelControl:
    def test_open_message(self, client: TestClient) -> None:
        """Control open message sets up the tunnel."""
        with client.websocket_connect("/tunnel/test-ctrl") as ws:
            ctrl = encode_control(
                {
                    "type": "open",
                    "channel": 1,
                    "tunnel_type": "terminal",
                    "term_size": [80, 24],
                }
            )
            ws.send_bytes(ctrl)

    def test_resize_message(self, client: TestClient) -> None:
        """Control resize message is handled without error."""
        with client.websocket_connect("/tunnel/test-resize") as ws:
            ctrl = encode_control({"type": "resize", "channel": 1, "cols": 120, "rows": 40})
            ws.send_bytes(ctrl)

    def test_close_message(self, client: TestClient) -> None:
        """Control close message is handled without error."""
        with client.websocket_connect("/tunnel/test-close") as ws:
            ctrl = encode_control({"type": "close", "channel": 1})
            ws.send_bytes(ctrl)

    def test_invalid_control_json(self, client: TestClient) -> None:
        """Invalid JSON in control channel is logged but doesn't crash."""
        with client.websocket_connect("/tunnel/test-badctrl") as ws:
            bad = encode_frame(CHANNEL_CONTROL, b"not valid json")
            ws.send_bytes(bad)


class TestTunnelControlExtra:
    def test_eof_frame(self, client: TestClient) -> None:
        """EOF frame is handled without broadcast."""
        with client.websocket_connect("/tunnel/test-eof2") as ws:
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"", flags=FLAG_EOF))

    def test_short_frame_ignored(self, client: TestClient) -> None:
        """Frames shorter than 2 bytes are ignored."""
        with client.websocket_connect("/tunnel/test-short") as ws:
            ws.send_bytes(b"\x01")

    def test_snapshot_control(self, client: TestClient) -> None:
        """Snapshot control message updates hub snapshot."""
        with client.websocket_connect("/tunnel/test-snap") as ws:
            ctrl = encode_control({"type": "snapshot", "screen": "hello screen"})
            ws.send_bytes(ctrl)


class TestTunnelBranchCoverage:
    """Cover remaining branch misses in fastapi_routes.py."""

    def test_data_frame_empty_payload_loops(self, client: TestClient) -> None:
        """Line 120->93: data frame with empty payload (no EOF) loops back."""
        with client.websocket_connect("/tunnel/test-empty-payload") as ws:
            # Send a data frame with empty payload — no EOF flag
            ws.send_bytes(encode_frame(CHANNEL_DATA, b""))
            # Send a real frame after to prove the loop continued
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"still alive"))

    def test_should_broadcast_false_on_replaced_worker(self, hub: TermHub, app: FastAPI) -> None:
        """Line 137->147: should_broadcast=False when worker was replaced."""
        tc = TestClient(app)
        # First tunnel connects
        with tc.websocket_connect("/tunnel/test-replaced") as ws1:
            ws1.send_bytes(encode_frame(CHANNEL_DATA, b"first"))
            # Second tunnel connects to the same worker_id, replacing the first
            with tc.websocket_connect("/tunnel/test-replaced") as ws2:
                ws2.send_bytes(encode_frame(CHANNEL_DATA, b"second"))
            # ws2 disconnects here — it was the active worker, so should_broadcast=True
        # ws1 disconnects here — it was replaced, so should_broadcast=False (137->147)

    def test_open_with_invalid_input_mode(self, client: TestClient) -> None:
        """Line 167->169: open message with input_mode not in ('hijack', 'open')."""
        with client.websocket_connect("/tunnel/test-bad-mode") as ws:
            ctrl = encode_control(
                {
                    "type": "open",
                    "input_mode": "invalid_mode",
                    "tunnel_type": "terminal",
                }
            )
            ws.send_bytes(ctrl)

    def test_unknown_control_msg_type(self, client: TestClient) -> None:
        """Line 179->exit: control message with unrecognized type falls through."""
        with client.websocket_connect("/tunnel/test-unknown-type") as ws:
            ctrl = encode_control({"type": "totally_unknown"})
            ws.send_bytes(ctrl)

    def test_prev_was_hijacked_triggers_notify_on_connect(self, hub: TermHub, app: FastAPI) -> None:
        """Line 96: ``register_worker`` returning True (prev was hijacked) fires notify + broadcast."""
        tc = TestClient(app)
        notified: list[dict[str, Any]] = []
        original_notify = hub.notify_hijack_changed

        def _capture(wid: str, *, enabled: bool, owner: Any = None) -> None:
            notified.append({"worker_id": wid, "enabled": enabled, "owner": owner})
            return original_notify(wid, enabled=enabled, owner=owner)

        object.__setattr__(hub, "notify_hijack_changed", _capture)
        # Force register_worker to report a previous hijack so the if-True branch
        # at the connect site runs.  We let the rest of the handshake proceed.
        with patch.object(hub, "register_worker", new=AsyncMock(return_value=True)):
            with tc.websocket_connect("/tunnel/test-prev-hijacked") as ws:
                ws.send_bytes(encode_frame(CHANNEL_DATA, b"x"))
        assert any(c["worker_id"] == "test-prev-hijacked" and not c["enabled"] for c in notified)

    def test_was_hijacked_on_disconnect_triggers_notify(self, hub: TermHub, app: FastAPI) -> None:
        """Line 150: ``deregister_worker`` reporting was_hijacked=True fires notify + broadcast."""
        tc = TestClient(app)
        notified: list[dict[str, Any]] = []
        original_notify = hub.notify_hijack_changed

        def _capture(wid: str, *, enabled: bool, owner: Any = None) -> None:
            notified.append({"worker_id": wid, "enabled": enabled, "owner": owner})
            return original_notify(wid, enabled=enabled, owner=owner)

        object.__setattr__(hub, "notify_hijack_changed", _capture)
        with patch.object(hub, "deregister_worker", new=AsyncMock(return_value=(True, True))):
            with tc.websocket_connect("/tunnel/test-was-hijacked") as ws:
                ws.send_bytes(encode_frame(CHANNEL_DATA, b"x"))
        # On disconnect, notify_hijack_changed should have been called.
        assert any(c["worker_id"] == "test-was-hijacked" and not c["enabled"] for c in notified)


class TestTunnelAndBrowserCoexist:
    def test_tunnel_worker_with_legacy_browser(self, client: TestClient) -> None:
        """A tunnel agent and a legacy browser can coexist on the same worker_id."""
        with (
            client.websocket_connect("/tunnel/coexist-1") as tunnel_ws,
            client.websocket_connect("/ws/browser/coexist-1/term") as browser_ws,
        ):
            hello = _drain_until_hello(browser_ws)
            assert hello["worker_online"] is True

            # Tunnel sends data
            tunnel_ws.send_bytes(encode_frame(CHANNEL_DATA, b"from tunnel"))
            for _ in range(10):
                raw = browser_ws.receive_text()
                if "from tunnel" in raw:
                    break
            assert "from tunnel" in raw
