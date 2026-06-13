#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for Sub-fix C: tunnel-worker WebSocket message-size cap.

The /tunnel/{worker_id} route must drop (continue) any raw message whose
byte length exceeds hub.max_ws_message_bytes.  Normal-sized frames must still
be forwarded to browsers.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.control_channel import ControlFrameDecoder
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.tunnel.fastapi_routes import register_tunnel_routes as _tunnel_registrar
from provide.uterm.tunnel.protocol import CHANNEL_DATA, encode_frame


def _decode_browser_msg(raw: str) -> dict:
    decoder = ControlFrameDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    for ev in events:
        if hasattr(ev, "control"):
            return ev.control
        return {"type": "data", "data": ev.data}
    raise ValueError("no events decoded")


def _drain_until_hello(ws) -> dict:
    for _ in range(10):
        raw = ws.receive_text()
        msg = _decode_browser_msg(raw)
        if msg.get("type") == "hello":
            return msg
    raise AssertionError("never received hello")


class TestTunnelOversizedMessage:
    def test_oversized_frame_is_dropped_silently(self) -> None:
        """A tunnel frame larger than max_ws_message_bytes must be dropped (not forwarded)."""
        # Create a hub with a small cap so we can generate oversized frames cheaply
        hub = TermHub(max_ws_message_bytes=1024)
        app = FastAPI()
        app.include_router(hub.create_router(extra_route_registrars=[_tunnel_registrar]))
        client = TestClient(app)

        with (
            client.websocket_connect("/tunnel/cap-test") as tunnel_ws,
            client.websocket_connect("/ws/browser/cap-test/term") as browser_ws,
        ):
            _drain_until_hello(browser_ws)

            # Send an oversized frame (payload > max_ws_message_bytes)
            big_payload = b"X" * 2000  # payload alone is 2000 bytes > 1024
            oversized_frame = encode_frame(CHANNEL_DATA, big_payload)
            tunnel_ws.send_bytes(oversized_frame)

            # Send a normal-sized frame immediately after to confirm the tunnel
            # is still alive and the oversized frame was skipped
            normal_payload = b"hello from tunnel"
            normal_frame = encode_frame(CHANNEL_DATA, normal_payload)
            tunnel_ws.send_bytes(normal_frame)

            # Browser must receive the normal frame but NOT the oversized one
            received_data = ""
            for _ in range(15):
                try:
                    raw = browser_ws.receive_text()
                    received_data += raw
                    if "hello from tunnel" in received_data:
                        break
                except Exception:
                    break

            assert "hello from tunnel" in received_data, "normal frame must pass through"
            # The oversized payload should not appear
            assert "X" * 100 not in received_data, "oversized payload must be dropped"

    def test_normal_sized_frame_passes_through(self) -> None:
        """A frame within max_ws_message_bytes must be broadcast normally."""
        hub = TermHub(max_ws_message_bytes=65536)
        app = FastAPI()
        app.include_router(hub.create_router(extra_route_registrars=[_tunnel_registrar]))
        client = TestClient(app)

        with (
            client.websocket_connect("/tunnel/normal-size") as tunnel_ws,
            client.websocket_connect("/ws/browser/normal-size/term") as browser_ws,
        ):
            _drain_until_hello(browser_ws)

            payload = b"normal output"
            frame = encode_frame(CHANNEL_DATA, payload)
            tunnel_ws.send_bytes(frame)

            received = ""
            for _ in range(10):
                try:
                    raw = browser_ws.receive_text()
                    received += raw
                    if "normal output" in received:
                        break
                except Exception:
                    break

            assert "normal output" in received, "normal frame must be forwarded to browser"

    def test_oversized_frame_does_not_crash_tunnel(self) -> None:
        """After an oversized frame the tunnel connection remains open."""
        hub = TermHub(max_ws_message_bytes=512)
        app = FastAPI()
        app.include_router(hub.create_router(extra_route_registrars=[_tunnel_registrar]))
        client = TestClient(app)

        with client.websocket_connect("/tunnel/crash-test") as tunnel_ws:
            # Send multiple oversized frames
            big = encode_frame(CHANNEL_DATA, b"Y" * 1000)
            tunnel_ws.send_bytes(big)
            tunnel_ws.send_bytes(big)

            # Tunnel must still be reachable (send a valid tiny frame)
            tiny = encode_frame(CHANNEL_DATA, b"ok")
            tunnel_ws.send_bytes(tiny)
            # If we get here without exception the tunnel is still alive
