#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SshWsGateway — split from _gateway.py to stay under the 500-line limit."""

from __future__ import annotations

from pathlib import Path

from provide.terminal.defaults import TerminalDefaults
from provide.terminal.gateway._gateway import (
    _make_no_auth_server_class,
    _make_process_handler,
    _require_websockets,
)

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
        token_file: Path to persist the resume token.
        color_mode: ANSI color downgrade mode — ``"passthrough"`` (default),
            ``"256"``, or ``"16"``.

    Example::

        gw = SshWsGateway("wss://warp.provide.io/ws/terminal")
        server = await gw.start(port=2222)
        await server.wait_closed()
    """

    def __init__(
        self,
        ws_url: str,
        *,
        server_key: str | Path | None = None,
        token_file: Path | None = None,
        color_mode: str = "passthrough",
    ) -> None:
        _require_websockets()
        try:
            import asyncssh  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "asyncssh is required for SSH gateway support: pip install 'provide-terminal[ssh]'"
            ) from exc
        self._ws_url = ws_url
        self._server_key = server_key
        self._token_file = token_file
        self._color_mode = color_mode

    async def start(
        self, host: str = TerminalDefaults.BIND_ALL, port: int = TerminalDefaults.GATEWAY_SSH_PORT
    ) -> object:  # nosec B104
        """Start the SSH server and return the server object.

        Args:
            host: Bind address. Defaults to ``"0.0.0.0"``.
            port: TCP port. Defaults to ``2222``.

        Returns:
            An asyncssh server object — call ``await server.wait_closed()``
            to block until shutdown.
        """
        import asyncssh

        if self._server_key:
            key_path = Path(self._server_key)
            if not key_path.exists():
                raise FileNotFoundError(f"SSH host key not found: {key_path}")
            if not key_path.is_file():
                raise ValueError(f"SSH host key path is not a file: {key_path}")
            host_keys = [asyncssh.read_private_key(str(key_path))]
        else:
            host_keys = [asyncssh.generate_private_key("ssh-ed25519")]

        no_auth_server_cls = _make_no_auth_server_class()
        process_handler = await _make_process_handler(self._ws_url, self._token_file, self._color_mode)

        return await asyncssh.create_server(
            no_auth_server_cls,
            host,
            port,
            server_host_keys=host_keys,
            process_factory=process_handler,
        )
