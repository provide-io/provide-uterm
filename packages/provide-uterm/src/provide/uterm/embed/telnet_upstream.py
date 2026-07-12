#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Policy-driven telnet helpers for embed sessions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from provide.uterm.embed import (
    DefaultTelnetPolicy,
    TelnetPolicy,
    WireEventKind,
)

if TYPE_CHECKING:
    from collections.abc import Callable

IAC = 255
WILL, WONT, DO, DONT = 251, 252, 253, 254
SB, SE = 250, 240


def escape_iac(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        out.append(b)
        if b == IAC:
            out.append(IAC)
    return bytes(out)


def parse_telnet_buffer(buf: bytes, final: bool = False) -> tuple[bytes, list[tuple[bool, int, int, bytes]], int]:
    """Return (payload, events, consumed). Event = (is_sub, cmd, opt, sub_payload)."""
    payload = bytearray()
    events: list[tuple[bool, int, int, bytes]] = []
    i = 0
    consumed = 0
    n = len(buf)
    while i < n:
        if buf[i] != IAC:
            payload.append(buf[i])
            i += 1
            consumed = i
            continue
        if i + 1 >= n:
            if final:
                payload.append(IAC)
                i += 1
                consumed = i
            break
        cmd = buf[i + 1]
        if cmd in (DO, DONT, WILL, WONT):
            if i + 2 >= n:
                if final:
                    payload.extend(buf[i:])
                    i = n
                    consumed = i
                break
            events.append((False, cmd, buf[i + 2], b""))
            i += 3
            consumed = i
            continue
        if cmd == SB:
            j = i + 2
            end = None
            while j < n - 1:
                if buf[j] == IAC and buf[j + 1] == SE:
                    end = j + 2
                    break
                j += 1
            if end is None:
                if final:
                    payload.extend(buf[i:])
                    i = n
                    consumed = i
                break
            body = buf[i + 2 : end - 2]
            opt = body[0] if body else 0
            sub = body[1:] if len(body) > 1 else b""
            events.append((True, SB, opt, bytes(sub)))
            i = end
            consumed = i
            continue
        if cmd == IAC:
            payload.append(IAC)
            i += 2
            consumed = i
            continue
        i += 2
        consumed = i
    return bytes(payload), events, consumed


class ScriptedTelnetUpstream:
    """Deterministic telnet wire for embed tests (no TCP)."""

    def __init__(self, policy: TelnetPolicy | None = None) -> None:
        self._policy: TelnetPolicy = policy or DefaultTelnetPolicy()
        self._in: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._sent: list[bytes] = []
        self._carry = bytearray()
        self._connected = False
        self.on_wire: Callable[[WireEventKind, bytes, str], None] | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def sent_wire(self) -> list[bytes]:
        return list(self._sent)

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        if self._connected:
            self._connected = False
            await self._in.put(None)

    async def send(self, data: bytes) -> None:
        self._sent.append(escape_iac(data))

    async def push_wire(self, data: bytes) -> None:
        await self._in.put(bytes(data))

    async def receive(self) -> bytes:
        while True:
            chunk = await self._in.get()
            if chunk is None:
                return b""
            self._carry.extend(chunk)
            payload, events, consumed = parse_telnet_buffer(bytes(self._carry), final=False)
            if consumed:
                del self._carry[:consumed]
            for is_sub, cmd, opt, sub in events:
                if self.on_wire is not None:
                    self.on_wire(
                        WireEventKind.NEGOTIATION if is_sub else WireEventKind.IAC,
                        sub if is_sub else bytes((IAC, cmd, opt)),
                        "sb" if is_sub else "neg",
                    )
                reply = self._policy.on_subnegotiation(opt, sub) if is_sub else self._policy.on_option(cmd, opt)
                if reply:
                    self._sent.append(bytes(reply))
            if payload:
                return payload
