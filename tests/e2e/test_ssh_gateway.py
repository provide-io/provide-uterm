#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SSH gateway end-to-end integration tests.

Tests the bidirectional SSH ↔ WebSocket data path using real asyncssh
connections against a real WebSocket echo server.  This exercises the
``_ssh_to_ws`` and ``_ws_to_ssh`` pump functions with genuine asyncssh I/O —
covering the ``_process_handler`` path that is marked ``pragma: no cover``
because it cannot be reached through ``SshWsGateway.start()`` in unit tests.
"""

from __future__ import annotations

import asyncio
import contextlib

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
from typing import Any, cast

import asyncssh
import websockets
import websockets.server

from provide.terminal.gateway import _ssh_to_ws, _ws_to_ssh
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


async def _make_ssh_server(
    ws_port: int,
) -> tuple[Any, int]:
    """Start a real asyncssh server that proxies each session to the WS echo server.

    Uses a permissive ``SSHServer`` subclass that accepts any password — safe for
    localhost-only test use.  The process factory calls ``_ssh_to_ws`` /
    ``_ws_to_ssh`` from ``provide.terminal.gateway`` directly.
    """

    class _TestSSHServer(asyncssh.SSHServer):
        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            return True  # accept any credentials

    host_key = asyncssh.generate_private_key("ssh-ed25519")

    async def _process_handler(process: asyncssh.SSHServerProcess[bytes]) -> None:
        try:
            async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as ws:
                t1 = asyncio.create_task(_ssh_to_ws(process, ws))
                t2 = asyncio.create_task(_ws_to_ssh(ws, process))
                _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
                # Grace period: let the echo arrive at SSH stdout before cancelling.
                if pending:
                    await asyncio.sleep(0.2)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*[*_done, *pending], return_exceptions=True)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                process.exit(0)

    srv = await asyncssh.create_server(
        _TestSSHServer,
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        process_factory=_process_handler,
    )
    port: int = srv.sockets[0].getsockname()[1]
    return srv, port


def _ssh_client_opts(ssh_port: int) -> dict[str, Any]:
    """Return asyncssh.connect kwargs for a no-auth-check test client."""
    return {
        "host": "127.0.0.1",
        "port": ssh_port,
        "known_hosts": None,
        "username": "testuser",
        "password": "anypassword",
        "config": [],  # skip ~/.ssh/config
    }


# ---------------------------------------------------------------------------
# Pump unit tests with real asyncssh-like objects (minimal mocks)
# ---------------------------------------------------------------------------


class TestSshToWsPumpUnit:
    """Unit-level tests for _ssh_to_ws using minimal in-process mocks."""

    async def test_str_data_forwarded_as_is(self) -> None:
        sent: list[str] = []

        class _MockWs:
            async def send(self, data: object) -> None:
                sent.append(data)  # type: ignore[arg-type]

        class _MockStdin:
            _items = ["hello ssh", ""]

            async def read(self, n: int) -> str:
                return self._items.pop(0) if self._items else ""

        class _MockProcess:
            stdin = _MockStdin()

        await _ssh_to_ws(cast("Any", _MockProcess()), cast("Any", _MockWs()))
        assert sent == ["hello ssh"]

    async def test_bytes_data_decoded_latin1(self) -> None:
        sent: list[str] = []

        class _MockWs:
            async def send(self, data: object) -> None:
                sent.append(data)  # type: ignore[arg-type]

        class _MockStdin:
            _items: list[bytes] = [b"\xc0\xc1", b""]

            async def read(self, n: int) -> bytes:
                return self._items.pop(0) if self._items else b""

        class _MockProcess:
            stdin = _MockStdin()

        await _ssh_to_ws(cast("Any", _MockProcess()), cast("Any", _MockWs()))
        assert sent == [b"\xc0\xc1".decode("latin-1", errors="replace")]

    async def test_read_exception_exits_cleanly(self) -> None:
        class _MockWs:
            async def send(self, data: object) -> None:
                pass

        class _MockStdin:
            async def read(self, n: int) -> str:
                raise RuntimeError("stdin broken")

        class _MockProcess:
            stdin = _MockStdin()

        await _ssh_to_ws(cast("Any", _MockProcess()), cast("Any", _MockWs()))
        # Must return without raising


class TestWsToSshPumpUnit:
    """Unit-level tests for _ws_to_ssh using minimal in-process mocks."""

    async def test_str_message_written_directly(self) -> None:
        written: list[object] = []

        class _MockStdout:
            def write(self, data: object) -> None:
                written.append(data)

        class _MockProcess:
            stdout = _MockStdout()

        async def _gen() -> Any:
            yield "from ws"

        await _ws_to_ssh(_gen(), cast("Any", _MockProcess()))
        assert "from ws" in written

    async def test_bytes_message_decoded_latin1(self) -> None:
        written: list[object] = []

        class _MockStdout:
            def write(self, data: object) -> None:
                written.append(data)

        class _MockProcess:
            stdout = _MockStdout()

        async def _gen() -> Any:
            yield b"\xff\xfe"

        await _ws_to_ssh(_gen(), cast("Any", _MockProcess()))
        assert b"\xff\xfe".decode("latin-1", errors="replace") in written


# ---------------------------------------------------------------------------
# Real asyncssh integration tests
# ---------------------------------------------------------------------------


class TestSshWsGatewayRealConnections:
    """Integration tests using a real asyncssh server + real WebSocket echo server."""

    async def test_ssh_client_data_echoed_back(self) -> None:
        """Data written to SSH stdin travels through WS echo and appears in SSH stdout."""
        ws_srv, ws_port = await _start_ws_echo_server()
        ssh_srv, ssh_port = await _make_ssh_server(ws_port)
        try:
            async with asyncssh.connect(**_ssh_client_opts(ssh_port)) as conn:  # noqa: SIM117
                async with conn.create_process() as proc:
                    proc.stdin.write("ping from ssh")
                    # Close stdin so _ssh_to_ws terminates, which lets _ws_to_ssh finish.
                    proc.stdin.write_eof()
                    data = await asyncio.wait_for(proc.stdout.read(4096), timeout=5.0)
            assert "ping from ssh" in (data if isinstance(data, str) else data.decode("latin-1"))
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

    async def test_ws_banner_received_in_ssh_stdout(self) -> None:
        """A banner sent by the WS server on connect appears in SSH stdout."""
        ws_srv, ws_port = await _start_ws_echo_server(banner="WELCOME BANNER\r\n")
        ssh_srv, ssh_port = await _make_ssh_server(ws_port)
        try:
            async with asyncssh.connect(**_ssh_client_opts(ssh_port)) as conn:  # noqa: SIM117
                async with conn.create_process() as proc:
                    proc.stdin.write_eof()
                    data = await asyncio.wait_for(proc.stdout.read(4096), timeout=5.0)
            text = data if isinstance(data, str) else data.decode("latin-1")
            assert "WELCOME BANNER" in text
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

    async def test_concurrent_ssh_sessions_isolated(self) -> None:
        """Two concurrent SSH sessions get independent WS connections and don't cross-talk."""
        ws_srv, ws_port = await _start_ws_echo_server()
        ssh_srv, ssh_port = await _make_ssh_server(ws_port)
        try:
            async with (
                asyncssh.connect(**_ssh_client_opts(ssh_port)) as conn1,
                asyncssh.connect(**_ssh_client_opts(ssh_port)) as conn2,
                conn1.create_process() as p1,
                conn2.create_process() as p2,
            ):
                p1.stdin.write("session-A-data")
                p1.stdin.write_eof()
                p2.stdin.write("session-B-data")
                p2.stdin.write_eof()

                d1, d2 = await asyncio.gather(
                    asyncio.wait_for(p1.stdout.read(4096), timeout=5.0),
                    asyncio.wait_for(p2.stdout.read(4096), timeout=5.0),
                )

            t1 = d1 if isinstance(d1, str) else d1.decode("latin-1")
            t2 = d2 if isinstance(d2, str) else d2.decode("latin-1")
            assert "session-A-data" in t1
            assert "session-B-data" in t2
            # Cross-contamination check
            assert "session-B-data" not in t1
            assert "session-A-data" not in t2
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

    async def test_ssh_disconnect_closes_ws_cleanly(self) -> None:
        """Closing the SSH client causes the WS connection to close without hanging."""
        ws_srv, ws_port = await _start_ws_echo_server()
        ssh_srv, ssh_port = await _make_ssh_server(ws_port)
        try:
            async with asyncssh.connect(**_ssh_client_opts(ssh_port)) as conn:  # noqa: SIM117
                async with conn.create_process() as proc:
                    proc.stdin.write_eof()
                    # Read until closed; ConnectionLost is expected when the server
                    # closes the channel after EOF — that's a clean disconnect.
                    try:  # noqa: SIM105
                        await asyncio.wait_for(proc.stdout.read(4096), timeout=5.0)
                    except asyncssh.ConnectionLost:
                        pass
            # Reaching here means no hang and no unclosed-task warning.
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()


