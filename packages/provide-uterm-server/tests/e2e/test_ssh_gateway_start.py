#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SSH gateway startup + process-handler integration tests.

Separated from :mod:`test_ssh_gateway` to keep individual test files
under the 500-LOC budget. Focuses on ``SshWsGateway.start()`` behaviour
and the per-connection ``_process_handler`` — host-key handling,
resume-token flow, live colormode derivation from ``pty-req``/``env``,
and clean shutdown when the upstream WS is unreachable.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import asyncssh
import websockets
import websockets.server

from tests.bridge.control_channel_helpers import decode_control_payload


async def _start_ws_echo_server(banner: str = "") -> tuple[Any, int]:
    """Start a localhost WS server that echoes every message (optionally with a banner)."""

    async def _handler(ws: Any) -> None:
        if banner:
            await ws.send(banner)
        async for msg in ws:
            await ws.send(msg)

    srv = await websockets.serve(_handler, "127.0.0.1", 0)
    port: int = srv.sockets[0].getsockname()[1]
    return srv, port


async def _ws_capture_server() -> tuple[Any, int, list[str]]:
    """WS server that records upgrade paths so tests can assert on query strings."""
    captured_paths: list[str] = []

    async def _handler(ws: Any) -> None:
        path = getattr(ws, "request", None)
        if path is not None:
            captured_paths.append(str(getattr(path, "path", "")))
        else:
            captured_paths.append(str(getattr(ws, "path", "") or ""))
        await ws.send("HI\r\n")
        with contextlib.suppress(Exception):
            async for _msg in ws:
                pass

    srv = await websockets.serve(_handler, "127.0.0.1", 0)
    port: int = srv.sockets[0].getsockname()[1]
    return srv, port, captured_paths


