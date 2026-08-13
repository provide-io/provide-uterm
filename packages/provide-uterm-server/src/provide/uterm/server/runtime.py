#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hosted session runtime that bridges a connector into TermHub."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import TYPE_CHECKING, Any, Literal, cast

from provide.telemetry import get_logger
from provide.uterm.control_channel import (
    ControlFrameDecoder,
    ControlFrameProtocolError,
    DataChunk,
)
from provide.uterm.server.connectors import SessionConnector, build_connector
from provide.uterm.server.models import RecordingConfig, SessionDefinition, SessionLifecycle, SessionRuntimeStatus

# Re-exported so the public import surface of ``runtime`` is unchanged: tests
# and callers import these helpers directly from ``provide.uterm.server.runtime``.
from provide.uterm.server.runtime_helpers import (
    RunOutcome,  # noqa: F401 — re-export
    _await_task_completion,
    _build_recording_redactor,
    _cancel_and_wait,
    _classify_run_error,
    _encode_runtime_frame,
)
from provide.uterm.session_logger import SessionLogger

if TYPE_CHECKING:
    from pathlib import Path

    from provide.uterm.annotation import PatternDetector, StreamingDetector
    from provide.uterm.recording import RecordingStore
    from provide.uterm.server.bridge.hub import TermHub

logger = get_logger(__name__)


