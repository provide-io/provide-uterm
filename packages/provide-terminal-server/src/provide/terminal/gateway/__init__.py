#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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

from provide.terminal.gateway._colors import (
    _apply_color_mode,
    _clamp8,
    _rgb_to_16_index,
    _rgb_to_256,
)
from provide.terminal.gateway._gateway import (
    TelnetWsGateway,
    _handle_ws_control,
    _normalize_crlf,
    _pipe_ws,
    _ssh_to_ws,
    _strip_iac,
    _tcp_to_ws,
    _ws_to_ssh,
    _ws_to_tcp,
)
from provide.terminal.gateway._ssh_gateway import SshWsGateway

__all__ = [
    "SshWsGateway",
    "TelnetWsGateway",
    "_apply_color_mode",
    "_clamp8",
    "_handle_ws_control",
    "_normalize_crlf",
    "_pipe_ws",
    "_rgb_to_16_index",
    "_rgb_to_256",
    "_ssh_to_ws",
    "_strip_iac",
    "_tcp_to_ws",
    "_ws_to_ssh",
    "_ws_to_tcp",
]
