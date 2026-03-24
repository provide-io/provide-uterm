#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""provide.terminal.shell — built-in Python REPL shell for provide-terminal.

Overview
--------
``ushell`` is a self-contained interactive shell that runs *inside* the
terminal session without requiring any external process or network
connection.  It provides a Python REPL with a small set of built-in
commands for inspecting and managing the session environment.

Architecture
------------

::

    ┌─────────────────────────────────────────────────────────────┐
    │               provide.terminal.shell (this package)           │
    │                                                             │
    │  _repl.py        LineBuffer — raw keystroke → line editor   │
    │  _sandbox.py     Sandbox   — restricted Python eval/exec    │
    │  _commands.py    CommandDispatcher — command routing        │
    │  _output.py      ANSI helpers, term() frame builder         │
    │  _connector.py   UshellConnector — SessionConnector impl    │
    └──────────────────────────────────────────────────────────────┘
               ↑                            ↑
    FastAPI / HostedSessionRuntime     CF Durable Object adapter
    (uses UshellConnector directly)    (provide_terminal_cloudflare.do.ushell)

Usage — FastAPI
---------------
::

    from provide.terminal.server.hosted import HostedSessionRuntime
    from provide.terminal.shell import UshellConnector

    runtime = HostedSessionRuntime(
        session_id="my-ushell",
        connector=UshellConnector("my-ushell"),
    )

Usage — Cloudflare DO
---------------------
Sessions with IDs starting with ``ushell-`` are automatically handled by
the DO without requiring an external worker process.  Create one via Quick
Connect (select "Ushell") or ``POST /api/connect`` with
``connector_type: "ushell"``.

Commands
--------
``help``            list all commands
``clear``           erase terminal screen
``py <expr>``       evaluate Python expression / exec statement
``sessions``        list active sessions from KV registry
``kv list``         list all KV session keys
``kv get <key>``    read a KV value
``fetch <url>``     HTTP GET request
``env``             show available context bindings
``exit``/``quit``   end the session
"""

from provide.terminal.shell._connector import UshellConnector

__all__ = ["UshellConnector"]
