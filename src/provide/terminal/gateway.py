#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Reverse-direction gateway classes for provide-terminal.

These accept inbound raw TCP (telnet) or SSH connections and proxy all I/O
outbound to a WebSocket terminal server — the mirror image of
:class:`~provide.terminal.fastapi.WsTerminalProxy`.

:class:`TelnetWsGateway`
    Raw TCP listener → WebSocket client.  Traditional telnet clients connect
    on a plain TCP port; the gateway opens a WebSocket to the upstream server
    and pipes both directions.

:class:`SshWsGateway`
    SSH server → WebSocket client.  SSH clients connect with standard
    ``ssh`` or ``putty``; the gateway accepts the shell channel and proxies
    it through a WebSocket to the upstream server.

Requires ``websockets`` (included in ``[cli]``)::

    pip install 'provide-terminal[cli]'

:class:`SshWsGateway` additionally requires the ``[ssh]`` extra::

    pip install 'provide-terminal[cli,ssh]'

Example — serve both telnet and SSH clients against a WS game endpoint::

    gw_telnet = TelnetWsGateway("wss://warp.provide.io/ws/terminal")
    gw_ssh    = SshWsGateway("wss://warp.provide.io/ws/terminal")

    async with asyncio.TaskGroup() as tg:
        tg.create_task((await gw_telnet.start("0.0.0.0", 2112)).serve_forever())
        tg.create_task((await gw_ssh.start("0.0.0.0", 2222)).wait_closed())
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from provide.telemetry import get_logger

from provide.terminal.defaults import TerminalDefaults

logger = get_logger(__name__)


def _require_websockets() -> None:
    try:
        import websockets  # noqa: F401
    except ImportError as exc:
        raise ImportError("websockets is required for gateway support: pip install 'provide-terminal[cli]'") from exc


# ---------------------------------------------------------------------------
# Shared pump helpers
# ---------------------------------------------------------------------------


async def _tcp_to_ws(reader: asyncio.StreamReader, ws: object) -> None:
    """Forward raw TCP bytes → WebSocket text frames."""
    while True:
        data = await reader.read(4096)
        if not data:
            break
        await ws.send(data.decode("latin-1", errors="replace"))  # type: ignore[attr-defined]


async def _ws_to_tcp(ws: object, writer: asyncio.StreamWriter) -> None:
    """Forward WebSocket messages → raw TCP bytes."""
    async for message in ws:  # type: ignore[attr-defined]
        raw = message.encode("latin-1", errors="replace") if isinstance(message, str) else message
        # Remap DEL (0x7F) → BS (0x08): xterm.js sends DEL for Backspace,
        # but many BBS/telnet servers expect BS for character deletion.
        writer.write(raw.replace(b"\x7f", b"\x08"))
        await writer.drain()


async def _pipe_ws(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ws_url: str) -> None:
    """Open a WebSocket to *ws_url* and bidirectionally pipe with reader/writer."""
    import websockets

    async with websockets.connect(ws_url) as ws:
        t1 = asyncio.create_task(_tcp_to_ws(reader, ws))
        t2 = asyncio.create_task(_ws_to_tcp(ws, writer))
        _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*[*_done, *pending], return_exceptions=True)


# ---------------------------------------------------------------------------
# TelnetWsGateway
# ---------------------------------------------------------------------------


