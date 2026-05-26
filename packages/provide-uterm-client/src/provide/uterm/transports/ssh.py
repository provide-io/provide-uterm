#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""AsyncSSH server transport for provide-uterm.

Requires the ``ssh`` extra::

    pip install 'provide-uterm[ssh]'

Provides:
- :class:`SSHStreamReader` — adapts asyncssh stdin to ``asyncio.StreamReader`` interface.
- :class:`SSHStreamWriter` — adapts asyncssh stdout to ``asyncio.StreamWriter`` interface.
- :func:`start_ssh_server` — callback-based SSH server (no game-state coupling).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import stat as stat_module
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from provide.telemetry import get_logger
from provide.uterm.defaults import TerminalDefaults

PasswordValidator = Callable[[str, str], bool]
PublicKeyValidator = Callable[[str, "asyncssh.SSHKey"], bool]

try:
    import asyncssh
except ImportError as _e:  # pragma: no cover
    raise ImportError("asyncssh is required for SSH transport: pip install 'provide-uterm[ssh]'") from _e

logger = get_logger(__name__)
ConnectionHandler = Callable[
    [Any, Any],  # (SSHStreamReader, SSHStreamWriter)
    Coroutine[Any, Any, None],
]
StreamAdapterFactory = Callable[[asyncssh.SSHServerProcess[bytes]], Any]


class SSHStreamReader:
    """Adapts an asyncssh process stdin to the ``asyncio.StreamReader`` interface."""

    def __init__(self, process: asyncssh.SSHServerProcess[bytes]) -> None:
        self.process = process

    async def read(self, n: int = -1) -> bytes:
        """Read up to *n* bytes from stdin (``-1`` reads until EOF)."""
        try:
            data = await self.process.stdin.read(n)
        except (asyncssh.Error, EOFError, asyncio.CancelledError):
            return b""
        if isinstance(data, str):
            return data.encode("utf-8", errors="replace")
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        return b""