class HostedSessionRuntime:
    """Long-lived worker runtime for one named hosted session."""

    def __init__(
        self,
        definition: SessionDefinition,
        *,
        public_base_url: str,
        recording: RecordingConfig,
        recording_store: RecordingStore | None = None,
        worker_bearer_token: str | None = None,
        hub: TermHub | None = None,
        detector: PatternDetector | None = None,
        max_buffer_bytes: int = 1_048_576,  # 1MB default
        block_private_connector_targets: bool = False,
    ) -> None:
        self.definition = definition
        self._public_base_url = public_base_url.rstrip("/")
        self._recording_cfg = recording
        if recording_store is None:
            from provide.uterm.recording import LocalFileRecordingStore

            self._recording_store: RecordingStore = LocalFileRecordingStore(recording.directory)
        else:
            self._recording_store = recording_store
        self._worker_bearer_token = worker_bearer_token
        self._connector: SessionConnector | None = None
        self._on_metric = hub.metric if hub is not None else (lambda *_a, **_kw: None)
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._queue_bytes = 0
        self._max_buffer_bytes = max_buffer_bytes
        self._block_private_connector_targets = block_private_connector_targets
        self._stop = asyncio.Event()
        self._connected = False
        self._state: SessionLifecycle = "stopped"
        self._stopped_at: float | None = None
        self._last_error: str | None = None
        self._recording_path: Path | None = None
        self._logger: SessionLogger | None = None
        self._detector = detector
        # Per-session streaming wrapper for the *send* path only: keystroke
        # input can fragment a pattern across chunks, while the *read* path
        # already receives the fully reassembled screen. Held per session so
        # carried text never bleeds between sessions (the detector is shared).
        self._send_stream: StreamingDetector | None = None
        if detector is not None:
            from provide.uterm.annotation import StreamingDetector

            self._send_stream = StreamingDetector(detector)
        self._event_seq: int = 0
        self._at_password_prompt: bool = False

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
            tags=self.definition.tags,
            recording_enabled=self._recording_enabled(),
            recording_available=self._recording_enabled(),
            owner=self.definition.owner,
            visibility=self.definition.visibility,
            stopped_at=self._stopped_at,
            last_error=self._last_error,
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._queue = asyncio.Queue(maxsize=2000)
        self._state = "starting"
        self._stopped_at = None
        self._last_error = None
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            if task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    task.result()
            else:
                running_loop = asyncio.get_running_loop()
                task_loop = task.get_loop()
                if task_loop is running_loop:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                else:
                    task_loop.call_soon_threadsafe(task.cancel)
                    future = asyncio.run_coroutine_threadsafe(_await_task_completion(task), task_loop)
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wrap_future(future)
        self._task = None
        await self._stop_connector()
        self._state = "stopped"
        self._stopped_at = time.time()
        self._connected = False

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def set_mode(self, mode: str) -> None:
        if mode not in {"hijack", "open"}:
            raise ValueError(f"invalid mode: {mode}")
        typed_mode = cast("Literal['hijack', 'open']", mode)
        if self._connector is not None:
            await self._enqueue_messages(await self._connector.set_mode(typed_mode))
        self.definition.input_mode = typed_mode

    async def clear(self) -> None:
        if self._connector is None:
            return
        await self._enqueue_messages(await self._connector.clear())

    def set_tunnel_state(self, connected: bool) -> None:
        """Update tunnel-backed liveness fields from the raw /tunnel websocket path.

        Encapsulates the lifecycle mutations the registry previously poked into
        via private-attribute writes. Mirrors that behavior exactly.
        """
        self._connected = connected
        if connected:
            self._state = "running"
            self._last_error = None
            self._stopped_at = None
        else:
            self._state = "stopped"
            self._stopped_at = time.time()

    async def flush_recording(self) -> None:
        """Flush the active recording logger, if any (no-op when not recording)."""
        if self._logger is not None:
            await self._logger.flush()

    async def analyze(self) -> str:
        if self._connector is None:
            return "connector offline"
        return await self._connector.get_analysis()

    async def get_recording_path(self) -> Path | None:
        return await self._recording_store.get_path(self.definition.session_id)

    async def _enqueue_messages(self, messages: list[dict[str, Any]]) -> None:
        if self._queue is None:
            return
        for msg in messages:
            encoded = _encode_runtime_frame(msg)
            msg_len = len(encoded)
            if self._queue_bytes + msg_len > self._max_buffer_bytes:
                logger.warning(
                    "hosted_session_runtime_buffer_full session_id=%s queue_bytes=%d msg_len=%d max=%d — dropping frame",
                    self.definition.session_id,
                    self._queue_bytes,
                    msg_len,
                    self._max_buffer_bytes,
                )
                self._on_metric("hosted_session_runtime_buffer_full")
                await self._queue.put({"type": "error", "message": "Buffer overflow — input dropped"})
                continue
            self._queue_bytes += msg_len
            await self._queue.put(msg)

    async def _start_connector(self) -> SessionConnector:
        connector_config = {
            **self.definition.connector_config,
            "input_mode": self.definition.input_mode,
        }
        if self.definition.connector_type in {"ssh", "telnet", "websocket"}:
            connector_config["block_private_connector_targets"] = self._block_private_connector_targets
        connector = build_connector(
            self.definition.session_id,
            self.definition.display_name,
            self.definition.connector_type,
            connector_config,
        )
        await connector.start()
        if connector.is_connected():
            self._connected = True
        return connector

    async def _start_recording(self) -> None:
        """Open a recording for one bridged connection.

        Separate from the connector because the two have different lifetimes: a
        recording spans one worker connection, and the connector spans the
        session those connections attach to.
        """

        if self._logger is None and self._recording_enabled():
            self._logger = SessionLogger(
                self._recording_store,
                max_bytes=self._recording_cfg.max_bytes,
                control_channel_mode=self._recording_cfg.control_channel_mode,
                redactor=_build_recording_redactor(self._recording_cfg.redact_sensitive),
                # Both flush knobs were previously omitted, so every hosted
                # session recorded at the SessionLogger defaults no matter what
                # the operator configured. It went unnoticed because the
                # interval default (5.0) matches the config default, and because
                # the config name (flush_batch_size) differs from the parameter
                # name (batch_size) — so the mismatch never looked like one.
                flush_interval_s=self._recording_cfg.flush_interval_s,
                batch_size=self._recording_cfg.flush_batch_size,
            )
            await self._logger.start(self.definition.session_id)
            self._recording_path = await self._recording_store.get_path(self.definition.session_id)

    async def _stop_recording(self) -> None:
        if self._logger is not None:
            await self._logger.stop()
            self._logger = None
        self._recording_path = None

    async def _discard_connector(self) -> None:
        """Drop the connector alone, leaving the recording where it is.

        Replacing a failed connector says nothing about the recording, which
        belongs to the worker connection and is opened and closed around it.
        """

        connector = self._connector
        self._connector = None
        if connector is not None:
            with contextlib.suppress(Exception):
                await connector.stop()

    async def _stop_connector(self) -> None:
        await self._stop_recording()
        await self._discard_connector()

    async def _log_snapshot(self, msg: dict[str, Any]) -> None:
        screen = str(msg.get("screen", ""))
        self._at_password_prompt = bool(re.search(r"(?i)(?:password|passphrase)[^\n]*:\s*$", screen.rstrip()))
        if self._logger is None:
            return
        await self._logger.log_screen(msg, screen.encode("cp437", errors="replace"))
        self._event_seq += 1
        if self._detector is not None:
            for annotation in self._detector.detect("read", screen, seq=self._event_seq):
                await self._logger.log_event("annotation", annotation.to_dict())

    async def _log_send(self, data: str) -> None:
        if self._logger is not None:
            if self._at_password_prompt:
                await self._logger.log_send_masked(len(data.encode("cp437", errors="replace")))
            else:
                await self._logger.log_send(data)
            self._event_seq += 1
            if self._send_stream is not None:
                for annotation in self._send_stream.detect("send", data, seq=self._event_seq):
                    await self._logger.log_event("annotation", annotation.to_dict())

    async def _log_event(self, event: str, payload: dict[str, Any]) -> None:
        if self._logger is not None:
            await self._logger.log_event(event, payload)

    async def _log_wire_send(self, payload: str, msg: dict[str, Any]) -> None:
        if self._logger is None:
            return
        await self._logger.log_wire("send", payload)
        if str(msg.get("type") or "") != "term":
            await self._logger.log_control("send", msg)

    async def _log_wire_recv(self, payload: str) -> None:
        if self._logger is not None:
            await self._logger.log_wire("recv", payload)

    async def _log_control_recv(self, msg: dict[str, Any]) -> None:
        if self._logger is not None:
            await self._logger.log_control("recv", msg)

    async def _send_outbound_frame(self, ws: Any, outbound: dict[str, Any]) -> None:
        """Encode and send one outbound frame, logging wire and snapshot events."""
        payload = _encode_runtime_frame(outbound)
        await ws.send(payload)
        await self._log_wire_send(payload, outbound)
        if outbound.get("type") == "snapshot":
            await self._log_snapshot(outbound)

    async def _process_control_msg(
        self,
        connector: Any,
        responses: list[dict[str, Any]],
        message: dict[str, Any],
    ) -> None:
        """Dispatch a single decoded control message to the connector."""
        await self._log_control_recv(message)
        mtype = message.get("type")
        if mtype == "snapshot_req":
            responses.append(await connector.get_snapshot())
        elif mtype == "analyze_req":
            responses.append(
                {
                    "type": "analysis",
                    "formatted": await connector.get_analysis(),
                    "ts": time.time(),
                }
            )
        elif mtype == "control":
            responses.extend(await connector.handle_control(str(message.get("action", ""))))

    async def _process_inbound(
        self,
        ws: Any,
        connector: Any,
        decoder: ControlFrameDecoder,
        raw_text: str,
    ) -> None:
        """Decode inbound data, dispatch events, and send responses."""
        await self._log_wire_recv(raw_text)
        try:
            events = decoder.feed(raw_text)
        except ControlFrameProtocolError as exc:
            raise RuntimeError(f"invalid control channel: {exc}") from exc
        responses: list[dict[str, Any]] = []
        for event in events:
            if isinstance(event, DataChunk):
                await self._log_send(event.data)
                responses.extend(await connector.handle_input(event.data))
                continue
            await self._process_control_msg(connector, responses, event.control)
        for outbound in responses:
            await self._send_outbound_frame(ws, outbound)

    async def _bridge_session(self, ws: Any) -> None:
        connector = self._connector
        if connector is None:
            raise RuntimeError("connector unavailable")
        decoder = ControlFrameDecoder(max_control_payload_bytes=1_048_576, on_error=self._on_metric)
        self._state = "running"
        self._connected = True
        await self._enqueue_messages(await connector.set_mode(self.definition.input_mode))
        await self._enqueue_messages([await connector.get_snapshot()])
        await self._log_event("runtime_started", {"session_id": self.definition.session_id})

        recv_task: asyncio.Task[Any] | None = None
        try:
            while not self._stop.is_set():
                if self._queue is not None and not self._queue.empty():
                    outbound = await self._queue.get()
                    encoded = _encode_runtime_frame(outbound)
                    self._queue_bytes = max(0, self._queue_bytes - len(encoded))
                    await self._send_outbound_frame(ws, outbound)
                    continue
                # If recv_task completed while we were processing poll output, handle it
                # now before creating a new one — otherwise its result would be discarded.
                if recv_task is not None and recv_task.done():
                    try:
                        raw = recv_task.result()
                    except asyncio.CancelledError:  # pragma: no cover — WS teardown race
                        break
                    recv_task = None
                    raw_text = raw if isinstance(raw, str) else raw.decode("latin-1", errors="replace")
                    await self._process_inbound(ws, connector, decoder, raw_text)
                    continue
                if recv_task is None:
                    recv_task = asyncio.create_task(ws.recv())
                poll_task = asyncio.create_task(connector.poll_messages())
                done, _ = await asyncio.wait({recv_task, poll_task}, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                await _cancel_and_wait({cast("asyncio.Task[object]", poll_task)})
                if not done:
                    continue
                if poll_task in done:
                    poll_result = poll_task.result()
                    for outbound in poll_result:
                        await self._send_outbound_frame(ws, outbound)
                    if not poll_result and recv_task not in done:
                        # poll_messages() returned empty instantly — backoff
                        # to avoid hot-spinning when the connector has no
                        # internal wait (e.g. shell, pty, capture connectors).
                        await asyncio.sleep(0.05)
                if recv_task in done:
                    try:
                        raw = recv_task.result()
                    except asyncio.CancelledError:
                        break
                    recv_task = None
                    raw_text = raw if isinstance(raw, str) else raw.decode("latin-1", errors="replace")
                    await self._process_inbound(ws, connector, decoder, raw_text)
        finally:
            if recv_task is not None and not recv_task.done():
                recv_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recv_task

    async def _run(self) -> None:
        import websockets

        backoff_s = [0.25, 0.5, 1.0, 2.0, 5.0]
        attempt = 0
        while not self._stop.is_set():
            self._state = "starting"
            try:
                # Kept across worker reconnects rather than rebuilt with each
                # one. A capture connector's socket is a rendezvous point that
                # a running session connected to once, at exec: rebinding it
                # leaves that session writing into a socket nobody holds, with
                # no way to reconnect, and the terminal silently stops
                # appearing anywhere. Only a connector that has actually failed
                # is replaced.
                if self._connector is not None and not self._connector.is_connected():
                    await self._discard_connector()
                if self._connector is None:
                    self._connector = await self._start_connector()
                await self._start_recording()
                worker_url = self._ws_url() + f"/ws/worker/{self.definition.session_id}/term"
                headers = {"Authorization": f"Bearer {self._worker_bearer_token}"} if self._worker_bearer_token else {}
                async with websockets.connect(worker_url, additional_headers=headers, open_timeout=10) as ws:
                    await self._bridge_session(ws)
                    # Reset backoff only after a session completes normally,
                    # not on bare TCP connect — prevents tight loops on auth errors.
                    attempt = 0
            except Exception as exc:
                outcome = _classify_run_error(exc)
                if (
                    outcome == "cancelled"
                ):  # pragma: no cover — break exit only reachable on shutdown cancellation, covered by integration
                    break
                self._state = "error"
                self._connected = False
                self._last_error = str(exc)
                if outcome == "permanent":
                    if isinstance(exc, ValueError):
                        logger.error(
                            "hosted_session_runtime_permanent_failure session_id=%s error=%s",
                            self.definition.session_id,
                            exc,
                        )
                        await self._log_event("runtime_error", {"error": str(exc), "permanent": True})
                    else:
                        # Permanent HTTP failure (401/403/404). Log the warning
                        # event first so observers see what failed, then the
                        # error line that explains the stop decision.
                        logger.warning(
                            "hosted_session_runtime_failed session_id=%s error=%s",
                            self.definition.session_id,
                            exc,
                        )
                        await self._log_event("runtime_error", {"error": str(exc)})
                        _status = getattr(exc, "status_code", None) or getattr(
                            getattr(exc, "response", None), "status_code", None
                        )
                        logger.error(
                            "hosted_session_runtime_permanent_http_error session_id=%s status=%s — stopping",
                            self.definition.session_id,
                            _status,
                        )
                    break
                logger.warning("hosted_session_runtime_failed session_id=%s error=%s", self.definition.session_id, exc)
                await self._log_event("runtime_error", {"error": str(exc)})
                delay = backoff_s[min(attempt, len(backoff_s) - 1)]
                attempt += 1
                await asyncio.sleep(delay)
            finally:
                self._connected = False
                # The recording belongs to the connection that just ended; the
                # connector belongs to the session and outlives it.
                await self._stop_recording()
        await self._stop_connector()
        self._state = "stopped"
        self._stopped_at = time.time()
