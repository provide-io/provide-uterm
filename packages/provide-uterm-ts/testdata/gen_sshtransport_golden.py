#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-client
"""Generate the differential golden corpus for the SSH transport's stream adapters.

The SSH server hands a session two halves of a process. The rest of the
platform speaks bytes to a reader and a writer, so these adapt one to the
other — and every decision they make is about what to do when the connection
underneath has already gone.

**A read that fails is end-of-stream, not an exception.** A dropped SSH
connection, a cancelled task and a clean EOF all reach a caller as "no more
bytes", because the caller's next move is the same for all three.

**Text arriving where bytes were expected is encoded, not refused.** A channel
negotiated with an encoding hands back a string; the platform below deals in
bytes, and refusing would drop a live session over a negotiation detail.

**A writer that has closed stays closed.** Writes and flushes after the fact
are silent no-ops rather than errors, because the session that closed it has
already moved on and a raised error there has nobody to catch it.

**A write that fails closes the writer.** There is nowhere left to send
anything, and continuing to try would raise once per frame for the life of
the session.

The server itself is not recorded here: the reference's process factory is
marked no-cover as a live-connection callback, so there is nothing to record.
The port stands up a real server on an ephemeral port and connects to it.

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_sshtransport_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import asyncssh
from provide.uterm.transports.ssh import SSHStreamReader, SSHStreamWriter

OUT = Path(__file__).with_name("sshtransport_golden.json")


class _Stdin:
    """A channel's input, which may answer with anything or fail."""

    def __init__(self, value: Any = b"", *, raises: BaseException | None = None) -> None:
        self._value = value
        self._raises = raises

    async def read(self, _n: int = -1) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._value


class _Stdout:
    """A channel's output, which records what it was given."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.written: list[Any] = []
        self.drained = 0
        self._raises = raises

    def write(self, data: Any) -> None:
        if self._raises is not None:
            raise self._raises
        self.written.append(data)

    async def drain(self) -> None:
        if self._raises is not None:
            raise self._raises
        self.drained += 1


class _Process:
    """The process an SSH session is given."""

    def __init__(
        self,
        stdin: _Stdin | None = None,
        stdout: _Stdout | None = None,
        peer: Any = None,
    ) -> None:
        self.stdin = stdin or _Stdin()
        self.stdout = stdout or _Stdout()
        self._peer = peer
        self.exited: list[int] = []
        self.closed = 0

    def exit(self, code: int) -> None:
        self.exited.append(code)

    def close(self) -> None:
        self.closed += 1

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "peername":
            return self._peer
        return default


# (name, what stdin yields, what it raises) — what a read returns.
READ_CASES: list[tuple[str, Any, BaseException | None]] = [
    ("bytes", b"hello", None),
    ("a bytearray", bytearray(b"hello"), None),
    ("text", "hello", None),
    ("text that is not ascii", "héllo → ✓", None),
    # A lone surrogate cannot be encoded; the reference replaces rather than
    # raising, because a session must not die over one bad character.
    ("text carrying a lone surrogate", "a\udc80b", None),
    ("nothing", b"", None),
    ("an empty string", "", None),
    ("something that is neither", 7, None),
    ("nothing at all", None, None),
    ("a closed connection", None, asyncssh.Error(1, "connection lost")),
    ("an end of file", None, EOFError()),
    ("a cancelled read", None, asyncio.CancelledError()),
]

# (name, peer) — what the writer reports about the other end.
PEER_CASES: list[tuple[str, Any]] = [
    ("an address and port", ["10.0.0.2", 5003]),
    ("an address alone", ["10.0.0.2"]),
    ("nothing", None),
    ("an empty tuple", []),
]


async def _reads() -> list[dict[str, Any]]:
    """What each kind of stdin yields through the adapter."""
    out = []
    for name, value, raises in READ_CASES:
        reader = SSHStreamReader(_Process(_Stdin(value, raises=raises)))  # type: ignore[arg-type]
        data = await reader.read()
        out.append({"name": name, "result": list(data)})
    return out


async def _writes() -> dict[str, Any]:
    """What the writer does with a live channel, and with a broken one."""
    live_out = _Stdout()
    live = SSHStreamWriter(_Process(stdout=live_out))  # type: ignore[arg-type]
    live.write(b"one")
    await live.drain()
    live.write(b"two")

    broken_out = _Stdout(raises=asyncssh.Error(1, "channel closed"))
    broken_process = _Process(stdout=broken_out)
    broken = SSHStreamWriter(broken_process)  # type: ignore[arg-type]
    broken.write(b"one")
    broken.write(b"two")
    await broken.drain()

    drain_out = _Stdout(raises=OSError("broken pipe"))
    drain_process = _Process(stdout=drain_out)
    drain_failed = SSHStreamWriter(drain_process)  # type: ignore[arg-type]
    await drain_failed.drain()

    closed_out = _Stdout()
    closed_process = _Process(stdout=closed_out)
    closed = SSHStreamWriter(closed_process)  # type: ignore[arg-type]
    closed.close()
    closed.write(b"after")
    await closed.drain()
    closed.close()

    return {
        "live": {"written": [list(item) for item in live_out.written], "drained": live_out.drained},
        # A failed write closes the writer, so the second never reaches the
        # channel and the process is exited exactly once.
        "write_failure": {
            "attempts": len(broken_out.written),
            "exited": broken_process.exited,
            "closed": broken_process.closed,
        },
        "drain_failure": {"exited": drain_process.exited, "closed": drain_process.closed},
        # Everything after close is a no-op, including closing again.
        "after_close": {
            "written": [list(item) for item in closed_out.written],
            "drained": closed_out.drained,
            "exited": closed_process.exited,
            "closed": closed_process.closed,
        },
    }


async def _peers() -> list[dict[str, Any]]:
    """What the writer reports about the other end."""
    out = []
    for name, peer in PEER_CASES:
        writer = SSHStreamWriter(_Process(peer=tuple(peer) if isinstance(peer, list) else peer))  # type: ignore[arg-type]
        found = writer.get_extra_info("peername")
        out.append(
            {
                "name": name,
                "peer": peer,
                "result": list(found) if isinstance(found, tuple) else found,
                # Anything else always takes the default.
                "other": writer.get_extra_info("sockname", "fallback"),
            }
        )
    return out


async def _build() -> dict[str, Any]:
    """Everything the adapters decide."""
    return {"reads": await _reads(), "writes": await _writes(), "peers": await _peers()}


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = asyncio.run(_build())
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(READ_CASES)} reads, {len(PEER_CASES)} peers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
