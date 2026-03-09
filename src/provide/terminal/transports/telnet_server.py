#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Asyncio TCP/telnet server helper for provide-terminal.

Provides :func:`start_telnet_server` — an asyncio TCP server that sends the
standard telnet negotiation preamble on each new connection, then hands off to
a caller-supplied async handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telnet protocol constants (subset used for server handshake)
# ---------------------------------------------------------------------------

IAC: int = 255
WILL: int = 251
DO: int = 253
DONT: int = 254
ECHO: int = 1
SGA: int = 3
NAWS: int = 31
LINEMODE: int = 34

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

ConnectionHandler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter],
    Coroutine[Any, Any, None],
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_telnet_handshake() -> bytes:
    """Build the initial telnet negotiation sequence.

    Sends:
    - IAC WILL ECHO   (server will handle echo)
    - IAC WILL SGA    (suppress go-ahead for full-duplex)
    - IAC DO SGA      (request client suppress go-ahead too)
    - IAC DONT LINEMODE (disable client-side line editing)
    - IAC DO NAWS     (request client window size)
    """
    return bytes(
        [
            IAC,
            WILL,
            ECHO,
            IAC,
            WILL,
            SGA,
            IAC,
            DO,
            SGA,
            IAC,
            DONT,
            LINEMODE,
            IAC,
            DO,
            NAWS,
        ]
    )


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


async def start_telnet_server(
    handler: ConnectionHandler,
    host: str = "0.0.0.0",  # nosec B104
    port: int = 2102,
    *,
    negotiation_delay_s: float = 0.1,
) -> asyncio.Server:
    """Create and start an asyncio TCP server with basic telnet negotiation.

    Sends the IAC negotiation preamble on each new connection, then delegates
    to *handler* with the raw ``(reader, writer)`` pair.

    Args:
        handler: Async callback ``(reader, writer) -> None`` called per connection.
        host: Network interface to bind to (default ``0.0.0.0``).
        port: TCP port number (default ``2102``).
        negotiation_delay_s: Seconds to pause after sending the IAC negotiation
            preamble before handing off to *handler*.  Gives slow clients time to
            process the negotiation options.  Defaults to ``0.1`` (100 ms).

    Returns:
        The running :class:`asyncio.Server` instance.
    """

    async def _client_cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        addr = f"{peername[0]}:{peername[1]}" if peername else "unknown"
        logger.info("telnet client connected addr=%s", addr)

        try:
            writer.write(_build_telnet_handshake())
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):  # pragma: no cover
            logger.warning("connection lost during handshake addr=%s", addr)
            writer.close()
            return

        # Brief pause for the client to process negotiation options.
        await asyncio.sleep(negotiation_delay_s)
        try:
            await handler(reader, writer)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(_client_cb, host, port)
    logger.info("telnet server started host=%s port=%d", host, port)
    return server
