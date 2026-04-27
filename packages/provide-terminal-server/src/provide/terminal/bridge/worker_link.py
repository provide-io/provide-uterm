#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Worker-side terminal bridge to a Swarm Manager.

Connects a running worker process to the manager WebSocket endpoint
``/ws/worker/{worker_id}/term``.

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
import time
from typing import Any, Protocol, runtime_checkable

from provide.telemetry import get_logger
from provide.terminal.bridge.models import _safe_int
from provide.terminal.control_channel import (
    ControlChannelDecoder,
    ControlChannelProtocolError,
    DataChunk,
    encode_control,
    encode_data,
)

logger = get_logger(__name__)
# Optional: websockets.exceptions.InvalidURI — used to detect malformed URLs
# as a permanent failure in the reconnect loop.  None when websockets < 10.
_InvalidURI: type | None = None
with contextlib.suppress(ImportError):
    from websockets.exceptions import InvalidURI as _InvalidURI


def _to_ws_url(manager_url: str, path: str) -> str:
    base = manager_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base + path


def _encode_bridge_frame(msg: dict[str, Any]) -> str:
    if str(msg.get("type") or "") == "term":
        return encode_data(str(msg.get("data") or ""))
    return encode_control(msg)


# ---------------------------------------------------------------------------
# Protocol — the worker interface expected by TermBridge
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkerSession(Protocol):
    """Minimal interface that a worker session must provide for TermBridge."""

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
class Worker(Protocol):
    """Minimal interface that a worker must provide for TermBridge."""

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
        worker: Object implementing :class:`Worker` (duck-typed).
        worker_id: Unique worker identifier used in the WebSocket URL.
        manager_url: Base URL of the Swarm Manager (``http://`` or ``https://``).
    """

    def __init__(
        self,
        worker: Any,
        worker_id: str,
        manager_url: str,
        *,
        max_ws_message_bytes: int = 1_048_576,
        encoding: str = "cp437",
    ) -> None:
        self._worker = worker
        self._worker_id = worker_id
        self._manager_url = manager_url
        self._max_ws_message_bytes = max(1024, int(max_ws_message_bytes))
        self._encoding = encoding
        self._send_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._latest_snapshot: dict[str, Any] | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._attached_session: Any | None = None

    def attach_session(self) -> None:
        """Attach a watcher to the worker's current session to forward terminal output."""
        session = getattr(self._worker, "session", None)
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
            text = raw.decode(self._encoding, errors="replace")
            try:
                self._send_q.put_nowait({"type": "term", "data": text, "ts": time.time()})
            except asyncio.QueueFull:
                logger.debug("term_bridge_drop worker_id=%s queue_full", self._worker_id)

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

    async def _handle_permanent_error(self, exc: Exception, status: Any) -> bool:
        """Check if an exception is a permanent/auth error that should stop retrying.

        Returns True if it is a permanent error (and sets ``_running`` to False).
        """
        if status in (401, 403, 404):
            logger.error(
                "term_bridge_permanent_error worker_id=%s status=%s — stopping reconnect",
                self._worker_id,
                status,
            )
            self._running = False
            return True
        if _InvalidURI is not None and isinstance(exc, _InvalidURI):
            logger.error(
                "term_bridge_permanent_error worker_id=%s error=%s — malformed URL, stopping reconnect",
                self._worker_id,
                exc,
            )
            self._running = False
            return True
        return False

    async def _handle_connection(self, ws: Any) -> None:
        """Handle one connection lifetime: create recv/send tasks and wait for completion."""
        send_task = asyncio.create_task(self._send_loop(ws))
        recv_task = asyncio.create_task(self._recv_loop(ws))
        # Use FIRST_COMPLETED so a normal recv-loop return (connection
        # closed cleanly) cancels the send-loop rather than leaving it
        # blocked forever on queue.get().
        try:
            done, pending = await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            send_task.cancel()
            recv_task.cancel()
            await asyncio.gather(send_task, recv_task, return_exceptions=True)
            raise
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            if not t.cancelled():  # pragma: no branch
                exc = t.exception()
                if exc:
                    raise exc

    async def _run(self) -> None:
        try:
            import websockets
        except ImportError:  # pragma: no cover
            logger.warning("term_bridge_no_websockets worker_id=%s", self._worker_id)
            return

        url = _to_ws_url(self._manager_url, f"/ws/worker/{self._worker_id}/term")
        logger.info("term_bridge_connecting worker_id=%s url=%s", self._worker_id, url)

        attempt = 0
        while self._running:
            try:
                # Explicit ping_interval/ping_timeout: passive bots (compare/
                # supervised mode) emit ~no terminal activity, so the WS goes
                # silent for minutes at a time. The websockets default
                # ping_interval (20s) is correct, but we set explicitly so a
                # future websockets default change doesn't silently regress.
                # ping_timeout=20 is more forgiving than the 20s default for
                # slow LAN/WAN paths.
                async with websockets.connect(
                    url,
                    max_size=self._max_ws_message_bytes,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as ws:
                    attempt = 0  # reset backoff on successful connect
                    await self._handle_connection(ws)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "term_bridge_disconnected worker_id=%s error=%s attempt=%d",
                    self._worker_id,
                    exc,
                    attempt,
                )
                # Permanent failures (auth rejected, wrong URL) will never resolve
                # on their own — stop retrying immediately rather than backing off.
                _status = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if await self._handle_permanent_error(exc, _status):
                    break

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
                try:
                    payload = _encode_bridge_frame(msg)
                except Exception as exc:
                    # Serialization failure: skip the bad message rather than
                    # tearing down the connection and triggering reconnect churn.
                    logger.warning("_send_loop serialization_error worker_id=%s: %s", self._worker_id, exc)
                    continue
                try:
                    await ws.send(payload)
                except Exception as exc:
                    logger.warning(
                        "_send_loop network_error worker_id=%s msg_type=%s: %s",
                        self._worker_id,
                        msg.get("type"),
                        exc,
                    )
                    raise  # propagate to _run to trigger reconnect
            finally:
                # Always mark the item done — including on CancelledError — so
                # queue.join() never deadlocks if used as a shutdown fence.
                self._send_q.task_done()

    async def _dispatch_control_msg(self, msg: dict[str, Any], ws: Any) -> None:
        """Handle the mtype dispatch for one control message from the hub."""
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
        elif mtype == "resize":
            await self._set_size(_safe_int(msg.get("cols"), 80, min_val=1), _safe_int(msg.get("rows"), 25, min_val=1))

    async def _recv_loop(self, ws: Any) -> None:
        decoder = ControlChannelDecoder(max_control_payload_bytes=self._max_ws_message_bytes)
        try:
            while self._running:
                try:
                    raw = await ws.recv()
                except Exception as exc:
                    logger.debug("_recv_loop recv error worker_id=%s: %s", self._worker_id, exc)
                    return
                try:
                    events = decoder.feed(raw if isinstance(raw, str) else raw.decode("latin-1", errors="replace"))
                except ControlChannelProtocolError as exc:
                    logger.debug("_recv_loop bad stream worker_id=%s: %s", self._worker_id, exc)
                    return
                for event in events:
                    if isinstance(event, DataChunk):
                        if event.data:  # pragma: no branch
                            await self._send_keys(event.data)
                        continue
                    await self._dispatch_control_msg(event.control, ws)
        finally:
            # Ensure the worker is never left permanently paused if the connection
            # drops while a hijack was active.  The hub clears its own hijack
            # state in ws_worker_term's finally block, but it cannot send a
            # resume over a closed socket — so the bridge must self-clear here.
            await self._set_hijacked(False)

    async def _send_snapshot(self, ws: Any) -> None:
        session = getattr(self._worker, "session", None)
        if session is None:
            return
        with contextlib.suppress(AttributeError, RuntimeError):
            self.attach_session()
        try:
            emulator = getattr(session, "emulator", None)
            snapshot = (emulator.get_snapshot() if emulator else None) or self._latest_snapshot or {}
            await ws.send(
                encode_control(
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
                    }
                )
            )
        except Exception as exc:
            logger.debug("_send_snapshot failed worker_id=%s: %s", self._worker_id, exc)
            return

    async def _send_keys(self, data: str) -> None:
        session = getattr(self._worker, "session", None)
        if session is None:
            return
        try:
            await session.send(data)
        except Exception as exc:
            logger.debug("_send_keys failed: %s", exc)

    async def _request_step(self) -> None:
        fn = getattr(self._worker, "request_step", None)
        if callable(fn):
            try:
                await fn()
            except Exception as exc:
                logger.debug("_request_step failed: %s", exc)

    async def _set_size(self, cols: int, rows: int) -> None:
        session = getattr(self._worker, "session", None)
        if session is None:
            return
        try:
            await session.set_size(cols, rows)
        except Exception as exc:
            logger.debug("_set_size failed: %s", exc)

    async def _set_hijacked(self, enabled: bool) -> None:
        fn = getattr(self._worker, "set_hijacked", None)
        if callable(fn):
            try:
                await fn(enabled)
            except Exception as exc:
                logger.debug("_set_hijacked failed: %s", exc)
        with contextlib.suppress(asyncio.QueueFull):
            self._send_q.put_nowait({"type": "status", "hijacked": enabled, "ts": time.time()})