class SSHStreamWriter:
    """Adapts an asyncssh process stdout to the ``asyncio.StreamWriter`` interface."""

    def __init__(self, process: asyncssh.SSHServerProcess[bytes]) -> None:
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
    """SSH server with optional credential validation hooks.

    When ``password_validator`` / ``public_key_validator`` are ``None`` the
    server is permissive (back-compat for gateways that perform authentication
    at the session layer).  :func:`start_ssh_server` enforces that this
    permissive mode is only used with a loopback bind or an explicit
    ``allow_unauthenticated=True`` opt-in.

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
        password_validator: PasswordValidator | None = None,
        public_key_validator: PublicKeyValidator | None = None,
    ) -> None:
        super().__init__()
        self._peer_ip: str = ""
        self._ip_connections = ip_connections
        self._max_connections_per_ip = max_connections_per_ip
        self._password_validator = password_validator
        self._public_key_validator = public_key_validator

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        peer = conn.get_extra_info("peername")
        peer_ip = peer[0] if peer else "unknown"
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
        _ = exc
        if self._peer_ip and self._peer_ip in self._ip_connections:
            self._ip_connections[self._peer_ip] -= 1
            if self._ip_connections[self._peer_ip] <= 0:
                del self._ip_connections[self._peer_ip]

    def begin_auth(self, username: str) -> bool:
        _ = username
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        if self._password_validator is not None:
            return self._password_validator(username, password)
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        if self._public_key_validator is not None:
            return self._public_key_validator(username, key)
        return True


def _make_ssh_server_factory(
    ip_connections: dict[str, int],
    max_connections_per_ip: int,
    password_validator: PasswordValidator | None = None,
    public_key_validator: PublicKeyValidator | None = None,
) -> type[TerminalSSHServer]:
    """Return a zero-arg factory class that passes per-server state to each instance."""

    class _BoundServer(TerminalSSHServer):
        def __init__(self) -> None:
            super().__init__(
                ip_connections,
                max_connections_per_ip,
                password_validator=password_validator,
                public_key_validator=public_key_validator,
            )

    return _BoundServer


def _is_loopback_bind(host: str) -> bool:
    """Return True if *host* resolves to a loopback-only bind address."""
    if not host:
        return False
    if host in ("localhost",):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(addr.is_loopback)


def _default_host_key_dir() -> Path:
    """Return the per-user default host key directory.

    Defaults to ``~/.uterm``.  Raises :class:`RuntimeError` if the home
    directory cannot be resolved (callers should pass an explicit
    ``host_key_path``).
    """
    try:
        home = Path.home()
    except (RuntimeError, KeyError) as exc:
        raise RuntimeError("cannot determine a default SSH host key directory: pass host_key_path explicitly") from exc
    if not str(home):
        raise RuntimeError("cannot determine a default SSH host key directory: pass host_key_path explicitly")
    return home / ".uterm"


def _verify_key_permissions(key_path: Path) -> None:
    """Raise if *key_path* is not 0600 and owned by the current user."""
    st = key_path.stat()
    mode = stat_module.S_IMODE(st.st_mode)
    if mode != 0o600:
        raise PermissionError(
            f"refusing to load SSH host key with insecure mode {oct(mode)} (expected 0o600): {key_path}"
        )
    current_uid = os.getuid() if hasattr(os, "getuid") else None  # pragma: no cover - non-POSIX
    if current_uid is not None and st.st_uid != current_uid:
        raise PermissionError(
            f"refusing to load SSH host key owned by uid {st.st_uid} (current uid {current_uid}): {key_path}"
        )


def _get_or_create_host_key(data_dir: Path) -> asyncssh.SSHKey:
    """Load or generate an ed25519 SSH host key in *data_dir*.

    When loading an existing key file, mode must be ``0o600`` and the file
    must be owned by the current user — otherwise :class:`PermissionError`
    is raised.
    """
    key_path = data_dir / "ssh_host_key"
    if key_path.exists():
        _verify_key_permissions(key_path)
        try:
            return asyncssh.import_private_key(key_path.read_bytes())
        except PermissionError:
            raise
        except Exception as exc:
            logger.warning("failed to load ssh host key, regenerating: %s", exc)

    key = asyncssh.generate_private_key("ssh-ed25519")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # Tighten directory permissions to 0700 so the key directory is private.
        with contextlib.suppress(OSError):
            data_dir.chmod(0o700)
        key_path.write_bytes(key.export_private_key())
        key_path.chmod(0o600)
        logger.info("generated new ssh host key path=%s", key_path)
    except Exception as exc:
        logger.error("failed to save ssh host key: %s", exc)
    return key


async def start_ssh_server(
    handler: ConnectionHandler,
    host: str = TerminalDefaults.BIND_ALL,  # nosec B104
    port: int = TerminalDefaults.SSH_PORT,
    host_key_path: Path | None = None,
    max_connections_per_ip: int = 5,
    *,
    reader_factory: StreamAdapterFactory | None = None,
    writer_factory: StreamAdapterFactory | None = None,
    credentials_validator: PasswordValidator | None = None,
    public_key_validator: PublicKeyValidator | None = None,
    allow_unauthenticated: bool = False,
) -> Any:
    """Create and start an asyncssh SSH server.

    Args:
        handler: Async callback ``(reader, writer) -> None`` called per connection.
                 *reader* is :class:`SSHStreamReader`, *writer* is :class:`SSHStreamWriter`.
        host: Network interface to bind to.
        port: TCP port number.
        host_key_path: Directory for host key storage.  Defaults to
            ``~/.uterm`` (created with 0700) when not provided.  Pre-existing
            host key files must be mode 0600 and owned by the current user.
        max_connections_per_ip: Max concurrent connections from a single IP.
        reader_factory: Optional per-connection reader adapter factory. Defaults
            to :class:`SSHStreamReader`.
        writer_factory: Optional per-connection writer adapter factory. Defaults
            to :class:`SSHStreamWriter`.
        credentials_validator: ``(username, password) -> bool`` called for
            password auth.  When ``None`` the server accepts any password —
            intended for gateways that authenticate at the session layer.
        public_key_validator: ``(username, key) -> bool`` called for public-key
            auth.  When ``None`` any key is accepted (same caveat as above).
        allow_unauthenticated: Opt-in override permitting the permissive
            ``None`` validators on a non-loopback bind.  Defaults to ``False``.

    Returns:
        The running asyncssh server instance.

    Raises:
        RuntimeError: When both validators are ``None``, the bind address is
            not loopback, and ``allow_unauthenticated`` is False.
    """
    if (
        credentials_validator is None
        and public_key_validator is None
        and not allow_unauthenticated
        and not _is_loopback_bind(host)
    ):
        raise RuntimeError(
            "refusing to start an SSH server that accepts any credential on a non-loopback "
            f"bind ({host!r}); pass credentials_validator / public_key_validator, bind to "
            "loopback, or set allow_unauthenticated=True to opt in explicitly"
        )

    ip_connections: dict[str, int] = {}
    server_class = _make_ssh_server_factory(
        ip_connections,
        max_connections_per_ip,
        password_validator=credentials_validator,
        public_key_validator=public_key_validator,
    )

    key_dir = host_key_path if host_key_path is not None else _default_host_key_dir()
    host_key = _get_or_create_host_key(key_dir)
    make_reader = reader_factory or SSHStreamReader
    make_writer = writer_factory or SSHStreamWriter

    async def _process_factory(process: asyncssh.SSHServerProcess[bytes]) -> None:  # pragma: no cover
        reader = make_reader(process)
        writer = make_writer(process)
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