# ---------------------------------------------------------------------------
# SshWsGateway class — start / key-loading paths (kept here for proximity)
# ---------------------------------------------------------------------------


class TestSshWsGatewayStart:
    async def test_start_ephemeral_host_key(self) -> None:
        import asyncssh

        from provide.terminal.gateway import SshWsGateway

        gw = SshWsGateway("wss://unreachable.invalid/ws")
        srv = await gw.start("127.0.0.1", 0)
        assert isinstance(srv, asyncssh.SSHAcceptor)
        try:
            pass
        finally:
            srv.close()
            await srv.wait_closed()

    async def test_start_with_file_key(self, tmp_path: Any) -> None:
        import asyncssh

        from provide.terminal.gateway import SshWsGateway

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
        import asyncssh

        from provide.terminal.gateway import SshWsGateway

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

    async def test_process_handler_sends_resume_token(self, tmp_path: Any) -> None:
        """When a token_file has a saved token, _process_handler sends a resume message."""
        import asyncssh

        from provide.terminal.gateway import SshWsGateway, _write_token

        resume_msgs: list[str] = []

        async def _handler(ws: Any) -> None:
            async for msg in ws:
                if isinstance(msg, str):
                    with contextlib.suppress(AssertionError):
                        payload = decode_control_payload(msg)
                        if payload.get("type") == "resume":
                            resume_msgs.append(str(payload.get("token") or ""))
                break  # only care about first message

        ws_srv = await websockets.serve(_handler, "127.0.0.1", 0)
        ws_port: int = ws_srv.sockets[0].getsockname()[1]

        token_file = tmp_path / "tok"
        _write_token(token_file, "my_resume_token")
        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", token_file=token_file)
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]
        try:
            with contextlib.suppress(Exception):
                async with asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="guest",
                    config=[],
                ) as conn:
                    async with conn.create_process() as proc:
                        proc.stdin.write_eof()
                        await asyncio.wait_for(proc.stdout.read(4096), timeout=3.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        assert "my_resume_token" in resume_msgs

    async def test_process_handler_exception_is_swallowed(self) -> None:
        """If WS is unreachable, _process_handler logs and exits cleanly (no hang)."""
        import asyncssh

        from provide.terminal.gateway import SshWsGateway

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
                    config=[],
                ) as conn:
                    async with conn.create_process() as proc:
                        await asyncio.wait_for(proc.stdout.read(4096), timeout=3.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
        # Reaching here means no hang and no unhandled exception.
