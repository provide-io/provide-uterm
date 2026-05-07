#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Static help text for ushell commands."""

from __future__ import annotations

from provide.terminal.shell._output import fmt_kv, heading

_HELP = (
    f"{heading('ushell commands')}"
    f"{fmt_kv('help [cmd]', 'this help text, or detail for <cmd>')}"
    f"{fmt_kv('clear', 'erase the terminal screen')}"
    f"{fmt_kv('py <expr>', 'evaluate Python expression or statement')}"
    f"{fmt_kv('sessions', 'list active sessions from KV registry')}"
    f"{fmt_kv('sessions kill <id>', 'force-terminate a session DO')}"
    f"{fmt_kv('kv list', 'list all KV session keys')}"
    f"{fmt_kv('kv get <key>', 'read a KV value')}"
    f"{fmt_kv('kv set <key> <value>', 'write a KV entry')}"
    f"{fmt_kv('kv delete <key>', 'delete a KV entry')}"
    f"{fmt_kv('fetch [-X METHOD] <url> [body]', 'HTTP request (GET by default)')}"
    f"{fmt_kv('render [flags] <url>', 'render image as ANSI art')}"
    f"{fmt_kv('cast [--fps N] [--loop] <url>', 'fetch and replay an asciicast v2 file')}"
    f"{fmt_kv('storage list', 'list DO storage keys')}"
    f"{fmt_kv('storage get <key>', 'read a DO storage value')}"
    f"{fmt_kv('env', 'show available context keys')}"
    f"{fmt_kv('exit / quit', 'end this shell session')}"
)

_COMMAND_HELP: dict[str, str] = {
    "help": "help [cmd] — show all commands or detail for <cmd>.\r\n",
    "clear": "clear — erase the terminal screen (ANSI reset).\r\n",
    "py": (
        "py <expr> — evaluate a Python expression or exec a statement.\r\n"
        "Variables persist across py calls for the session lifetime.\r\n"
        "Available: json, datetime, re, hashlib, base64, plus safe builtins.\r\n"
    ),
    "sessions": (
        "sessions — list all sessions from the KV registry.\r\n"
        "sessions kill <id> — force-terminate a session Durable Object.\r\n"
    ),
    "kv": (
        "kv list                  — list all KV keys with session: prefix.\r\n"
        "kv get <key>             — read a KV value (session: prefix added if absent).\r\n"
        "kv set <key> <value>     — write a KV entry.\r\n"
        "kv delete <key>          — delete a KV entry.\r\n"
    ),
    "fetch": (
        "fetch [-X METHOD] <url> [body] — HTTP request.\r\n"
        "  Default method is GET.  Use -X POST, -X PUT, etc. to change it.\r\n"
        "  Optional body is sent as the request body.\r\n"
    ),
    "storage": (
        "storage list         — list all DO storage keys.\r\nstorage get <key>    — read a DO storage value by key.\r\n"
    ),
    "env": "env — show available context keys and their types.\r\n",
    "render": (
        "render [--mode truecolor|256|16] [--cols N] [--rows N] [--fps N] [--loop] <url>\r\n"
        "  Render an image as ANSI art in the terminal.\r\n"
        "  Supports PNG, JPEG, GIF, APNG, WebP, BMP, TIFF, and more.\r\n"
        "  Animated images stream frames; use --loop to repeat.\r\n"
        "  Requires: pip install 'provide-uterm[emulator]'\r\n"
    ),
    "cast": (
        "cast [--fps N] [--loop] <url>\r\n"
        "  Fetch and replay an asciicast v2 (.cast) recording file.\r\n"
        "  Supports http://, https://, and file:// URLs.\r\n"
        "  --fps N    playback speed (default: 15)\r\n"
        "  --loop     repeat the recording until interrupted\r\n"
    ),
    "exit": "exit / quit — end this shell session.\r\n",
    "quit": "exit / quit — end this shell session.\r\n",
}
