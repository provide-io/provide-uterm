#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.embed (in-process multi-client proxy)."""

from __future__ import annotations

import asyncio

import pytest
from provide.uterm.embed import (
    BackpressurePolicy,
    ClientFilter,
    ClientMetadata,
    DefaultTelnetPolicy,
    EmbedHub,
    InterceptAction,
    InterceptContext,
    InterceptResult,
    MemoryUpstream,
    SessionLifecycle,
    WireEventKind,
)


class _ScriptInterceptor:
    def __init__(self) -> None:
        self.next_up: InterceptResult | None = None
        self.next_cli: InterceptResult | None = None
        self.after_inject = False
        self._inject_depth = 0
        self.reenter_pong: bytes | None = None

    async def on_upstream(self, context: InterceptContext) -> InterceptResult:
        if self.reenter_pong is not None and context.data == b"PING":
            await context.session.send_to_upstream(self.reenter_pong)
            return InterceptResult.pass_()
        if self.next_up is not None:
            r = self.next_up
            if r.action is InterceptAction.INJECT and self.after_inject:
                self._inject_depth += 1
                if self._inject_depth > 1:
                    self.next_up = None
                    return InterceptResult.pass_()
            else:
                self.next_up = None
            return r
        return InterceptResult.pass_()

    async def on_client(self, context: InterceptContext) -> InterceptResult:
        if self.next_cli is not None:
            r = self.next_cli
            self.next_cli = None
            return r
        return InterceptResult.pass_()


@pytest.mark.asyncio
async def test_create_connect_fanout() -> None:
    hub = EmbedHub()
    session = await hub.create_session(session_id="s1")
    assert hub.get_session("s1") is session
    up = MemoryUpstream()
    await session.connect_upstream(up)
    c1 = await session.attach_client(ClientMetadata(client_id="std", tags={"standard"}))
    await session.attach_client(ClientMetadata(client_id="deaf", tags={"deaf"}))
    await up.push_from_remote(b"HELLO")
    assert await c1.receive() == b"HELLO"
    await session.send_to_clients(b"X", ClientFilter(require_any_tag=["standard"]))
    assert await c1.receive() == b"X"
    await session.send_to_upstream(b"CMD")
    assert b"CMD" in up.sent
    await session.aclose()
    hub.remove_session("s1")


@pytest.mark.asyncio
async def test_interceptor_outcomes() -> None:
    si = _ScriptInterceptor()
    hub = EmbedHub()
    session = await hub.create_session(interceptor=si)
    up = MemoryUpstream()
    await session.connect_upstream(up)
    client = await session.attach_client(ClientMetadata(client_id="c1"))

    si.next_up = InterceptResult.consume()
    await up.push_from_remote(b"NOPE")
    await asyncio.sleep(0.03)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.receive(), timeout=0.04)

    si.next_up = InterceptResult.replace(b"REP")
    await up.push_from_remote(b"ORIG")
    assert await client.receive() == b"REP"

    si.next_up = InterceptResult.inject(b"INJ")
    si.after_inject = True
    await up.push_from_remote(b"DROPME")
    assert await client.receive() == b"INJ"

    si.next_up = InterceptResult.defer()
    await up.push_from_remote(b"LATER")
    await asyncio.sleep(0.03)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.receive(), timeout=0.04)
    si.next_up = InterceptResult.pass_()
    await session.flush_deferred()
    assert await client.receive() == b"LATER"

    si.next_cli = InterceptResult.consume()
    await session.send_to_upstream(b"LOCAL")
    assert b"LOCAL" not in up.sent

    si.reenter_pong = b"PONG"
    await up.push_from_remote(b"PING")
    assert await client.receive() == b"PING"
    await asyncio.sleep(0.05)
    assert b"PONG" in up.sent
    await session.aclose()


@pytest.mark.asyncio
async def test_replace_upstream_policy_wire() -> None:
    hub = EmbedHub()
    session = await hub.create_session(
        services={"db": "g1"},
        telnet_policy=DefaultTelnetPolicy(terminal_type="TWGS"),
    )
    assert session.services["db"] == "g1"
    up1 = MemoryUpstream()
    await session.connect_upstream(up1)
    client = await session.attach_client(ClientMetadata(client_id="c"))
    up2 = MemoryUpstream()
    await session.replace_upstream(up2)
    await up2.push_from_remote(b"AFTER")
    assert await client.receive() == b"AFTER"
    await session.mark_negotiated()
    pol = DefaultTelnetPolicy()
    assert pol.on_option(253, 0)
    assert pol.on_subnegotiation(24, b"\x01")
    assert pol.on_subnegotiation(31, b"")
    assert pol.on_option(200, 1) == b""
    wires: list[WireEventKind] = []
    session.on_wire(lambda k, _d, _x: wires.append(k))
    await session.raise_wire(WireEventKind.IAC, b"\xff", "x")
    assert WireEventKind.IAC in wires
    f = ClientFilter(exclude_tags=["deaf"])
    assert not f.matches(ClientMetadata(client_id="b", tags={"deaf"}))
    await session.aclose()


