#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SshWsGateway — split from _gateway.py to stay under the 500-line limit."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import TYPE_CHECKING

from provide.uterm.defaults import TerminalDefaults
from provide.uterm.gateway._gateway import _require_websockets
from provide.uterm.gateway._ssh_handler import (
    _make_no_auth_server_class,
    _make_process_handler,
)

if TYPE_CHECKING:
    import ssl as _ssl

    from provide.uterm.auth import SSHKeyResolver
    from provide.uterm.colors import ColorMode

# ---------------------------------------------------------------------------
# SshWsGateway
# ---------------------------------------------------------------------------


class SshWsGateway:
    """SSH server that proxies shell sessions to a WebSocket terminal server.

    Accepts standard SSH client connections (``ssh``, ``putty``, etc.).
    Each shell channel gets its own outbound WebSocket connection and the
    I/O is bridged bidirectionally. By default the session token received
    from the server is kept in-memory per connection and used for
    transparent WS reconnects.

    Callers that want the session to survive a proxy restart can pass
    ``token_file`` — the token will be written there on issuance (mode
    0600) and read back on the next inbound SSH channel.

    If ``token_file`` points at a **directory** (or a path that has no
    suffix and does not yet exist), the gateway treats it as a per-user
    token *dir* and appends a filename derived from the SSH client's
    pubkey fingerprint. This makes multi-user SSH proxies safe: each
    user gets their own isolated token keyed by their SSH key, so two
    simultaneous users no longer fight over a single token file. A
    connection without a presented pubkey falls back to the directory
    root — suitable for single-user setups where a literal file path
    works fine.

    Requires the ``[ssh]`` extra (asyncssh)::

        pip install 'provide-uterm[cli,ssh]'

    Args:
        ws_url: WebSocket URL of the upstream terminal server.
        server_key: Path to a PEM-encoded SSH host private key file.
            If ``None`` an ephemeral RSA key is generated for each run.
        color_mode: ANSI color downgrade mode — ``"passthrough"`` (default),
            ``"256"``, or ``"16"``.
        token_file: Optional path for persisting the resume token across
            proxy restarts. ``None`` (default) = in-memory only.

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
        color_mode: ColorMode = "passthrough",
        token_file: Path | None = None,
        key_resolver: SSHKeyResolver | None = None,
        require_resolver: bool = False,
        ws_ssl: _ssl.SSLContext | bool | None = None,
        upstream_proxy_secret: str | bytes | None = None,
        client_cert: Path | str | None = None,
        client_key: Path | str | None = None,
    ) -> None:
        """Configure a new SSH→WebSocket gateway.

        Args:
            ws_url: Upstream WebSocket URL.
            server_key: PEM host key path; ephemeral ed25519 if omitted.
            color_mode: ANSI downgrade applied to upstream output.
            token_file: Persist resume tokens here for cross-restart sessions.
            key_resolver: Pluggable :class:`SSHKeyResolver`. When set, the
                gateway calls ``resolver.resolve(...)`` during pubkey auth;
                resolved identities are forwarded to the upstream WS as an
                ``identity`` control frame (the first message on each WS).
            require_resolver: When True, pubkeys the resolver doesn't know
                are rejected outright (no password fallback). Defaults to
                False — unknown keys fall through to password auth.
            ws_ssl: Optional ``ssl.SSLContext`` (or ``False`` to disable
                verification) forwarded to ``websockets.connect`` when the
                upstream URL is ``wss://``. For production ``wss://`` with
                a CA-signed cert, leave as ``None`` (Python's default trust
                store handles it). Required for self-signed / internal-CA
                deployments. ``False`` disables cert checks entirely —
                don't ship that to prod.
        """
        _require_websockets()
        try:
            import asyncssh  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError("asyncssh is required for SSH gateway support: pip install 'provide-uterm[ssh]'") from exc
        self._ws_url = ws_url
        self._server_key = server_key
        self._color_mode = color_mode
        self._token_file = token_file
        self._key_resolver = key_resolver
        self._require_resolver = require_resolver
        self._upstream_proxy_secret = upstream_proxy_secret

        if client_cert and client_key:
            if ws_ssl is not None and not isinstance(ws_ssl, bool):
                raise ValueError("Cannot provide both ws_ssl and client_cert/client_key")
            context = ssl.create_default_context()
            context.load_cert_chain(certfile=client_cert, keyfile=client_key)
            self._ws_ssl = context
        else:
            self._ws_ssl = ws_ssl

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

        no_auth_server_cls = _make_no_auth_server_class(
            self._key_resolver,
            require_resolver=self._require_resolver,
        )
        process_handler = await _make_process_handler(
            self._ws_url,
            self._color_mode,
            token_file=self._token_file,
            ws_ssl=self._ws_ssl,
            upstream_proxy_secret=self._upstream_proxy_secret,
        )

        return await asyncssh.create_server(
            no_auth_server_cls,
            host,
            port,
            server_host_keys=host_keys,
            process_factory=process_handler,
        )
