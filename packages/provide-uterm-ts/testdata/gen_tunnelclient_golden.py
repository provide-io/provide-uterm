#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the tunnel client's transport.

The client is what a worker talks to a tunnel through, so what it puts on the
wire is the whole contract: an `open` that names the terminal's size, data
frames on a channel, a resize, an end-of-file, and a reconnect schedule that
backs off rather than hammering a server that is already down.

Two things are worth pinning beyond the bytes:

* **Nothing is sent before there is a connection.** Every call refuses rather
  than queueing, because a frame that is silently dropped looks to a caller
  exactly like one that was delivered and ignored.
* **The backoff schedule is fixed and it saturates.** A client that kept
  doubling would eventually stop retrying at all, and one that never backed
  off would be a client attacking its own server.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_tunnelclient_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.tunnel import client as tunnel_client
from provide.uterm.tunnel.protocol import CHANNEL_DATA, FLAG_EOF, encode_control, encode_frame

OUT = Path(__file__).resolve().parent / "tunnelclient_golden.json"


def main() -> None:
    schedule = list(tunnel_client.BACKOFF_SCHEDULE)
    corpus = {
        "backoff_schedule": schedule,
        # What a caller waits before each attempt, saturating at the last step.
        "delays": [
            {"attempt": attempt, "delay": schedule[min(attempt, len(schedule) - 1)]}
            for attempt in range(len(schedule) + 4)
        ],
        "frames": {
            "open": encode_control(
                {"type": "open", "channel": 1, "tunnel_type": "terminal", "term_size": [80, 25]}
            ).decode("latin-1"),
            "open_other_size": encode_control(
                {"type": "open", "channel": 1, "tunnel_type": "terminal", "term_size": [132, 43]}
            ).decode("latin-1"),
            "resize": encode_control({"type": "resize", "channel": 1, "cols": 100, "rows": 30}).decode("latin-1"),
            "data": encode_frame(CHANNEL_DATA, b"hello").decode("latin-1"),
            "data_other_channel": encode_frame(2, b"hello").decode("latin-1"),
            "data_empty": encode_frame(CHANNEL_DATA, b"").decode("latin-1"),
            "eof": encode_frame(CHANNEL_DATA, b"", flags=FLAG_EOF).decode("latin-1"),
            "eof_other_channel": encode_frame(3, b"", flags=FLAG_EOF).decode("latin-1"),
        },
        "channels": {"data": CHANNEL_DATA, "eof_flag": FLAG_EOF},
        "auth_header": "Bearer a-token",
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(schedule)} backoff steps)")


if __name__ == "__main__":
    main()
