#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Embed telnet upstream / parser tests."""

from __future__ import annotations

import pytest
from provide.uterm.embed import (
    ClientMetadata,
    DefaultTelnetPolicy,
    EmbedHub,
    WireEventKind,
)


@pytest.mark.asyncio
async def test_scripted_telnet_upstream() -> None:
    from provide.uterm.embed.telnet_upstream import (
        ScriptedTelnetUpstream,
        escape_iac,
        parse_telnet_buffer,
    )

    payload, events, cons = parse_telnet_buffer(b"Hi\xff\xff!")
    assert payload == b"Hi\xff!"
    assert not events
    assert escape_iac(b"\xff\x01") == b"\xff\xff\x01"

    up = ScriptedTelnetUpstream(DefaultTelnetPolicy(terminal_type="ANSI-BBS"))
    wires: list[WireEventKind] = []

    def _wire(k: WireEventKind, _d: bytes, _x: str) -> None:
        wires.append(k)

    up.on_wire = _wire
    await up.connect()
    await up.push_wire(bytes((255, 253, 0)))  # DO BINARY
    await up.push_wire(b"GO")
    hub = EmbedHub()
    session = await hub.create_session()
    await session.connect_upstream(up)
    client = await session.attach_client(ClientMetadata(client_id="c1"))
    assert await client.receive() == b"GO"
    assert wires
    assert up.sent_wire
    await session.send_to_upstream(b"\xff\x01")
    assert any(b == b"\xff\xff\x01" for b in up.sent_wire)
    await session.aclose()


def test_parse_telnet_edge_cases() -> None:
    from provide.uterm.embed.telnet_upstream import parse_telnet_buffer

    # truncated IAC
    p, e, c = parse_telnet_buffer(b"Z\xff", final=False)
    assert p == b"Z" and c == 1 and not e
    p, e, c = parse_telnet_buffer(b"Z\xff", final=True)
    assert p == b"Z\xff" and c == 2
    # truncated DO
    p, e, c = parse_telnet_buffer(bytes((255, 253)), final=True)
    assert p == bytes((255, 253)) and not e
    # incomplete SB
    p, e, c = parse_telnet_buffer(bytes((255, 250, 24, 1)), final=False)
    assert p == b"" and c == 0
    p, e, c = parse_telnet_buffer(bytes((255, 250, 24, 1)), final=True)
    assert len(p) == 4
    # unknown cmd
    p, e, c = parse_telnet_buffer(bytes((255, 241, ord("Q"))))
    assert p == b"Q"


def test_parse_telnet_incomplete_without_final() -> None:
    from provide.uterm.embed.telnet_upstream import parse_telnet_buffer

    p, e, c = parse_telnet_buffer(bytes((255, 253)), final=False)
    assert p == b"" and c == 0
    p, e, c = parse_telnet_buffer(bytes((255, 250, 24)), final=False)
    assert c == 0


@pytest.mark.asyncio
async def test_scripted_telnet_no_wire_hook() -> None:
    from provide.uterm.embed.telnet_upstream import ScriptedTelnetUpstream

    up = ScriptedTelnetUpstream()
    await up.connect()
    # incomplete SB first → consumed=0 path, then complete
    await up.push_wire(bytes((255, 250, 24, 1)))
    await up.push_wire(bytes((255, 240)) + b"A")
    assert await up.receive() == b"A"
    # IAC-only then app (payload empty then non-empty loop)
    await up.push_wire(bytes((255, 253, 0)))
    await up.push_wire(b"B")
    assert await up.receive() == b"B"
    await up.disconnect()
    await up.disconnect()


@pytest.mark.asyncio
async def test_scripted_telnet_subneg_disconnect() -> None:
    from provide.uterm.embed.telnet_upstream import ScriptedTelnetUpstream

    up = ScriptedTelnetUpstream(DefaultTelnetPolicy(terminal_type="X"))
    await up.connect()
    await up.push_wire(bytes((255, 250, 24, 1, 255, 240)))
    await up.push_wire(b"Z")
    assert await up.receive() == b"Z"
    assert up.sent_wire
    await up.disconnect()
    assert await up.receive() == b""


@pytest.mark.asyncio
async def test_scripted_telnet_empty_policy_reply() -> None:
    from provide.uterm.embed.telnet_upstream import ScriptedTelnetUpstream

    class EmptyPolicy:
        terminal_type = "ANSI"
        window_size = (80, 25)

        def on_option(self, command: int, option: int) -> bytes:
            return b""

        def on_subnegotiation(self, option: int, body: bytes) -> bytes:
            return b""

    up = ScriptedTelnetUpstream(EmptyPolicy())  # type: ignore[arg-type]
    await up.connect()
    await up.push_wire(bytes((255, 253, 0)))
    await up.push_wire(b"X")
    assert await up.receive() == b"X"
    assert up.sent_wire == []
    await up.disconnect()
