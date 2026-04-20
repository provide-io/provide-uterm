#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Gap #10: TelnetWsGateway must never emit an ``identity`` control frame.

Identity frames are pubkey-driven (SSH path only).  Telnet has no pubkey and
no ``key_resolver`` parameter, so no identity frame must ever reach the
upstream WebSocket server regardless of:

- whether IAC negotiation is enabled or disabled,
- whether an ``authorized_keys``-style file exists in the environment.

Each test captures every WS text frame the gateway forwards upstream and
asserts that none has ``type == "identity"``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import pytest
import websockets
import websockets.server

from provide.terminal.gateway import TelnetWsGateway
from tests.bridge.control_channel_helpers import decode_control_payload

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _start_capture_server() -> tuple[Any, int, list[dict]]:
    """Start a WS server that records every control frame it receives.

    Returns ``(server, port, captured_controls)`` where *captured_controls*
    is a live list that grows as frames arrive.
    """
    captured: list[dict] = []

    async def _handler(ws: Any) -> None:
        async for msg in ws:
            if not isinstance(msg, str):
                continue
            try:
                frame = decode_control_payload(msg)
                captured.append(frame)
            except Exception:
                # Plain data frames (binary/ANSI) are not control frames;
                # ignore decode failures.
                pass

    srv = await websockets.serve(_handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, port, captured


async def _start_gateway(ws_port: int, *, iac_negotiate: bool = False) -> tuple[asyncio.AbstractServer, int]:
    """Create a TelnetWsGateway on an ephemeral port; return (server, port)."""
    gw = TelnetWsGateway(f"ws://127.0.0.1:{ws_port}", iac_negotiate=iac_negotiate)
    tcp_srv = await gw.start("127.0.0.1", 0)
    assert tcp_srv.sockets is not None
    tcp_port = tcp_srv.sockets[0].getsockname()[1]
    return tcp_srv, tcp_port


def _assert_no_identity(captured: list[dict], *, context: str = "") -> None:
    """Fail if any captured control frame has type == 'identity'."""
    identity_frames = [f for f in captured if f.get("type") == "identity"]
    assert not identity_frames, (
        f"TelnetWsGateway emitted {len(identity_frames)} identity frame(s)"
        + (f" [{context}]" if context else "")
        + f": {identity_frames}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTelnetNoIdentityFrame:
    """TelnetWsGateway must never forward an identity control frame upstream."""

    async def test_baseline_no_identity(self) -> None:
        """Connect a plain TCP client, send data, assert no identity frame upstream.

        This is the minimal regression guard: plain connect + send + close
        must not trigger any identity frame regardless of what the gateway
        does internally.
        """
        ws_srv, ws_port, captured = await _start_capture_server()
        tcp_srv, tcp_port = await _start_gateway(ws_port, iac_negotiate=False)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.write(b"hello telnet no-identity\r\n")
            await writer.drain()

            # Give the gateway a moment to forward any frames upstream.
            await asyncio.wait_for(asyncio.sleep(0.5), timeout=2.0)

            writer.close()
            await asyncio.sleep(0.1)

            _assert_no_identity(captured, context="baseline iac_negotiate=False")
        finally:
            tcp_srv.close()
            ws_srv.close()

    async def test_iac_negotiation_no_identity(self) -> None:
        """IAC TTYPE/NEW-ENVIRON handshake must not produce an identity frame.

        With ``iac_negotiate=True`` the gateway sends RFC 1091 / 1572 option
        requests to the TCP client and appends ``?colormode=…`` to the WS URL.
        Neither of those touches identity machinery.
        """
        ws_srv, ws_port, captured = await _start_capture_server()
        tcp_srv, tcp_port = await _start_gateway(ws_port, iac_negotiate=True)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            # Read and discard any IAC negotiation bytes the gateway sends.
            try:
                await asyncio.wait_for(reader.read(256), timeout=0.5)
            except TimeoutError:
                pass

            writer.write(b"data after iac negotiation\r\n")
            await writer.drain()

            # Wait for upstream forwarding (negotiation timeout is 0.4 s by default).
            await asyncio.wait_for(asyncio.sleep(1.0), timeout=2.0)

            writer.close()
            await asyncio.sleep(0.1)

            _assert_no_identity(captured, context="iac_negotiate=True")
        finally:
            tcp_srv.close()
            ws_srv.close()

    async def test_authorized_keys_env_no_identity(self) -> None:
        """Presence of an authorized_keys-style file must not leak identity into telnet.

        Even when ``SSH_AUTHORIZED_KEYS`` or similar env vars point at a real
        key file, TelnetWsGateway has no ``key_resolver`` parameter and must
        never emit an identity frame.  This rules out accidental env-variable
        path leakage from the SSH gateway into the telnet handler.
        """
        # Write a syntactically valid authorized_keys entry to a temp file.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False) as f:
            f.write(
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTestingPurposesOnly test@example.com\n"
            )
            key_path = f.name

        old_env = os.environ.copy()
        try:
            # Set env vars that an SSH gateway might read.
            os.environ["SSH_AUTHORIZED_KEYS"] = key_path
            os.environ["AUTHORIZED_KEYS_FILE"] = key_path

            ws_srv, ws_port, captured = await _start_capture_server()
            tcp_srv, tcp_port = await _start_gateway(ws_port, iac_negotiate=False)
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
                writer.write(b"authorized keys env test\r\n")
                await writer.drain()

                await asyncio.wait_for(asyncio.sleep(0.5), timeout=2.0)

                writer.close()
                await asyncio.sleep(0.1)

                _assert_no_identity(captured, context="authorized_keys env set")
            finally:
                tcp_srv.close()
                ws_srv.close()
        finally:
            # Restore environment.
            os.environ.clear()
            os.environ.update(old_env)
            os.unlink(key_path)
