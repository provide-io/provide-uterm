#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Hosted session runtime that bridges a connector into TermHub."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING, Any, Literal, cast

from provide.telemetry import get_logger

from provide.terminal.server.connectors import SessionConnector, build_connector
from provide.terminal.server.models import RecordingConfig, SessionDefinition, SessionLifecycle, SessionRuntimeStatus
from provide.terminal.session_logger import SessionLogger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


async def _cancel_and_wait(tasks: set[asyncio.Task[object]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class HostedSessionRuntime:
    """Long-lived worker runtime for one named hosted session."""

    def __init__(
        self,
        definition: SessionDefinition,
        *,
        public_base_url: str,
        recording: RecordingConfig,
        worker_bearer_token: str | None = None,
    ) -> None:
        self.definition = definition
        self._public_base_url = public_base_url.rstrip("/")
        self._recording_cfg = recording
        self._worker_bearer_token = worker_bearer_token
        self._connector: SessionConnector | None = None
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._stop = asyncio.Event()
        self._connected = False
        self._state: SessionLifecycle = "stopped"
        self._last_error: str | None = None
        self._logger: SessionLogger | None = None
        self._recording_path: Path | None = None

    def _ws_url(self) -> str:
        if self._public_base_url.startswith("https://"):
            return "wss://" + self._public_base_url.removeprefix("https://")
        return "ws://" + self._public_base_url.removeprefix("http://")

    def _recording_enabled(self) -> bool:
        if self.definition.recording_enabled is not None:
            return bool(self.definition.recording_enabled)
        return self._recording_cfg.enabled_by_default

    def status(self) -> SessionRuntimeStatus:
        return SessionRuntimeStatus(
            session_id=self.definition.session_id,
            display_name=self.definition.display_name,
            created_at=self.definition.created_at,
            connector_type=self.definition.connector_type,
            lifecycle_state=self._state,
            input_mode=self.definition.input_mode,
            connected=self._connected,
            auto_start=self.definition.auto_start,
            tags=list(self.definition.tags),
            recording_enabled=self._recording_enabled(),
            recording_available=(self._recording_path is not None and self._recording_path.exists()),
            owner=self.definition.owner,
            visibility=self.definition.visibility,
            last_error=self._last_error,
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._queue = asyncio.Queue(maxsize=2000)
        self._state = "starting"
        self._last_error = None
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        await self._stop_connector()
        self._state = "stopped"
        self._connected = False

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def set_mode(self, mode: str) -> None:
        if mode not in {"hijack", "open"}:
            raise ValueError(f"invalid mode: {mode}")
        typed_mode = cast("Literal['hijack', 'open']", mode)
        self.definition.input_mode = typed_mode
        if self._connector is None:
            return
        await self._enqueue_messages(await self._connector.set_mode(typed_mode))

    async def clear(self) -> None:
        if self._connector is None:
            return
        await self._enqueue_messages(await self._connector.clear())

    async def analyze(self) -> str:
        if self._connector is None:
            return "connector offline"
        return await self._connector.get_analysis()

    @property
    def recording_path(self) -> Path | None:
        return self._recording_path

    async def _enqueue_messages(self, messages: list[dict[str, Any]]) -> None:
        if self._queue is None:
            return
        for msg in messages:
            await self._queue.put(msg)

    async def _start_connector(self) -> SessionConnector:
        connector = build_connector(
            self.definition.session_id,
            self.definition.display_name,
            self.definition.connector_type,
            {**self.definition.connector_config, "input_mode": self.definition.input_mode},
        )
        await connector.start()
        if connector.is_connected():
            self._connected = True
        if self._recording_enabled():
            self._recording_path = self._recording_cfg.directory / f"{self.definition.session_id}.jsonl"
            self._logger = SessionLogger(self._recording_path, max_bytes=self._recording_cfg.max_bytes)
            await self._logger.start(self.definition.session_id)
        return connector

    async def _stop_connector(self) -> None:
        if self._logger is not None:
            await self._logger.stop()
            self._logger = None
        connector = self._connector
        self._connector = None
        if connector is not None:
            with contextlib.suppress(Exception):
                await connector.stop()

    async def _log_snapshot(self, msg: dict[str, Any]) -> None:
        if self._logger is None:
            return
        screen = str(msg.get("screen", ""))
        await self._logger.log_screen(msg, screen.encode("cp437", errors="replace"))

    async def _log_send(self, data: str) -> None:
        if self._logger is not None:
            await self._logger.log_send(data)

    async def _log_event(self, event: str, payload: dict[str, Any]) -> None:
        if self._logger is not None:
            await self._logger.log_event(event, payload)

    async def _bridge_session(self, ws: Any) -> None:
        connector = self._connector
        if connector is None:
            raise RuntimeError("connector unavailable")
        self._state = "running"
        self._connected = True
        await self._enqueue_messages(await connector.set_mode(self.definition.input_mode))
        await self._enqueue_messages([await connector.get_snapshot()])
        await self._log_event("runtime_started", {"session_id": self.definition.session_id})

        while not self._stop.is_set():
            if self._queue is not None and not self._queue.empty():
                outbound = await self._queue.get()
                await ws.send(json.dumps(outbound))
                if outbound.get("type") == "snapshot":
                    await self._log_snapshot(outbound)
                continue
            recv_task = asyncio.create_task(ws.recv())
            poll_task = asyncio.create_task(connector.poll_messages())
            done, pending = await asyncio.wait({recv_task, poll_task}, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            await _cancel_and_wait(cast("set[asyncio.Task[object]]", pending))
            if not done:
                continue
            if poll_task in done:
                for outbound in poll_task.result():
                    await ws.send(json.dumps(outbound))
                    if outbound.get("type") == "snapshot":
                        await self._log_snapshot(outbound)
                with contextlib.suppress(asyncio.CancelledError):
                    await recv_task
                continue

            raw = recv_task.result()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
            try:
                message = json.loads(raw)
            except Exception:
                continue
            mtype = message.get("type")
            responses: list[dict[str, Any]] = []
            if mtype == "snapshot_req":
                responses = [await connector.get_snapshot()]
            elif mtype == "analyze_req":
                responses = [
                    {
                        "type": "analysis",
                        "formatted": await connector.get_analysis(),
                        "ts": time.time(),
                    }
                ]
            elif mtype == "control":
                responses = await connector.handle_control(str(message.get("action", "")))
            elif mtype == "input":
                payload = str(message.get("data", ""))
                await self._log_send(payload)
                responses = await connector.handle_input(payload)
            for outbound in responses:
                await ws.send(json.dumps(outbound))
                if outbound.get("type") == "snapshot":
                    await self._log_snapshot(outbound)

    async def _run(self) -> None:
        import websockets

        backoff_s = [0.25, 0.5, 1.0, 2.0, 5.0]
        attempt = 0
        while not self._stop.is_set():
            try:
                self._connector = await self._start_connector()
                worker_url = self._ws_url() + f"/ws/worker/{self.definition.session_id}/term"
                headers = {"Authorization": f"Bearer {self._worker_bearer_token}"} if self._worker_bearer_token else {}
                async with websockets.connect(worker_url, additional_headers=headers, open_timeout=10) as ws:
                    await self._bridge_session(ws)
                    # Reset backoff only after a session completes normally,
                    # not on bare TCP connect — prevents tight loops on auth errors.
                    attempt = 0
            except asyncio.CancelledError:
                break
            except ValueError as exc:
                # Permanent configuration error (e.g. unsupported connector_type,
                # missing known_hosts) — retrying will never succeed.
                self._state = "error"
                self._connected = False
                self._last_error = str(exc)
                logger.error(
                    "hosted_session_runtime_permanent_failure session_id=%s error=%s",
                    self.definition.session_id,
                    exc,
                )
                await self._log_event("runtime_error", {"error": str(exc), "permanent": True})
                break
            except Exception as exc:
                self._state = "error"
                self._connected = False
                self._last_error = str(exc)
                logger.warning("hosted_session_runtime_failed session_id=%s error=%s", self.definition.session_id, exc)
                await self._log_event("runtime_error", {"error": str(exc)})
                # Permanent HTTP failures (auth rejected, wrong endpoint) will
                # never resolve on their own — stop retrying immediately.
                _status = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if _status in (401, 403, 404):
                    logger.error(
                        "hosted_session_runtime_permanent_http_error session_id=%s status=%s — stopping",
                        self.definition.session_id,
                        _status,
                    )
                    break
                delay = backoff_s[min(attempt, len(backoff_s) - 1)]
                attempt += 1
                await asyncio.sleep(delay)
            finally:
                self._connected = False
                await self._stop_connector()
        self._state = "stopped"
