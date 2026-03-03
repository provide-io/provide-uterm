#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Worker-side terminal bridge to a Swarm Manager.

Connects a running bot/worker process to the manager WebSocket endpoint
``/ws/worker/{bot_id}/term``.

Forwards:
- Live terminal output from session watchers → hub → browsers.
- Snapshot responses on request.
- Hijack control and input commands from the hub → session.

Requires the ``websocket`` extra (uses the ``websockets`` package that ships
with FastAPI)::

    pip install 'provide-terminal[websocket]'
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


def _to_ws_url(manager_url: str, path: str) -> str:
    base = manager_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base + path


# ---------------------------------------------------------------------------
# Protocol — the worker interface expected by TermBridge
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkerSession(Protocol):
    """Minimal interface that a bot session must provide for TermBridge."""

    def add_watch(self, fn: Any, *, interval_s: float) -> None:
        """Register a callback invoked on each screen update."""
        ...

    async def send(self, data: str) -> None:
        """Send keystrokes to the remote terminal."""
        ...

    async def set_size(self, cols: int, rows: int) -> None:
        """Resize the terminal."""
        ...


@runtime_checkable
class WorkerBot(Protocol):
    """Minimal interface that a bot must provide for TermBridge."""

    @property
    def session(self) -> WorkerSession | None:
        """Active session (``None`` when disconnected)."""
        ...

    async def set_hijacked(self, enabled: bool) -> None:
        """Pause (``True``) or resume (``False``) automation."""
        ...

    async def request_step(self) -> None:
        """Allow one loop iteration while hijacked."""
        ...


# ---------------------------------------------------------------------------
# TermBridge
# ---------------------------------------------------------------------------


class TermBridge:
    """Worker-side WebSocket bridge to the Swarm Manager terminal hub.

    Args:
        bot: Object implementing :class:`WorkerBot` (duck-typed).
        bot_id: Unique bot identifier used in the WebSocket URL.
        manager_url: Base URL of the Swarm Manager (``http://`` or ``https://``).
    """

    def __init__(self, bot: Any, bot_id: str, manager_url: str) -> None:
        self._bot = bot
        self._bot_id = bot_id
        self._manager_url = manager_url
        self._send_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._latest_snapshot: dict[str, Any] | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._attached_session: Any | None = None

    def attach_session(self) -> None:
        """Attach a watcher to the bot's current session to forward terminal output."""
        session = getattr(self._bot, "session", None)
        if session is None or self._attached_session is session:
            return
        self._attached_session = session

        def _watch(snapshot: dict[str, Any], raw: bytes) -> None:
            # Called from the session's watcher thread (same event loop thread as
            # the bridge tasks).  Writing _latest_snapshot here is safe because all
            # accesses happen on the single asyncio event-loop thread.
            self._latest_snapshot = snapshot
            if not raw:
                return
            text = raw.decode("cp437", errors="replace")
            try:
                self._send_q.put_nowait({"type": "term", "data": text, "ts": time.time()})
            except asyncio.QueueFull:
                logger.debug("term_bridge_drop bot_id=%s queue_full", self._bot_id)

        session.add_watch(_watch, interval_s=0.0)

    async def start(self) -> None:
        """Start the bridge background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the bridge and wait for it to clean up."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    _RECONNECT_BACKOFF: tuple[int, ...] = (1, 2, 5, 10, 30)

    async def _run(self) -> None:
        try:
            import websockets
        except ImportError:  # pragma: no cover
            logger.warning("term_bridge_no_websockets bot_id=%s", self._bot_id)
            return

        url = _to_ws_url(self._manager_url, f"/ws/worker/{self._bot_id}/term")
        logger.info("term_bridge_connecting bot_id=%s url=%s", self._bot_id, url)

        attempt = 0
        while self._running:
            try:
                async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
                    attempt = 0  # reset backoff on successful connect
                    send_task = asyncio.create_task(self._send_loop(ws))
                    recv_task = asyncio.create_task(self._recv_loop(ws))
                    # Use FIRST_COMPLETED so a normal recv-loop return (connection
                    # closed cleanly) cancels the send-loop rather than leaving it
                    # blocked forever on queue.get().
                    done, pending = await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for t in done:
                        if not t.cancelled():
                            exc = t.exception()
                            if exc:
                                raise exc
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "term_bridge_disconnected bot_id=%s error=%s attempt=%d",
                    self._bot_id,
                    exc,
                    attempt,
                )

            if not self._running:
                break
            delay = self._RECONNECT_BACKOFF[min(attempt, len(self._RECONNECT_BACKOFF) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    async def _send_loop(self, ws: Any) -> None:
        while self._running:
            msg = await self._send_q.get()
            try:
                await ws.send(json.dumps(msg, ensure_ascii=True))
            finally:
                # Always mark the item done so queue.join() never deadlocks if
                # it is added as a shutdown fence in the future.
                self._send_q.task_done()

    async def _recv_loop(self, ws: Any) -> None:
        while self._running:
            try:
                raw = await ws.recv()
            except Exception:
                return
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: S112
                continue
            mtype = msg.get("type")
            if mtype == "snapshot_req":
                await self._send_snapshot(ws)
            elif mtype == "control":
                action = msg.get("action")
                if action == "pause":
                    await self._set_hijacked(True)
                elif action == "resume":
                    await self._set_hijacked(False)
                elif action == "step":
                    await self._request_step()
            elif mtype == "input":
                data = msg.get("data", "")
                if data:
                    await self._send_keys(data)
            elif mtype == "resize":
                await self._set_size(int(msg.get("cols", 80) or 80), int(msg.get("rows", 25) or 25))

    async def _send_snapshot(self, ws: Any) -> None:
        session = getattr(self._bot, "session", None)
        if session is None:
            return
        with contextlib.suppress(AttributeError, RuntimeError):
            self.attach_session()
        try:
            emulator = getattr(session, "emulator", None)
            snapshot = self._latest_snapshot or (emulator.get_snapshot() if emulator else {})
            await ws.send(
                json.dumps(
                    {
                        "type": "snapshot",
                        "screen": snapshot.get("screen", ""),
                        "cursor": snapshot.get("cursor", {"x": 0, "y": 0}),
                        "cols": int(snapshot.get("cols", 80) or 80),
                        "rows": int(snapshot.get("rows", 25) or 25),
                        "screen_hash": snapshot.get("screen_hash", ""),
                        "cursor_at_end": bool(snapshot.get("cursor_at_end", True)),
                        "has_trailing_space": bool(snapshot.get("has_trailing_space", False)),
                        "prompt_detected": snapshot.get("prompt_detected"),
                        "ts": time.time(),
                    },
                    ensure_ascii=True,
                )
            )
        except Exception:
            return

    async def _send_keys(self, data: str) -> None:
        session = getattr(self._bot, "session", None)
        if session is None:
            return
        with contextlib.suppress(Exception):
            await session.send(data)

    async def _request_step(self) -> None:
        fn = getattr(self._bot, "request_step", None)
        if callable(fn):
            with contextlib.suppress(Exception):
                await fn()

    async def _set_size(self, cols: int, rows: int) -> None:
        session = getattr(self._bot, "session", None)
        if session is None:
            return
        with contextlib.suppress(Exception):
            await session.set_size(cols, rows)

    async def _set_hijacked(self, enabled: bool) -> None:
        fn = getattr(self._bot, "set_hijacked", None)
        if callable(fn):
            with contextlib.suppress(Exception):
                await fn(enabled)
        with contextlib.suppress(asyncio.QueueFull):
            self._send_q.put_nowait({"type": "status", "hijacked": enabled, "ts": time.time()})
