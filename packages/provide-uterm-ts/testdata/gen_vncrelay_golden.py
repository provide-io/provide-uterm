#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the VNC human relay.

The relay pumps a graphical session between a browser and an upstream RFB
server. Four orderings in it are load-bearing, and each one is a bug if it is
got wrong rather than a preference:

* **The update driver waits for the client's first update request.** The
  client's pixel format and encodings precede that request; a driver that
  started injecting earlier would have the server answer in its own format and
  the client render those frames with swapped colours.
* **One lock guards every write upstream.** The driver and the browser→upstream
  filter both write there, and a message split down the middle by the other is
  not a message.
* **Teardown stops the driver before it closes anything**, so the driver cannot
  write into a stream being torn down.
* **A shutdown race is logged, not raised.** A closed pipe while the relay is
  ending is the ordinary way this stops; anything else is a real fault and is
  re-raised.

The driver exists because a client like noVNC sends one full update request
and then goes quiet — without it, an animating screen freezes on frame one.

# uv-package: provide-uterm

Usage (from the repository root)::

    uv run --package provide-uterm python \\
        packages/provide-uterm-ts/testdata/gen_vncrelay_golden.py
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from provide.uterm.vnc import human_relay

OUT = Path(__file__).resolve().parent / "vncrelay_golden.json"


def _unblockable(stream: Any) -> bool:
    """Whether the teardown would close this stream.

    A real socket or pipe is closed to unblock a stuck pump; a ``BytesIO`` is
    not, because closing it would clobber a caller's buffer.
    """
    before = getattr(stream, "closed", None)
    human_relay._unblock_fd_stream(stream)
    after = getattr(stream, "closed", None)
    return bool(after) and not bool(before)


class NoFileno:
    """A stream with no file descriptor at all."""

    closed = False

    def fileno(self) -> int:
        raise OSError("no fd")

    def close(self) -> None:
        self.closed = True


class RaisingClose:
    """A stream whose close fails, which the teardown tolerates."""

    def fileno(self) -> int:
        return 3

    def close(self) -> None:
        raise OSError("already gone")


def main() -> None:
    with open("/dev/null", "rb") as real_fd:  # noqa: PTH123
        real_fd_unblocked = _unblockable(real_fd)

    tolerated = True
    try:
        human_relay._unblock_fd_stream(RaisingClose())
    except OSError:
        tolerated = False

    corpus = {
        "pump_chunk": human_relay._PUMP_CHUNK,
        "join_timeout_s": human_relay._JOIN_TIMEOUT_S,
        "default_update_drive_interval_s": human_relay.DEFAULT_UPDATE_DRIVE_INTERVAL_S,
        "drive_handshake_wait_s": human_relay._DRIVE_HANDSHAKE_WAIT_S,
        # The incremental FramebufferUpdateRequest the driver injects: whole
        # surface, incremental, with the width and height left at their
        # sixteen-bit maximum for the server to clamp.
        "drive_fbur": list(human_relay._DRIVE_FBUR),
        "unblock": [
            {"name": "a buffer in memory", "closed": _unblockable(io.BytesIO(b"x"))},
            {"name": "a stream with no descriptor", "closed": _unblockable(NoFileno())},
            {"name": "a real file descriptor", "closed": real_fd_unblocked},
        ],
        "close_failure_tolerated": tolerated,
        # Which pump failures are a shutdown race and which are a real fault.
        "pump_errors": [
            {"error": "OSError", "reraised": False},
            {"error": "ValueError", "reraised": False},
            {"error": "RuntimeError", "reraised": True},
            {"error": "KeyError", "reraised": True},
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
