#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E: identity control frame is re-emitted on WS reconnect.

After the upstream WebSocket drops and the gateway reconnects, the gateway
re-sends the ``identity`` control frame as the *first* message on the new
connection.  This file pins that current behaviour so a future refactor
(e.g. "send identity once, use resume afterward") doesn't silently change it.

Two tests:
1. identity frame is re-emitted as the first message on the reconnected WS.
2. the second-connection identity frame carries identical subject, fingerprint,
   and claims to the first one (no mutation across reconnects).

The gateway uses a ``reconnect_delay = 3.0`` s backoff between attempts.
Both tests wait up to 5 s for the second connection (matching the pattern in
``test_process_handler_resumes_after_token_received``).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import asyncssh
import pytest
import websockets
import websockets.server

from provide.terminal.auth import AuthorizedKeysFileResolver
from provide.terminal.gateway import SshWsGateway
from tests.bridge.control_channel_helpers import decode_control_payload

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_authorized_keys(tmp_path: Path, pubkey: asyncssh.SSHKey) -> Path:
    """Write an authorized_keys file with subject/role metadata."""
    line = pubkey.export_public_key().decode("ascii").strip()
    opts = 'subject="sre:alice",claim-role="oncall"'
    path = tmp_path / "authorized_keys"
    path.write_text(f"{opts} {line}\n", encoding="utf-8")
    return path


async def _make_gateway_and_ssh_server(
    ws_port: int,
    resolver: AuthorizedKeysFileResolver,
) -> tuple[Any, int]:
    """Start SshWsGateway pointing at ws_port; return (ssh_srv, ssh_port)."""
    gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", key_resolver=resolver)
    ssh_srv = await gw.start("127.0.0.1", 0)
    ssh_port: int = ssh_srv.sockets[0].getsockname()[1]
    return ssh_srv, ssh_port


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSshReconnectIdentity:
    async def test_identity_frame_reemitted_on_reconnect(self, tmp_path: Path) -> None:
        """After the first WS closes, the gateway reconnects and re-emits identity.

        The WS handler deliberately closes the connection right after receiving
        the identity frame on the first connection.  We then verify the second
        connection also begins with an identity frame.
        """
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        authorized_keys = await _write_authorized_keys(tmp_path, client_key)
        resolver = AuthorizedKeysFileResolver(authorized_keys)

        connection_count = 0
        identity_frames_per_conn: list[list[dict[str, Any]]] = []
        second_conn_ready = asyncio.Event()

        async def _handler(ws: Any) -> None:
            nonlocal connection_count
            connection_count += 1
            conn_index = connection_count  # capture before we yield

            frames_this_conn: list[dict[str, Any]] = []

            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                identity_frames_per_conn.append(frames_this_conn)
                return

            if isinstance(msg, str):
                with contextlib.suppress(Exception):
                    payload = decode_control_payload(msg)
                    if payload.get("type") == "identity":
                        frames_this_conn.append(payload)

            identity_frames_per_conn.append(frames_this_conn)

            if conn_index == 1:
                # Close immediately to trigger the gateway's reconnect loop.
                await ws.close()
            else:
                # Signal the test that the second connection has arrived.
                second_conn_ready.set()
                # Keep WS open so the SSH session doesn't error out.
                with contextlib.suppress(Exception):
                    async for _ in ws:
                        pass

        ws_srv = await websockets.serve(_handler, "127.0.0.1", 0)
        ws_port: int = ws_srv.sockets[0].getsockname()[1]

        ssh_srv, ssh_port = await _make_gateway_and_ssh_server(ws_port, resolver)

        try:
            with contextlib.suppress(Exception):
                async with asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="alice",
                    client_keys=[client_key],
                    preferred_auth="publickey",
                    agent_path=None,
                    config=[],
                ) as conn:
                    async with conn.create_process() as proc:
                        # Reconnect delay is 3 s — allow up to 5 s for second conn.
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(second_conn_ready.wait(), timeout=5.0)
                        proc.stdin.write_eof()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        assert len(identity_frames_per_conn) >= 2, (
            f"Expected frames from at least 2 WS connections, got {len(identity_frames_per_conn)}"
        )
        assert identity_frames_per_conn[0], (
            f"No identity frame on first WS connection: {identity_frames_per_conn}"
        )
        assert identity_frames_per_conn[1], (
            "No identity frame re-emitted on second (reconnected) WS connection — "
            "current behaviour is to re-send identity on every new WS. "
            "If this fails after a refactor, update the test accordingly."
        )

    async def test_reconnect_identity_frame_content_unchanged(self, tmp_path: Path) -> None:
        """The identity frame on the reconnected WS has identical subject/fingerprint/claims.

        Both identity frames must carry the same resolved identity — the
        gateway must not mutate or reset the resolved identity across reconnects.
        """
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        authorized_keys = await _write_authorized_keys(tmp_path, client_key)
        resolver = AuthorizedKeysFileResolver(authorized_keys)

        connection_count = 0
        first_identity: dict[str, Any] | None = None
        second_identity: dict[str, Any] | None = None
        second_conn_ready = asyncio.Event()

        async def _handler(ws: Any) -> None:
            nonlocal connection_count, first_identity, second_identity
            connection_count += 1
            conn_index = connection_count

            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                return

            payload: dict[str, Any] | None = None
            if isinstance(msg, str):
                with contextlib.suppress(Exception):
                    candidate = decode_control_payload(msg)
                    if candidate.get("type") == "identity":
                        payload = candidate

            if conn_index == 1:
                first_identity = payload
                await ws.close()
            else:
                second_identity = payload
                second_conn_ready.set()
                with contextlib.suppress(Exception):
                    async for _ in ws:
                        pass

        ws_srv = await websockets.serve(_handler, "127.0.0.1", 0)
        ws_port: int = ws_srv.sockets[0].getsockname()[1]

        ssh_srv, ssh_port = await _make_gateway_and_ssh_server(ws_port, resolver)

        try:
            with contextlib.suppress(Exception):
                async with asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="alice",
                    client_keys=[client_key],
                    preferred_auth="publickey",
                    agent_path=None,
                    config=[],
                ) as conn:
                    async with conn.create_process() as proc:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(second_conn_ready.wait(), timeout=5.0)
                        proc.stdin.write_eof()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        assert first_identity is not None, "No identity frame captured on first WS connection"
        assert second_identity is not None, (
            "No identity frame captured on reconnected WS — "
            "current behaviour is to re-send identity on every new WS. "
            "If this fails after a refactor, update both tests accordingly."
        )

        assert second_identity["subject"] == first_identity["subject"], (
            f"Subject changed across reconnect: {first_identity['subject']!r} → "
            f"{second_identity['subject']!r}"
        )
        assert second_identity["fingerprint"] == first_identity["fingerprint"], (
            f"Fingerprint changed across reconnect: {first_identity['fingerprint']!r} → "
            f"{second_identity['fingerprint']!r}"
        )
        assert second_identity["claims"] == first_identity["claims"], (
            f"Claims changed across reconnect: {first_identity['claims']!r} → "
            f"{second_identity['claims']!r}"
        )
        # Sanity-check the values themselves.
        assert first_identity["subject"] == "sre:alice"
        assert first_identity["fingerprint"].startswith("SHA256:")
        assert first_identity["claims"] == {"role": "oncall"}
