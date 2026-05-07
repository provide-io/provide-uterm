#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Command dispatcher and built-in command handlers for provide.terminal.shell.

Commands
--------
help [cmd]          — list all commands, or show detail for <cmd>
clear               — erase the terminal screen
py <expr>           — evaluate a Python expression (or exec a statement)
sessions            — list active sessions from the KV registry
sessions kill <id>  — force-terminate a session DO
kv list             — list all KV keys with the session: prefix
kv get <key>        — read a KV value by key
kv set <key> <val>  — write a KV entry
kv delete <key>     — delete a KV entry
fetch [-X METHOD] <url> [body] — HTTP request (GET by default)
render [flags] <url>    — render image as ANSI art (requires provide-uterm[emulator])
cast [--fps N] [--loop] <url> — fetch and replay an asciicast v2 (.cast) file
storage list        — list DO storage keys
storage get <key>   — read a DO storage value
env                 — show available context keys
exit / quit         — end the shell session
"""

from __future__ import annotations

from provide.terminal.shell.commands.dispatcher import CommandDispatcher
from provide.terminal.shell.commands.help import _COMMAND_HELP, _HELP
from provide.terminal.shell.commands.kv import _KV_PREFIX
from provide.terminal.shell.commands.types import AnimatedResult

__all__ = [
    "AnimatedResult",
    "CommandDispatcher",
    "_COMMAND_HELP",
    "_HELP",
    "_KV_PREFIX",
]