@pytest.mark.asyncio
async def test_more_branches_for_coverage() -> None:
    hub = EmbedHub()
    assert hub.session_ids == []
    session = await hub.create_session()
    assert session.session_id.startswith("embed-")
    assert session.session_id in hub.session_ids
    apps: list[bytes] = []
    clients: list[bytes] = []
    session.on_application_data(lambda _d, data, _c: apps.append(data))
    session.on_client_data(lambda data, _c: clients.append(data))
    up = MemoryUpstream()
    await session.connect_upstream(up)
    await session.send_to_upstream(b"via-client")
    assert b"via-client" in clients
    assert b"via-client" in up.sent
    await session.send_to_clients(b"")  # empty forward
    await session.send_to_clients(b"Z")
    assert b"Z" in apps
    c = await session.attach_client(
        ClientMetadata(client_id="n", queue_capacity=1, backpressure=BackpressurePolicy.DROP_NEWEST)
    )
    await up.push_from_remote(b"\x01")
    await up.push_from_remote(b"\x02")  # drop newest when full
    await asyncio.sleep(0.03)
    assert await c.receive() == b"\x01"
    pol = DefaultTelnetPolicy()
    assert pol.on_option(251, 1)  # will
    assert pol.on_option(252, 1)  # wont
    assert pol.on_option(254, 1)  # dont
    assert pol.on_subnegotiation(99, b"") == b""
    assert InterceptResult.replace(b"x").action is InterceptAction.REPLACE
    assert InterceptResult.defer().action is InterceptAction.DEFER
    assert InterceptResult.inject(b"y").action is InterceptAction.INJECT
    f = ClientFilter(require_any_tag=["need"], exclude_tags=["no"], predicate=lambda m: True)
    assert not f.matches(ClientMetadata(client_id="z", tags={"no"}))
    assert not f.matches(ClientMetadata(client_id="z", tags=set()))
    assert f.matches(ClientMetadata(client_id="z", tags={"need"}))
    assert not ClientFilter(predicate=lambda m: False).matches(ClientMetadata(client_id="z"))
    with pytest.raises(ValueError):
        await session.attach_client(ClientMetadata(client_id=""))
    # disconnect policy
    s2 = await hub.create_session(session_id="d2")
    up2 = MemoryUpstream()
    await s2.connect_upstream(up2)
    await s2.attach_client(
        ClientMetadata(
            client_id="g",
            queue_capacity=1,
            backpressure=BackpressurePolicy.DISCONNECT,
        )
    )
    await up2.push_from_remote(b"a")
    await up2.push_from_remote(b"b")
    await asyncio.sleep(0.03)
    # send without upstream
    s3 = await hub.create_session(session_id="nou")
    with pytest.raises(RuntimeError):
        await s3.send_to_upstream(b"x")
    with pytest.raises(RuntimeError):
        await hub.create_session(session_id="nou")
    # mark negotiated without connected upstream still ok after connect
    await s3.mark_negotiated()
    mem = MemoryUpstream()
    await mem.connect()
    await mem.send(b"1")
    mem.complete_remote()
    with pytest.raises(RuntimeError):
        await mem.push_from_remote(b"2")
    assert await mem.receive() == b""
    # client-direction inject + deferred client flush
    si = _ScriptInterceptor()
    s4 = await hub.create_session(session_id="inj", interceptor=si)
    up4 = MemoryUpstream()
    await s4.connect_upstream(up4)
    si.next_cli = InterceptResult.inject(b"INJ2")
    si.after_inject = False

    class _OnceInject:
        def __init__(self) -> None:
            self.n = 0

        async def on_upstream(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.pass_()

        async def on_client(self, context: InterceptContext) -> InterceptResult:
            self.n += 1
            if self.n == 1:
                return InterceptResult.inject(b"I2")
            return InterceptResult.pass_()

    s5 = await hub.create_session(session_id="inj2", interceptor=_OnceInject())
    up5 = MemoryUpstream()
    await s5.connect_upstream(up5)
    await s5.send_to_upstream(b"orig")
    assert b"I2" in up5.sent

    # replace with empty payload hits empty-forward
    class _EmptyRep:
        async def on_upstream(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.replace(b"")

        async def on_client(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.pass_()

    s5b = await hub.create_session(session_id="emptyrep", interceptor=_EmptyRep())
    up5b = MemoryUpstream()
    await s5b.connect_upstream(up5b)
    c5b = await s5b.attach_client(ClientMetadata(client_id="c5b"))
    await up5b.push_from_remote(b"x")
    await asyncio.sleep(0.03)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(c5b.receive(), timeout=0.04)

    class _DeferCli:
        async def on_upstream(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.pass_()

        async def on_client(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.defer()

    s6 = await hub.create_session(session_id="defc", interceptor=_DeferCli())
    up6 = MemoryUpstream()
    await s6.connect_upstream(up6)
    await s6.send_to_upstream(b"D")
    assert b"D" not in up6.sent

    class _Pass:
        async def on_upstream(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.pass_()

        async def on_client(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.pass_()

    s6._interceptor = _Pass()  # type: ignore[method-assign]
    await s6.flush_deferred()
    assert b"D" in up6.sent

    # MemoryUpstream disconnect path when already connected
    mem2 = MemoryUpstream()
    await mem2.connect()
    await mem2.disconnect()
    with pytest.raises(RuntimeError):
        await mem2.send(b"z")

    # replace with no prior connect (old_task/old_up None arms)
    s7a = await hub.create_session(session_id="rep-none")
    await s7a.replace_upstream(MemoryUpstream())

    s7 = await hub.create_session(session_id="rep0")
    up7 = MemoryUpstream()
    await s7.connect_upstream(up7)
    await s7.replace_upstream(MemoryUpstream())
    await s7.replace_upstream(MemoryUpstream())

    # DEFER while from_defer=True: interceptor always defers; flush should not loop forever
    class _AlwaysDeferUp:
        async def on_upstream(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.defer()

        async def on_client(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.pass_()

    s8 = await hub.create_session(session_id="defup", interceptor=_AlwaysDeferUp())
    up8 = MemoryUpstream()
    await s8.connect_upstream(up8)
    c8 = await s8.attach_client(ClientMetadata(client_id="c8"))
    await up8.push_from_remote(b"hold")
    await asyncio.sleep(0.03)
    await s8.flush_deferred()  # from_defer True + DEFER → drop re-queue
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(c8.receive(), timeout=0.04)

    # inject with None payload (or b"")
    class _InjEmpty:
        def __init__(self) -> None:
            self.n = 0

        async def on_upstream(self, context: InterceptContext) -> InterceptResult:
            self.n += 1
            if self.n == 1:
                return InterceptResult(action=InterceptAction.INJECT, payload=None)
            return InterceptResult.pass_()

        async def on_client(self, context: InterceptContext) -> InterceptResult:
            return InterceptResult.pass_()

    s9 = await hub.create_session(session_id="inje", interceptor=_InjEmpty())
    up9 = MemoryUpstream()
    await s9.connect_upstream(up9)
    await up9.push_from_remote(b"x")
    await asyncio.sleep(0.03)

    # reader EOF while RECONNECTING skips UPSTREAM_LOST
    s10 = await hub.create_session(session_id="recon")
    up10 = MemoryUpstream()
    await s10.connect_upstream(up10)
    async with s10._lock:
        s10.lifecycle = SessionLifecycle.RECONNECTING
    up10.complete_remote()
    await asyncio.sleep(0.05)
    assert s10.lifecycle is SessionLifecycle.RECONNECTING

    await session.aclose()
    await s2.aclose()
    await s3.aclose()
    await s4.aclose()
    await s5.aclose()
    await s5b.aclose()
    await s6.aclose()
    await s7a.aclose()
    await s7.aclose()
    await s8.aclose()
    await s9.aclose()
    await s10.aclose()


@pytest.mark.asyncio
async def test_upstream_lost_and_backpressure() -> None:
    hub = EmbedHub()
    session = await hub.create_session()
    up = MemoryUpstream()
    await session.connect_upstream(up)
    await session.attach_client(ClientMetadata(client_id="x"))
    with pytest.raises(RuntimeError):
        await session.attach_client(ClientMetadata(client_id="x"))
    lost = asyncio.Event()
    session.on_lifecycle(lambda p, _d: lost.set() if p is SessionLifecycle.UPSTREAM_LOST else None)
    up.complete_remote()
    await asyncio.wait_for(lost.wait(), timeout=2.0)

    s2 = await hub.create_session(session_id="bp")
    up2 = MemoryUpstream()
    await s2.connect_upstream(up2)
    await s2.attach_client(
        ClientMetadata(
            client_id="slow",
            queue_capacity=2,
            backpressure=BackpressurePolicy.DROP_OLDEST,
        )
    )
    for i in range(5):
        await up2.push_from_remote(bytes([i]))
    await asyncio.sleep(0.04)
    await s2.send_to_upstream(b"\x09")
    assert b"\x09" in up2.sent
    await session.aclose()
    await s2.aclose()


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
