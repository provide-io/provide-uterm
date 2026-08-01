#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fakes and builders for the owned-input fencing test modules."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Coroutine
from types import SimpleNamespace

from provide.uterm.cloudflare.api.http_routes._hijack import route_hijack
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder

_KEY = "test-secret-key-32-bytes-minimum!"


class _Request:
    def __init__(
        self,
        body: dict[str, object] | None = None,
        *,
        url: str = "https://example.invalid/",
        method: str = "POST",
    ) -> None:
        self.headers = {"Content-Type": "application/json"}
        self._body = json.dumps(body or {})
        self.url = url
        self.method = method

    async def text(self) -> str:
        return self._body


class _BrowserWs:
    def __init__(self, role: str = "admin") -> None:
        self.role = role
        self.sent: list[str] = []

    def deserializeAttachment(self) -> str:  # noqa: N802 - Cloudflare WebSocket API
        return f"browser:{self.role}:fence-worker"

    def serializeAttachment(self, attachment: str) -> None:  # noqa: N802 - Cloudflare WebSocket API
        self.attachment = attachment

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _BlockingWorkerWs:
    """Hold the first worker send so a lifecycle transition can race it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.sent: list[str] = []
        self._blocked = False
        self.attachment = "worker:admin:fence-worker"
        self.closed: tuple[int, str] | None = None

    def deserializeAttachment(self) -> str:  # noqa: N802 - Cloudflare WebSocket API
        return self.attachment

    def serializeAttachment(self, attachment: str) -> None:  # noqa: N802 - Cloudflare WebSocket API
        self.attachment = attachment

    def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    async def send(self, data: str) -> None:
        self.sent.append(data)
        if not self._blocked:
            self._blocked = True
            self.started.set()
            await self.release.wait()


class _FailingBrowserWs(_BrowserWs):
    async def send(self, data: str) -> None:
        raise RuntimeError("browser disconnected")


class _AttachmentWs(_BrowserWs):
    def __init__(self, attachment: str) -> None:
        super().__init__()
        self.attachment = attachment

    def deserializeAttachment(self) -> str:  # noqa: N802 - Cloudflare WebSocket API
        return self.attachment


def _runtime() -> SessionRuntime:
    conn = sqlite3.connect(":memory:")
    ctx = SimpleNamespace(
        storage=SimpleNamespace(sql=SimpleNamespace(exec=conn.execute), setAlarm=lambda _ms: None),
        id=SimpleNamespace(name=lambda: "fence-worker"),
        getWebSockets=list,
    )
    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=_KEY,
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
    )
    runtime = SessionRuntime(ctx, env)

    async def admin_role(_request: object) -> str:
        return "admin"

    runtime.browser_role_for_request = admin_role  # type: ignore[method-assign]
    return runtime


def _control(raw: str) -> dict[str, object]:
    chunks = ControlFrameDecoder().feed(raw)
    assert len(chunks) == 1 and isinstance(chunks[0], ControlChunk)
    return chunks[0].control


def _send(
    runtime: SessionRuntime, hijack_id: str, body: dict[str, object] | None = None
) -> Coroutine[object, object, object]:
    """Return the owned-input send call, unawaited.

    Callers variously await it, wrap it in ``asyncio.wait_for``, or hand it to
    ``asyncio.create_task`` to hold a delivery in flight, so this hands back the
    coroutine rather than awaiting it.
    """
    return route_hijack(
        runtime,
        _Request(body if body is not None else {"keys": "owned-input"}),
        f"/worker/{runtime.worker_id}/hijack/{hijack_id}/send",
        "https://example.invalid/send",
        "POST",
    )


async def _release(runtime: SessionRuntime, hijack_id: str) -> object:
    return await route_hijack(
        runtime,
        _Request(),
        f"/worker/{runtime.worker_id}/hijack/{hijack_id}/release",
        "https://example.invalid/release",
        "POST",
    )
