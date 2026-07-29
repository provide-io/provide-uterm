#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the telnet server's preamble.

A telnet client arriving at a terminal server has to be told, before anything
else, that this end handles echo and that the connection is full-duplex.
Getting the preamble wrong is a session where every keystroke appears twice,
or one that waits for a go-ahead nobody sends.

The bytes are the whole contract, so they are recorded exactly.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_telnetserver_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.transports import _telnet_const as const
from provide.uterm.transports import telnet_server

from provide.uterm.defaults import TerminalDefaults

OUT = Path(__file__).resolve().parent / "telnetserver_golden.json"


def main() -> None:
    corpus = {
        "handshake": telnet_server._build_telnet_handshake().decode("latin-1"),
        "handshake_bytes": list(telnet_server._build_telnet_handshake()),
        "codes": {
            "IAC": const.IAC,
            "WILL": const.WILL,
            "DO": const.DO,
            "DONT": const.DONT,
            "ECHO": const.ECHO,
            "SGA": const.SGA,
            "LINEMODE": const.LINEMODE,
            "NAWS": const.NAWS,
        },
        "defaults": {
            "bind_all": TerminalDefaults.BIND_ALL,
            "telnet_port": TerminalDefaults.TELNET_PORT,
            "negotiation_delay_s": 0.1,
        },
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['handshake_bytes'])} preamble bytes)")


if __name__ == "__main__":
    main()
