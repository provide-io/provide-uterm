#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""AsyncSSH server transport for provide-terminal.

Requires the ``ssh`` extra::

    pip install 'provide-terminal[ssh]'

Provides:
- :class:`SSHStreamReader` — adapts asyncssh stdin to ``asyncio.StreamReader`` interface.
- :class:`SSHStreamWriter` — adapts asyncssh stdout to ``asyncio.StreamWriter`` interface.
- :func:`start_ssh_server` — callback-based SSH server (no game-state coupling).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

try:
    import asyncssh
except ImportError as _e:  # pragma: no cover
    raise ImportError("asyncssh is required for SSH transport: pip install 'provide-terminal[ssh]'") from _e

logger = logging.getLogger(__name__)


ConnectionHandler = Callable[
    [Any, Any],  # (SSHStreamReader, SSHStreamWriter)
    Coroutine[Any, Any, None],
]


class SSHStreamReader:
    """Adapts an asyncssh process stdin to the ``asyncio.StreamReader`` interface."""

    def __init__(self, process: asyncssh.SSHServerProcess) -> None:
        self.process = process

    async def read(self, n: int = -1) -> bytes:
        """Read up to *n* bytes from stdin (``-1`` reads until EOF)."""
        try:
            data = await self.process.stdin.read(n)
        except (asyncssh.Error, EOFError, asyncio.CancelledError):
            return b""
        if isinstance(data, str):
            return data.encode("latin-1", errors="replace")
        return data


class SSHStreamWriter:
    """Adapts an asyncssh process stdout to the ``asyncio.StreamWriter`` interface."""

    def __init__(self, process: asyncssh.SSHServerProcess) -> None:
        self.process = process
        self._closed = False

    def write(self, data: bytes) -> None:
        """Write bytes to stdout."""
        if self._closed:
            return
        try:
            self.process.stdout.write(data)
        except (OSError, asyncssh.Error):
            self.close()

    async def drain(self) -> None:
        """Flush the write buffer."""
        if self._closed:
            return
        try:
            await self.process.stdout.drain()
        except (OSError, asyncssh.Error):
            self.close()

    def close(self) -> None:
        """Close the SSH process."""
        if not self._closed:
            self._closed = True
            with contextlib.suppress(Exception):
                self.process.exit(0)
            with contextlib.suppress(Exception):
                self.process.close()

    async def wait_closed(self) -> None:
        """No-op; asyncssh manages its own lifecycle."""

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Return transport metadata (supports ``"peername"``)."""
        if name == "peername":
            peer = self.process.get_extra_info("peername")
            if peer:
                return peer
        return default


class TerminalSSHServer(asyncssh.SSHServer):
    """SSH server that accepts all credentials (session handler performs auth).

    Each server instance gets its own ``ip_connections`` dict and
    ``max_connections_per_ip`` limit, passed via
    :func:`_make_ssh_server_factory`.  This avoids the module-global state
    that caused different server instances to share connection counts and
    overwrite each other's per-IP limit.
    """

    def __init__(
        self,
        ip_connections: dict[str, int],
        max_connections_per_ip: int,
    ) -> None:
        super().__init__()
        self._peer_ip: str = ""
        self._ip_connections = ip_connections
        self._max_connections_per_ip = max_connections_per_ip

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        peer = conn.get_extra_info("peername")
        peer_ip = peer[0] if peer else ""
        addr = f"{peer[0]}:{peer[1]}" if peer else "unknown"

        count = self._ip_connections.get(peer_ip, 0)
        if count >= self._max_connections_per_ip:
            logger.warning("ssh connection rejected: per-IP limit exceeded addr=%s count=%d", addr, count)
            conn.close()
            return

        self._peer_ip = peer_ip
        self._ip_connections[self._peer_ip] = count + 1
        logger.info("ssh connection made addr=%s", addr)

    def connection_lost(self, exc: Exception | None) -> None:
        if self._peer_ip and self._peer_ip in self._ip_connections:
            self._ip_connections[self._peer_ip] -= 1
            if self._ip_connections[self._peer_ip] <= 0:
                del self._ip_connections[self._peer_ip]

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:  # noqa: ARG002
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:  # noqa: ARG002
        return True


def _make_ssh_server_factory(
    ip_connections: dict[str, int],
    max_connections_per_ip: int,
) -> type[TerminalSSHServer]:
    """Return a zero-arg factory class that passes per-server state to each instance."""

    class _BoundServer(TerminalSSHServer):
        def __init__(self) -> None:
            super().__init__(ip_connections, max_connections_per_ip)

    return _BoundServer


def _get_or_create_host_key(data_dir: Path) -> asyncssh.SSHKey:
    """Load or generate an ed25519 SSH host key in *data_dir*."""
    key_path = data_dir / "ssh_host_key"
    if key_path.exists():
        try:
            return asyncssh.import_private_key(key_path.read_bytes())
        except Exception as exc:
            logger.warning("failed to load ssh host key, regenerating: %s", exc)

    key = asyncssh.generate_private_key("ssh-ed25519")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key.export_private_key())
        key_path.chmod(0o600)
        logger.info("generated new ssh host key path=%s", key_path)
    except Exception as exc:
        logger.error("failed to save ssh host key: %s", exc)
    return key


async def start_ssh_server(
    handler: ConnectionHandler,
    host: str = "0.0.0.0",  # nosec B104
    port: int = 2222,
    host_key_path: Path | None = None,
    max_connections_per_ip: int = 5,
) -> Any:
    """Create and start an asyncssh SSH server.

    Args:
        handler: Async callback ``(reader, writer) -> None`` called per connection.
                 *reader* is :class:`SSHStreamReader`, *writer* is :class:`SSHStreamWriter`.
        host: Network interface to bind to.
        port: TCP port number.
        host_key_path: Directory for host key storage (defaults to ``Path.cwd()``).
        max_connections_per_ip: Max concurrent connections from a single IP.

    Returns:
        The running asyncssh server instance.
    """
    ip_connections: dict[str, int] = {}
    server_class = _make_ssh_server_factory(ip_connections, max_connections_per_ip)

    key_dir = host_key_path if host_key_path is not None else Path.cwd()
    host_key = _get_or_create_host_key(key_dir)

    async def _process_factory(process: asyncssh.SSHServerProcess) -> None:  # pragma: no cover
        reader = SSHStreamReader(process)
        writer = SSHStreamWriter(process)
        await handler(reader, writer)
        with contextlib.suppress(Exception):
            process.exit(0)

    logger.info("ssh server starting host=%s port=%d", host, port)
    server = await asyncssh.create_server(
        server_class,
        host,
        port,
        server_host_keys=[host_key],
        process_factory=_process_factory,
        encoding=None,
    )
    logger.info("ssh server started host=%s port=%d", host, port)
    return server