class TelnetWsGateway:
    """Raw TCP (telnet) listener that proxies connections to a WebSocket server.

    Each inbound TCP connection gets its own outbound WebSocket connection.
    Both directions are pumped concurrently; whichever side closes first
    cancels the other and the TCP connection is cleaned up.

    Args:
        ws_url: WebSocket URL of the upstream terminal server
            (e.g. ``"wss://warp.provide.io/ws/terminal"``).

    Example::

        gw = TelnetWsGateway("wss://warp.provide.io/ws/terminal")
        server = await gw.start(port=2112)
        await server.serve_forever()
    """

    def __init__(self, ws_url: str) -> None:
        _require_websockets()
        self._ws_url = ws_url

    async def start(
        self,
        host: str = "0.0.0.0",
        port: int = TerminalDefaults.GATEWAY_TELNET_PORT,  # nosec B104
    ) -> asyncio.AbstractServer:
        """Start the TCP listener and return the server object.

        Args:
            host: Bind address. Defaults to ``"0.0.0.0"``.
            port: TCP port. Defaults to ``2112``.

        Returns:
            An :class:`asyncio.AbstractServer` — call
            ``await server.serve_forever()`` to block until shutdown.
        """
        return await asyncio.start_server(self._handle, host, port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _pipe_ws(reader, writer, self._ws_url)
        except Exception as exc:
            logger.debug("telnet_ws_session_ended: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()


# ---------------------------------------------------------------------------
# SshWsGateway
# ---------------------------------------------------------------------------


class SshWsGateway:
    """SSH server that proxies shell sessions to a WebSocket terminal server.

    Accepts standard SSH client connections (``ssh``, ``putty``, etc.).
    Each shell channel gets its own outbound WebSocket connection and the
    I/O is bridged bidirectionally.

    Requires the ``[ssh]`` extra (asyncssh)::

        pip install 'provide-terminal[cli,ssh]'

    Args:
        ws_url: WebSocket URL of the upstream terminal server.
        server_key: Path to a PEM-encoded SSH host private key file.
            If ``None`` an ephemeral RSA key is generated for each run.

    Example::

        gw = SshWsGateway("wss://warp.provide.io/ws/terminal")
        server = await gw.start(port=2222)
        await server.wait_closed()
    """

    def __init__(self, ws_url: str, *, server_key: str | Path | None = None) -> None:
        _require_websockets()
        try:
            import asyncssh  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "asyncssh is required for SSH gateway support: pip install 'provide-terminal[ssh]'"
            ) from exc
        self._ws_url = ws_url
        self._server_key = server_key

    async def start(self, host: str = "0.0.0.0", port: int = TerminalDefaults.GATEWAY_SSH_PORT) -> object:  # nosec B104
        """Start the SSH server and return the server object.

        Args:
            host: Bind address. Defaults to ``"0.0.0.0"``.
            port: TCP port. Defaults to ``2222``.

        Returns:
            An asyncssh server object — call ``await server.wait_closed()``
            to block until shutdown.
        """
        import asyncssh

        ws_url = self._ws_url

        class _NoAuthServer(asyncssh.SSHServer):
            # begin_auth returns False → no credentials required from any SSH
            # client.  This is intentional: the gateway trusts the caller to
            # provide network-level access control.  Do NOT bind host="0.0.0.0"
            # on a public interface without an external firewall or auth layer.
            def begin_auth(self, username: str) -> bool:  # noqa: ARG002  # pragma: no cover
                return False

        if self._server_key:
            key_path = Path(self._server_key)
            if not key_path.exists():
                raise FileNotFoundError(f"SSH host key not found: {key_path}")
            if not key_path.is_file():
                raise ValueError(f"SSH host key path is not a file: {key_path}")
            host_keys = [asyncssh.read_private_key(str(key_path))]
        else:
            host_keys = [asyncssh.generate_private_key("ssh-ed25519")]

        async def _process_handler(process: asyncssh.SSHServerProcess[bytes]) -> None:  # pragma: no cover
            try:
                import websockets

                async with websockets.connect(ws_url) as ws:
                    t1 = asyncio.create_task(_ssh_to_ws(process, ws))
                    t2 = asyncio.create_task(_ws_to_ssh(ws, process))
                    _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*[*_done, *pending], return_exceptions=True)
            except Exception as exc:
                logger.debug("ssh_ws_session_ended: %s", exc)
            finally:
                with contextlib.suppress(Exception):
                    process.exit(0)

        return await asyncssh.create_server(
            _NoAuthServer,
            host,
            port,
            server_host_keys=host_keys,
            process_factory=_process_handler,
        )


async def _ssh_to_ws(process: object, ws: object) -> None:
    """Forward SSH stdin → WebSocket text frames."""
    stdin = process.stdin  # type: ignore[attr-defined]
    while True:
        try:
            data = await stdin.read(4096)
        except Exception:
            break
        if not data:
            break
        await ws.send(data if isinstance(data, str) else data.decode("latin-1", errors="replace"))  # type: ignore[attr-defined]


async def _ws_to_ssh(ws: object, process: object) -> None:
    """Forward WebSocket messages → SSH stdout."""
    stdout = process.stdout  # type: ignore[attr-defined]
    async for message in ws:  # type: ignore[attr-defined]
        if isinstance(message, str):
            stdout.write(message)
        else:
            stdout.write(message.decode("latin-1", errors="replace"))