class TestSshWsGatewayStart:
    async def test_start_ephemeral_host_key(self) -> None:
        from provide.uterm.gateway import SshWsGateway

        gw = SshWsGateway("wss://unreachable.invalid/ws")
        srv = await gw.start("127.0.0.1", 0)
        assert isinstance(srv, asyncssh.SSHAcceptor)
        try:
            pass
        finally:
            srv.close()
            await srv.wait_closed()

    async def test_start_with_file_key(self, tmp_path: Any) -> None:
        from provide.uterm.gateway import SshWsGateway

        key = asyncssh.generate_private_key("ssh-ed25519")
        key_path = tmp_path / "host_key.pem"
        key_path.write_bytes(key.export_private_key())

        gw = SshWsGateway("wss://unreachable.invalid/ws", server_key=str(key_path))
        srv = await gw.start("127.0.0.1", 0)
        assert isinstance(srv, asyncssh.SSHAcceptor)
        try:
            pass
        finally:
            srv.close()
            await srv.wait_closed()

    async def test_process_handler_runs_on_ssh_connect(self) -> None:
        """Connecting a real SSH client through SshWsGateway exercises _process_handler."""
        from provide.uterm.gateway import SshWsGateway

        ws_srv, ws_port = await _start_ws_echo_server(banner="HELLO\r\n")
        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}")
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]
        try:
            async with (
                asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="guest",
                    password="anything",
                    config=[],
                ) as conn,
                conn.create_process() as proc,
            ):
                proc.stdin.write_eof()
                data = await asyncio.wait_for(proc.stdout.read(4096), timeout=5.0)
            text = data if isinstance(data, str) else data.decode("latin-1")
            assert "HELLO" in text
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

    async def test_keyboard_interactive_auth_completes_handshake(self) -> None:
        """A client forced to keyboard-interactive auth completes the handshake.

        Exercises the kbdint path end to end against a real asyncssh client:
        kbdint_auth_supported() advertises it, get_kbdint_challenge() issues an
        empty challenge (no prompts), and validate_kbdint_response() accepts the
        empty response. Before the gateway overrode those, advertising kbdint
        left the client's attempt failing silently. Restricting the client to
        ``keyboard-interactive`` (no pubkey/password) proves that path alone auths.
        """
        from provide.uterm.gateway import SshWsGateway

        ws_srv, ws_port = await _start_ws_echo_server(banner="KBDINT-OK\r\n")
        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}")
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]
        try:
            async with (
                asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="guest",
                    preferred_auth="keyboard-interactive",  # force kbdint only
                    client_keys=[],  # no pubkey fallback
                    # asyncssh's default client uses `password` as its kbdint
                    # response source; the gateway issues an empty (no-prompt)
                    # challenge so the value is unused — it just enables the
                    # client to attempt keyboard-interactive at all.
                    password="x",
                    config=[],
                ) as conn,
                conn.create_process() as proc,
            ):
                # Reaching here means kbdint auth succeeded (empty challenge accepted).
                proc.stdin.write_eof()
                data = await asyncio.wait_for(proc.stdout.read(4096), timeout=5.0)
            text = data if isinstance(data, str) else data.decode("latin-1")
            assert "KBDINT-OK" in text
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

    async def test_process_handler_resumes_after_token_received(self) -> None:
        """After server sends session_token, next WS reconnect sends a resume frame."""
        from provide.uterm.control_channel import encode_control_frame
        from provide.uterm.gateway import SshWsGateway

        connection_count = 0
        resume_msgs: list[str] = []
        reconnected = asyncio.Event()

        async def _handler(ws: Any) -> None:
            nonlocal connection_count
            connection_count += 1
            if connection_count == 1:
                # First connection: send a session_token frame, then drop transiently
                # (close code 1001 "going away", not a clean 1000) so the gateway
                # reconnects and replays the token as a resume frame. A 1000 close
                # would be treated as a deliberate end (no reconnect, no resume).
                await ws.send(encode_control_frame({"type": "session_token", "token": "in_memory_token"}))
                await asyncio.sleep(0.2)  # let the gateway read+store the token before the drop
                await ws.close(code=1001)
            else:
                # Second connection: the gateway replays the stored token as a
                # resume frame. It need not be the very first frame (the capability
                # hello can precede it), so scan every frame until the conn drains.
                async for msg in ws:
                    reconnected.set()
                    if isinstance(msg, str):
                        with contextlib.suppress(Exception):
                            payload = decode_control_payload(msg)
                            if payload.get("type") == "resume":
                                resume_msgs.append(str(payload.get("token") or ""))

        ws_srv = await websockets.serve(_handler, "127.0.0.1", 0)
        ws_port: int = ws_srv.sockets[0].getsockname()[1]

        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}")
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]
        try:
            with contextlib.suppress(Exception):
                async with asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="guest",
                    password="anything",
                    config=[],
                ) as conn:
                    async with conn.create_process() as proc:
                        # Reconnect delay is 3s — allow up to 5s.
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(reconnected.wait(), timeout=5.0)
                        proc.stdin.write_eof()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        assert "in_memory_token" in resume_msgs

    async def test_colormode_256_from_term_only(self) -> None:
        """Live: TERM=xterm-256color with no COLORTERM → ?colormode=256."""
        from provide.uterm.gateway import SshWsGateway

        ws_srv, ws_port, captured_paths = await _ws_capture_server()
        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}/ws")
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]
        try:
            async with (
                asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="guest",
                    password="anything",
                    config=[],
                ) as conn,
                conn.create_process(term_type="xterm-256color") as proc,
            ):
                proc.stdin.write_eof()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.stdout.read(4096), timeout=3.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        assert captured_paths
        joined = " ".join(captured_paths)
        assert "colormode=256" in joined, f"expected colormode=256 in {joined!r}"

    async def test_no_pty_no_env_leaves_url_untouched(self) -> None:
        """Live: no pty-req, no env → no colormode in URL."""
        from provide.uterm.gateway import SshWsGateway

        ws_srv, ws_port, captured_paths = await _ws_capture_server()
        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}/ws")
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]
        try:
            async with (
                asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="guest",
                    password="anything",
                    config=[],
                ) as conn,
                # No term_type, no env — bare session.
                conn.create_process() as proc,
            ):
                proc.stdin.write_eof()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.stdout.read(4096), timeout=3.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        assert captured_paths
        assert "colormode=" not in " ".join(captured_paths)

    async def test_process_handler_exception_is_swallowed(self) -> None:
        """If WS is unreachable, _process_handler logs and exits cleanly (no hang)."""
        from provide.uterm.gateway import SshWsGateway

        # Point gateway at a port with nothing listening — WS connect will fail.
        gw = SshWsGateway("ws://127.0.0.1:1")
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]
        try:
            with contextlib.suppress(Exception):
                async with asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="guest",
                    password="anything",
                    config=[],
                ) as conn:
                    async with conn.create_process() as proc:
                        await asyncio.wait_for(proc.stdout.read(4096), timeout=3.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
        # Reaching here means no hang and no unhandled exception.
