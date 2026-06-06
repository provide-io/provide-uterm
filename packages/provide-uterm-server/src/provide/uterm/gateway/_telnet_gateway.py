#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TelnetWsGateway — raw TCP listener that proxies to a WebSocket server.

Split from :mod:`_gateway` to keep each module under the 500-LOC
budget. The class is a thin wrapper around :func:`_pipe_ws` in
:mod:`_gateway` — which handles IAC negotiation, bidirectional pumps,
and reconnect with resume tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.defaults import TerminalDefaults
from provide.uterm.gateway._gateway import (
    _pipe_ws,
    _read_token,
    _require_websockets,
)

if TYPE_CHECKING:
    from pathlib import Path

    from provide.uterm.colors import ColorMode

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# TelnetWsGateway
# ---------------------------------------------------------------------------


class TelnetWsGateway:
    """Raw TCP (telnet) listener that proxies connections to a WebSocket server.

    Each inbound TCP connection gets its own outbound WebSocket connection.
    Both directions are pumped concurrently; whichever side closes first
    cancels the other and the TCP connection is cleaned up.

    If the upstream WebSocket closes while the TCP client is still connected
    (e.g. Cloudflare DO hibernation), the gateway reconnects automatically
    using the session token received in-memory from the server.

    By default the token is held in memory per-connection and discarded when
    the TCP client disconnects. Callers that want the session to survive a
    proxy restart can pass ``token_file`` — the token will be written there
    (mode 0600, parent dir 0700) on issuance and read back on the next
    inbound connection. The file is a single-session claim: concurrent
    connections sharing one token will fight over one session at the
    upstream server. Use a per-user path and don't enable it on shared
    multi-user proxies.

    Args:
        ws_url: WebSocket URL of the upstream terminal server
            (e.g. ``"wss://warp.provide.io/ws/terminal"``).
        color_mode: ANSI color downgrade mode — ``"passthrough"`` (default),
            ``"256"``, or ``"16"``.
        token_file: Optional path for persisting the resume token across
            proxy restarts. ``None`` (default) = in-memory only.
        iac_negotiate: When True (default), negotiate RFC 1091 TTYPE + RFC
            1572 NEW-ENVIRON with the TCP client on connect, derive a
            colour palette from the reported ``TERM`` / ``COLORTERM`` and
            append it to the upstream WS URL as ``?colormode=…``. Set to
            False to disable — useful for tests and for clients that mis-
            handle ``IAC DO`` options.
        iac_negotiate_timeout: Seconds to wait for a client response
            before giving up and opening the WS without a hint. Defaults
            to 0.4s — fast clients reply in <50ms; slow links still make
            the window comfortably.

    Example::

        gw = TelnetWsGateway("wss://warp.provide.io/ws/terminal")
        server = await gw.start(port=2112)
        await server.serve_forever()
    """

    def __init__(
        self,
        ws_url: str,
        *,
        color_mode: ColorMode = "passthrough",
        token_file: Path | None = None,
        iac_negotiate: bool = True,
        iac_negotiate_timeout: float = 0.4,
        ws_ssl: ssl.SSLContext | bool | None = None,
        client_cert: Path | str | None = None,
        client_key: Path | str | None = None,
        allow_unauthenticated: bool = False,
    ) -> None:
        _require_websockets()
        self._ws_url = ws_url
        self._color_mode = color_mode
        self._token_file = token_file
        # Telnet is plaintext + unauthenticated by nature; binding it to a
        # non-loopback address requires this explicit opt-in (mirrors the SSH
        # gateway), so it is not silently exposed to the network.
        self._allow_unauthenticated = allow_unauthenticated
        self._iac_negotiate = iac_negotiate
        self._iac_negotiate_timeout = iac_negotiate_timeout
        self._ws_ssl: ssl.SSLContext | bool | None

        if client_cert and client_key:
            if ws_ssl is not None and not isinstance(ws_ssl, bool):
                raise ValueError("Cannot provide both ws_ssl and client_cert/client_key")
            context = ssl.create_default_context()
            context.load_cert_chain(certfile=client_cert, keyfile=client_key)
            self._ws_ssl = context
        else:
            self._ws_ssl = ws_ssl

    async def start(
        self,
        host: str = TerminalDefaults.TELNET_HOST,
        port: int = TerminalDefaults.GATEWAY_TELNET_PORT,
    ) -> asyncio.AbstractServer:
        """Start the TCP listener and return the server object.

        Args:
            host: Bind address. Defaults to ``"127.0.0.1"`` (loopback). Binding
                a non-loopback address (e.g. ``"0.0.0.0"``) requires
                ``allow_unauthenticated=True`` — telnet is plaintext and
                unauthenticated, so it must not be exposed to the network silently.
            port: TCP port. Defaults to ``2112``.

        Returns:
            An :class:`asyncio.AbstractServer` — call
            ``await server.serve_forever()`` to block until shutdown.
        """
        from provide.uterm.gateway._ssh_gateway import _is_loopback_bind_host

        if not self._allow_unauthenticated and not _is_loopback_bind_host(host):
            raise RuntimeError(
                "refusing to start an unauthenticated telnet gateway on a non-loopback bind address; "
                "set allow_unauthenticated=True only when this listener is protected by another access-control layer"
            )
        return await asyncio.start_server(self._handle, host, port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle one inbound telnet connection, reconnecting on WS-side drops.

        When the upstream WebSocket closes unexpectedly (e.g. Cloudflare DO
        hibernation) while the TCP client is still connected, this method
        waits briefly and reconnects — using the in-memory resume token so
        the DO restores the session seamlessly.  If the TCP client closes
        first, no retry is attempted.
        """
        max_reconnects = 12
        reconnect_delay = 3.0

        # Per-connection token-holder, optionally seeded from disk when the
        # gateway was configured with ``token_file``. Updated when the server
        # sends a session_token frame; persisted to disk in the handler.
        token_holder: list[dict[str, Any] | None] = [None]
        if self._token_file is not None:  # pragma: no cover — saved-token load runs inside the live telnet handler
            saved = _read_token(self._token_file)
            if saved:
                token_holder[0] = saved

        try:
            for attempt in range(max_reconnects + 1):
                if reader.at_eof():
                    break
                close_code: int | None = None
                try:
                    close_code = await _pipe_ws(
                        reader,
                        writer,
                        self._ws_url,
                        ws_ssl=self._ws_ssl,
                        token_holder=token_holder,
                        color_mode=self._color_mode,
                        telnet=True,
                        token_file=self._token_file,
                        iac_negotiate=self._iac_negotiate,
                        iac_negotiate_timeout=self._iac_negotiate_timeout,
                    )
                except Exception as exc:
                    logger.debug("telnet_ws_pipe_error attempt=%d: %s", attempt, exc)

                # TCP client closed — we're done
                if reader.at_eof():
                    break

                # Deliberate server-side close (WS normal closure 1000): the
                # session ended on purpose (e.g. the user quit), so end the
                # telnet connection instead of reconnecting. Transient drops /
                # DO hibernation use 1006/None and still reconnect below.
                if close_code == 1000:
                    logger.debug("ws_closed_normally: not reconnecting (deliberate close)")
                    break

                # WS closed while TCP is still alive (hibernation or transient drop)
                if attempt < max_reconnects:
                    logger.debug(
                        "ws_disconnected_tcp_alive: reconnecting in %.1fs (attempt %d/%d)",
                        reconnect_delay,
                        attempt + 1,
                        max_reconnects,
                    )
                    # Show a reconnect indicator on the bottom row so telnet/SSH
                    # clients get the same feedback as the browser WebSocket client.
                    # Uses save/restore cursor so the game display is not disturbed.
                    try:
                        writer.write(b"\x1b7\x1b[999;1H\x1b[2;36m* reconnecting...\x1b[0m\x1b8")
                        await writer.drain()
                    except Exception:
                        pass
                    await asyncio.sleep(reconnect_delay)
                else:
                    logger.debug("ws_reconnect_exhausted: giving up after %d attempts", max_reconnects)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
