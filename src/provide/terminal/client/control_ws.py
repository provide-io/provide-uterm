#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""First-class inline control-stream WebSocket clients."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any, Literal

from provide.terminal.control_stream import ControlChunk, ControlStreamDecoder, DataChunk, encode_control, encode_data

WsRole = Literal["browser", "worker"]


def encode_logical_frame(payload: Mapping[str, Any]) -> str:
    """Encode one logical terminal/control frame for the inline WS protocol."""
    frame_type = payload.get("type")
    if frame_type in {"input", "term"}:
        return encode_data(str(payload.get("data", "")))
    return encode_control(dict(payload))


class LogicalFrameDecoder:
    """Incremental decoder that maps inline chunks back to logical WS frames."""

    def __init__(self, *, role: WsRole) -> None:
        self._role = role
        self._decoder = ControlStreamDecoder()

    def feed(self, raw: str) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for event in self._decoder.feed(raw):
            if isinstance(event, ControlChunk):
                frames.append(event.control)
            elif isinstance(event, DataChunk):  # pragma: no branch
                frames.append({"type": self._data_type(), "data": event.data})
        return frames

    def finish(self) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for event in self._decoder.finish():
            if isinstance(event, ControlChunk):
                frames.append(event.control)
            elif isinstance(event, DataChunk):  # pragma: no branch
                frames.append({"type": self._data_type(), "data": event.data})
        return frames

    def _data_type(self) -> str:
        return "input" if self._role == "worker" else "term"


class SyncInlineWebSocketClient:
    """Codec-aware wrapper for sync Starlette/FastAPI test WebSockets."""

    def __init__(self, websocket: Any, *, role: WsRole) -> None:
        self._ws = websocket
        self._decoder = LogicalFrameDecoder(role=role)
        self._pending: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    def send_frame(self, payload: Mapping[str, Any]) -> None:
        self._ws.send_text(encode_logical_frame(payload))

    def send_json(self, data: Any, mode: str = "text") -> None:  # noqa: ARG002
        if not isinstance(data, Mapping):
            raise TypeError(f"expected mapping payload, got {type(data).__name__}")
        self.send_frame(data)

    def recv_frame(self) -> dict[str, Any]:
        while True:
            if self._pending:
                return self._pending.pop(0)
            self._pending.extend(self._decoder.feed(self._ws.receive_text()))

    def receive_json(self, mode: str = "text") -> dict[str, Any]:  # noqa: ARG002
        return self.recv_frame()


class AsyncInlineWebSocketClient:
    """Codec-aware wrapper for async WebSocket clients."""

    def __init__(self, websocket: Any, *, role: WsRole) -> None:
        self._ws = websocket
        self._decoder = LogicalFrameDecoder(role=role)
        self._pending: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    async def send_frame(self, payload: Mapping[str, Any]) -> None:
        await self._ws.send(encode_logical_frame(payload))

    async def send_json(self, data: Any) -> None:
        if not isinstance(data, Mapping):
            raise TypeError(f"expected mapping payload, got {type(data).__name__}")
        await self.send_frame(data)

    async def send(self, data: Any) -> None:
        if isinstance(data, bytes):
            await self._ws.send(data)
            return
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                await self._ws.send(data)
                return
            if isinstance(parsed, Mapping):
                await self.send_frame(parsed)
                return
        elif isinstance(data, Mapping):
            await self.send_frame(data)
            return
        await self._ws.send(data)

    async def recv_frame(self) -> dict[str, Any]:
        while True:
            if self._pending:
                return self._pending.pop(0)
            raw = await self._ws.recv()
            if not isinstance(raw, str):
                raise TypeError(f"expected text WebSocket payload, got {type(raw).__name__}")
            self._pending.extend(self._decoder.feed(raw))

    async def receive_json(self) -> dict[str, Any]:
        return await self.recv_frame()

    async def recv(self) -> Any:
        frame = await self.recv_frame()
        return json.dumps(frame, ensure_ascii=True)


class _SyncTestWsConnection:
    def __init__(self, client: Any, url: str, args: tuple[Any, ...], kwargs: dict[str, Any], role: WsRole) -> None:
        self._client = client
        self._url = url
        self._args = args
        self._kwargs = kwargs
        self._role = role
        self._ctx: Any = None
        self._ws: SyncInlineWebSocketClient | None = None

    def __enter__(self) -> SyncInlineWebSocketClient:
        self._ctx = self._client.websocket_connect(self._url, *self._args, **self._kwargs)
        raw_ws = self._ctx.__enter__()
        self._ws = SyncInlineWebSocketClient(raw_ws, role=self._role)
        return self._ws

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if self._ctx is None:
            return None
        try:
            return self._ctx.__exit__(exc_type, exc, tb)
        finally:
            self._ctx = None
            self._ws = None

    def __getattr__(self, name: str) -> Any:
        if self._ws is None:
            raise AttributeError(name)
        return getattr(self._ws, name)


def connect_test_ws(client: Any, url: str, *args: Any, role: WsRole | None = None, **kwargs: Any) -> Any:
    """Wrap ``TestClient.websocket_connect`` with the inline protocol client."""
    actual_role = role or ("worker" if "/ws/worker/" in url else "browser")
    return _SyncTestWsConnection(client, url, args, kwargs, actual_role)


@asynccontextmanager
async def connect_async_ws(uri: str, *args: Any, role: WsRole | None = None, **kwargs: Any) -> Any:
    """Wrap ``websockets.connect`` with the inline protocol client."""
    import websockets

    actual_role = role or ("worker" if "/ws/worker/" in uri else "browser")
    async with websockets.connect(uri, *args, **kwargs) as ws:
        yield AsyncInlineWebSocketClient(ws, role=actual_role)
