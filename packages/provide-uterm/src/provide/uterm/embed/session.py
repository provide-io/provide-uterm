#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Embed session, interceptors, and memory upstream."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from provide.uterm.embed.types import (
    BackpressurePolicy,
    ByteDirection,
    ClientFilter,
    ClientMetadata,
    DefaultTelnetPolicy,
    InterceptAction,
    InterceptResult,
    SessionLifecycle,
    TelnetPolicy,
    UpstreamPipe,
    WireEventKind,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "ByteInterceptor",
    "ClientHandle",
    "EmbedSession",
    "InterceptContext",
    "MemoryUpstream",
    "PassThroughInterceptor",
]


@dataclass(slots=True)
class InterceptContext:
    session: EmbedSession
    direction: ByteDirection
    data: bytes
    client_id: str | None = None


class ByteInterceptor(Protocol):
    async def on_upstream(self, context: InterceptContext) -> InterceptResult: ...

    async def on_client(self, context: InterceptContext) -> InterceptResult: ...


class PassThroughInterceptor:
    async def on_upstream(self, context: InterceptContext) -> InterceptResult:
        return InterceptResult.pass_()

    async def on_client(self, context: InterceptContext) -> InterceptResult:
        return InterceptResult.pass_()


class MemoryUpstream:
    """Deterministic test upstream."""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._sent: list[bytes] = []
        self._connected = False
        self._closed = False

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._closed

    @property
    def sent(self) -> list[bytes]:
        return list(self._sent)

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        if not self._closed:
            self._closed = True
            await self._inbound.put(None)
        self._connected = False

    async def send(self, data: bytes) -> None:
        if not self.is_connected:
            raise RuntimeError("not connected")
        self._sent.append(bytes(data))

    async def receive(self) -> bytes:
        item = await self._inbound.get()
        if item is None:
            return b""
        return item

    async def push_from_remote(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("upstream closed")
        await self._inbound.put(bytes(data))

    def complete_remote(self) -> None:
        self._closed = True
        self._connected = False
        self._inbound.put_nowait(None)


class ClientHandle:
    def __init__(self, client_id: str, meta: ClientMetadata, queue: asyncio.Queue[bytes]) -> None:
        self.client_id = client_id
        self.metadata = meta
        self._queue = queue
        self.is_attached = True

    async def receive(self) -> bytes:
        return await self._queue.get()


@dataclass
class _Deferred:
    direction: ByteDirection
    data: bytes
    client_id: str | None


class EmbedSession:
    """Ordered multi-client proxy session (re-entrancy-safe via single lock)."""

    def __init__(
        self,
        session_id: str,
        *,
        interceptor: ByteInterceptor | None = None,
        telnet_policy: TelnetPolicy | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> None:
        self.session_id = session_id
        self._interceptor: ByteInterceptor = interceptor or PassThroughInterceptor()
        self._telnet = telnet_policy or DefaultTelnetPolicy()
        self.services: dict[str, Any] = dict(services or {})
        self.services["telnet_policy"] = self._telnet
        self.lifecycle = SessionLifecycle.CREATED
        self._upstream: UpstreamPipe | None = None
        self._clients: dict[str, tuple[ClientMetadata, asyncio.Queue[bytes]]] = {}
        self._deferred: list[_Deferred] = []
        self._lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._pipeline_depth: contextvars.ContextVar[int] = contextvars.ContextVar("pipeline_depth", default=0)
        self._on_app: list[Callable[[ByteDirection, bytes, str | None], None]] = []
        self._on_client: list[Callable[[bytes, str | None], None]] = []
        self._on_wire: list[Callable[[WireEventKind, bytes, str], None]] = []
        self._on_life: list[Callable[[SessionLifecycle, str], None]] = []

    def on_application_data(self, cb: Callable[[ByteDirection, bytes, str | None], None]) -> None:
        self._on_app.append(cb)

    def on_client_data(self, cb: Callable[[bytes, str | None], None]) -> None:
        self._on_client.append(cb)

    def on_wire(self, cb: Callable[[WireEventKind, bytes, str], None]) -> None:
        self._on_wire.append(cb)

    def on_lifecycle(self, cb: Callable[[SessionLifecycle, str], None]) -> None:
        self._on_life.append(cb)

    def _set_life(self, phase: SessionLifecycle, detail: str = "") -> None:
        self.lifecycle = phase
        for cb in self._on_life:
            cb(phase, detail)

    async def connect_upstream(self, upstream: UpstreamPipe) -> None:
        async with self._lock:
            original_state = self.lifecycle
            self._set_life(SessionLifecycle.CONNECTING)
            try:
                await upstream.connect()
            except Exception:
                self._set_life(original_state)
                raise
            self._upstream = upstream
            self._start_reader()
            self._set_life(SessionLifecycle.CONNECTED)

    async def replace_upstream(self, upstream: UpstreamPipe) -> None:
        async with self._lock:
            original_state = self.lifecycle
            self._set_life(SessionLifecycle.RECONNECTING)
            old_task = self._reader_task
            self._reader_task = None
            old_up = self._upstream
        if old_task is not None:
            old_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old_task
        if old_up is not None:
            with contextlib.suppress(Exception):
                await old_up.disconnect()
        async with self._lock:
            self._set_life(SessionLifecycle.CONNECTING)
            try:
                await upstream.connect()
            except Exception:
                self._set_life(original_state)
                raise
            self._upstream = upstream
            self._start_reader()
            self._set_life(SessionLifecycle.CONNECTED)

    async def mark_negotiated(self) -> None:
        async with self._lock:
            self._set_life(SessionLifecycle.NEGOTIATED)
            if self._upstream is not None and self._upstream.is_connected:
                self._set_life(SessionLifecycle.CONNECTED)

    async def attach_client(self, meta: ClientMetadata) -> ClientHandle:
        async with self._lock:
            if not meta.client_id:
                raise ValueError("client_id required")
            if meta.client_id in self._clients:
                raise RuntimeError(f"client already attached: {meta.client_id}")
            cap = max(1, meta.queue_capacity)
            q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=cap)
            self._clients[meta.client_id] = (meta, q)
            self._set_life(SessionLifecycle.CLIENT_ATTACHED, meta.client_id)
            return ClientHandle(meta.client_id, meta, q)

    async def send_to_upstream(self, data: bytes) -> None:
        payload = bytes(data)
        # Re-entrant from interceptor while lock is held (pipeline_depth > 0).
        if self._pipeline_depth.get() > 0:
            await self._process_client(payload, None)
            return
        async with self._lock:
            await self._process_client(payload, None)

    async def send_to_clients(self, data: bytes, filter_: ClientFilter | None = None) -> None:
        async with self._lock:
            f = filter_ or ClientFilter()
            payload = bytes(data)
            self._deliver(payload, f)
            for cb in self._on_app:
                cb(ByteDirection.UPSTREAM_TO_APP, payload, None)

    async def flush_deferred(self) -> None:
        async with self._lock:
            while self._deferred:
                item = self._deferred.pop(0)
                if item.direction is ByteDirection.UPSTREAM_TO_APP:
                    await self._process_upstream(item.data, from_defer=True)
                else:
                    await self._process_client(item.data, item.client_id)

    async def raise_wire(self, kind: WireEventKind, data: bytes, detail: str = "") -> None:
        for cb in self._on_wire:
            cb(kind, bytes(data), detail)

    async def aclose(self) -> None:
        async with self._lock:
            old_task = self._reader_task
            self._reader_task = None
            old_up = self._upstream
            self._upstream = None
            self._clients.clear()
        if old_task is not None:
            old_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old_task
        if old_up is not None:
            with contextlib.suppress(Exception):
                await old_up.disconnect()
        async with self._lock:
            self._set_life(SessionLifecycle.SHUTDOWN)

    def _start_reader(self) -> None:
        assert self._upstream is not None
        upstream = self._upstream

        async def _loop() -> None:
            try:
                while True:
                    chunk = await upstream.receive()
                    if not chunk:
                        async with self._lock:
                            if self.lifecycle not in (
                                SessionLifecycle.SHUTDOWN,
                                SessionLifecycle.RECONNECTING,
                            ):
                                self._set_life(SessionLifecycle.UPSTREAM_LOST)
                        return
                    async with self._lock:
                        await self._process_upstream(chunk)
            except asyncio.CancelledError:
                return

        self._reader_task = asyncio.create_task(_loop())

    async def _process_upstream(self, data: bytes, *, from_defer: bool = False) -> None:
        token = self._pipeline_depth.set(self._pipeline_depth.get() + 1)
        try:
            ctx = InterceptContext(self, ByteDirection.UPSTREAM_TO_APP, data)
            result = await self._interceptor.on_upstream(ctx)
            await self._apply(result, data, ByteDirection.UPSTREAM_TO_APP, None, from_defer)
        finally:
            self._pipeline_depth.reset(token)

    async def _process_client(self, data: bytes, client_id: str | None) -> None:
        token = self._pipeline_depth.set(self._pipeline_depth.get() + 1)
        try:
            for cb in self._on_client:
                cb(data, client_id)
            ctx = InterceptContext(self, ByteDirection.CLIENT_TO_UPSTREAM, data, client_id)
            result = await self._interceptor.on_client(ctx)
            await self._apply(result, data, ByteDirection.CLIENT_TO_UPSTREAM, client_id, False)
        finally:
            self._pipeline_depth.reset(token)

    async def _apply(
        self,
        result: InterceptResult,
        original: bytes,
        direction: ByteDirection,
        client_id: str | None,
        from_defer: bool,
    ) -> None:
        if result.action is InterceptAction.PASS:
            await self._forward(original, direction)
        elif result.action is InterceptAction.REPLACE:
            await self._forward(result.payload or b"", direction)
        elif result.action is InterceptAction.CONSUME:
            return
        elif result.action is InterceptAction.DEFER:
            if not from_defer:
                self._deferred.append(_Deferred(direction, original, client_id))
        else:
            # INJECT (only remaining InterceptAction)
            payload = result.payload or b""
            if direction is ByteDirection.UPSTREAM_TO_APP:
                await self._process_upstream(payload)
            else:
                await self._process_client(payload, client_id)

    async def _forward(self, data: bytes, direction: ByteDirection) -> None:
        if not data:
            return
        if direction is ByteDirection.UPSTREAM_TO_APP:
            self._deliver(data, ClientFilter())
            for cb in self._on_app:
                cb(direction, data, None)
            return
        if self._upstream is None or not self._upstream.is_connected:
            raise RuntimeError("upstream not connected")
        await self._upstream.send(data)

    def _deliver(self, data: bytes, filter_: ClientFilter) -> None:
        drop: list[str] = []
        for cid, (meta, q) in list(self._clients.items()):
            if not filter_.matches(meta):
                continue
            if not self._try_enqueue(meta, q, data) and meta.backpressure is BackpressurePolicy.DISCONNECT:
                drop.append(cid)
        for cid in drop:
            self._clients.pop(cid, None)

    @staticmethod
    def _try_enqueue(meta: ClientMetadata, q: asyncio.Queue[bytes], data: bytes) -> bool:
        payload = bytes(data)
        if q.full():
            if meta.backpressure is BackpressurePolicy.DROP_NEWEST:
                return False
            if meta.backpressure is BackpressurePolicy.DISCONNECT:
                return False
            # DROP_OLDEST
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
        q.put_nowait(payload)
        return True
